"""Google Sheets: a spreadsheet's tabs and cells, read and changed.

Cells are named as Sheets names them, by a column letter and a row number such as
D7, and every row keeps the number the sheet gives it. So what Orbit says, what the
card shows and what a change touches always point to the same place.

A change is written as if the user had typed it: a number stays a number, a date a
date, and a value that starts with "=" is a formula. A formula that fetches from the
web is refused (`reaches_out`). A change to cells that are already there is made
only if they still hold what the user was shown when they agreed to it.
"""

import asyncio
import csv
import io
import logging
import re
from typing import Any

import aiohttp

from . import api, gdrive

logger = logging.getLogger("orbit.sheets")

_API = "https://sheets.googleapis.com/v4/spreadsheets"

# A tab is read down to this row at most. Few sheets a person keeps are longer.
MAX_READ_ROWS = 10_000
# The rows a tab's headers are looked for in.
_HEADER_ROWS = 10
_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_CELL = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,6})$")
_CORNER = re.compile(r"^([A-Z]{1,3})?([1-9][0-9]{0,6})?$")
# Functions that fetch something from the web whenever the sheet is opened. What
# Orbit writes can be steered by what it has read, such as an email, and one of
# these could carry the sheet's contents off to whoever wrote that.
_REACHES_OUT = re.compile(
    r"\b(IMPORTDATA|IMPORTFEED|IMPORTHTML|IMPORTXML|IMPORTRANGE|IMAGE)\s*\(",
    re.IGNORECASE,
)


class SheetError(Exception):
    """Something about the sheet stands in the way. The message says what, and what
    the agent can do about it."""


class ChangedError(Exception):
    """The cells a draft was to change no longer hold what the user was shown."""


def letter(index: int) -> str:
    """The letters a column is named by, from its place counted from 0: 0 is A, 26 AA."""
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(ord("A") + rest) + name
    return name


def column_index(letters: str) -> int:
    """A column's place counted from 0, from the letters it is named by."""
    index = 0
    for char in letters.upper():
        index = index * 26 + ord(char) - ord("A") + 1
    return index - 1


def cell(name: str) -> tuple[int, int]:
    """A cell named like D7, as its row number and its column counted from 0."""
    match = _CELL.match(name.strip().upper())
    if match is None:
        raise SheetError(
            f"{name!r} is not a cell. Name a cell by its column letter and row "
            "number, such as D7."
        )
    return int(match[2]), column_index(match[1])


def quoted(tab: str) -> str:
    """A tab's name as a range names it: quoted, so that any name works."""
    return "'" + tab.replace("'", "''") + "'"


def reaches_out(value: str) -> str | None:
    """The function fetching from the web that a value would call, if any.

    Only a value that can be taken for a formula calls anything. Words such as
    "photo image (front)" are only words.
    """
    if not value.lstrip().startswith(("=", "+", "-", "@")):
        return None
    match = _REACHES_OUT.search(value)
    return match[1].upper() if match else None


def area(text: str) -> tuple[str, tuple[int, int] | None]:
    """Part of a tab as the user or the model named it, such as A2:D50, B7, A:C or
    5:9, checked and written the way the API takes it. With it, the columns it
    covers counted from 0, or None when it covers whole rows."""
    named = text.strip().upper().replace(" ", "").replace("$", "")
    ends = named.split(":")
    corners = [_CORNER.match(end) for end in ends]
    if (
        not named
        or len(ends) > 2
        or not all(corners)
        or any(not (match[1] or match[2]) for match in corners)
        # A single cell needs both its letter and its number.
        or (len(ends) == 1 and not (corners[0][1] and corners[0][2]))
        # Either both ends name a column, or neither does.
        or len({bool(match[1]) for match in corners}) > 1
    ):
        raise SheetError(
            f"{text!r} is not a part of a sheet. Give cells such as A2:D50, a cell "
            "such as B7, columns such as A:C or rows such as 5:9."
        )
    if not corners[0][1]:
        return named, None
    columns = sorted(column_index(match[1]) for match in corners)
    return named, (columns[0], columns[-1])


async def spreadsheet(
    session: aiohttp.ClientSession, spreadsheet_id: str
) -> dict[str, Any]:
    """A spreadsheet's name, where it opens, and its tabs in order."""
    if not _ID.match(spreadsheet_id):
        raise SheetError(
            "That is not the id of a spreadsheet. Find the sheet with sheets_read."
        )
    fields = (
        "spreadsheetId,spreadsheetUrl,properties(title),sheets(properties("
        "sheetId,title,sheetType,hidden,gridProperties(rowCount,columnCount)))"
    )
    try:
        data = await api.get_json(
            session, f"{_API}/{spreadsheet_id}", {"fields": fields}
        )
    except api.GoogleApiError as e:
        # What the Sheets API says when the id is a document or another file.
        if e.status == 400 and "not supported for this document" in e.message:
            raise SheetError(
                "That file is not a Google Sheet. Use drive_read to open it."
            ) from e
        raise
    tabs = []
    for sheet in data.get("sheets", []):
        properties = sheet.get("properties", {})
        # A tab that only holds a chart has no cells.
        if properties.get("sheetType", "GRID") != "GRID":
            continue
        grid = properties.get("gridProperties", {})
        tabs.append(
            {
                "id": properties.get("sheetId", 0),
                "title": properties["title"],
                "rows": grid.get("rowCount", 0),
                "columns": grid.get("columnCount", 0),
                "hidden": bool(properties.get("hidden")),
            }
        )
    return {
        "id": data.get("spreadsheetId", spreadsheet_id),
        "title": data["properties"]["title"],
        "link": data.get("spreadsheetUrl"),
        "tabs": tabs,
    }


def tab_named(book: dict[str, Any], name: str) -> dict[str, Any]:
    """The tab with this name, or with no name the first tab the user can see."""
    tabs = book["tabs"]
    if not tabs:
        raise SheetError("That spreadsheet has no tabs with cells in them.")
    said = name.strip().casefold()
    if not said:
        return next((tab for tab in tabs if not tab["hidden"]), tabs[0])
    exact = [tab for tab in tabs if tab["title"].casefold() == said]
    partly = [tab for tab in tabs if said in tab["title"].casefold()]
    found = exact or partly
    if len(found) == 1:
        return found[0]
    names = ", ".join(tab["title"] for tab in tabs)
    raise SheetError(
        f"The spreadsheet {book['title']} has no single tab called {name}. Its tabs "
        f"are: {names}. Ask the user which one."
    )


def _top(sheet: dict[str, Any], rows: int) -> str:
    """The first rows of a tab, as many as it has of them. A range past the end of
    a tab is refused."""
    return f"{quoted(sheet['title'])}!1:{max(1, min(rows, sheet['rows']))}"


def tab_with_id(book: dict[str, Any], tab_id: int) -> dict[str, Any]:
    """The tab a draft was made for, found again even if it has been renamed."""
    found = next((tab for tab in book["tabs"] if tab["id"] == tab_id), None)
    if found is None:
        raise SheetError("The tab that was to be changed is no longer there.")
    return found


async def _value_ranges(
    session: aiohttp.ClientSession,
    spreadsheet_id: str,
    ranges: list[str],
    render: str = "FORMATTED_VALUE",
) -> list[dict[str, Any]]:
    """The values of several ranges in one call, in the order asked."""
    params: list[tuple[str, str]] = [("ranges", part) for part in ranges]
    params += [("valueRenderOption", render), ("majorDimension", "ROWS")]
    data = await api.get_json(
        session, f"{_API}/{spreadsheet_id}/values:batchGet", params
    )
    return data.get("valueRanges", [])


def _start(value_range: dict[str, Any]) -> tuple[int, int]:
    """Where a range's values begin: the row number, and the column counted from 0."""
    corner = value_range.get("range", "").rsplit("!", 1)[-1].split(":")[0]
    match = _CORNER.match(corner)
    letters, digits = (match[1], match[2]) if match else (None, None)
    return int(digits or 1), column_index(letters) if letters else 0


def _lines(value_range: dict[str, Any]) -> list[tuple[int, list[str]]]:
    """The rows of a range with anything in them, each with its row number and its
    cells from column A on."""
    top, left = _start(value_range)
    lines = []
    for offset, values in enumerate(value_range.get("values", [])):
        cells = [""] * left + [str(value) for value in values]
        if any(text.strip() for text in cells):
            lines.append((top + offset, cells))
    return lines


def _header(lines: list[tuple[int, list[str]]]) -> tuple[int, list[str]] | None:
    """The row of headers: the first of the top rows that is at least half as full as
    the fullest of them. A title above the headers is passed over that way."""
    top = lines[:_HEADER_ROWS]
    if not top:
        return None
    filled = [sum(1 for text in cells if text.strip()) for _, cells in top]
    most = max(filled)
    return next(line for line, count in zip(top, filled) if count * 2 >= most)


def _first_filled(cells: list[str]) -> int:
    return next((n for n, text in enumerate(cells) if text.strip()), len(cells))


def _span(cells: list[str], left: int, right: int) -> list[str]:
    """The cells from column `left` to `right`, empty where a row is shorter."""
    return [cells[n] if n < len(cells) else "" for n in range(left, right + 1)]


async def read(
    session: aiohttp.ClientSession,
    spreadsheet_id: str,
    tab: str = "",
    cells: str = "",
    matching: str = "",
) -> dict[str, Any]:
    """One tab of a spreadsheet, or part of it, as the user sees it.

    Comes back with the tab's headers and its rows. Rows with nothing in them are
    left out, and every other row keeps its number. With `cells`, only that part of
    the tab is read. With `matching`, only the rows with those words in a cell.
    """
    book = await spreadsheet(session, spreadsheet_id)
    sheet = tab_named(book, tab)
    where = quoted(sheet["title"])
    columns: tuple[int, int] | None = None
    last_row: int | None = None
    if cells.strip():
        named, columns = area(cells)
        top, part = await _value_ranges(
            session,
            spreadsheet_id,
            [_top(sheet, _HEADER_ROWS), f"{where}!{named}"],
        )
        header = _header(_lines(top))
        lines = [
            line for line in _lines(part) if header is None or line[0] != header[0]
        ]
    else:
        (part,) = await _value_ranges(
            session, spreadsheet_id, [_top(sheet, MAX_READ_ROWS)]
        )
        lines = _lines(part)
        header = _header(lines)
        last_row = lines[-1][0] if lines else 0
        lines = [line for line in lines if header is None or line[0] != header[0]]
    if matching.strip():
        said = matching.strip().casefold()
        lines = [
            line for line in lines if any(said in text.casefold() for text in line[1])
        ]

    # The columns shown run from the first with anything in it to the last.
    everything = [values for _, values in lines] + ([header[1]] if header else [])
    if columns is None:
        left = min((_first_filled(values) for values in everything), default=0)
        right = max((len(values) for values in everything), default=1) - 1
    else:
        left, right = columns
    right = max(left, right)
    return {
        "id": book["id"],
        "title": book["title"],
        "link": book["link"],
        "tabs": [tab["title"] for tab in book["tabs"]],
        "tab": sheet["title"],
        "tab_id": sheet["id"],
        # How many rows and columns the tab has room for, used or not.
        "grid": (sheet["rows"], sheet["columns"]),
        "first_column": left,
        "header_row": header[0] if header else None,
        "headers": _span(header[1], left, right) if header else [],
        "rows": [
            {"row": number, "cells": _span(values, left, right)}
            for number, values in lines
        ],
        # The last row with anything in it, when the whole tab was read.
        "last_row": last_row,
        # False when the tab has more rows than are read at once.
        "complete": bool(cells.strip()) or sheet["rows"] <= MAX_READ_ROWS,
    }


async def headers_and_rows(
    session: aiohttp.ClientSession,
    spreadsheet_id: str,
    sheet: dict[str, Any],
    rows: list[int],
) -> tuple[list[str], dict[int, list[str]], dict[int, list[Any]]]:
    """A tab's headers from column A on, and some of its rows: as the user sees
    them, and as they are kept, with each formula as it is written. A change is
    checked against the second when it is carried out."""
    where = quoted(sheet["title"])
    distinct = sorted(set(rows))
    shown, kept = await asyncio.gather(
        _value_ranges(
            session,
            spreadsheet_id,
            [_top(sheet, _HEADER_ROWS), *(f"{where}!{n}:{n}" for n in distinct)],
        ),
        _value_ranges(
            session, spreadsheet_id, [f"{where}!{n}:{n}" for n in distinct], "FORMULA"
        ),
    )
    header = _header(_lines(shown[0]))
    as_shown = {
        n: [str(value) for value in (part.get("values") or [[]])[0]]
        for n, part in zip(distinct, shown[1:])
    }
    as_kept = {n: _kept(part) for n, part in zip(distinct, kept)}
    return (header[1] if header else []), as_shown, as_kept


def _kept(part: dict[str, Any]) -> list[Any]:
    """One row as it is kept, without the empty cells at its end."""
    values = list((part.get("values") or [[]])[0])
    while values and values[-1] == "":
        values.pop()
    return values


async def _kept_rows(
    session: aiohttp.ClientSession, spreadsheet_id: str, tab: str, rows: list[int]
) -> dict[int, list[Any]]:
    where = quoted(tab)
    distinct = sorted(set(rows))
    parts = await _value_ranges(
        session, spreadsheet_id, [f"{where}!{n}:{n}" for n in distinct], "FORMULA"
    )
    return {n: _kept(part) for n, part in zip(distinct, parts)}


def kept_cell(rows: dict[int, list[Any]], row: int, column: int) -> Any:
    """What one cell holds as it is kept: a formula as written, "" when empty."""
    values = rows.get(row, [])
    return values[column] if column < len(values) else ""


async def _batch(
    session: aiohttp.ClientSession, spreadsheet_id: str, requests: list[dict]
) -> dict[str, Any]:
    return await api.send_json(
        session,
        "POST",
        f"{_API}/{spreadsheet_id}:batchUpdate",
        {"requests": requests},
    )


async def _write(
    session: aiohttp.ClientSession, spreadsheet_id: str, data: list[dict]
) -> dict[str, Any]:
    """Writes values as if the user had typed them."""
    return await api.send_json(
        session,
        "POST",
        f"{_API}/{spreadsheet_id}/values:batchUpdate",
        {"valueInputOption": "USER_ENTERED", "data": data},
    )


async def add_rows(
    session: aiohttp.ClientSession,
    spreadsheet_id: str,
    tab_id: int,
    columns: list[int],
    rows: list[list[str]],
) -> dict[str, Any]:
    """Writes rows under the last row of the tab that has anything in it.

    `columns` are where the values of each row go, counted from 0. Only those cells
    are written: a column the user gave nothing for is left alone, so a formula
    that fills it is not in the way. The tab is made larger if it has to be.
    """
    book = await spreadsheet(session, spreadsheet_id)
    sheet = tab_with_id(book, tab_id)
    if sheet["rows"] > MAX_READ_ROWS:
        raise SheetError(
            f"The tab {sheet['title']} has room for more than {MAX_READ_ROWS} rows, "
            "more than Orbit reads, so Orbit cannot tell where it ends."
        )
    where = quoted(sheet["title"])
    (part,) = await _value_ranges(session, spreadsheet_id, [_top(sheet, MAX_READ_ROWS)])
    lines = _lines(part)
    first = (lines[-1][0] if lines else 0) + 1
    last = first + len(rows) - 1

    larger = []
    if last > sheet["rows"]:
        larger.append(("ROWS", last - sheet["rows"]))
    if max(columns) >= sheet["columns"]:
        larger.append(("COLUMNS", max(columns) + 1 - sheet["columns"]))
    if larger:
        await _batch(
            session,
            spreadsheet_id,
            [
                {
                    "appendDimension": {
                        "sheetId": tab_id,
                        "dimension": dimension,
                        "length": length,
                    }
                }
                for dimension, length in larger
            ],
        )
    # One range for each column, holding the values of every row in it.
    await _write(
        session,
        spreadsheet_id,
        [
            {
                "range": f"{where}!{letter(column)}{first}:{letter(column)}{last}",
                "majorDimension": "COLUMNS",
                "values": [[row[n] for row in rows]],
            }
            for n, column in enumerate(columns)
        ],
    )
    return {
        "spreadsheet_id": spreadsheet_id,
        "tab": sheet["title"],
        "rows": list(range(first, last + 1)),
    }


async def set_cells(
    session: aiohttp.ClientSession,
    spreadsheet_id: str,
    tab_id: int,
    cells: list[str],
    values: list[str],
    before: list[Any],
) -> dict[str, Any]:
    """Writes the values into the cells, if each still holds what it held before.

    Raises ChangedError, and changes nothing, when one of them does not.
    """
    book = await spreadsheet(session, spreadsheet_id)
    sheet = tab_with_id(book, tab_id)
    places = [cell(name) for name in cells]
    rows = await _kept_rows(
        session, spreadsheet_id, sheet["title"], [row for row, _ in places]
    )
    now = [kept_cell(rows, row, column) for row, column in places]
    if now != before:
        raise ChangedError
    where = quoted(sheet["title"])
    await _write(
        session,
        spreadsheet_id,
        [
            {"range": f"{where}!{name}", "values": [[value]]}
            for name, value in zip(cells, values)
        ],
    )
    return {"spreadsheet_id": spreadsheet_id, "tab": sheet["title"], "cells": cells}


async def delete_rows(
    session: aiohttp.ClientSession,
    spreadsheet_id: str,
    tab_id: int,
    rows: list[int],
    before: dict[int, list[Any]],
) -> dict[str, Any]:
    """Deletes whole rows, if each still holds what it held before. The rows below
    move up.

    Raises ChangedError, and deletes nothing, when one of them does not: a row
    added above them would otherwise have the wrong rows deleted.
    """
    book = await spreadsheet(session, spreadsheet_id)
    sheet = tab_with_id(book, tab_id)
    now = await _kept_rows(session, spreadsheet_id, sheet["title"], rows)
    if now != before:
        raise ChangedError
    # From the bottom up, so that no deletion moves a row still to be deleted. One
    # call: Google makes all of them or none.
    await _batch(
        session,
        spreadsheet_id,
        [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": tab_id,
                        "dimension": "ROWS",
                        "startIndex": row - 1,
                        "endIndex": row,
                    }
                }
            }
            for row in sorted(set(rows), reverse=True)
        ],
    )
    return {"spreadsheet_id": spreadsheet_id, "tab": sheet["title"], "deleted": rows}


async def create(
    session: aiohttp.ClientSession,
    title: str,
    columns: list[str],
    rows: list[list[str]],
) -> dict[str, Any]:
    """A new spreadsheet with these headers and rows, at the top of the user's Drive.

    Drive makes it from the rows written as CSV, the way Sheets imports a file, so a
    number becomes a number and a date a date, and it needs only Drive's own write
    access. The first row is then frozen and made bold as headers, if the Sheets
    API allows: the spreadsheet is there whether or not that works.
    """
    text = io.StringIO()
    csv.writer(text, lineterminator="\n").writerows([columns, *rows])
    created = await gdrive.create_spreadsheet(session, title, text.getvalue())
    try:
        book = await spreadsheet(session, created["id"])
        await _batch(session, created["id"], _headed(book["tabs"][0]["id"]))
    except (
        api.GoogleApiError,
        SheetError,
        IndexError,
        aiohttp.ClientError,
        asyncio.TimeoutError,
    ):
        logger.info("the new spreadsheet's headers were left as they are")
    return {
        "spreadsheet_id": created["id"],
        "title": created["name"],
        "link": created["link"],
    }


def _headed(tab_id: int) -> list[dict[str, Any]]:
    """What makes a tab's first row stand out as its headers."""
    return [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": tab_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        },
    ]
