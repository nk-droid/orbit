"""Lists: one task done for every item of a list, such as accepting each of 75
invitations, and how far it has got.

A list is kept from the moment it is drafted, item by item, so that a conversation
that ends partway, or a voice session that breaks, leaves it to be carried on from
the next item. Kept in the local database the agent and the API share.
"""

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from storage import connect

# What a list is doing: waiting for the user's yes, being done, paused until they
# say to go on, done for every item, or turned down.
DRAFTED = "drafted"
RUNNING = "running"
PAUSED = "paused"
FINISHED = "finished"
CANCELLED = "cancelled"

# Where each item has got. A written draft, such as an email, is kept for the user to
# look over with the others, and sent with them once they say yes.
WAITING = "waiting"
DONE = "done"
FAILED = "failed"
KEPT = "kept"

_LISTS = """
CREATE TABLE IF NOT EXISTS lists (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    changes INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""
_ITEMS = """
CREATE TABLE IF NOT EXISTS list_items (
    list_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    item TEXT NOT NULL,
    link TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    said TEXT NOT NULL DEFAULT '',
    checked INTEGER NOT NULL DEFAULT 1,
    draft TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (list_id, number)
)
"""


@dataclass(frozen=True)
class Item:
    number: int
    # The item as it was given: its words and what tells it apart, such as its link.
    item: str
    # Its web address, when it has one, for the card.
    link: str
    state: str
    # What came of it, in a few words: "accepted", or why it could not be done.
    said: str
    # False when the model said it was done but nothing it did could be seen.
    checked: bool
    # A written draft kept for the user to look over: its kind, what it does, and
    # what the user is shown. Empty for any other item.
    draft: dict[str, Any]


@dataclass(frozen=True)
class TaskList:
    id: str
    # What is done for each item, in words that fit every one.
    task: str
    # Whether doing it changes or sends something, rather than only reading.
    changes: bool
    state: str
    created_at: float
    updated_at: float
    items: list[Item]

    def counted(self, state: str) -> int:
        return sum(1 for item in self.items if item.state == state)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def left(self) -> int:
        return self.counted(WAITING)

    def next_item(self) -> Item | None:
        """The first item not yet done, in the list's order."""
        return next((item for item in self.items if item.state == WAITING), None)

    def item(self, number: int) -> Item:
        return next(item for item in self.items if item.number == number)


def _connect():
    connection = connect(_LISTS)
    connection.execute(_ITEMS)
    return connection


def _item(row) -> Item:
    return Item(
        number=row["number"],
        item=row["item"],
        link=row["link"],
        state=row["state"],
        said=row["said"],
        checked=bool(row["checked"]),
        draft=json.loads(row["draft"]) if row["draft"] else {},
    )


def create(task: str, changes: bool, items: list[tuple[str, str]]) -> TaskList:
    """Keeps a new list, waiting for the user's yes: each item with its link, or ""
    when it has none. A list drafted before and never agreed to is dropped."""
    now = time.time()
    list_id = uuid.uuid4().hex[:10]
    with _connect() as connection:
        for (stale,) in connection.execute(
            "SELECT id FROM lists WHERE state = ?", (DRAFTED,)
        ).fetchall():
            _drop(connection, stale)
        connection.execute(
            "INSERT INTO lists (id, task, changes, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (list_id, task, int(changes), DRAFTED, now, now),
        )
        connection.executemany(
            "INSERT INTO list_items (list_id, number, item, link, state) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (list_id, number, item, link, WAITING)
                for number, (item, link) in enumerate(items, start=1)
            ],
        )
    found = get(list_id)
    assert found is not None
    return found


def _drop(connection, list_id: str) -> None:
    connection.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
    connection.execute("DELETE FROM lists WHERE id = ?", (list_id,))


def get(list_id: str) -> TaskList | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM lists WHERE id = ?", (list_id,)
        ).fetchone()
        if row is None:
            return None
        items = connection.execute(
            "SELECT * FROM list_items WHERE list_id = ? ORDER BY number", (list_id,)
        ).fetchall()
    return TaskList(
        id=row["id"],
        task=row["task"],
        changes=bool(row["changes"]),
        state=row["state"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        items=[_item(item) for item in items],
    )


def unfinished(since: float) -> TaskList | None:
    """The list last worked on since then that still has items to do: paused, or
    left running by a conversation that ended. None if there is none."""
    with _connect() as connection:
        row = connection.execute(
            "SELECT id FROM lists WHERE state IN (?, ?) AND updated_at >= ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (RUNNING, PAUSED, since),
        ).fetchone()
    if row is None:
        return None
    found = get(row["id"])
    if found is None or (found.left == 0 and not found.counted(KEPT)):
        return None
    return found


def set_state(list_id: str, state: str) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE lists SET state = ?, updated_at = ? WHERE id = ?",
            (state, time.time(), list_id),
        )


def mark(
    list_id: str,
    number: int,
    state: str,
    said: str,
    *,
    checked: bool = True,
    draft: dict[str, Any] | None = None,
) -> None:
    """Notes what came of one item."""
    with _connect() as connection:
        connection.execute(
            "UPDATE list_items SET state = ?, said = ?, checked = ?, draft = ? "
            "WHERE list_id = ? AND number = ?",
            (
                state,
                said,
                int(checked),
                json.dumps(draft) if draft else "",
                list_id,
                number,
            ),
        )
        connection.execute(
            "UPDATE lists SET updated_at = ? WHERE id = ?", (time.time(), list_id)
        )
