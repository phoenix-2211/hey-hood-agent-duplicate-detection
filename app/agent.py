from __future__ import annotations

import os
import sys
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps.app import App
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.workflow import Edge, Workflow, node
from pydantic import BaseModel, Field

current_dir = os.path.dirname(os.path.abspath(__file__))
shared_dir = os.path.abspath(os.path.join(current_dir, "..", "shared"))
if not os.path.exists(os.path.join(shared_dir, "firestore_client.py")):
    shared_dir = os.path.abspath(os.path.join(current_dir, "..", "..", "shared"))
sys.path.append(shared_dir)
import firestore_client  # noqa: E402
from firebase_admin import firestore  # noqa: E402
from security_checkpoint import security_checkpoint  # noqa: E402


class SimilarityResult(BaseModel):
    is_duplicate: bool = Field(description="True if this issue is a duplicate")
    similarity_score: float = Field(description="Similarity score 0.0 to 1.0")
    duplicate_of: str = Field(description="Issue ID of the duplicate, or empty string")
    reasoning: str = Field(description="Brief explanation of the decision")


@node
def fetch_existing_issues(ctx: Context, node_input: dict):
    """Fetch active issues in same ward and category."""
    db = firestore_client.get_db()
    ward_id = node_input.get("ward_id")
    category = node_input.get("category")

    existing = (
        db.collection("issues")
        .where("ward_id", "==", ward_id)
        .where("category", "==", category)
        .limit(30)
        .stream()
    )

    existing_list = []
    for doc in existing:
        data = doc.to_dict()
        if data.get("status") != "Resolved" and data.get("issue_id") != node_input.get("issue_id"):
            existing_list.append(
                {
                    "issue_id": data.get("issue_id"),
                    "title": data.get("title"),
                    "description": data.get("description"),
                }
            )
            if len(existing_list) >= 10:
                break

    ctx.state["existing_issues"] = existing_list
    ctx.state["new_issue"] = node_input

    if not existing_list:
        yield Event(output=node_input, actions=EventActions(route="publish_new"))
        return

    yield Event(
        output={"new_issue": node_input, "existing_issues": existing_list},
        actions=EventActions(route="check_similarity"),
    )


similarity_agent = LlmAgent(
    name="similarity_checker",
    model="gemini-flash-latest",
    instruction="""You are a duplicate issue detector for a civic app.

Compare the new issue against existing issues.
Determine if they describe the same real-world problem
in the same neighborhood.

Score above 0.75 means duplicate.
Consider: same location, same problem type,
same visible symptom.

Be strict — different locations or different
problems should NOT be marked as duplicates.

Respond with structured JSON only.""",
    output_key="similarity_result",
    output_schema=SimilarityResult,
)


@node
def route_duplicate_decision(ctx: Context, node_input: Any):
    """Routes based on similarity result."""
    result = ctx.state.get("similarity_result", {})
    is_duplicate = result.get("is_duplicate", False)
    score = result.get("similarity_score", 0.0)

    if is_duplicate and score >= 0.75:
        yield Event(
            output={
                **ctx.state.get("new_issue", {}),
                "duplicate_of": result.get("duplicate_of"),
                "similarity_score": score,
            },
            actions=EventActions(route="merge_duplicate"),
        )
    else:
        yield Event(
            output=ctx.state.get("new_issue", {}),
            actions=EventActions(route="publish_new"),
        )


@node
def merge_duplicate(ctx: Context, node_input: dict):
    """Increments support on existing issue."""
    db = firestore_client.get_db()
    duplicate_id = node_input.get("duplicate_of")
    new_issue_id = node_input.get("issue_id")

    # Update existing duplicate issue
    db.collection("issues").document(duplicate_id).update(
        {"support_count": firestore.Increment(1)}
    )

    # Update the new issue to mark it as duplicate
    if new_issue_id:
        db.collection("issues").document(new_issue_id).update(
            {"status": "Duplicate", "duplicate_of": duplicate_id}
        )

    yield Event(
        output={
            "action": "merged",
            "duplicate_of": duplicate_id,
            "message": "Already reported — tap to support",
        }
    )


@node
def publish_new(ctx: Context, node_input: dict):
    """Passes new unique issue to routing agent."""
    yield Event(output={"action": "publish", "issue": node_input})


root_agent = Workflow(
    name="duplicate_detection_workflow",
    edges=[
        ("START", security_checkpoint),
        Edge(
            from_node=security_checkpoint, to_node=fetch_existing_issues, route="clean"
        ),
        Edge(from_node=security_checkpoint, to_node=publish_new, route="human_review"),
        (fetch_existing_issues, similarity_agent, route_duplicate_decision),
        Edge(
            from_node=route_duplicate_decision,
            to_node=merge_duplicate,
            route="merge_duplicate",
        ),
        Edge(
            from_node=route_duplicate_decision, to_node=publish_new, route="publish_new"
        ),
        Edge(from_node=fetch_existing_issues, to_node=publish_new, route="publish_new"),
    ],
)

app = App(
    name="duplicate_detection_agent",
    root_agent=root_agent,
)
