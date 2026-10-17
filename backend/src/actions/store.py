"""A local record of every action Orbit drafted or took, for the app and for audit.

The gate in memory decides what may run; this is only the log of what happened.
SQLite, because the agent and the API are separate processes reading the same file.
"""

import json
from typing import Any

from storage import connect

from .gate import Action

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    preview TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""


def save(action: Action) -> None:
    with connect(_SCHEMA) as connection:
        connection.execute(
            """
            INSERT INTO actions (id, kind, preview, status, result, error,
                                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status, result = excluded.result,
                error = excluded.error, updated_at = excluded.updated_at
            """,
            (
                action.id,
                action.kind,
                json.dumps(action.preview),
                action.status,
                json.dumps(action.result) if action.result is not None else None,
                action.error,
                action.created_at,
                action.updated_at,
            ),
        )


def recent(limit: int = 20) -> list[dict[str, Any]]:
    """The latest actions, newest first."""
    with connect(_SCHEMA) as connection:
        rows = connection.execute(
            "SELECT * FROM actions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        {
            **dict(row),
            "preview": json.loads(row["preview"]),
            "result": json.loads(row["result"]) if row["result"] else None,
        }
        for row in rows
    ]
