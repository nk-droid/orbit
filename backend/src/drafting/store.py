"""What Orbit concluded about each email, and what became of the reply it suggested."""

import time
from typing import Any, Literal

from storage import connect

from .triage import Triage

# suggested: waiting for the user. sending: reserved while Gmail is called.
# in_gmail: put in the user's Gmail drafts for them to finish there.
# none: the email needs no reply.
DraftStatus = Literal["suggested", "sending", "sent", "in_gmail", "discarded", "none"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_triage (
    message_id TEXT PRIMARY KEY,
    gist TEXT NOT NULL,
    needs_reply INTEGER NOT NULL,
    reply TEXT,
    status TEXT NOT NULL,
    sent_message_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""


def put(message_id: str, triage: Triage) -> None:
    """Records Orbit's read of an email. An email is only ever triaged once."""
    now = time.time()
    with connect(_SCHEMA) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO email_triage
                (message_id, gist, needs_reply, reply, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                triage.gist,
                int(triage.needs_reply),
                triage.reply,
                "suggested" if triage.reply else "none",
                now,
                now,
            ),
        )


def get(message_id: str) -> dict[str, Any] | None:
    with connect(_SCHEMA) as connection:
        row = connection.execute(
            "SELECT * FROM email_triage WHERE message_id = ?", (message_id,)
        ).fetchone()
    return dict(row) if row else None


def get_many(message_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not message_ids:
        return {}
    marks = ",".join("?" * len(message_ids))
    with connect(_SCHEMA) as connection:
        rows = connection.execute(
            f"SELECT * FROM email_triage WHERE message_id IN ({marks})", message_ids
        ).fetchall()
    return {row["message_id"]: dict(row) for row in rows}


def _move(message_id: str, to: str, *, only_from: str, **fields: Any) -> bool:
    """Changes a draft's status only if it is in the expected one. True if it moved."""
    assignments = "".join(f", {name} = ?" for name in fields)
    with connect(_SCHEMA) as connection:
        cursor = connection.execute(
            f"UPDATE email_triage SET status = ?, updated_at = ?{assignments} "
            "WHERE message_id = ? AND status = ?",
            (to, time.time(), *fields.values(), message_id, only_from),
        )
    return cursor.rowcount == 1


def edit(message_id: str, reply: str) -> bool:
    return _move(message_id, "suggested", only_from="suggested", reply=reply)


def reserve(message_id: str) -> bool:
    """Claims a draft for sending. Only one caller can win, so it is sent once."""
    return _move(message_id, "sending", only_from="suggested")


def release(message_id: str) -> None:
    """Puts a draft back after a failed send, so it can be tried again."""
    _move(message_id, "suggested", only_from="sending")


def mark_sent(message_id: str, sent_message_id: str) -> None:
    _move(message_id, "sent", only_from="sending", sent_message_id=sent_message_id)


def hand_over(message_id: str, gmail_message_id: str) -> None:
    """The reply is in the user's Gmail drafts now, as the message with this id."""
    _move(message_id, "in_gmail", only_from="sending", sent_message_id=gmail_message_id)


def discard(message_id: str) -> bool:
    return _move(message_id, "discarded", only_from="suggested")
