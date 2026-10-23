"""Orbit's own reminders, kept in the local database the agent and the API share."""

import time
import uuid
from dataclasses import dataclass
from typing import Literal

from storage import connect

# cancelled: the user did not want it after all. It is kept, but never shown.
Status = Literal["open", "done", "cancelled"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    note TEXT,
    due_at REAL NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""
_COLUMNS = "id, title, note, due_at, status, created_at, updated_at"


@dataclass(frozen=True)
class Reminder:
    id: str
    title: str
    note: str | None
    # Times are seconds since the epoch.
    due_at: float
    status: Status
    created_at: float
    updated_at: float


def add(title: str, due_at: float, note: str | None = None) -> Reminder:
    now = time.time()
    reminder = Reminder(
        id=uuid.uuid4().hex[:8],
        title=title,
        note=note,
        due_at=due_at,
        status="open",
        created_at=now,
        updated_at=now,
    )
    with connect(_SCHEMA) as connection:
        connection.execute(
            f"INSERT INTO reminders ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                reminder.id,
                reminder.title,
                reminder.note,
                reminder.due_at,
                reminder.status,
                reminder.created_at,
                reminder.updated_at,
            ),
        )
    return reminder


def get(reminder_id: str) -> Reminder | None:
    with connect(_SCHEMA) as connection:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
    return Reminder(**row) if row else None


def open_reminders() -> list[Reminder]:
    """Everything still to do, soonest first."""
    with connect(_SCHEMA) as connection:
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM reminders WHERE status = 'open' ORDER BY due_at"
        ).fetchall()
    return [Reminder(**row) for row in rows]


def every() -> list[Reminder]:
    """Every reminder, done and cancelled ones included, by when it is due."""
    with connect(_SCHEMA) as connection:
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM reminders ORDER BY due_at"
        ).fetchall()
    return [Reminder(**row) for row in rows]


def done_since(since: float) -> list[Reminder]:
    """What was marked done after `since`, most recent first."""
    with connect(_SCHEMA) as connection:
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM reminders WHERE status = 'done' "
            "AND updated_at >= ? ORDER BY updated_at DESC",
            (since,),
        ).fetchall()
    return [Reminder(**row) for row in rows]


def set_status(reminder_id: str, to: Status, *, only_from: Status) -> Reminder | None:
    """Moves a reminder from one status to another. None if it was not in the
    expected one, or does not exist."""
    with connect(_SCHEMA) as connection:
        cursor = connection.execute(
            "UPDATE reminders SET status = ?, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (to, time.time(), reminder_id, only_from),
        )
    return get(reminder_id) if cursor.rowcount == 1 else None


def change(
    reminder_id: str, *, title: str | None = None, due_at: float | None = None
) -> Reminder | None:
    """Changes an open reminder. What is left as None stays as it was. None if there
    is no open reminder with that id."""
    current = get(reminder_id)
    if current is None or current.status != "open":
        return None
    with connect(_SCHEMA) as connection:
        connection.execute(
            "UPDATE reminders SET title = ?, due_at = ?, updated_at = ? WHERE id = ?",
            (
                current.title if title is None else title,
                current.due_at if due_at is None else due_at,
                time.time(),
                reminder_id,
            ),
        )
    return get(reminder_id)
