"""Orbit jobs: the Google Sheet the user reviews jobs in.

Orbit makes it in the user's Drive the first time, with their yes, and writes to it
freely after that, since it is Orbit's own file. It has three tabs, each in the order
things happened, numbered from the top, with each new row added at the end:

- Jobs: one row per job. Orbit fills in its columns. Apply, a tick box, and Notes are
  the user's. Rows are matched by their number and columns by their headers, so the
  user can sort, filter, move columns and add their own.
- Searches: what each search read, kept and left out, and why.
- Applications: what was sent with each application.

A job's row is written once, when the job is found. After that Orbit writes only its
Status, Applied on and Confirmation, so an edit of the user's anywhere else stays. A
row the user deletes is a job they do not want: it is skipped, never written back.
A tab Orbit once wrote newest first is put in order before anything is added to it.
A column Orbit has come to write since the Sheet was made is put in once, filled in
for every job; one the user deletes after is not put back.
"""

import random
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

import aiohttp

from integrations.google import api, gsheets

from . import forms, store

BOOK_TITLE = "Orbit jobs"
JOBS = "Jobs"
SEARCHES = "Searches"
APPLICATIONS = "Applications"
JOB_COLUMNS = (
    "No.",
    "Found",
    "Company",
    "Role",
    "Location",
    "Remote",
    "Experience",
    "Posted",
    "Salary",
    "Fit",
    "Why it fits",
    "Link",
    "Site",
    "Applies on",
    "Apply",
    "Status",
    "Applied on",
    "Confirmation",
    "Notes",
)
# The columns of a Sheet made before Orbit noted which it set up.
FIRST_JOB_COLUMNS = tuple(name for name in JOB_COLUMNS if name != "Experience")
# What Orbit writes again after a job's row was first written.
CHANGING = ("Status", "Applied on", "Confirmation")
SEARCH_COLUMNS = ("No.", "When", "Site", "Searched for", "Read", "Kept", "Left out")
APPLICATION_COLUMNS = (
    "Job",
    "Company",
    "Role",
    "Submitted",
    "Résumé",
    "Cover letter",
    "Answers",
)
# How many rows of the Jobs tab have a tick box in Apply when it is made. Rows added
# at the end later take theirs from the row above.
_TICK_ROWS = 1000

NOT_A_CHANGE = (
    'Write each change by the job\'s number: "untick 3", "tick 5", "skip 7" or '
    '"note 4: ask about the visa".'
)
NOT_LISTED = "There is no job number {number} in Orbit jobs."


class GoneError(Exception):
    """Orbit jobs, or one of its tabs, is no longer there."""


def _gone(error: Exception) -> bool:
    if isinstance(error, gsheets.SheetError):
        return True
    if not isinstance(error, api.GoogleApiError):
        return False
    told = error.message.casefold()
    return error.status == 404 or (
        error.status == 400 and "unable to parse range" in told
    )


def _key(header: Any) -> str:
    return " ".join(str(header).casefold().split())


def _bold(tab_id: int, columns: tuple[str, ...], first: int = 0) -> dict[str, Any]:
    """A request writing a tab's headers, in bold, from its column `first` on."""
    return {
        "updateCells": {
            "start": {"sheetId": tab_id, "rowIndex": 0, "columnIndex": first},
            "rows": [
                {
                    "values": [
                        {
                            "userEnteredValue": {"stringValue": name},
                            "userEnteredFormat": {"textFormat": {"bold": True}},
                        }
                        for name in columns
                    ]
                }
            ],
            "fields": "userEnteredValue,userEnteredFormat.textFormat.bold",
        }
    }


def _new_tab(tab_id: int, title: str) -> dict[str, Any]:
    return {
        "addSheet": {
            "properties": {
                "sheetId": tab_id,
                "title": title,
                "gridProperties": {"frozenRowCount": 1},
            }
        }
    }


async def make_book(session: aiohttp.ClientSession) -> store.Book:
    """Makes Orbit jobs in the user's Drive: the Jobs tab with a tick box in Apply,
    and the Searches and Applications tabs."""
    made = await gsheets.create(session, BOOK_TITLE, list(JOB_COLUMNS), [])
    spreadsheet_id = made["spreadsheet_id"]
    jobs_tab = (await gsheets.spreadsheet(session, spreadsheet_id))["tabs"][0]["id"]
    searches_tab, applications_tab = random.sample(range(1, 2**31 - 1), 2)
    apply_column = JOB_COLUMNS.index("Apply")
    await gsheets.update(
        session,
        spreadsheet_id,
        [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": jobs_tab, "title": JOBS},
                    "fields": "title",
                }
            },
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": jobs_tab,
                        "startRowIndex": 1,
                        "endRowIndex": _TICK_ROWS,
                        "startColumnIndex": apply_column,
                        "endColumnIndex": apply_column + 1,
                    },
                    "rule": {"condition": {"type": "BOOLEAN"}},
                }
            },
            _new_tab(searches_tab, SEARCHES),
            _bold(searches_tab, SEARCH_COLUMNS),
            _new_tab(applications_tab, APPLICATIONS),
            _bold(applications_tab, APPLICATION_COLUMNS),
        ],
    )
    book = store.Book(
        spreadsheet_id,
        made["title"],
        made["link"],
        jobs_tab,
        searches_tab,
        applications_tab,
        JOB_COLUMNS,
    )
    store.keep_book(book)
    return book


def _day(moment: float | None) -> str:
    return datetime.fromtimestamp(moment).strftime("%Y-%m-%d") if moment else ""


def _when(moment: float | None) -> str:
    return datetime.fromtimestamp(moment).strftime("%Y-%m-%d %H:%M") if moment else ""


def job_row(job: store.Job) -> dict[str, Any]:
    """A job's row in the Jobs tab, by column."""
    return {
        "No.": job.number,
        "Found": _day(job.found_at),
        "Company": job.company,
        "Role": job.title,
        "Location": job.location,
        "Remote": job.remote,
        "Experience": job.experience,
        "Posted": job.posted,
        "Salary": job.salary,
        "Fit": job.fit.capitalize(),
        "Why it fits": job.why,
        "Link": job.url,
        "Site": job.site,
        "Applies on": "Company site" if job.applies_on == "company" else job.applies_on,
        "Apply": job.apply,
        "Status": job.status,
        "Applied on": _when(job.applied_at),
        "Confirmation": job.confirmation,
        "Notes": job.notes,
    }


def _left_out(left_out: dict[str, int]) -> str:
    return ", ".join(f"{count} {why}" for why, count in left_out.items() if count)


def search_row(search: store.Search) -> dict[str, Any]:
    return {
        "No.": search.number,
        "When": _when(search.started_at),
        "Site": search.site,
        "Searched for": search.query,
        "Read": search.read,
        "Kept": search.kept,
        "Left out": _left_out(search.left_out),
    }


# A cell holds at most 50,000 characters.
CELL_CHARS = 45_000


def application_row(application: store.Application, job: store.Job) -> dict[str, Any]:
    """What was sent in an application, as its row in the Applications tab: the
    résumé, the cover letter, and each question with its answer."""
    answers = forms.from_saved(application.answers)
    resume = next((a.shown() for a in answers if a.asks == "resume"), "")
    asked = "\n".join(
        f"{answer.question}: {answer.shown()}"
        for answer in answers
        if answer.asks not in ("resume", "cover_letter") and not answer.blank
    )
    return {
        "Job": job.number,
        "Company": job.company,
        "Role": job.title,
        "Submitted": _when(application.submitted_at),
        "Résumé": resume,
        "Cover letter": application.letter[:CELL_CHARS],
        "Answers": asked[:CELL_CHARS],
    }


def _ticked(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in ("true", "yes", "y", "x", "1", "✓")


def _number(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _cell(row: list[Any], index: int | None) -> Any:
    return row[index] if index is not None and index < len(row) else ""


@dataclass
class Tab:
    """A tab as it is read: its title, its columns by header, and its rows."""

    title: str
    columns: dict[str, int]
    rows: list[list[Any]] = field(default_factory=list)

    def index(self, name: str) -> int | None:
        return self.columns.get(_key(name))

    def by_number(self) -> dict[int, tuple[int, list[Any]]]:
        """Each row by its number, with where it is in the tab, counted from 1."""
        found = {}
        for at, row in enumerate(self.rows, start=2):
            number = _number(_cell(row, self.index("No.")))
            if number is not None:
                found[number] = (at, row)
        return found

    def last(self, name: str) -> int:
        """How far down the tab, under its headers, the last row with something in a
        column is: 0 when none has. Rows read back below it may hold nothing but
        empty tick boxes, which Google reads as false."""
        index = self.index(name)
        return max(
            (
                at
                for at, row in enumerate(self.rows, start=1)
                if _cell(row, index) not in ("", None)
            ),
            default=0,
        )

    def laid_out(self, values: dict[str, Any]) -> list[Any]:
        """A row's values in the tab's own order of columns."""
        width = max(self.columns.values(), default=-1) + 1
        row: list[Any] = [""] * width
        for name, value in values.items():
            if (at := self.index(name)) is not None:
                row[at] = value
        return row


async def _read(session: aiohttp.ClientSession, book: store.Book, tab_id: int) -> Tab:
    try:
        found = await gsheets.spreadsheet(session, book.spreadsheet_id)
        title = gsheets.tab_with_id(found, tab_id)["title"]
        table = await gsheets.table(session, book.spreadsheet_id, title)
    except (api.GoogleApiError, gsheets.SheetError) as e:
        if _gone(e):
            raise GoneError from e
        raise
    header = table[0] if table else []
    return Tab(title, {_key(name): at for at, name in enumerate(header)}, table[1:])


def _range(tab: Tab, column: int, row: int) -> str:
    return f"{gsheets.quoted(tab.title)}!{gsheets.letter(column)}{row}"


def _rows_range(tab: Tab, first: int, count: int) -> str:
    width = max(len(tab.columns), 1)
    return (
        f"{gsheets.quoted(tab.title)}!A{first}:{gsheets.letter(width - 1)}"
        f"{first + count - 1}"
    )


def _adding(tab_id: int, after: int, count: int) -> dict[str, Any]:
    """A request making room for `count` rows after the first `after` rows of a tab,
    its headers among them. Each is set out as the row above it, with a tick box
    where that row has one; right under the headers, as the row below instead."""
    return {
        "insertDimension": {
            "range": {
                "sheetId": tab_id,
                "dimension": "ROWS",
                "startIndex": after,
                "endIndex": after + count,
            },
            "inheritFromBefore": after > 1,
        }
    }


def _newest_first(tab: Tab, name: str) -> bool:
    """Whether a tab's rows go from the newest to the oldest by a column, as Orbit
    once wrote them: not as the user may have sorted them otherwise."""
    index = tab.index(name)
    if index is None:
        return False
    values = [
        float(value) if isinstance(value, (int, float)) else str(value)
        for row in tab.rows
        if (value := _cell(row, index)) not in ("", None)
    ]
    if len(values) < 2 or len({type(value) for value in values}) > 1:
        return False
    return values[0] > values[-1] and all(a >= b for a, b in zip(values, values[1:]))


async def _in_order(
    session: aiohttp.ClientSession, book: store.Book, tab_id: int, name: str
) -> Tab:
    """A tab, read, in the order things happened. One Orbit once wrote newest first
    is sorted by `name`, oldest first, and read again."""
    tab = await _read(session, book, tab_id)
    if not _newest_first(tab, name):
        return tab
    sorting = {
        "sortRange": {
            "range": {
                "sheetId": tab_id,
                "startRowIndex": 1,
                "endRowIndex": 1 + len(tab.rows),
            },
            "sortSpecs": [
                {"dimensionIndex": tab.index(name), "sortOrder": "ASCENDING"}
            ],
        }
    }
    try:
        await gsheets.update(session, book.spreadsheet_id, [sorting])
    except (api.GoogleApiError, gsheets.SheetError) as e:
        if _gone(e):
            raise GoneError from e
        raise
    return await _read(session, book, tab_id)


async def _set_up(
    session: aiohttp.ClientSession, book: store.Book, tab: Tab
) -> tuple[Tab, list[str]]:
    """The Jobs tab with the columns Orbit has come to write since it was made, and
    which those are: each put in after the column before it in JOB_COLUMNS, or else
    after the last, unless the tab has one of its name already. They are noted, so
    that one the user deletes is not put in again."""
    noted = book.columns or FIRST_JOB_COLUMNS
    missing = [name for name in JOB_COLUMNS if name not in noted]
    if not missing or not tab.columns:
        return tab, []
    columns = dict(tab.columns)
    requests: list[dict[str, Any]] = []
    added = []
    for name in missing:
        if _key(name) in columns:
            continue
        before = JOB_COLUMNS[JOB_COLUMNS.index(name) - 1]
        at = (
            columns[_key(before)] if _key(before) in columns else max(columns.values())
        ) + 1
        requests += [
            {
                "insertDimension": {
                    "range": {
                        "sheetId": book.jobs_tab,
                        "dimension": "COLUMNS",
                        "startIndex": at,
                        "endIndex": at + 1,
                    },
                    # Not as a tick box, should the column before be Apply.
                    "inheritFromBefore": before != "Apply",
                }
            },
            _bold(book.jobs_tab, (name,), at),
        ]
        columns = {key: i + (i >= at) for key, i in columns.items()}
        columns[_key(name)] = at
        added.append(name)
    try:
        if requests:
            await gsheets.update(session, book.spreadsheet_id, requests)
    except (api.GoogleApiError, gsheets.SheetError) as e:
        if _gone(e):
            raise GoneError from e
        raise
    store.keep_book(replace(book, columns=JOB_COLUMNS))
    if not requests:
        return tab, []
    return await _read(session, book, book.jobs_tab), added


@dataclass(frozen=True)
class Synced:
    """What a sync of Orbit jobs did."""

    added: int
    deleted: list[int]
    # The jobs' numbers in the order the Jobs tab lists them now, the user's own
    # sorting included.
    order: list[int] = field(default_factory=list)


def _changes(written: list[str]) -> dict[int, dict[str, Any]]:
    """Changes the user asked for by voice, by job number: tick boxes, skips, notes."""
    changes: dict[int, dict[str, Any]] = {}
    for item in written:
        found = re.fullmatch(
            r"\s*(untick|drop|remove|tick|add|keep|skip)\s+(?:job\s+|no\.?\s*|#)?(\d+)\s*"
            r"|\s*note\s+(?:job\s+|no\.?\s*|#)?(\d+)\s*:\s*(.*)",
            item,
            re.IGNORECASE | re.DOTALL,
        )
        if not found:
            raise ValueError(NOT_A_CHANGE)
        if found[3]:
            changes.setdefault(int(found[3]), {})["notes"] = found[4].strip()
            continue
        how, number = found[1].casefold(), int(found[2])
        if how == "skip":
            changes.setdefault(number, {}).update(apply=False, status=store.SKIPPED)
        else:
            changes.setdefault(number, {})["apply"] = how in ("tick", "add", "keep")
    for number in changes:
        if store.job(number) is None:
            raise ValueError(NOT_LISTED.format(number=number))
    return changes


async def sync(
    session: aiohttp.ClientSession, said: list[str] | None = None
) -> Synced | None:
    """Brings Orbit jobs and Orbit's record together: the user's ticks and notes are
    read back, a deleted row is a job skipped, Orbit's own changing columns are
    written, and new jobs, searches and applications go in at the end, in order.
    `said` are changes the user asked for by voice, written into the Sheet as if they
    had made them there. None when Orbit jobs has not been made yet."""
    book = store.book()
    if book is None:
        return None
    changes = _changes(said or [])
    jobs_tab = await _in_order(session, book, book.jobs_tab, "No.")
    jobs_tab, added = await _set_up(session, book, jobs_tab)
    rows = jobs_tab.by_number()
    deleted = []
    for job in store.jobs():
        if job.number in rows:
            _, row = rows[job.number]
            apply = _ticked(_cell(row, jobs_tab.index("Apply")))
            notes = str(_cell(row, jobs_tab.index("Notes")) or "")
            kept = {"apply": apply, "notes": notes, "in_sheet": True}
            store.change_job(
                job.number,
                **{k: v for k, v in kept.items() if getattr(job, k) != v},
            )
        elif job.in_sheet and job.status not in (store.SKIPPED, store.SUBMITTED):
            store.change_job(job.number, status=store.SKIPPED, apply=False)
            deleted.append(job.number)
    for number, change in changes.items():
        store.change_job(number, **change)

    new = sorted(
        (
            job
            for job in store.jobs()
            if not job.in_sheet
            and job.number not in rows
            and job.status != store.SKIPPED
        ),
        key=lambda job: job.number,
    )
    data: list[dict[str, Any]] = []
    for job in store.jobs():
        if job.number not in rows:
            continue
        at, row = rows[job.number]
        wanted = job_row(job)
        names = [*CHANGING, *added]
        if job.number in changes:
            names += ["Apply", "Notes"]
        for name in names:
            index = jobs_tab.index(name)
            if index is not None and _cell(row, index) != wanted[name]:
                data.append(
                    {
                        "range": _range(jobs_tab, index, at),
                        "values": [[wanted[name]]],
                    }
                )
    requests = []
    if new:
        after = 1 + jobs_tab.last("No.")
        requests.append(_adding(book.jobs_tab, after, len(new)))
        data.append(
            {
                "range": _rows_range(jobs_tab, after + 1, len(new)),
                "values": [jobs_tab.laid_out(job_row(job)) for job in new],
            }
        )

    searches = sorted(
        (s for s in store.searches() if s.finished_at and not s.in_sheet),
        key=lambda search: search.number,
    )
    sent = sorted(
        (
            (application, job)
            for application in store.applications()
            if application.status == store.SENT
            and not application.in_sheet
            and (job := store.job(application.job)) is not None
        ),
        key=lambda sent: (sent[0].submitted_at or 0, sent[0].number),
    )
    if searches:
        searches_tab = await _in_order(session, book, book.searches_tab, "No.")
        if not searches_tab.columns:
            searches_tab = Tab(
                searches_tab.title, {_key(n): at for at, n in enumerate(SEARCH_COLUMNS)}
            )
        after = 1 + searches_tab.last("No.")
        requests.append(_adding(book.searches_tab, after, len(searches)))
        data.append(
            {
                "range": _rows_range(searches_tab, after + 1, len(searches)),
                "values": [searches_tab.laid_out(search_row(s)) for s in searches],
            }
        )
    if sent:
        applications_tab = await _in_order(
            session, book, book.applications_tab, "Submitted"
        )
        if not applications_tab.columns:
            applications_tab = Tab(
                applications_tab.title,
                {_key(n): at for at, n in enumerate(APPLICATION_COLUMNS)},
            )
        after = 1 + applications_tab.last("Job")
        requests.append(_adding(book.applications_tab, after, len(sent)))
        data.append(
            {
                "range": _rows_range(applications_tab, after + 1, len(sent)),
                "values": [
                    applications_tab.laid_out(application_row(application, job))
                    for application, job in sent
                ],
            }
        )
    try:
        if requests:
            await gsheets.update(session, book.spreadsheet_id, requests)
        if data:
            await gsheets.write(session, book.spreadsheet_id, data)
    except (api.GoogleApiError, gsheets.SheetError) as e:
        if _gone(e):
            raise GoneError from e
        raise
    for job in new:
        store.change_job(job.number, in_sheet=True)
    store.searches_in_sheet([s.number for s in searches])
    store.applications_in_sheet([application.number for application, _ in sent])
    listed = sorted(rows, key=lambda number: rows[number][0])
    return Synced(
        added=len(new), deleted=deleted, order=listed + [job.number for job in new]
    )


# Errors reading or writing Orbit jobs that say Google could not be used.
TROUBLE = (api.GoogleApiError, api.NotConnectedError, aiohttp.ClientError, TimeoutError)


def why(error: Exception) -> str:
    """Why Orbit jobs could not be read or written, for the user."""
    if isinstance(error, api.NotConnectedError):
        return "Orbit is not connected to Google."
    if isinstance(error, api.GoogleApiError) and error.api_not_enabled:
        return "the Google Sheets API is switched off for Orbit's Google project."
    if isinstance(error, api.GoogleApiError) and error.status == 403:
        return "Google refused: Orbit may lack access to the spreadsheet."
    return "Google could not be reached."


def link(book: store.Book) -> str:
    """The address that opens Orbit jobs on its Jobs tab."""
    return f"{book.link.split('#')[0]}#gid={book.jobs_tab}"
