"""The user's reminders, for the Reminders screen and the app's alerts."""

import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from reminders import store
from reminders.store import Reminder, Status

from .guard import require_app

router = APIRouter()

# A reminder marked done stays listed this long, so a slip can be undone.
DONE_SHOWN_SECONDS = 24 * 60 * 60


class ReminderOut(BaseModel):
    id: str
    title: str
    note: str | None
    # ISO 8601 with the machine's offset.
    due_at: str
    status: Status
    created_at: str


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds).astimezone().isoformat(timespec="seconds")


def _out(reminder: Reminder) -> ReminderOut:
    return ReminderOut(
        id=reminder.id,
        title=reminder.title,
        note=reminder.note,
        due_at=_iso(reminder.due_at),
        status=reminder.status,
        created_at=_iso(reminder.created_at),
    )


# Plain `def`s, so FastAPI runs the SQLite calls in a worker thread.
@router.get("/reminders", response_model=list[ReminderOut])
def reminders() -> list[ReminderOut]:
    """Open reminders, soonest first, then the ones done in the last day."""
    listed = store.open_reminders()
    listed += store.done_since(time.time() - DONE_SHOWN_SECONDS)
    return [_out(reminder) for reminder in listed]


def _move(reminder_id: str, to: Status, only_from: Status) -> ReminderOut:
    reminder = store.set_status(reminder_id, to, only_from=only_from)
    if reminder is None:
        if store.get(reminder_id) is None:
            raise HTTPException(404, "There is no such reminder.")
        raise HTTPException(409, f"That reminder is not {only_from} any more.")
    return _out(reminder)


@router.post(
    "/reminders/{reminder_id}/done",
    response_model=ReminderOut,
    dependencies=[Depends(require_app)],
)
def mark_done(reminder_id: str) -> ReminderOut:
    return _move(reminder_id, "done", "open")


@router.post(
    "/reminders/{reminder_id}/reopen",
    response_model=ReminderOut,
    dependencies=[Depends(require_app)],
)
def reopen(reminder_id: str) -> ReminderOut:
    return _move(reminder_id, "open", "done")


@router.post(
    "/reminders/{reminder_id}/cancel",
    response_model=ReminderOut,
    dependencies=[Depends(require_app)],
)
def cancel(reminder_id: str) -> ReminderOut:
    return _move(reminder_id, "cancelled", "open")
