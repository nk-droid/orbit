"""Charts kept by Orbit, in the local database the agent and the API share.

- `charts` records each chart saved into Google Sheets: what it measures (its key),
  where it is saved, the request it was made from, and its view as last drawn. It
  lasts as long as the chart does, so a chart asked for again is pulled from Sheets.
- `chart_views` holds the worked-out chart a card fetches, saved or not. It is kept
  for 30 days.
- `chart_book` remembers "Orbit charts", the spreadsheet Orbit saves charts into.
"""

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from storage import connect

KEPT_FOR_DAYS = 30

_CHARTS = """
CREATE TABLE IF NOT EXISTS charts (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    request TEXT NOT NULL,
    view TEXT NOT NULL,
    spreadsheet_id TEXT NOT NULL,
    spreadsheet_title TEXT NOT NULL,
    tab_id INTEGER NOT NULL,
    tab_title TEXT NOT NULL,
    sheet_chart_id INTEGER NOT NULL,
    link TEXT NOT NULL,
    numbers_from TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""
_VIEWS = """
CREATE TABLE IF NOT EXISTS chart_views (
    id TEXT PRIMARY KEY,
    view TEXT NOT NULL,
    made_at REAL NOT NULL
)
"""
_BOOK = """
CREATE TABLE IF NOT EXISTS chart_book (
    one INTEGER PRIMARY KEY CHECK (one = 1),
    spreadsheet_id TEXT NOT NULL,
    title TEXT NOT NULL,
    link TEXT NOT NULL
)
"""
_COLUMNS = (
    "id, key, title, kind, request, view, spreadsheet_id, spreadsheet_title, tab_id, "
    "tab_title, sheet_chart_id, link, numbers_from, created_at, updated_at"
)


@dataclass(frozen=True)
class Saved:
    """A chart saved into Google Sheets."""

    id: str
    key: str
    title: str
    # Its kind, as show_chart names kinds.
    kind: str
    # What it was made from: the source, and how it was charted.
    request: dict[str, Any]
    # The chart as last drawn. Its numbers are pulled from the tab.
    view: dict[str, Any]
    spreadsheet_id: str
    spreadsheet_title: str
    tab_id: int
    tab_title: str
    sheet_chart_id: int
    # Where its tab opens.
    link: str
    numbers_from: str
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Book:
    spreadsheet_id: str
    title: str
    link: str


def _connect():
    connection = connect(_CHARTS)
    connection.execute(_VIEWS)
    connection.execute(_BOOK)
    return connection


def _saved(row) -> Saved:
    return Saved(
        **{
            **dict(row),
            "request": json.loads(row["request"]),
            "view": json.loads(row["view"]),
        }
    )


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def save(
    chart_id: str,
    key: str,
    kind: str,
    request: dict[str, Any],
    view: dict[str, Any],
    spreadsheet_id: str,
    spreadsheet_title: str,
    tab_id: int,
    tab_title: str,
    sheet_chart_id: int,
    link: str,
) -> Saved:
    """Records a chart saved into Sheets. One already recorded with that key is
    replaced: there is one chart for each thing measured."""
    now = time.time()
    with _connect() as connection:
        connection.execute(
            "DELETE FROM charts WHERE key = ? OR id = ?", (key, chart_id)
        )
        connection.execute(
            f"INSERT INTO charts ({_COLUMNS}) VALUES ({', '.join('?' * 15)})",
            (
                chart_id,
                key,
                view["title"],
                kind,
                json.dumps(request),
                json.dumps(view),
                spreadsheet_id,
                spreadsheet_title,
                tab_id,
                tab_title,
                sheet_chart_id,
                link,
                view["numbers_from"],
                now,
                now,
            ),
        )
    found = get(chart_id)
    assert found is not None
    return found


def changed(
    chart_id: str,
    kind: str,
    view: dict[str, Any],
    request: dict[str, Any] | None = None,
    key: str = "",
) -> None:
    """A saved chart drawn as another kind, or with fresh numbers. Fresh numbers from
    a conversation change what it measures, given as `request` and `key`."""
    with _connect() as connection:
        connection.execute(
            "UPDATE charts SET kind = ?, view = ?, title = ?, numbers_from = ?, "
            "updated_at = ? WHERE id = ?",
            (
                kind,
                json.dumps(view),
                view["title"],
                view["numbers_from"],
                time.time(),
                chart_id,
            ),
        )
        if request is not None:
            # One chart is kept for what it measures, as save() keeps it.
            connection.execute(
                "DELETE FROM charts WHERE key = ? AND id != ?", (key, chart_id)
            )
            connection.execute(
                "UPDATE charts SET request = ?, key = ? WHERE id = ?",
                (json.dumps(request), key, chart_id),
            )


def get(chart_id: str) -> Saved | None:
    with _connect() as connection:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM charts WHERE id = ?", (chart_id,)
        ).fetchone()
    return _saved(row) if row else None


def with_key(key: str) -> Saved | None:
    with _connect() as connection:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM charts WHERE key = ?", (key,)
        ).fetchone()
    return _saved(row) if row else None


def listed() -> list[Saved]:
    """Every saved chart, oldest first."""
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM charts ORDER BY created_at"
        ).fetchall()
    return [_saved(row) for row in rows]


def named(asked: str) -> list[Saved]:
    """The saved charts a name can mean: the one called exactly that, or else those
    whose names start with it, or else those that contain it."""
    said = " ".join(asked.casefold().split())
    charts = listed()
    for found in (
        [c for c in charts if c.title.casefold() == said],
        [c for c in charts if said and c.title.casefold().startswith(said)],
        [c for c in charts if said and said in c.title.casefold()],
    ):
        if found:
            return found
    return []


def forget(chart_id: str) -> None:
    with _connect() as connection:
        connection.execute("DELETE FROM charts WHERE id = ?", (chart_id,))


def put_view(chart_id: str, view: dict[str, Any]) -> None:
    """Keeps the worked-out chart a card fetches, and lets go of old ones."""
    now = time.time()
    with _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO chart_views (id, view, made_at) VALUES (?, ?, ?)",
            (chart_id, json.dumps(view), now),
        )
        connection.execute(
            "DELETE FROM chart_views WHERE made_at < ?",
            (now - KEPT_FOR_DAYS * 86400,),
        )


def view(chart_id: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT view FROM chart_views WHERE id = ?", (chart_id,)
        ).fetchone()
    return json.loads(row["view"]) if row else None


def book() -> Book | None:
    """Orbit charts, the spreadsheet charts are saved into, once it has been made."""
    with _connect() as connection:
        row = connection.execute(
            "SELECT spreadsheet_id, title, link FROM chart_book"
        ).fetchone()
    return Book(row["spreadsheet_id"], row["title"], row["link"]) if row else None


def keep_book(spreadsheet_id: str, title: str, link: str) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO chart_book (one, spreadsheet_id, title, link) "
            "VALUES (1, ?, ?, ?)",
            (spreadsheet_id, title, link),
        )


def lose_book() -> None:
    """Forgets Orbit charts, after it was found to be gone: the next save makes it again."""
    with _connect() as connection:
        connection.execute("DELETE FROM chart_book")
