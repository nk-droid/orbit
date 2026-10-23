"""The agent's tools for acting in the user's name: draft first, then confirm.

Drafting changes nothing. Carrying a draft out goes through the approval gate,
which lets it run only after the user has heard the draft and then answered.
"""

import html
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

import aiohttp
from livekit.agents import RunContext, function_tool, utils
from livekit.agents.llm import ToolError

import attachments as stored
import writing
from actions.effects import keep_in_gmail, perform
from actions.gate import Action, GateError, NotDoneError
from charts import saving as chart_saving
from charts import store as chart_store
from charts.build import table_of
from integrations.google import gcal, gdrive, gmail, gsheets, tokens
from integrations.google.api import GoogleApiError
from integrations.google.scopes import SERVICES, access_level
from reminders.when import WhenError, in_words, resolve

from . import attaching, stage
from .google import reading
from .heard import said_so_far
from .state import OrbitState
from .workflows import trace_of

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EVENT_MINUTES = 12 * 60
# A Drive link written the way one is said, without its beginning.
_SPOKEN_LINK = re.compile(r"(?<![\w/@.-])((?:docs|drive)\.google\.com/[^\s<>\"')\]]+)")

NEXT_STEP = (
    "Read this draft to the user in full and ask whether to go ahead. Call "
    "confirm_action only after they clearly agree in their next message."
)
# A change to a sheet with more rows or cells than this is summed up aloud. Its
# table is on the user's screen.
READ_OUT_WHOLE = 5
FIRST_CHART_NEXT_STEP = (
    "The chart is on the user's screen. Tell them it will be kept in a new "
    "spreadsheet in their Drive, Orbit charts, where every chart goes from now on, "
    "and ask whether to go ahead. Call confirm_action only after they clearly agree "
    "in their next message."
)
CHART_NEXT_STEP = (
    "Say where the chart will go, a new tab in that spreadsheet, and ask whether to "
    "go ahead. Call confirm_action only after they clearly agree in their next "
    "message."
)
NO_CHART = (
    "There is no chart with that id on the user's screen. Show it with show_chart "
    "first."
)
SHEET_NEXT_STEP = (
    "This draft is on the user's screen as a table. Say what it does and how many "
    "rows or cells it touches, read out the first few, and ask whether to go ahead. "
    "Call confirm_action only after they clearly agree in their next message."
)
# The most one draft adds, changes or deletes in a sheet, and the most rows a new
# sheet starts with.
MAX_SHEET_ROWS = 100
MAX_SHEET_CELLS = 100
MAX_NEW_SHEET_ROWS = 500
# What the model is told about a draft it wrote with formatting.
FORMATTED = (
    "The draft is on the user's screen with its headings, lists and tables. Read "
    "them its words as given here, and never say a formatting symbol or a tag."
)
# The kinds of draft that are an email.
_EMAILS = ("gmail.send_email", "gmail.send_reply")
# What the model is told once a draft is in the user's Gmail drafts.
IN_GMAIL = (
    "Tell the user the email is in their Gmail drafts and was not sent, and that "
    "the draft's card has a button that opens it in Gmail."
)
# What the model is told about a draft that carries files.
ATTACHED = "This draft carries {count}. When you read it out, say so and name each one."
# How a file kept as it is written is named and stored, for each thing it is written in.
_KEPT_AS: dict[writing.Format, tuple[str, str, str]] = {
    "plain": ("text file", ".txt", "text/plain"),
    "markdown": ("Markdown file", ".md", "text/markdown"),
    "html": ("HTML file", ".html", "text/html"),
}


def _http() -> aiohttp.ClientSession:
    return utils.http_context.http_session()


def _require_write(service: str) -> None:
    """Refuses to draft what could not be carried out with the current sign-in."""
    granted = (tokens.load() or {}).get("scopes", ())
    if access_level(service, granted) != "write":
        raise ToolError(
            f"Orbit only has read access to {SERVICES[service].label}. Tell the user "
            "to grant write access by running the Google connect command with "
            "--write, then ask again."
        )


def _check_addresses(addresses: list[str]) -> None:
    bad = [address for address in addresses if not _EMAIL.match(address)]
    if bad:
        raise ToolError(
            f"These are not email addresses: {', '.join(bad)}. Ask the user for the "
            "full address."
        )


def whole_links(text: str) -> str:
    """The text with its Drive links complete.

    The model is told to leave https:// out of an address it says aloud, and now
    and then writes one that way too. Without it, what the reader gets may not be
    a link at all.
    """
    return _SPOKEN_LINK.sub(r"https://\1", text)


def _written(
    text: str, written_in: writing.Format, *, breaks: bool = True
) -> tuple[str, dict[str, str]]:
    """What the model wrote, as its words and, if it has formatting, how it looks.

    The words are what is read out. The formatting is clean HTML made from the
    model's Markdown or HTML: the user is shown it, and that same HTML is what is
    sent or saved, so nothing can go out that was not on their screen.
    """
    text = whole_links(writing.as_meant(text))
    html = writing.to_html(text, written_in, breaks=breaks)
    if html is None:
        return text, {}
    if not writing.words(html):
        raise ToolError("There are no words in that. Write the text again.")
    return writing.words(html), {"format": writing.NAMES[written_in], "html": html}


def _told(preview: dict[str, Any]) -> dict[str, Any]:
    """A draft as the model is told it: its words, without the HTML or the table the
    card draws, and each of its files as a name to say."""
    told = {
        name: value
        for name, value in preview.items()
        if name not in ("html", "table", "questions")
    }
    if "attachments" in told:
        told["attachments"] = [attaching.said_as(file) for file in told["attachments"]]
    return told


# What a draft's payload gains by its files, what its preview gains, and what the
# model is to tell the user about them.
Carried = tuple[dict[str, Any], dict[str, Any], tuple[str, ...]]
NOTHING_CARRIED: Carried = ({}, {}, ())


def _already_on(context: RunContext[OrbitState], to: list[str]) -> set[tuple]:
    """The files on the draft that is being changed, as their names and sizes.

    The user asked for those when that draft was made. They need not ask again
    for the draft to keep them, as long as it still goes to the same people.
    """
    waiting = context.userdata.gate.awaiting()
    if waiting is None or sorted(waiting.payload.get("to") or []) != sorted(to):
        return set()
    return {
        (file["name"], file["size"])
        for file in waiting.preview.get("attachments") or []
    }


async def _carrying(
    context: RunContext[OrbitState],
    key: str,
    gathered: attaching.Gathered,
    to: list[str],
    *,
    forwarding: bool = False,
) -> Carried:
    """What a draft's payload and its preview gain by the files copied in for it,
    and what the model is to say about them.

    Nothing when there are none. Raises ToolError, and lets the copies go, when
    the user did not ask for a file to be sent.
    """
    if not gathered.kept:
        return NOTHING_CARRIED
    listed = stored.listed(gathered.kept)
    kept_on = _already_on(context, to)
    new = [
        file["name"] for file in listed if (file["name"], file["size"]) not in kept_on
    ]
    if new:
        fetched = [name for name in gathered.fetched if name in new]
        try:
            await attaching.asked_for(context, new, fetched, forwarding=forwarding)
        # Also when the user talks over it and the wait is cut short.
        except BaseException:
            attaching.let_go(key)
            raise
    carried = {
        "attachments": [file.id for file in gathered.kept],
        "from_tray": gathered.from_tray,
    }
    return carried, {"attachments": listed}, gathered.notes


async def _attached(
    context: RunContext[OrbitState], references: list[str] | None, to: list[str]
) -> Carried:
    """The files the model wants on a draft, fetched, checked and copied in.

    Raises ToolError when a file cannot be had, when they are too much for one
    email, and when the user did not ask for them.
    """
    references = [reference for reference in references or [] if reference.strip()]
    if not references:
        return NOTHING_CARRIED
    if any(
        reference.strip().casefold().startswith("added") for reference in references
    ):
        attaching.handed_over(context)
    key = uuid.uuid4().hex
    # A draft that is being changed keeps the kind of file it had.
    was_office = any(attaching.is_office(name) for name, _ in _already_on(context, to))
    office = await attaching.wants_office(context, was_office)
    gathered = await attaching.gather(_http(), references, office=office, key=key)
    return await _carrying(context, key, gathered, to)


def _chart_table(view: dict[str, Any]) -> dict[str, Any]:
    """A chart's numbers as the draft's table on the user's screen."""

    def shown(value: Any) -> str:
        if value is None:
            return ""
        return f"{value:g}" if isinstance(value, float) else str(value)

    table = table_of(view)
    return {
        "columns": [str(header) for header in table[0]],
        "rows": [[shown(value) for value in row] for row in table[1:]],
    }


def draft_first_save(
    context: RunContext[OrbitState],
    chart_id: str,
    measured: str,
    made: dict[str, Any],
    view: dict[str, Any],
) -> dict[str, Any]:
    """Drafts making Orbit charts, with this chart in it: the one save that waits for
    the user's yes, since it makes a file in their Drive."""
    _require_write("gdrive")
    preview = {
        "what": "Keep charts in Google Sheets",
        "where": f"{chart_saving.BOOK_TITLE}, a new spreadsheet in your Drive",
        "chart": view["title"],
        "chart_id": chart_id,
        "table": _chart_table(view),
    }
    payload = {
        "chart_id": chart_id,
        "into": "",
        "key": measured,
        "kind": made["kind"],
        "request": made,
        "view": view,
    }
    return _draft(
        context,
        "sheets.add_chart",
        payload,
        preview,
        next_step=FIRST_CHART_NEXT_STEP,
    )


def _draft(
    context: RunContext[OrbitState],
    kind: str,
    payload: dict[str, Any],
    preview: dict[str, Any],
    notes: tuple[str, ...] = (),
    *,
    next_step: str = NEXT_STEP,
) -> dict[str, Any]:
    # The session marks the draft as heard once later speech finishes: see agent.py.
    action = context.userdata.gate.propose(kind, payload, preview)
    drafted = {
        "action_id": action.id,
        "status": action.status,
        "draft": _told(preview),
        "next": next_step,
    }
    if "html" in preview:
        drafted["formatting"] = FORMATTED
    if preview.get("attachments"):
        count = len(preview["attachments"])
        files = "1 attachment" if count == 1 else f"{count} attachments"
        drafted["attached"] = " ".join([ATTACHED.format(count=files), *notes])
    return drafted


def _why_not(kind: str, error: Exception) -> str:
    """Why Google refused an approved action, in words the user can act on."""
    if isinstance(error, gsheets.ChangedError):
        return (
            "The sheet has changed since the draft was made, so nothing was changed. "
            "Read it again before drafting the change again."
        )
    if isinstance(error, gsheets.SheetError):
        return str(error)
    if not isinstance(error, GoogleApiError):
        return "Google did not accept it."
    if error.api_not_enabled:
        return (
            "The Google API this needs is not enabled in the user's Google Cloud "
            f"project. Google said: {error.message[:300]}"
        )
    if error.insufficient_scope:
        return "Orbit only has read access. The user needs to grant write access."
    if kind == "drive.add_to_document" and error.status in (403, 404):
        return "Orbit can only change documents it created itself, and this is not one."
    if kind.startswith("sheets.") and error.status == 403:
        return (
            "Google refused the change: the user may only be allowed to view this "
            "spreadsheet, or the cells may be protected."
        )
    return "Google did not accept it."


@function_tool()
async def gmail_draft_reply(
    context: RunContext[OrbitState],
    message_id: str,
    body: str,
    written_in: writing.Format = "plain",
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    """Draft a reply to an email. Nothing is sent by this tool.

    Use this when the user wants to answer an email. Find the email first with
    gmail_search. The reply goes to the sender and stays in the same thread.

    Args:
        message_id: The id of the email being answered, from gmail_search.
        body: The full text of the reply, written as the user would write it.
        written_in: What the body is written in. "plain" for an ordinary reply.
            "markdown" or "html" when it needs formatting, such as a list, a
            table, headings or bold text: write the body in that.
        attachments: Files to send with it, and only ones the user asked for. Each
            is "drive:<file id>", "email:<message id>/<file name>", or
            "added:<file name>" for a file from the user's computer.
    """
    _require_write("gmail")
    body, formatted = _written(body, written_in)
    with reading("Gmail"):
        target = await gmail.reply_target(_http(), message_id)
    carried, shown, notes = await _attached(context, attachments, target["to"])
    preview = {
        "what": "Reply to email",
        "to": target["to"],
        "subject": target["subject"],
        "body": body,
        **formatted,
        **shown,
    }
    payload = {**target, "body": body, "html": formatted.get("html"), **carried}
    return _draft(context, "gmail.send_reply", payload, preview, notes)


@function_tool()
async def gmail_draft_email(
    context: RunContext[OrbitState],
    to: list[str],
    subject: str,
    body: str,
    written_in: writing.Format = "plain",
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    """Draft a new email. Nothing is sent by this tool.

    Use this when the user wants to write to someone and it is not a reply to an
    existing email. You need each recipient's full email address; ask if you do
    not have it.

    Args:
        to: The recipients' email addresses.
        subject: The subject line.
        body: The full text of the email, written as the user would write it.
        written_in: What the body is written in. "plain" for an ordinary email.
            "markdown" or "html" when it needs formatting, such as a list, a
            table, headings or bold text: write the body in that.
        attachments: Files to send with it, and only ones the user asked for. Each
            is "drive:<file id>", "email:<message id>/<file name>", or
            "added:<file name>" for a file from the user's computer.
    """
    _require_write("gmail")
    _check_addresses(to)
    body, formatted = _written(body, written_in)
    carried, shown, notes = await _attached(context, attachments, to)
    sent = {"to": to, "subject": subject, "body": body}
    payload = {**sent, "html": formatted.get("html"), **carried}
    preview = {"what": "Send new email", **sent, **formatted, **shown}
    return _draft(context, "gmail.send_email", payload, preview, notes)


def _passed_on(original: dict[str, str]) -> tuple[str, str]:
    """An email quoted under a forward, as words and as clean HTML."""
    lines = [
        "---------- Forwarded message ----------",
        f"From: {original['from']}",
        f"Date: {original['date']}",
        f"Subject: {original['subject']}",
        f"To: {original['to']}",
    ]
    said = original["body"]
    quoted = "<br>".join(html.escape(line, quote=False) for line in lines)
    paragraphs = "".join(
        "<p>"
        + "<br>".join(html.escape(line, quote=False) for line in part.split("\n"))
        + "</p>"
        for part in re.split(r"\n\s*\n", said)
        if part.strip()
    )
    return "\n".join([*lines, "", said]), f"<p>{quoted}</p>{paragraphs}"


@function_tool()
async def gmail_draft_forward(
    context: RunContext[OrbitState],
    message_id: str,
    to: list[str],
    note: str = "",
    written_in: writing.Format = "plain",
) -> dict[str, Any]:
    """Draft forwarding an email, with every file attached to it. Nothing is sent
    by this tool.

    Args:
        message_id: The id of the email to forward, from gmail_search.
        to: The email addresses of the people it goes to.
        note: What the user wants to say above the forwarded email, if anything.
        written_in: What the note is written in: "plain", "markdown" or "html".
    """
    _require_write("gmail")
    _check_addresses(to)
    with reading("Gmail"):
        message = await gmail.raw_message(_http(), message_id)
    original = gmail.passed_on(message)
    quoted_words, quoted_html = _passed_on(original)
    subject = original["subject"]
    if not subject.lower().startswith(("fwd:", "fw:")):
        subject = f"Fwd: {subject}"

    said, formatted = _written(note, written_in) if note.strip() else ("", {})
    body = f"{said}\n\n{quoted_words}".strip()
    if formatted:
        formatted = {
            **formatted,
            "html": formatted["html"] + writing.clean(quoted_html),
        }
    key = uuid.uuid4().hex
    kept = await attaching.take_all(_http(), message, key=key)
    came_with_it = attaching.Gathered(kept, [], [file.name for file in kept])
    carried, shown, _ = await _carrying(context, key, came_with_it, to, forwarding=True)
    sent = {"to": to, "subject": subject, "body": body}
    payload = {**sent, "html": formatted.get("html"), **carried}
    preview = {"what": "Forward email", **sent, **formatted, **shown}
    return _draft(context, "gmail.send_email", payload, preview)


@function_tool()
async def calendar_draft_event(
    context: RunContext[OrbitState],
    title: str,
    day: str,
    time: str,
    duration_minutes: int = 30,
    attendees: list[str] | None = None,
    location: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Draft a calendar event. Nothing is created by this tool.

    Use this when the user wants to put something on their calendar or set up a
    meeting. Attendees are emailed an invitation once the event is created.

    Args:
        title: The name of the event.
        day: The day it is on, as the user said it: today, tomorrow, a weekday such as friday, or a date as YYYY-MM-DD.
            Do not work out the date yourself.
        time: When it starts on the user's clock, as a 24-hour HH:MM.
        duration_minutes: How long it lasts, in minutes.
        attendees: Email addresses of the people to invite. Leave empty for none.
        location: Where it is, if anywhere.
        description: Notes or an agenda, if any.
    """
    _require_write("gcal")
    invited = attendees or []
    _check_addresses(invited)
    now = datetime.now().astimezone()
    if not day.strip():
        raise ToolError("An event needs a day. Ask the user which day.")
    try:
        begins = resolve(day, time, 0, now)
    except WhenError as e:
        raise ToolError(str(e)) from e
    if not 5 <= duration_minutes <= MAX_EVENT_MINUTES:
        raise ToolError("duration_minutes must be between 5 and 720.")
    ends = begins + timedelta(minutes=duration_minutes)

    payload = {
        "title": title,
        "start": begins.isoformat(),
        "end": ends.isoformat(),
        "attendees": invited,
        "location": location,
        "description": description,
    }
    preview = {
        "what": "Create calendar event",
        "title": title,
        # Worked out by the clock here. Say it to the user as it is written.
        "when": f"{in_words(begins, now)} to {ends:%H:%M}",
        "invites": invited or "nobody else",
        "location": location or None,
        "description": description or None,
    }
    return _draft(context, "calendar.create_event", payload, preview)


@function_tool()
async def calendar_draft_response(
    context: RunContext[OrbitState],
    event_id: str,
    response: Literal["accepted", "declined", "tentative"],
) -> dict[str, Any]:
    """Draft an answer to a calendar invitation. Nothing is sent by this tool.

    Use this when the user wants to accept, decline or tentatively accept an
    invitation. Find the event first with calendar_events. The organiser is told.

    Args:
        event_id: The id of the event, from calendar_events.
        response: The answer to give.
    """
    _require_write("gcal")
    with reading("Google Calendar"):
        event = await gcal.get_event(_http(), event_id)
    preview = {
        "what": "Answer invitation",
        "event": event.get("summary", "(no title)"),
        "organiser": event.get("organizer", {}).get("email"),
        "answer": response,
    }
    payload = {"event_id": event_id, "response": response}
    return _draft(context, "calendar.respond", payload, preview)


@function_tool()
async def drive_draft_document(
    context: RunContext[OrbitState],
    title: str,
    content: str,
    written_in: writing.Format = "plain",
    keep_as_written: bool = False,
) -> dict[str, Any]:
    """Draft a new document or file. Nothing is created by this tool.

    Use this when the user wants something written into a new document, such as
    notes, a list, a report or a summary. It is created in their Google Drive once
    they agree. It becomes a Google Doc unless they ask for a Markdown, an HTML
    or a text file.

    Args:
        title: The name of the document.
        content: The full text of the document.
        written_in: What the content is written in. "plain" for text with no
            formatting. "markdown" or "html" when it has headings, lists, a table
            or bold text: write the content in that, and the document keeps them.
        keep_as_written: False for a Google Doc. True only when the user asks for
            a Markdown file, an HTML file or a text file: the content is then
            saved as that kind of file, as it is written.
    """
    _require_write("gdrive")
    if not title.strip() or not content.strip():
        raise ToolError("A document needs a title and some text. Ask the user.")
    # A Markdown file is drawn the way Markdown files are read, so the draft looks
    # as the file will: there the end of a line does not end the line.
    words, formatted = _written(content, written_in, breaks=not keep_as_written)
    if not keep_as_written:
        payload = {"title": title, "content": words, "html": formatted.get("html")}
        preview = {
            "what": "Create Google Doc",
            "title": title,
            "content": words,
            **formatted,
        }
        return _draft(context, "drive.create_document", payload, preview)

    kind, suffix, mime_type = _KEPT_AS[written_in]
    name = title if title.lower().endswith(suffix) else title + suffix
    # A Markdown file keeps what was written. An HTML file is the clean page of it.
    kept = whole_links(writing.as_meant(content))
    if written_in == "html":
        kept = writing.page(title, formatted["html"])
    payload = {"name": name, "content": kept, "mime_type": mime_type}
    preview = {"what": f"Create {kind}", "name": name, "content": words, **formatted}
    return _draft(context, "drive.create_file", payload, preview)


@function_tool()
async def drive_draft_addition(
    context: RunContext[OrbitState], file_id: str, text: str
) -> dict[str, Any]:
    """Draft adding text to the end of a Google Doc. Nothing is changed by this tool.

    Works only for documents Orbit created itself: it has no access to change the
    user's other files. Find the document first with drive_search.

    Args:
        file_id: The id of the document, from drive_search.
        text: The text to add at the end, in plain text.
    """
    _require_write("gdrive")
    if not text.strip():
        raise ToolError("There is nothing to add. Ask the user what to write.")
    text = whole_links(text)
    with reading("Google Drive"):
        file = await gdrive.describe(_http(), file_id)
    if file["kind"] != "Google Doc":
        raise ToolError(
            f"That file is a {file['kind']}. Orbit can only add text to Google Docs."
        )
    preview = {"what": "Add to Google Doc", "document": file["name"], "add": text}
    payload = {"file_id": file_id, "text": text}
    return _draft(context, "drive.add_to_document", payload, preview)


def _sheet_tab(context: RunContext[OrbitState], spreadsheet_id: str, tab: str) -> str:
    """The tab a change is meant for: the one named, or else the one read last."""
    return tab.strip() or context.userdata.sheet_tabs.get(spreadsheet_id, "")


def _read_first(context: RunContext[OrbitState], spreadsheet_id: str) -> None:
    """Refuses to change cells or rows of a sheet that has not been read: their
    numbers would be guesses."""
    if spreadsheet_id not in context.userdata.sheet_tabs:
        raise ToolError(
            "Read the sheet with sheets_read first, to find the right rows and "
            "cells, then draft the change."
        )


def _nothing_reaching_out(values: list[str]) -> None:
    for value in values:
        if function := gsheets.reaches_out(value):
            raise ToolError(
                f"That would put a formula with {function} in the sheet, which fetches "
                "from the web whenever the sheet is opened. Orbit does not write those. "
                "Tell the user."
            )


def _header_of(headers: list[str], column: int) -> str:
    """A column's header, or its letter when it has none."""
    header = headers[column].strip() if column < len(headers) else ""
    return header or gsheets.letter(column)


def _places(sheet: dict[str, Any], named: list[str]) -> list[int]:
    """Where each named column is, counted from 0: by its header, or else by its letter."""
    left = sheet["first_column"]
    headers = [
        (left + n, header.strip())
        for n, header in enumerate(sheet["headers"])
        if header.strip()
    ]
    places = []
    for name in named:
        said = name.strip().casefold()
        exact = [n for n, header in headers if header.casefold() == said]
        # The header may say more than the user did, as "Amount (Rs)" for "amount".
        partly = [
            n for n, header in headers if len(said) > 2 and said in header.casefold()
        ]
        found = exact or partly
        if len(found) == 1:
            places.append(found[0])
        elif re.fullmatch(r"[a-z]{1,2}", said) and (
            gsheets.column_index(said) < sheet["grid"][1]
        ):
            places.append(gsheets.column_index(said))
        elif not headers:
            raise ToolError(
                f"The tab {sheet['tab']} has no headers, so name its columns by letter, "
                "such as A and B."
            )
        else:
            listed = ", ".join(f"{gsheets.letter(n)} {header}" for n, header in headers)
            raise ToolError(
                f"The tab {sheet['tab']} has no single column called {name}. Its "
                f"columns are: {listed}. Use those headers."
            )
    if len(set(places)) < len(places):
        raise ToolError("Two of the columns given are the same one. Give each once.")
    return places


@function_tool()
async def sheets_draft_rows(
    context: RunContext[OrbitState],
    spreadsheet_id: str,
    columns: list[str],
    rows: list[list[str]],
    tab: str = "",
) -> dict[str, Any]:
    """Draft adding rows at the end of a tab of a Google Sheet. Nothing is changed by
    this tool.

    Read the sheet with sheets_read first, to learn its columns. Put in exactly what
    the user said, and leave out a column they gave nothing for.

    Args:
        spreadsheet_id: The id of the spreadsheet, from sheets_read.
        columns: The columns the values go in, by their headers, such as
            ["Date", "Item", "Amount"].
        rows: The rows to add. Each is one value for each of the columns, in the
            same order. Write a date as YYYY-MM-DD.
        tab: The tab, by name. Leave empty for the tab read last.
    """
    _require_write("gsheets")
    if not columns or not rows:
        raise ToolError("Say what to add: give the columns and at least one row.")
    if len(rows) > MAX_SHEET_ROWS:
        raise ToolError(f"Orbit adds at most {MAX_SHEET_ROWS} rows at a time.")
    if any(len(row) != len(columns) for row in rows):
        raise ToolError(
            f"Each row needs {len(columns)} values, one for each of the columns given."
        )
    if any(not any(value.strip() for value in row) for row in rows):
        raise ToolError("A row has nothing in it. Ask the user what goes in it.")
    _nothing_reaching_out([value for row in rows for value in row])
    with reading("Google Sheets"):
        sheet = await gsheets.read(
            _http(), spreadsheet_id, _sheet_tab(context, spreadsheet_id, tab)
        )
    if not sheet["complete"]:
        raise ToolError(
            f"The tab {sheet['tab']} has room for more rows than Orbit reads, so Orbit "
            "cannot tell where it ends. Tell the user."
        )
    places = _places(sheet, columns)

    # The new rows are shown across all the columns in use, empty where the user
    # gave nothing.
    headers = [""] * sheet["first_column"] + sheet["headers"]
    known = sheet["first_column"] + len(sheet["headers"]) - 1
    left = min(places) if not sheet["headers"] else min(sheet["first_column"], *places)
    right = max(known, *places)
    shown = []
    for row in rows:
        full = [""] * (right - left + 1)
        for place, value in zip(places, row):
            full[place - left] = value
        shown.append(full)
    said = [
        ", ".join(
            f"{_header_of(headers, place)}: {value}"
            for place, value in zip(places, row)
            if value.strip()
        )
        for row in rows
    ]
    last = sheet["last_row"]
    preview = {
        "what": "Add a row" if len(rows) == 1 else f"Add {len(rows)} rows",
        "sheet": sheet["title"],
        "tab": sheet["tab"],
        "where": f"At the end, under row {last}"
        if last
        else "At the top: the tab is empty",
        "rows": said,
        "table": {
            "columns": [_header_of(headers, n) for n in range(left, right + 1)],
            "rows": shown,
        },
    }
    payload = {
        "spreadsheet_id": sheet["id"],
        "tab_id": sheet["tab_id"],
        "columns": places,
        "rows": rows,
    }
    return _draft(
        context,
        "sheets.add_rows",
        payload,
        preview,
        next_step=NEXT_STEP if len(rows) <= READ_OUT_WHOLE else SHEET_NEXT_STEP,
    )


def _row_label(cells: list[str], column: int) -> str:
    """What a row is known by, such as the item it is about: its first cell with
    anything in it, other than the one being changed."""
    return next(
        (text.strip() for n, text in enumerate(cells) if n != column and text.strip()),
        "",
    )


@function_tool()
async def sheets_draft_cells(
    context: RunContext[OrbitState],
    spreadsheet_id: str,
    cells: list[str],
    values: list[str],
    tab: str = "",
) -> dict[str, Any]:
    """Draft changing cells of a Google Sheet. Nothing is changed by this tool.

    Read the sheet with sheets_read first, to find the cells. A cell is its column
    letter and row number, such as D7.

    Args:
        spreadsheet_id: The id of the spreadsheet, from sheets_read.
        cells: The cells to change, such as ["D7", "E7"].
        values: The new value of each cell, in the same order, as the user said it.
            "" empties a cell. Write a date as YYYY-MM-DD.
        tab: The tab, by name. Leave empty for the tab read last.
    """
    _require_write("gsheets")
    _read_first(context, spreadsheet_id)
    if not cells or len(cells) != len(values):
        raise ToolError("Give one value for each cell.")
    if len(cells) > MAX_SHEET_CELLS:
        raise ToolError(f"Orbit changes at most {MAX_SHEET_CELLS} cells at a time.")
    _nothing_reaching_out(values)
    session = _http()
    with reading("Google Sheets"):
        places = [gsheets.cell(name) for name in cells]
        book = await gsheets.spreadsheet(session, spreadsheet_id)
        sheet = gsheets.tab_named(book, _sheet_tab(context, spreadsheet_id, tab))
    names = [f"{gsheets.letter(column)}{row}" for row, column in places]
    if len(set(names)) < len(names):
        raise ToolError("A cell is named twice. Give each cell once.")
    outside = [
        name
        for name, (row, column) in zip(names, places)
        if row > sheet["rows"] or column >= sheet["columns"]
    ]
    if outside:
        raise ToolError(
            f"{', '.join(outside)} is past the end of the tab {sheet['title']}, which "
            f"has {sheet['rows']} rows and {sheet['columns']} columns. To add rows at "
            "its end, use sheets_draft_rows."
        )
    with reading("Google Sheets"):
        headers, shown, kept = await gsheets.headers_and_rows(
            session, spreadsheet_id, sheet, [row for row, _ in places]
        )

    said, table, before, formulas = [], [], [], []
    for name, (row, column), value in zip(names, places, values):
        row_shown = shown.get(row, [])
        was = gsheets.kept_cell(kept, row, column)
        formula = isinstance(was, str) and was.startswith("=")
        now = was if formula else (row_shown[column] if column < len(row_shown) else "")
        header = headers[column].strip() if column < len(headers) else ""
        label = _row_label(row_shown, column)
        where = ", ".join([name, *([header] if header else []), f"row {row}"]) + (
            f" ({label})" if label else ""
        )
        said.append(f"{where}: from {now or 'empty'} to {value or 'empty'}")
        table.append([name, header, str(now), value])
        before.append(was)
        if formula:
            formulas.append(name)
    if all(str(now) == value for _, _, now, value in table):
        raise ToolError(
            "Those cells already hold those values, so nothing needs changing. Tell "
            "the user."
        )
    preview = {
        "what": "Change a cell" if len(cells) == 1 else f"Change {len(cells)} cells",
        "sheet": book["title"],
        "tab": sheet["title"],
        "changes": said,
        "table": {"columns": ["Cell", "Column", "Now", "Becomes"], "rows": table},
    }
    if formulas:
        preview["note"] = (
            f"{', '.join(formulas)} {'holds a formula' if len(formulas) == 1 else 'hold formulas'}, "
            "which this replaces."
        )
    payload = {
        "spreadsheet_id": book["id"],
        "tab_id": sheet["id"],
        "cells": names,
        "values": values,
        "before": before,
    }
    return _draft(
        context,
        "sheets.set_cells",
        payload,
        preview,
        next_step=NEXT_STEP if len(cells) <= READ_OUT_WHOLE else SHEET_NEXT_STEP,
    )


@function_tool()
async def sheets_draft_delete_rows(
    context: RunContext[OrbitState],
    spreadsheet_id: str,
    rows: list[int],
    tab: str = "",
) -> dict[str, Any]:
    """Draft deleting whole rows of a Google Sheet. Nothing is changed by this tool.

    Read the sheet with sheets_read first, to find the rows. The rows below the
    deleted ones move up.

    Args:
        spreadsheet_id: The id of the spreadsheet, from sheets_read.
        rows: The numbers of the rows to delete, as sheets_read gave them.
        tab: The tab, by name. Leave empty for the tab read last.
    """
    _require_write("gsheets")
    _read_first(context, spreadsheet_id)
    rows = sorted(set(rows))
    if not rows or rows[0] < 1:
        raise ToolError("Give the numbers of the rows to delete, from sheets_read.")
    if len(rows) > MAX_SHEET_ROWS:
        raise ToolError(f"Orbit deletes at most {MAX_SHEET_ROWS} rows at a time.")
    session = _http()
    with reading("Google Sheets"):
        book = await gsheets.spreadsheet(session, spreadsheet_id)
        sheet = gsheets.tab_named(book, _sheet_tab(context, spreadsheet_id, tab))
    if rows[-1] > sheet["rows"]:
        raise ToolError(
            f"The tab {sheet['title']} has only {sheet['rows']} rows. Check the row "
            "numbers with sheets_read."
        )
    with reading("Google Sheets"):
        headers, shown, kept = await gsheets.headers_and_rows(
            session, spreadsheet_id, sheet, rows
        )
    empty = [str(row) for row in rows if not any(t.strip() for t in shown.get(row, []))]
    if empty:
        raise ToolError(
            f"Row {', '.join(empty)} of the tab {sheet['title']} has nothing in it. "
            "Check the row numbers with sheets_read."
        )

    lines = [shown[row] for row in rows]
    left = min(
        next(n for n, text in enumerate(cells) if text.strip()) for cells in lines
    )
    right = max(len(cells) for cells in lines) - 1
    said = [
        f"Row {row}: " + ", ".join(t.strip() for t in shown[row] if t.strip())
        for row in rows
    ]
    preview = {
        "what": "Delete a row" if len(rows) == 1 else f"Delete {len(rows)} rows",
        "sheet": book["title"],
        "tab": sheet["title"],
        "rows": said,
        "table": {
            "columns": [
                "Row",
                *(_header_of(headers, n) for n in range(left, right + 1)),
            ],
            "rows": [
                [
                    str(row),
                    *(
                        cells[n] if n < len(cells) else ""
                        for n in range(left, right + 1)
                    ),
                ]
                for row, cells in zip(rows, lines)
            ],
        },
        "note": "The rows below move up.",
    }
    payload = {
        "spreadsheet_id": book["id"],
        "tab_id": sheet["id"],
        "rows": rows,
        "before": {row: kept[row] for row in rows},
    }
    return _draft(
        context,
        "sheets.delete_rows",
        payload,
        preview,
        next_step=NEXT_STEP if len(rows) <= READ_OUT_WHOLE else SHEET_NEXT_STEP,
    )


@function_tool()
async def sheets_draft_new(
    context: RunContext[OrbitState],
    title: str,
    columns: list[str],
    rows: list[list[str]] | None = None,
) -> dict[str, Any]:
    """Draft creating a new Google Sheet in the user's Drive. Nothing is created by
    this tool.

    Args:
        title: The name of the spreadsheet.
        columns: The headers of its columns, for its first row.
        rows: Rows to put under the headers, each one value for each of the
            columns, in the same order. Leave empty for none. Write a date as
            YYYY-MM-DD.
    """
    _require_write("gdrive")
    title, columns, rows = title.strip(), [c.strip() for c in columns], rows or []
    if not title or not any(columns):
        raise ToolError(
            "A new sheet needs a name and its column headers. Ask the user."
        )
    if len(rows) > MAX_NEW_SHEET_ROWS:
        raise ToolError(f"A new sheet starts with at most {MAX_NEW_SHEET_ROWS} rows.")
    if any(len(row) > len(columns) for row in rows):
        raise ToolError(
            f"Each row has at most {len(columns)} values, one for each column."
        )
    rows = [row + [""] * (len(columns) - len(row)) for row in rows]
    _nothing_reaching_out([*columns, *(value for row in rows for value in row)])
    said = [
        ", ".join(f"{c}: {v}" for c, v in zip(columns, row) if v.strip())
        for row in rows
    ]
    preview = {
        "what": "Create Google Sheet",
        "title": title,
        "columns": columns,
        "rows": said or "none yet",
        "table": {"columns": columns, "rows": rows},
    }
    payload = {"title": title, "columns": columns, "rows": rows}
    return _draft(
        context,
        "sheets.create",
        payload,
        preview,
        next_step=NEXT_STEP if len(rows) <= READ_OUT_WHOLE else SHEET_NEXT_STEP,
    )


async def _show_changed_sheet(action: Action, result: dict[str, Any]) -> None:
    """Puts a sheet that was just changed on the user's screen, the change marked."""
    spreadsheet_id = result.get("spreadsheet_id") or action.payload.get(
        "spreadsheet_id"
    )
    if not spreadsheet_id:
        return
    await stage.show_sheet(
        spreadsheet_id,
        action.preview.get("sheet") or action.preview.get("title") or "",
        result.get("tab") or action.preview.get("tab") or "",
        changed_rows=result.get("rows") or (),
        changed_cells=result.get("cells") or (),
    )


@function_tool()
async def sheets_draft_chart(
    context: RunContext[OrbitState], chart_id: str, spreadsheet: str
) -> dict[str, Any]:
    """Draft saving a chart on the user's screen into one of their own Google Sheets,
    as a new tab holding the chart's numbers and the chart. Nothing is saved by this
    tool. Charts are kept in Orbit charts by themselves: use this only when the user
    asks for a chart in a spreadsheet of theirs.

    Args:
        chart_id: The chart's id, as show_chart gave it.
        spreadsheet: The spreadsheet's name as the user said it, without the word
            sheet or spreadsheet.
    """
    _require_write("sheets")
    view = chart_store.view(chart_id.strip())
    if view is None:
        raise ToolError(NO_CHART)
    name = spreadsheet.strip()
    with reading("Google Sheets"):
        found = await gdrive.spreadsheets_named(_http(), name)
    meant = [f for f in found if f["name"].casefold() == name.casefold()] or found
    if len(meant) != 1:
        names = ", ".join(f["name"] for f in found[:8]) or "none"
        raise ToolError(
            f"{len(meant) or 'No'} spreadsheets fit {name!r}: {names}. Ask which one."
        )
    preview = {
        "what": "Save chart to Google Sheets",
        "where": f"{meant[0]['name']}, in a new tab",
        "chart": view["title"],
        "chart_id": view.get("chart_id", chart_id),
        "table": _chart_table(view),
    }
    payload = {"chart_id": chart_id.strip(), "into": meant[0]["id"], "view": view}
    return _draft(
        context, "sheets.add_chart", payload, preview, next_step=CHART_NEXT_STEP
    )


@function_tool()
async def confirm_action(
    context: RunContext[OrbitState], action_id: str
) -> dict[str, Any]:
    """Carry out a drafted action: send the email, create the event or document.

    Call this only after you have read the draft to the user and they have then
    clearly agreed. Never call it in the same turn as the draft tool, and never
    because you expect the user to agree.

    Args:
        action_id: The action_id returned by the draft tool.
    """
    # Once this starts it must finish: the user talking over it must not leave the
    # action half done.
    context.disallow_interruptions()
    gate = context.userdata.gate
    try:
        action = gate.approve(action_id)
    except GateError as e:
        raise ToolError(str(e)) from e

    handler = context.userdata.handlers.get(action.kind)
    try:
        if handler is not None:
            result = await handler(context, action)
        else:
            result = await perform(_http(), action.kind, action.payload)
    except NotDoneError as e:
        gate.fail(action.id, str(e))
        raise ToolError(str(e)) from e
    except Exception as e:
        gate.fail(action.id, str(e))
        reason = _why_not(action.kind, e)
        raise ToolError(
            f"It was not done. {reason} Tell the user nothing was sent or changed."
        ) from e

    gate.complete(action.id, result)
    done = {"status": "done", "action": _told(action.preview)}
    if handler is not None:
        done.update(result.get("told") or {})
        return done
    # A task done for something the user calls theirs may be done again.
    trace = trace_of(context)
    if trace is not None and (
        offer := trace.offer_after(action.kind, said_so_far(context))
    ):
        done["then"] = offer
    if action.kind == "sheets.add_chart":
        # The chart's card shows where it is now kept.
        if (shown := chart_store.view(action.payload["chart_id"])) is not None:
            await stage.show_chart(
                action.payload["chart_id"],
                shown["title"],
                shown["source"].get("label", ""),
            )
        if result.get("link"):
            done["saved_in"] = f"{result['spreadsheet']}, in the tab {result['tab']}"
    elif action.kind.startswith("sheets."):
        await _show_changed_sheet(action, result)
        if spreadsheet_id := result.get("spreadsheet_id"):
            done["spreadsheet_id"] = spreadsheet_id
    return done


@function_tool()
async def cancel_action(
    context: RunContext[OrbitState], action_id: str
) -> dict[str, Any]:
    """Withdraw a drafted action that the user does not want carried out.

    Args:
        action_id: The action_id returned by the draft tool.
    """
    try:
        action = context.userdata.gate.cancel(action_id)
    except GateError as e:
        raise ToolError(str(e)) from e
    return {"status": action.status}


@function_tool()
async def save_to_gmail_drafts(
    context: RunContext[OrbitState], action_id: str
) -> dict[str, Any]:
    """Put a drafted email in the user's Gmail drafts, with its attachments,
    instead of sending it, for them to finish in Gmail themselves. Call it only
    after they have heard the draft and asked for this.

    Args:
        action_id: The action_id returned by the draft tool.
    """
    context.disallow_interruptions()
    gate = context.userdata.gate
    action = gate.get(action_id)
    if action is not None and action.kind not in _EMAILS:
        raise ToolError("Only a drafted email can be put in Gmail drafts.")
    try:
        action = gate.approve(action_id)
    except GateError as e:
        raise ToolError(str(e)) from e

    try:
        saved = await keep_in_gmail(_http(), action.payload)
    except Exception as e:
        gate.fail(action.id, str(e))
        reason = _why_not(action.kind, e)
        raise ToolError(
            f"It was not saved. {reason} Tell the user nothing was sent or saved."
        ) from e

    account = (tokens.load() or {}).get("account", "")
    link = gmail.draft_link(saved.get("message_id") or "", account)
    gate.set_aside(action.id, {**saved, "gmail_draft": link})
    return {"status": "saved in Gmail drafts, not sent", "next": IN_GMAIL}


ACTION_TOOLS = [
    gmail_draft_reply,
    gmail_draft_email,
    gmail_draft_forward,
    save_to_gmail_drafts,
    calendar_draft_event,
    calendar_draft_response,
    drive_draft_document,
    drive_draft_addition,
    sheets_draft_rows,
    sheets_draft_cells,
    sheets_draft_delete_rows,
    sheets_draft_new,
    sheets_draft_chart,
    confirm_action,
    cancel_action,
]
