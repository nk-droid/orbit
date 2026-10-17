"""What Orbit has drafted and done, for the app's approval cards and activity log."""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from actions import store
from actions.gate import Status

router = APIRouter()


class ActionRecord(BaseModel):
    id: str
    kind: str
    # The draft as the user was told it: what, to whom, the text.
    preview: dict[str, Any]
    status: Status
    error: str | None = None
    # Where the email opens in Gmail, if it was put in the drafts there instead
    # of being sent.
    gmail_draft: str | None = None
    # Seconds since the epoch.
    created_at: float
    updated_at: float


# A plain `def`, so FastAPI runs the SQLite read in a worker thread.
@router.get("/actions", response_model=list[ActionRecord])
def actions(limit: int = 20) -> list[dict[str, Any]]:
    """The most recent actions, newest first."""
    return [
        {**action, "gmail_draft": (action["result"] or {}).get("gmail_draft")}
        for action in store.recent(min(max(limit, 1), 100))
    ]
