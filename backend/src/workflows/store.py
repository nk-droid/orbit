"""Workflows: tasks the user asked Orbit to remember, so that next time they take
one step.

A workflow is how a task is done, written so that it works again: its steps, with
a {name} where each part that changes goes, and the values that stay the same,
such as the address the user calls "my test email". A task in the browser also
keeps where it begins and the browser it ran in. What the user asked for that
time, a song or a message, is never kept: it is taken from what they ask for
next time. Kept in the local database the agent and the API share.
"""

import json
import re
import time
import uuid
from dataclasses import dataclass

from storage import connect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    steps TEXT NOT NULL,
    asks TEXT NOT NULL,
    keeps TEXT NOT NULL,
    start_url TEXT NOT NULL,
    shown INTEGER,
    profile TEXT,
    created_at REAL NOT NULL,
    used_at REAL,
    uses INTEGER NOT NULL DEFAULT 0
)
"""
_COLUMNS = (
    "id, name, steps, asks, keeps, start_url, shown, profile, created_at, used_at, uses"
)


@dataclass(frozen=True)
class Workflow:
    id: str
    # What the user calls the task, in words that fit any time it is done, such as
    # "play music on YouTube".
    name: str
    # What to do, in order, each with {name} where a part goes.
    steps: list[str]
    # The parts that change, by name: taken from what the user asks for each time.
    asks: list[str]
    # The parts that stay the same, by name, with their values.
    keeps: dict[str, str]
    # Where a task in the browser begins, which may hold parts too. Empty for any
    # other task.
    start_url: str
    # The browser a task in the browser ran in: shown or hidden, and the user's
    # profile it was on. None for any other task.
    shown: bool | None
    profile: str | None
    created_at: float
    used_at: float | None
    uses: int


def _workflow(row) -> Workflow:
    return Workflow(
        id=row["id"],
        name=row["name"],
        steps=json.loads(row["steps"]),
        asks=json.loads(row["asks"]),
        keeps=json.loads(row["keeps"]),
        start_url=row["start_url"],
        shown=None if row["shown"] is None else bool(row["shown"]),
        profile=row["profile"],
        created_at=row["created_at"],
        used_at=row["used_at"],
        uses=row["uses"],
    )


def _connect():
    connection = connect(_SCHEMA)
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(workflows)")
    }
    if "value_name" in columns:
        _upgrade(connection)
    return connection


def _upgrade(connection) -> None:
    """Rewrites workflows saved by the first version, which had one part that
    changed, written {value}, and named it in value_name: the part is named now.
    All in one transaction, which the agent of another conversation may wait on."""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        columns = connection.execute("PRAGMA table_info(workflows)").fetchall()
        if "value_name" not in {column["name"] for column in columns}:
            return
        rows = connection.execute("SELECT * FROM workflows").fetchall()
        connection.execute("DROP TABLE workflows")
        connection.execute(_SCHEMA)
        for row in rows:
            _insert_upgraded(connection, row)


def _insert_upgraded(connection, row) -> None:
    part = re.sub(r"^(the|a|an) ", "", row["value_name"].strip().casefold()) or "value"
    old_steps = json.loads(row["steps"])
    used = "{value}" in row["start_url"] or any("{value}" in s for s in old_steps)
    connection.execute(
        f"INSERT INTO workflows ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["id"],
            row["name"],
            json.dumps([s.replace("{value}", f"{{{part}}}") for s in old_steps]),
            json.dumps([part] if used else []),
            json.dumps({}),
            row["start_url"].replace("{value}", f"{{{part}}}"),
            row["shown"],
            row["profile"],
            row["created_at"],
            row["used_at"],
            row["uses"],
        ),
    )


def listed() -> list[Workflow]:
    """Every saved workflow, oldest first."""
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM workflows ORDER BY created_at"
        ).fetchall()
    return [_workflow(row) for row in rows]


def matching(asked: str) -> list[Workflow]:
    """The workflows a name can mean: the one called exactly that, or else those
    whose names start with it, or else those that contain it."""
    said = " ".join(asked.casefold().split())
    workflows = listed()
    for found in (
        [w for w in workflows if w.name.casefold() == said],
        [w for w in workflows if said and w.name.casefold().startswith(said)],
        [w for w in workflows if said and said in w.name.casefold()],
    ):
        if found:
            return found
    return []


def save(
    name: str,
    steps: list[str],
    asks: list[str],
    keeps: dict[str, str],
    start_url: str = "",
    shown: bool | None = None,
    profile: str | None = None,
) -> Workflow:
    """Saves a workflow. One saved under a name already in use replaces it."""
    workflow = Workflow(
        id=uuid.uuid4().hex[:8],
        name=name,
        steps=steps,
        asks=asks,
        keeps=keeps,
        start_url=start_url,
        shown=shown,
        profile=profile,
        created_at=time.time(),
        used_at=None,
        uses=0,
    )
    with _connect() as connection:
        connection.execute(
            "DELETE FROM workflows WHERE lower(name) = lower(?)", (name,)
        )
        connection.execute(
            f"INSERT INTO workflows ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workflow.id,
                workflow.name,
                json.dumps(workflow.steps),
                json.dumps(workflow.asks),
                json.dumps(workflow.keeps),
                workflow.start_url,
                None if workflow.shown is None else int(workflow.shown),
                workflow.profile,
                workflow.created_at,
                None,
                0,
            ),
        )
    return workflow


def used(workflow_id: str) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE workflows SET used_at = ?, uses = uses + 1 WHERE id = ?",
            (time.time(), workflow_id),
        )


def remove(workflow_id: str) -> None:
    with _connect() as connection:
        connection.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
