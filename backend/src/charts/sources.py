"""Where a chart's numbers come from, each read into a dataset in full.

The search tools give the model one page of results to talk about. A chart has to
count everything, so its sources are named by what they cover, such as a calendar
range or a Gmail search, and read here page after page.

The model names a source in one of these forms, and parse() turns it into a source
that says exactly what to read, kept with a saved chart so it can be read again.
Numbers from the conversation, which the user said or a tool gave, are passed by the
model itself and checked by the tool.
"""

import email.utils
import re
from datetime import date, datetime, time, timedelta
from typing import Any

import aiohttp

import attachments
import files
from integrations.google import gcal, gdrive, gmail, gsheets
from reminders import store as reminders

from .build import ChartError
from .data import Dataset, from_csv, number

# Numbers from the conversation; "said" is how charts were named before.
CONVERSATION = "conversation"
GIVEN = (CONVERSATION, "said")
MAX_EVENTS = 2500
# Gmail looks up about 50 messages a second for one user: more keeps the user waiting.
MAX_MESSAGES = 500

FORMS = (
    'Name the source as one of: a dataset sheets_read gave; "conversation", with '
    "points, for numbers the user said or a tool gave, such as on a web page; "
    '"calendar: YYYY-MM-DD to YYYY-MM-DD"; "mail: <a Gmail search>"; "reminders"; '
    '"drive:<file id>" for a CSV file in Drive; "added:<file name>" for a file the '
    "user handed over."
)
NO_SUCH_SOURCE = "{asked!r} is not a source I know. " + FORMS + " {datasets}"
SHEET_FILE = (
    "That file is a Google Sheet. Read it with sheets_read, then chart the dataset "
    "it gives."
)
NOT_A_TABLE = (
    "{name} is not a CSV file, so it cannot be charted. A spreadsheet in Excel can be "
    "opened as a Google Sheet first."
)
NO_ADDED = (
    "The user has handed over no file called {asked!r}. Their files are: {names}."
)
NOTHING_ADDED = "The user has handed over no files."
POINTS_WRITTEN = (
    'Write each point as "label: value", such as "March: 8" or "Python: 5y+": a '
    "number in digits, or a level as it was written. With several columns in y, "
    'give a value for each, as "March: 8 | 3".'
)
NO_POINTS = 'For "conversation", give the numbers as points.'
EMPTY = "{name} has nothing in it to chart."

_CALENDAR = re.compile(
    r"^calendar\s*:?\s*(\d{4}-\d{2}-\d{2})\s*(?:to|until|through|-|\u2013)\s*"
    r"(\d{4}-\d{2}-\d{2})$",
    re.IGNORECASE,
)
_CATEGORIES = {
    "CATEGORY_PERSONAL": "Primary",
    "CATEGORY_PROMOTIONS": "Promotions",
    "CATEGORY_SOCIAL": "Social",
    "CATEGORY_UPDATES": "Updates",
    "CATEGORY_FORUMS": "Forums",
}
# Places that write the month before the day, as in 10/3/2026 for 3 October.
_MONTH_FIRST = ("en_US", "es_US", "en_PH", "fil_PH")


def parse(asked: str, datasets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The source the model named, as what to read. `datasets` are those read in this
    conversation, by name."""
    said = " ".join(asked.split())
    folded = said.casefold()
    if folded in GIVEN:
        return {"from": CONVERSATION, "label": "This conversation", "noun": "Values"}
    if folded == "reminders":
        return {"from": "reminders", "label": "Reminders", "noun": "Reminders"}
    if folded.startswith("calendar"):
        match = _CALENDAR.match(said)
        if not match:
            raise ChartError(FORMS)
        start, end = sorted([match[1], match[2]])
        return {
            "from": "calendar",
            "start": start,
            "end": end,
            "label": f"Calendar, {start} to {end}",
            "noun": "Events",
        }
    if folded.startswith("mail:") or folded.startswith("gmail:"):
        query = said.split(":", 1)[1].strip()
        return {
            "from": "mail",
            "query": query,
            "label": f"Mail: {query}" if query else "Inbox",
            "noun": "Emails",
        }
    if folded.startswith("drive:"):
        return {
            "from": "drive",
            "file_id": said.split(":", 1)[1].strip(),
            "noun": "Rows",
        }
    if folded.startswith("added:"):
        return {"from": "added", "name": said.split(":", 1)[1].strip(), "noun": "Rows"}
    for found in (
        [name for name in datasets if name.casefold() == folded],
        [name for name in datasets if folded and name.casefold().startswith(folded)],
        [name for name in datasets if folded and folded in name.casefold()],
    ):
        if len(found) == 1:
            return dict(datasets[found[0]])
    known = (
        "The datasets read in this conversation are: " + "; ".join(datasets) + "."
        if datasets
        else ""
    )
    raise ChartError(NO_SUCH_SOURCE.format(asked=asked, datasets=known).strip())


def sheet_source(
    sheet: dict[str, Any], cells: str = "", matching: str = ""
) -> dict[str, Any]:
    """The source of a dataset that sheets_read gave: a tab, or part of one."""
    return {
        "from": "sheet",
        "spreadsheet_id": sheet["id"],
        "tab": sheet["tab"],
        "cells": cells.strip(),
        "matching": matching.strip(),
        "label": f"{sheet['title']} / {sheet['tab']}",
        "link": sheet.get("link"),
        "noun": "Rows",
    }


async def load(
    session: aiohttp.ClientSession,
    source: dict[str, Any],
    points: list[str] | None = None,
) -> Dataset:
    """The dataset a source names, read now."""
    kind = source["from"]
    if kind == "sheet":
        return await from_sheet(session, source)
    if kind == "calendar":
        return await from_calendar(session, source)
    if kind == "mail":
        return await from_mail(session, source)
    if kind == "drive":
        return await from_drive(session, source)
    if kind == "reminders":
        return from_reminders(source)
    if kind == "added":
        return from_added(source)
    if kind in GIVEN:
        return from_points(source, points or source.get("points") or [])
    raise ChartError(FORMS)


async def from_sheet(session: aiohttp.ClientSession, source: dict[str, Any]) -> Dataset:
    """A tab of a Google Sheet: its rows as the user sees them, for the units their
    cells show, and as kept, for numbers as numbers."""
    sheet = await gsheets.read(
        session,
        source["spreadsheet_id"],
        source["tab"],
        source.get("cells", ""),
        source.get("matching", ""),
    )
    kept = await gsheets.raw_rows(
        session, source["spreadsheet_id"], sheet["tab"], source.get("cells", "")
    )
    left = sheet["first_column"]
    width = len(sheet["headers"]) or max(
        (len(r["cells"]) for r in sheet["rows"]), default=0
    )
    headers = [
        (sheet["headers"][n] if n < len(sheet["headers"]) else "").strip()
        or f"Column {gsheets.letter(left + n)}"
        for n in range(width)
    ]
    rows = []
    for row in sheet["rows"]:
        values = kept.get(row["row"], [])
        rows.append(
            [values[c] if c < len(values) else "" for c in range(left, left + width)]
        )
    notes = []
    if not sheet["complete"]:
        notes.append(
            f"Only the first {gsheets.MAX_READ_ROWS} rows of the tab were read."
        )
    return Dataset(
        name=f"{sheet['title']} / {sheet['tab']}",
        source={**source, "tab": sheet["tab"], "link": sheet.get("link")},
        headers=headers,
        rows=rows,
        shown=[row["cells"] for row in sheet["rows"]],
        notes=notes,
        day_first=not str(sheet.get("locale", "")).startswith(_MONTH_FIRST),
    )


async def from_calendar(
    session: aiohttp.ClientSession, source: dict[str, Any]
) -> Dataset:
    """Every event in a range of days on the user's calendar."""
    first, last = date.fromisoformat(source["start"]), date.fromisoformat(source["end"])
    zone = datetime.now().astimezone().tzinfo
    start = datetime.combine(first, time.min, zone)
    end = datetime.combine(last + timedelta(days=1), time.min, zone)
    events, more = await gcal.every_event(session, start, end, MAX_EVENTS)
    rows = []
    for event in events:
        all_day = "date" in event["start"]
        if all_day:
            when, begins, ends, hours = event["start"]["date"], "", "", None
        else:
            began = datetime.fromisoformat(event["start"]["dateTime"]).astimezone()
            ended = datetime.fromisoformat(event["end"]["dateTime"]).astimezone()
            when = began.date().isoformat()
            begins, ends = began.strftime("%H:%M"), ended.strftime("%H:%M")
            hours = round((ended - began).total_seconds() / 3600, 2)
        guests = sum(1 for a in event.get("attendees", []) if not a.get("self"))
        rows.append(
            [
                when,
                begins,
                ends,
                hours,
                event.get("summary", "(no title)"),
                guests,
                "yes" if all_day else "no",
            ]
        )
    notes = [f"Only the first {MAX_EVENTS} events were read."] if more else []
    return Dataset(
        name=source["label"],
        source=source,
        headers=["Date", "Start", "End", "Hours", "Title", "Guests", "All day"],
        rows=rows,
        notes=notes,
    )


async def from_mail(session: aiohttp.ClientSession, source: dict[str, Any]) -> Dataset:
    """Every email a Gmail search finds, up to a limit: when, from whom, and what."""
    ids, more = await gmail.every_id(session, source["query"], MAX_MESSAGES)
    rows = []
    for message in await gmail.headers_of(session, ids):
        name, address = email.utils.parseaddr(gmail.header(message, "From"))
        labels = message.get("labelIds", [])
        category = next((_CATEGORIES[n] for n in labels if n in _CATEGORIES), "")
        if "SENT" in labels:
            category = "Sent"
        rows.append(
            [
                gmail.received_at(message).date().isoformat(),
                name or address,
                address.rpartition("@")[2].casefold(),
                gmail.header(message, "Subject"),
                "yes" if "UNREAD" in labels else "no",
                category or "Primary",
            ]
        )
    notes = [f"Only the latest {MAX_MESSAGES} emails were read."] if more else []
    return Dataset(
        name=source["label"],
        source=source,
        headers=["Date", "From", "Domain", "Subject", "Unread", "Category"],
        rows=rows,
        notes=notes,
    )


def from_reminders(source: dict[str, Any]) -> Dataset:
    """Every reminder the user has, done and cancelled ones included."""
    rows = [
        [
            reminder.title,
            datetime.fromtimestamp(reminder.due_at).date().isoformat(),
            reminder.status,
            datetime.fromtimestamp(reminder.created_at).date().isoformat(),
        ]
        for reminder in reminders.every()
    ]
    return Dataset(
        name=source["label"],
        source=source,
        headers=["Title", "Due", "Status", "Created"],
        rows=rows,
    )


def _from_text(name: str, text: str, source: dict[str, Any]) -> Dataset:
    headers, rows = from_csv(text)
    if not headers:
        raise ChartError(EMPTY.format(name=name))
    return Dataset(
        name=name, source={**source, "label": name}, headers=headers, rows=rows
    )


async def from_drive(session: aiohttp.ClientSession, source: dict[str, Any]) -> Dataset:
    """A CSV file in the user's Drive."""
    name, mime_type, text = await gdrive.table_file(session, source["file_id"])
    if mime_type == gdrive.SPREADSHEET:
        raise ChartError(SHEET_FILE)
    if text is None:
        raise ChartError(NOT_A_TABLE.format(name=name))
    return _from_text(name, text, source)


def from_added(source: dict[str, Any]) -> Dataset:
    """A CSV file the user handed over from their computer."""
    tray = attachments.held_by(attachments.TRAY)
    if not tray:
        raise ChartError(NOTHING_ADDED)
    asked = source["name"].casefold()
    if asked in ("", "latest", "last"):
        found = tray[-1:]
    else:
        found = [f for f in tray if f.name.casefold() == asked] or [
            f for f in tray if asked in f.name.casefold()
        ]
    if not found:
        names = ", ".join(f.name for f in tray)
        raise ChartError(NO_ADDED.format(asked=source["name"], names=names))
    held = found[-1]
    if not held.name.casefold().endswith((".csv", ".tsv", ".txt")):
        raise ChartError(NOT_A_TABLE.format(name=held.name))
    text = files.decode(attachments.read(held))
    return _from_text(held.name, text, {**source, "name": held.name})


def from_points(source: dict[str, Any], points: list[str]) -> Dataset:
    """Numbers from the conversation, each with its label, under the columns the
    model named. That each was said or given is checked by the tool."""
    if not points:
        raise ChartError(NO_POINTS)
    headers = source.get("columns") or ["Label", "Value"]
    # Each number as it was written where it was said or given, such as ₹79,999,
    # for its unit and decimals.
    written = source.get("written") or []
    rows, shown = [], []
    for n, item in enumerate(points):
        label, parts = split_point(item)
        # A number, or a level in words, such as 5y+ or Expert.
        values = [
            found if (found := number(part)) is not None else part for part in parts
        ]
        if not label or len(values) != len(headers) - 1 or "" in values:
            raise ChartError(POINTS_WRITTEN)
        rows.append([label, *values])
        shown.append([label, *(written[n] if n < len(written) else parts)])
    return Dataset(
        name=source.get("label", "This conversation"),
        source=source,
        headers=headers,
        rows=rows,
        shown=shown,
    )


# What may stand between a point's label and its values, where there is no colon.
_SPLITS = (";", "=", " - ", " \u2013 ", " \u2014 ")


def split_point(item: Any) -> tuple[str, list[str]]:
    """A point as its label and its values: "March: 8 | 3" is March, 8 and 3."""
    text = str(item)
    label, split, values = text.rpartition(":")
    for other in _SPLITS:
        if split:
            break
        label, split, values = text.rpartition(other)
    if not split:
        return "", []
    return " ".join(label.split()), [part.strip() for part in values.split("|")]
