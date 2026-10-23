"""Saving charts into Google Sheets, and pulling them back.

A chart is saved as a tab of its numbers with a native Sheets chart beside them: in
Orbit charts, a spreadsheet Orbit makes in the user's Drive the first time, or in
one of the user's own spreadsheets when they ask for it there. A saved chart asked
for again is pulled from its tab, where the user may have corrected it. Its tab is
found by its id, so a tab the user renamed is still found.
"""

import random
from dataclasses import dataclass
from typing import Any

import aiohttp

from integrations.google import api, gsheets

from . import sheet, store
from .build import from_table

BOOK_TITLE = "Orbit charts"
_ABOUT = [
    ["Charts saved by Orbit"],
    ["Each tab holds one chart: its numbers, and the chart beside them."],
]


class GoneError(Exception):
    """The spreadsheet, tab or chart a chart was saved in is no longer there."""


@dataclass(frozen=True)
class Place:
    """Where a chart was saved."""

    spreadsheet_id: str
    spreadsheet_title: str
    tab_id: int
    tab_title: str
    chart_id: int
    link: str


def _gone(error: Exception) -> bool:
    if isinstance(error, gsheets.SheetError):
        return True
    if not isinstance(error, api.GoogleApiError):
        return False
    if error.status == 404:
        return True
    told = error.message.casefold()
    return error.status == 400 and any(
        words in told
        for words in (
            "unable to parse range",
            "no grid with id",
            "no chart with id",
            "invalid chartid",
        )
    )


async def add(
    session: aiohttp.ClientSession, spreadsheet_id: str, view: dict[str, Any]
) -> Place:
    """Adds a chart to a spreadsheet: a tab named after it, its numbers, and the chart."""
    try:
        book = await gsheets.spreadsheet(session, spreadsheet_id)
        taken = {tab["id"] for tab in book["tabs"]}
        tab_id = random.randrange(1, 2**31 - 1)
        while tab_id in taken:
            tab_id = random.randrange(1, 2**31 - 1)
        title = sheet.tab_title(view["title"], [tab["title"] for tab in book["tabs"]])
        reply = await gsheets.update(
            session, spreadsheet_id, sheet.adding(view, tab_id, title)
        )
    except (api.GoogleApiError, gsheets.SheetError) as e:
        if _gone(e):
            raise GoneError from e
        raise
    chart_id = reply["replies"][2]["addChart"]["chart"]["chartId"]
    link = (
        book.get("link")
        or f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    )
    return Place(
        spreadsheet_id=book["id"],
        spreadsheet_title=book["title"],
        tab_id=tab_id,
        tab_title=title,
        chart_id=chart_id,
        link=f"{link.split('#')[0]}#gid={tab_id}",
    )


async def make_book(session: aiohttp.ClientSession) -> store.Book:
    """Makes Orbit charts in the user's Drive, and remembers it."""
    created = await gsheets.create(session, BOOK_TITLE, _ABOUT[0], _ABOUT[1:])
    store.keep_book(created["spreadsheet_id"], created["title"], created["link"])
    found = store.book()
    assert found is not None
    return found


async def _tab(session: aiohttp.ClientSession, saved: store.Saved) -> dict[str, Any]:
    book = await gsheets.spreadsheet(session, saved.spreadsheet_id)
    return gsheets.tab_with_id(book, saved.tab_id)


async def pull(session: aiohttp.ClientSession, saved: store.Saved) -> dict[str, Any]:
    """A saved chart with the numbers its tab holds now."""
    try:
        tab = await _tab(session, saved)
        table = await gsheets.table(session, saved.spreadsheet_id, tab["title"])
    except (api.GoogleApiError, gsheets.SheetError) as e:
        if _gone(e):
            raise GoneError from e
        raise
    if len(table) < 2:
        raise GoneError
    return from_table(saved.view, table)


async def rewrite(
    session: aiohttp.ClientSession, saved: store.Saved, view: dict[str, Any]
) -> None:
    """Puts a chart's fresh numbers in its tab, and draws it again."""
    await _changed(
        session, saved, sheet.rewriting(view, saved.tab_id, saved.sheet_chart_id)
    )


async def redraw(
    session: aiohttp.ClientSession, saved: store.Saved, view: dict[str, Any]
) -> None:
    """Draws a saved chart as another kind."""
    await _changed(
        session, saved, sheet.redrawing(view, saved.tab_id, saved.sheet_chart_id)
    )


async def _changed(
    session: aiohttp.ClientSession, saved: store.Saved, requests: list[dict]
) -> None:
    try:
        await gsheets.update(session, saved.spreadsheet_id, requests)
    except (api.GoogleApiError, gsheets.SheetError) as e:
        if _gone(e):
            raise GoneError from e
        raise


def saved_as(place: Place) -> dict[str, str]:
    """Where a chart is saved, as its view tells the card."""
    return {
        "title": place.spreadsheet_title,
        "tab": place.tab_title,
        "link": place.link,
    }
