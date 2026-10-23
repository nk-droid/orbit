"""What Orbit keeps for job applications, in the local database the agent and the API
share.

- `job_profile` holds the user's profile, one row per field: its value, where the
  value came from, and what other sources said, so a disagreement can be shown.
- `job_answers` holds answers the user gave to questions Orbit asks only when a form
  needs them, such as their expected salary, kept when they said to keep them.
- `job_settings` holds how searches run, as the user set them by voice.
- `job_resume` holds the résumé uploaded with every application, and its text.
- `jobs` holds every job a search found, numbered as the Sheet numbers it, with what
  the Sheet does not keep: the posting's text, and whether it is in the Sheet yet.
- `job_searches` holds what each search read, kept and left out.
- `job_applications` holds each application: the form as it was read, the answers
  drafted and approved, the cover letter, what the checks found, and how it ended.
  A form over several steps has a row for each step, all with the number of the
  first.
- `job_boards` holds each company's own job board, as Orbit found it, or that it
  found none, so that it is not looked for again at every search.
- `job_book` remembers "Orbit jobs", the spreadsheet the user reviews jobs in.
"""

import json
import time
from dataclasses import dataclass
from typing import Any

from storage import connect

from . import search

_PROFILE = """
CREATE TABLE IF NOT EXISTS job_profile (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    others TEXT NOT NULL,
    updated_at REAL NOT NULL
)
"""
_ANSWERS = """
CREATE TABLE IF NOT EXISTS job_answers (
    question TEXT PRIMARY KEY,
    answer TEXT NOT NULL,
    updated_at REAL NOT NULL
)
"""
_SETTINGS = """
CREATE TABLE IF NOT EXISTS job_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""
# Where the one résumé Orbit once kept was. It moves to job_resumes when read.
_RESUME = """
CREATE TABLE IF NOT EXISTS job_resume (
    one INTEGER PRIMARY KEY CHECK (one = 1),
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    kept_at REAL NOT NULL
)
"""


_RESUMES = """
CREATE TABLE IF NOT EXISTS job_resumes (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    role TEXT NOT NULL,
    kept_at REAL NOT NULL
)
"""
_JOBS = """
CREATE TABLE IF NOT EXISTS jobs (
    number INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    site TEXT NOT NULL,
    url TEXT NOT NULL,
    apply_url TEXT NOT NULL,
    applies_on TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT NOT NULL,
    remote TEXT NOT NULL,
    posted TEXT NOT NULL,
    experience TEXT NOT NULL DEFAULT '',
    salary TEXT NOT NULL,
    fit TEXT NOT NULL,
    why TEXT NOT NULL,
    posting TEXT NOT NULL,
    search INTEGER NOT NULL,
    status TEXT NOT NULL,
    apply INTEGER NOT NULL,
    notes TEXT NOT NULL,
    applied_at REAL,
    confirmation TEXT NOT NULL,
    in_sheet INTEGER NOT NULL,
    found_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""
_SEARCHES = """
CREATE TABLE IF NOT EXISTS job_searches (
    number INTEGER PRIMARY KEY,
    site TEXT NOT NULL,
    query TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    read INTEGER NOT NULL,
    kept INTEGER NOT NULL,
    left_out TEXT NOT NULL,
    in_sheet INTEGER NOT NULL
)
"""
_APPLICATIONS = """
CREATE TABLE IF NOT EXISTS job_applications (
    number INTEGER PRIMARY KEY,
    job INTEGER NOT NULL,
    step INTEGER NOT NULL,
    first INTEGER NOT NULL,
    url TEXT NOT NULL,
    form TEXT NOT NULL,
    answers TEXT NOT NULL,
    letter TEXT NOT NULL,
    findings TEXT NOT NULL,
    status TEXT NOT NULL,
    confirmation TEXT NOT NULL,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    submitted_at REAL,
    in_sheet INTEGER NOT NULL,
    resume TEXT NOT NULL DEFAULT ''
)
"""
_BOARDS = """
CREATE TABLE IF NOT EXISTS job_boards (
    company TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    system TEXT NOT NULL,
    slug TEXT NOT NULL,
    source TEXT NOT NULL,
    checked_at REAL NOT NULL
)
"""
_BOOK = """
CREATE TABLE IF NOT EXISTS job_book (
    one INTEGER PRIMARY KEY CHECK (one = 1),
    spreadsheet_id TEXT NOT NULL,
    title TEXT NOT NULL,
    link TEXT NOT NULL,
    jobs_tab INTEGER NOT NULL,
    searches_tab INTEGER NOT NULL,
    applications_tab INTEGER NOT NULL,
    columns TEXT NOT NULL DEFAULT '[]'
)
"""


def _connect():
    connection = connect(_PROFILE)
    for schema in (
        _ANSWERS, _SETTINGS, _RESUME, _RESUMES, _JOBS, _SEARCHES, _APPLICATIONS,
        _BOARDS, _BOOK,
    ):  # fmt: skip
        connection.execute(schema)
    _brought_up_to_date(connection)
    return connection


def _brought_up_to_date(connection: Any) -> None:
    """What was kept before Orbit held several résumés: the one résumé joins the
    others, and each application gains the résumé it sends. What was kept before
    Orbit noted the experience a job asks for: each job gains it, from what was kept
    of its posting. And Orbit jobs gains a note of the columns Orbit set up in it."""
    if connection.execute("SELECT 1 FROM job_resume").fetchone():
        connection.execute(
            "INSERT OR IGNORE INTO job_resumes (name, path, source, text, role, "
            "kept_at) SELECT name, path, source, text, '', kept_at FROM job_resume"
        )
        connection.execute("DELETE FROM job_resume")
    if "resume" not in _columns(connection, "job_applications"):
        connection.execute(
            "ALTER TABLE job_applications ADD COLUMN resume TEXT NOT NULL DEFAULT ''"
        )
        # An application drafted before sends the résumé its draft names.
        for number, answers in connection.execute(
            "SELECT number, answers FROM job_applications"
        ).fetchall():
            sent = next(
                (
                    answer.get("value")
                    for answer in json.loads(answers or "[]")
                    if answer.get("asks") == "resume" and answer.get("value")
                ),
                "",
            )
            if isinstance(sent, str) and sent:
                connection.execute(
                    "UPDATE job_applications SET resume = ? WHERE number = ?",
                    (sent, number),
                )
    if "experience" not in _columns(connection, "jobs"):
        connection.execute(
            "ALTER TABLE jobs ADD COLUMN experience TEXT NOT NULL DEFAULT ''"
        )
        for number, url, why, posting in connection.execute(
            "SELECT number, url, why, posting FROM jobs"
        ).fetchall():
            asked = (
                search.experience_in(url)
                or next(
                    (
                        said
                        for quote in why.split("; ")
                        if (said := search.experience_of(quote))
                    ),
                    "",
                )
                or search.experience_said(posting)
            )
            if asked:
                connection.execute(
                    "UPDATE jobs SET experience = ? WHERE number = ?", (asked, number)
                )
    if "columns" not in _columns(connection, "job_book"):
        connection.execute(
            "ALTER TABLE job_book ADD COLUMN columns TEXT NOT NULL DEFAULT '[]'"
        )


def _columns(connection: Any, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


@dataclass(frozen=True)
class Known:
    """A field of the profile as it is known: its value, a text or a list of texts,
    where it came from, and what other sources said instead."""

    key: str
    value: str | list[str]
    source: str
    others: list[dict[str, Any]]


def profile() -> dict[str, Known]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT key, value, source, others FROM job_profile"
        ).fetchall()
    return {
        row["key"]: Known(
            row["key"],
            json.loads(row["value"]),
            row["source"],
            json.loads(row["others"]),
        )
        for row in rows
    }


def keep(known: list[Known], *, replace_all: bool = False) -> None:
    """Records fields of the profile. With `replace_all`, a profile built again from
    its sources: what the user said themselves is kept, and the rest is replaced."""
    now = time.time()
    with _connect() as connection:
        if replace_all:
            connection.execute("DELETE FROM job_profile WHERE source != 'you'")
        for field in known:
            connection.execute(
                "INSERT OR REPLACE INTO job_profile (key, value, source, others, "
                "updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    field.key,
                    json.dumps(field.value, ensure_ascii=False),
                    field.source,
                    json.dumps(field.others, ensure_ascii=False),
                    now,
                ),
            )


def answers() -> dict[str, str]:
    with _connect() as connection:
        rows = connection.execute("SELECT question, answer FROM job_answers").fetchall()
    return {row["question"]: row["answer"] for row in rows}


def keep_answer(question: str, answer: str) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO job_answers (question, answer, updated_at) "
            "VALUES (?, ?, ?)",
            (question, answer, time.time()),
        )


def settings() -> dict[str, Any]:
    with _connect() as connection:
        rows = connection.execute("SELECT key, value FROM job_settings").fetchall()
    return {row["key"]: json.loads(row["value"]) for row in rows}


def keep_setting(key: str, value: Any) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO job_settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )


@dataclass(frozen=True)
class Resume:
    """One of the user's résumés, such as one for each kind of role they apply to:
    each application sends the one that suits its job."""

    name: str
    path: str
    # Where it came from, as the user knows it: "from your computer", or the address.
    source: str
    text: str
    # The work it is for, as the user said, such as "Backend Developer"; "" when
    # they have not said.
    role: str
    kept_at: float


def resumes() -> list[Resume]:
    """Every résumé the user has, the one kept first, first."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT name, path, source, text, role, kept_at FROM job_resumes "
            "ORDER BY kept_at, name"
        ).fetchall()
    return [Resume(**dict(row)) for row in rows]


def resume(name: str) -> Resume | None:
    """The résumé of that name, if the user has one."""
    return next((kept for kept in resumes() if kept.name == name), None)


def keep_resume(name: str, path: str, source: str, text: str, role: str = "") -> None:
    """Keeps a résumé with the others: one of the same name takes its place."""
    with _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO job_resumes (name, path, source, text, role, "
            "kept_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, path, source, text, role, time.time()),
        )


def resume_for(name: str, role: str) -> None:
    """Says what work a résumé is for."""
    with _connect() as connection:
        connection.execute(
            "UPDATE job_resumes SET role = ? WHERE name = ?", (role, name)
        )


def forget_resume(name: str) -> None:
    with _connect() as connection:
        connection.execute("DELETE FROM job_resumes WHERE name = ?", (name,))


# Where a job stands. The Sheet's Status column shows the same words.
FOUND = "Found"
APPROVED = "Approved"
DRAFTING = "Drafting"
FILLED = "Filled"
SUBMITTED = "Submitted"
SKIPPED = "Skipped"
NEEDS_YOU = "Needs you"
FAILED = "Failed"


@dataclass(frozen=True)
class Job:
    """A job a search found."""

    number: int
    key: str
    site: str
    url: str
    apply_url: str
    # "company" when the application is on the company's own page, or else the job
    # board it can only be applied to on, such as "LinkedIn".
    applies_on: str
    company: str
    title: str
    location: str
    remote: str
    posted: str
    # The years of experience it asks for, as search.experience_of() writes them.
    experience: str
    salary: str
    fit: str
    why: str
    posting: str
    search: int
    status: str
    apply: bool
    notes: str
    applied_at: float | None
    confirmation: str
    in_sheet: bool
    found_at: float
    updated_at: float


def _job(row: Any) -> Job:
    return Job(
        **{**dict(row), "apply": bool(row["apply"]), "in_sheet": bool(row["in_sheet"])}
    )


def add_job(**fields: Any) -> Job | None:
    """Records a job, numbered after the last. None if it is one already recorded:
    the same posting, or the same company, role and place."""
    now = time.time()
    with _connect() as connection:
        if connection.execute(
            "SELECT 1 FROM jobs WHERE key = ?", (fields["key"],)
        ).fetchone():
            return None
        number = connection.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 FROM jobs"
        ).fetchone()[0]
        row = {
            "number": number,
            "apply_url": "",
            "applies_on": "company",
            "remote": "",
            "posted": "",
            "experience": "",
            "salary": "",
            "posting": "",
            "status": FOUND,
            "apply": False,
            "notes": "",
            "applied_at": None,
            "confirmation": "",
            "in_sheet": False,
            **fields,
            "found_at": now,
            "updated_at": now,
        }
        columns = ", ".join(row)
        connection.execute(
            f"INSERT INTO jobs ({columns}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )
    return job(number)


def job(number: int) -> Job | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE number = ?", (number,)
        ).fetchone()
    return _job(row) if row else None


def jobs() -> list[Job]:
    """Every job, newest first."""
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM jobs ORDER BY number DESC").fetchall()
    return [_job(row) for row in rows]


def change_job(number: int, **changes: Any) -> None:
    if not changes:
        return
    sets = ", ".join(f"{name} = ?" for name in changes)
    with _connect() as connection:
        connection.execute(
            f"UPDATE jobs SET {sets}, updated_at = ? WHERE number = ?",
            (*changes.values(), time.time(), number),
        )


@dataclass(frozen=True)
class Search:
    """A search run on one site: what was searched, and what came of it."""

    number: int
    site: str
    query: str
    started_at: float
    finished_at: float | None
    read: int
    kept: int
    # How many postings were left out, by why.
    left_out: dict[str, int]
    in_sheet: bool


def _search(row: Any) -> Search:
    return Search(
        **{
            **dict(row),
            "left_out": json.loads(row["left_out"]),
            "in_sheet": bool(row["in_sheet"]),
        }
    )


def start_search(site: str, query: str) -> int:
    with _connect() as connection:
        cursor = connection.execute(
            "INSERT INTO job_searches (site, query, started_at, read, kept, left_out, "
            "in_sheet) VALUES (?, ?, ?, 0, 0, '{}', 0)",
            (site, query, time.time()),
        )
        return int(cursor.lastrowid or 0)


def finish_search(number: int, read: int, kept: int, left_out: dict[str, int]) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE job_searches SET finished_at = ?, read = ?, kept = ?, left_out = ? "
            "WHERE number = ?",
            (time.time(), read, kept, json.dumps(left_out), number),
        )


def searches() -> list[Search]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM job_searches ORDER BY number DESC"
        ).fetchall()
    return [_search(row) for row in rows]


def searches_in_sheet(numbers: list[int]) -> None:
    with _connect() as connection:
        connection.executemany(
            "UPDATE job_searches SET in_sheet = 1 WHERE number = ?",
            [(number,) for number in numbers],
        )


# Where an application stands.
DRAFTING_IT = "drafting"
DRAFTED = "drafted"
FILLED_IN = "filled"
SENT = "submitted"
STOPPED = "stopped"
FAILED_IT = "failed"


@dataclass(frozen=True)
class Application:
    """An application to one job: the form as it was read (the form script's JSON),
    the answers as saved by jobs/forms.py, the cover letter, what the checks found,
    and where it stands."""

    number: int
    job: int
    # Its step, from 1, on a form over several steps, and the number of the
    # application's first step.
    step: int
    first: int
    url: str
    form: dict[str, Any]
    answers: list[dict[str, Any]]
    letter: str
    findings: list[dict[str, Any]]
    status: str
    confirmation: str
    started_at: float
    updated_at: float
    submitted_at: float | None
    in_sheet: bool
    # The name of the résumé it sends.
    resume: str = ""


_JSON = ("form", "answers", "findings")


def _application(row: Any) -> Application:
    values = dict(row)
    for name in _JSON:
        values[name] = json.loads(values[name])
    return Application(**{**values, "in_sheet": bool(values["in_sheet"])})


def start_application(
    job: int,
    url: str,
    form: dict[str, Any],
    step: int = 1,
    first: int = 0,
    resume: str = "",
) -> int:
    """Records an application's form, or the next step of one whose first step is
    `first`, and the résumé it sends."""
    now = time.time()
    with _connect() as connection:
        cursor = connection.execute(
            "INSERT INTO job_applications (job, step, first, url, form, answers, "
            "letter, findings, status, confirmation, started_at, updated_at, "
            "in_sheet, resume) VALUES (?, ?, ?, ?, ?, '[]', '', '[]', ?, '', ?, ?, 0, ?)",
            (job, step, first, url, json.dumps(form), DRAFTING_IT, now, now, resume),
        )
        number = int(cursor.lastrowid or 0)
        if not first:
            connection.execute(
                "UPDATE job_applications SET first = ? WHERE number = ?",
                (number, number),
            )
        return number


def steps_of(first: int) -> list[Application]:
    """Every step of an application, in order."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM job_applications WHERE first = ? ORDER BY step", (first,)
        ).fetchall()
    return [_application(row) for row in rows]


def application(number: int) -> Application | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM job_applications WHERE number = ?", (number,)
        ).fetchone()
    return _application(row) if row else None


def applications() -> list[Application]:
    """Every application, the newest first."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM job_applications ORDER BY number DESC"
        ).fetchall()
    return [_application(row) for row in rows]


def change_application(number: int, **changes: Any) -> None:
    if not changes:
        return
    values = {
        name: json.dumps(value) if name in _JSON else value
        for name, value in changes.items()
    }
    sets = ", ".join(f"{name} = ?" for name in values)
    with _connect() as connection:
        connection.execute(
            f"UPDATE job_applications SET {sets}, updated_at = ? WHERE number = ?",
            (*values.values(), time.time(), number),
        )


def applications_in_sheet(numbers: list[int]) -> None:
    with _connect() as connection:
        connection.executemany(
            "UPDATE job_applications SET in_sheet = 1 WHERE number = ?",
            [(number,) for number in numbers],
        )


@dataclass(frozen=True)
class Board:
    """A company's own job board: the hiring system it is on and the company's name
    there, or an empty system when none was found. `source` says how Orbit came to
    know it: "you" for a company the user named, "found" for one it came across."""

    company: str
    name: str
    system: str
    slug: str
    source: str
    checked_at: float


def _company(name: str) -> str:
    return " ".join(name.casefold().split())


def board(company: str) -> Board | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM job_boards WHERE company = ?", (_company(company),)
        ).fetchone()
    return Board(**dict(row)) if row else None


def boards() -> list[Board]:
    """Every company's board Orbit knows of, and those it found none for."""
    with _connect() as connection:
        rows = connection.execute("SELECT * FROM job_boards ORDER BY name").fetchall()
    return [Board(**dict(row)) for row in rows]


def keep_board(name: str, system: str, slug: str, source: str) -> None:
    """Records a company's board, or that it has none when `system` is empty. A
    company the user named stays theirs."""
    was = board(name)
    if was is not None and was.source == "you":
        source = "you"
    with _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO job_boards (company, name, system, slug, source, "
            "checked_at) VALUES (?, ?, ?, ?, ?, ?)",
            (_company(name), " ".join(name.split()), system, slug, source, time.time()),
        )


@dataclass(frozen=True)
class Book:
    """Orbit jobs, the spreadsheet the user reviews jobs in, and its tabs by id, so
    a tab the user renamed is still found."""

    spreadsheet_id: str
    title: str
    link: str
    jobs_tab: int
    searches_tab: int
    applications_tab: int
    # The columns of its Jobs tab that Orbit has set up there, so that one the user
    # deleted is not set up again: none noted for a book made before Orbit noted
    # them.
    columns: tuple[str, ...] = ()


def book() -> Book | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT spreadsheet_id, title, link, jobs_tab, searches_tab, "
            "applications_tab, columns FROM job_book"
        ).fetchone()
    if not row:
        return None
    return Book(**{**dict(row), "columns": tuple(json.loads(row["columns"] or "[]"))})


def keep_book(book: Book) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO job_book (one, spreadsheet_id, title, link, "
            "jobs_tab, searches_tab, applications_tab, columns) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (
                book.spreadsheet_id,
                book.title,
                book.link,
                book.jobs_tab,
                book.searches_tab,
                book.applications_tab,
                json.dumps(list(book.columns)),
            ),
        )


def lose_book() -> None:
    """Orbit jobs is gone: the next save makes it again, with every job in it."""
    with _connect() as connection:
        connection.execute("DELETE FROM job_book")
        connection.execute("UPDATE jobs SET in_sheet = 0")
        connection.execute("UPDATE job_searches SET in_sheet = 0")
        connection.execute("UPDATE job_applications SET in_sheet = 0")
