"""The agent's Google tools. Read-only: they look things up and change nothing.

Each tool returns facts as data and leaves the wording to the model, and asks the
app to show what it found as a card. Failures are raised as ToolError with what
went wrong and what the user can do, so the model reports the failure instead of
improvising an answer.
"""

import asyncio
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from email.utils import parseaddr
from typing import Any, Literal

import aiohttp
from livekit.agents import RunContext, function_tool, utils
from livekit.agents.llm import ToolError

from api.deck import LOOKAHEAD, unanswered
from api.mail import waiting_mail
from files import WRITTEN, kind_of, view_of
from integrations.google import gcal, gchat, gdrive, gmail, gsheets
from integrations.google.api import GoogleApiError, NotConnectedError, TooLargeError
from integrations.google.auth import ReconnectRequiredError
from integrations.google.client import ClientNotConfiguredError
from reminders import store as reminders
from reminders.when import WhenError, earlier

from . import stage
from .state import OrbitState

MAX_RESULTS = 10
MAX_FOLDER_ITEMS = 30
MAX_DAYS = 31
MAX_CHATS = 20
# With no day named, the latest few messages. With days named, all of them, up to
# what a busy day in one conversation comes to.
RECENT_CHAT_MESSAGES = 20
MAX_CHAT_MESSAGES = 200
# The model gets enough of each message to say what it is about. The card has all of it.
CHAT_TEXT_FOR_MODEL = 300
# How many conversations are offered when a name fits several.
MAX_CHATS_OFFERED = 8
# What the model is given of a sheet at most. The card shows more of it, and the
# model can read on from where this ends.
SHEET_ROWS_FOR_MODEL = 500
SHEET_CHARS_FOR_MODEL = 30_000
MAX_SHEETS_OFFERED = 8
# An amount such as 4.50, and what in a cell or a header names its currency. Told
# nothing, the model says an amount with two decimals in dollars.
_AMOUNT = re.compile(r"-?\d[\d,]*\.\d+")
_CURRENCY = re.compile(
    r"[$€£₹¥₩₽¢]|\b(?:rs|inr|usd|eur|gbp|jpy|aud|cad|sgd|aed)\b", re.IGNORECASE
)
NO_CURRENCY = (
    "No cell or header shows a currency. Say the amounts as plain numbers, never in "
    "dollars or any other currency."
)

ChatKind = Literal["any", "space", "group", "direct"]
_CHAT_KINDS = {"space": "Space", "group": "Group chat", "direct": "Direct message"}


def _clamp(value: int, upper: int) -> int:
    return max(1, min(value, upper))


@contextmanager
def reading(service: str) -> Iterator[None]:
    """Turns anything that goes wrong reading a Google service into a ToolError."""
    try:
        yield
    except (NotConnectedError, ClientNotConfiguredError) as e:
        raise ToolError(
            f"The user's Google account is not connected to Orbit, so {service} "
            "cannot be read. Tell the user to connect Google in Orbit."
        ) from e
    except ReconnectRequiredError as e:
        raise ToolError(
            "Orbit's Google sign-in has expired. Tell the user to connect Google again."
        ) from e
    except GoogleApiError as e:
        if e.api_not_enabled:
            raise ToolError(
                f"{service} is not switched on for Orbit yet. Tell the user the "
                f"{service} API needs enabling in their Google Cloud project."
            ) from e
        if e.status in (401, 403):
            raise ToolError(
                f"Orbit is not allowed to read {service}. Tell the user to "
                "reconnect Google and grant that access."
            ) from e
        if e.status == 404:
            raise ToolError(
                "Nothing was found with that id. Search again to get a current id."
            ) from e
        raise ToolError(
            f"{service} returned an error. Tell the user it failed and offer to retry."
        ) from e
    except TooLargeError as e:
        raise ToolError(
            "That file is too large for Orbit to open. Tell the user."
        ) from e
    except gsheets.SheetError as e:
        raise ToolError(str(e)) from e
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise ToolError(
            f"{service} could not be reached. Tell the user and offer to retry."
        ) from e


def _unseen(kind: str) -> str:
    """What the model is told about a file it has put on screen but cannot read."""
    return (
        f"This {kind} is on the user's screen now. You cannot see what is in it. "
        "Say that it is on their screen, and say nothing about its contents."
    )


@function_tool()
async def gmail_search(
    context: RunContext, query: str = "", max_results: int = 5
) -> dict[str, Any]:
    """Look up emails in the user's Gmail, newest first.

    Use this for any question about the user's email: the latest email, unread mail,
    mail from a person, or mail about a topic. Call it before saying anything about
    what is in the inbox.

    Args:
        query: Gmail search terms, for example "is:unread", "from:priya" or
            "subject:invoice newer_than:7d". Leave empty for the latest inbox mail.
        max_results: How many emails to return, from 1 to 10.
    """
    with reading("Gmail"):
        emails = await gmail.search(
            utils.http_context.http_session(), query, _clamp(max_results, MAX_RESULTS)
        )
    await stage.show_list(
        "found:mail",
        "Mail",
        "mail",
        [
            stage.row(
                parseaddr(email["from"])[0] or email["from"],
                email["subject"],
                email["snippet"],
                email["received"],
            )
            for email in emails
        ],
        tag=f"{len(emails)} found",
    )
    return {"emails": emails, "count": len(emails)}


@function_tool()
async def gmail_read(context: RunContext, message_id: str) -> dict[str, Any]:
    """Read one email in full, including its body and attachment names.

    Use this after gmail_search, when the user wants the contents or details of a
    specific email rather than the list. To show the user one of its attachments,
    call gmail_open_attachment afterwards.

    Args:
        message_id: The id of the email, as returned by gmail_search.
    """
    with reading("Gmail"):
        email = await gmail.read(utils.http_context.http_session(), message_id)
    # The app loads the email itself, with Orbit's draft if there is one.
    await stage.show(
        {"id": f"mail:{message_id}", "kind": "mail", "message_id": message_id}
    )
    return email


def _attachment_meant(
    attached: list[dict[str, Any]], name: str
) -> dict[str, Any] | None:
    """The one attachment this name points to, or None when it is none or several."""
    said = name.casefold().strip()
    exact = [file for file in attached if file["name"].casefold() == said]
    if len(exact) == 1:
        return exact[0]
    partly = [file for file in attached if said and said in file["name"].casefold()]
    if not exact and len(partly) == 1:
        return partly[0]
    # With one file attached there is no doubt which is meant.
    return attached[0] if len(attached) == 1 else None


@function_tool()
async def gmail_open_attachment(
    context: RunContext, message_id: str, name: str = ""
) -> dict[str, Any]:
    """Open a file attached to an email: put it on the user's screen.

    Use this when the user wants to see, open or show an attachment, such as a
    PDF, a picture or a document that came with an email. Call gmail_read first
    to learn which attachments the email has.

    The file itself is shown to the user. You get its text as `content` only when
    it is a text file. A picture or a PDF has none you can read: `note` then says
    so, and you must not describe what is in it.

    Args:
        message_id: The id of the email, as returned by gmail_search.
        name: The file name of the attachment, as listed by gmail_read. May be
            left empty when the email has only one.
    """
    session = utils.http_context.http_session()
    with reading("Gmail"):
        message = await gmail.raw_message(session, message_id)
        attached = gmail.attachments(message)
        if not attached:
            raise ToolError("That email has no attachments. Tell the user.")
        meant = _attachment_meant(attached, name)
        if meant is None:
            names = ", ".join(file["name"] for file in attached)
            raise ToolError(
                f"No single attachment matches that name. The email has: {names}. "
                "Ask the user which one they mean."
            )
        view = view_of(meant["name"], meant["mime_type"])
        kind = kind_of(meant["name"], meant["mime_type"])
        if view == "none":
            raise ToolError(
                f"{meant['name']} is a kind of file Orbit cannot show. Tell the "
                "user they can open it from the email in Gmail."
            )
        opened: dict[str, Any] = {"name": meant["name"], "kind": kind}
        if view in WRITTEN:
            part = gmail.file_part(message, meant["part"])
            data = await gmail.part_bytes(session, message_id, part)
            content = gdrive.readable(view, data)
            opened.update(
                content=content[: gmail.MAX_BODY_CHARS],
                content_truncated=len(content) > gmail.MAX_BODY_CHARS,
            )
        else:
            opened.update(content=None, note=_unseen(kind))
    await stage.show_attachment(message_id, meant["part"], meant["name"], kind)
    return {**opened, "on_screen": True}


@function_tool()
async def calendar_events(
    context: RunContext, start_date: str = "", days: int = 1
) -> dict[str, Any]:
    """List events on the user's Google Calendar.

    Use this for any question about the user's schedule, meetings or free time.
    Call it before saying anything about what is on the calendar.

    Args:
        start_date: The first day to look at, as YYYY-MM-DD. Leave empty for today.
        days: How many days to cover starting from start_date, from 1 to 31.
            Use 1 for a single day and 7 for a week.
    """
    try:
        first_day = (
            datetime.strptime(start_date, "%Y-%m-%d") if start_date else datetime.now()
        )
    except ValueError as e:
        raise ToolError("start_date must look like 2026-10-03.") from e
    # Midnight in the machine's local time zone, so "today" means the user's day.
    start = first_day.replace(hour=0, minute=0, second=0, microsecond=0).astimezone()
    end = start + timedelta(days=_clamp(days, MAX_DAYS))

    with reading("Google Calendar"):
        events = await gcal.events(utils.http_context.http_session(), start, end)
    last_day = end - timedelta(days=1)
    await stage.show_list(
        "found:calendar",
        "Calendar",
        "calendar",
        [
            stage.row(
                event["title"],
                "All day"
                if event["all_day"]
                else f"{event['start']} to {event['end']}",
                event["location"] or "",
            )
            for event in events
        ],
        tag=f"{start:%a %d %b}"
        if last_day.date() == start.date()
        else f"{start:%d %b} to {last_day:%d %b}",
    )
    return {
        "from": start.strftime("%a %d %b %Y"),
        "until": last_day.strftime("%a %d %b %Y"),
        "events": events,
        "count": len(events),
    }


@function_tool()
async def chat_recent_messages(
    context: RunContext, max_results: int = 10
) -> dict[str, Any]:
    """Get the newest messages across all the user's Google Chat conversations.

    Use this for what is new in Google Chat: what was said recently, or whether
    there are new messages. Call it before saying anything about the user's chats.
    It only has the last few messages of the chats most recently active: for one
    particular chat, or for anything earlier, use chat_read.

    Args:
        max_results: How many messages to return, from 1 to 10.
    """
    with reading("Google Chat"):
        messages = await gchat.recent_messages(
            utils.http_context.http_session(), _clamp(max_results, MAX_RESULTS)
        )
    await stage.show_list(
        "found:chat",
        "Chat",
        "chat",
        [
            stage.row(
                message["sender"], message["text"], message["space"], message["sent"]
            )
            for message in messages
        ],
        tag=f"{len(messages)} recent",
    )
    return {"messages": messages, "count": len(messages)}


def _chat_rows(chats: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        stage.row(chat["name"], _CHAT_KINDS[chat["kind"]], "", chat["last_active"])
        for chat in chats
    ]


def _chat_meant(chats: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one conversation a name points to, or None when it is none or several.

    Someone the user writes to one-to-one is usually in groups with them as well.
    Their name on its own means the direct message.
    """
    if len(chats) == 1:
        return chats[0]
    direct = [chat for chat in chats if chat["kind"] == "direct"]
    return direct[0] if len(direct) == 1 else None


def _day(said: str) -> datetime:
    """Midnight at the start of a day the user named, on their own clock."""
    try:
        return datetime.combine(
            earlier(said, datetime.now().date()), time()
        ).astimezone()
    except WhenError as e:
        raise ToolError(str(e)) from e


@function_tool()
async def chat_conversations(
    context: RunContext,
    matching: str = "",
    kind: ChatKind = "any",
    max_results: int = 10,
) -> dict[str, Any]:
    """List the user's Google Chat conversations, the most recently active first.

    Use this when the user asks which chats they have or who they have been
    chatting with, or to find a conversation whose name they only half remember.
    A space is listed by its name, a direct message by the other person, and a
    group nobody named by the people who write in it. To read what was said in
    one, use chat_read.

    Args:
        matching: Words from the conversation's name, or from the name of a person
            in it. Leave empty for all conversations.
        kind: "direct" for one-to-one chats with a person, "group" for group
            chats, "space" for named spaces, "any" for all of them.
        max_results: How many conversations to return, from 1 to 20.
    """
    with reading("Google Chat"):
        chats = await gchat.conversations(utils.http_context.http_session())
    wanted = "" if kind == "any" else kind
    if matching.strip():
        chats = gchat.matching(chats, matching, wanted)
    elif wanted:
        chats = [chat for chat in chats if chat["kind"] == wanted]
    listed = chats[: _clamp(max_results, MAX_CHATS)]
    await stage.show_list(
        "found:chat", "Chats", "chat", _chat_rows(listed), tag=f"{len(chats)} chats"
    )
    return {
        "chats": listed,
        "count": len(listed),
        "more_not_listed": len(chats) > len(listed),
    }


@function_tool()
async def chat_read(
    context: RunContext,
    chat: str = "",
    chat_id: str = "",
    kind: ChatKind = "any",
    since: str = "",
    until: str = "",
    older_than: str = "",
    max_results: int = 0,
) -> dict[str, Any]:
    """Read the messages of one Google Chat conversation, in the order they were sent.

    Use this for anything about one particular chat, and for anything earlier than
    the last few messages: what someone wrote, what was said on a day, or what
    came before the messages already read. It finds the conversation from its
    name itself, so the conversations need not be listed first.

    With days given, every message of those days comes back, and all of them are
    put on the user's screen in full. With no day given, it is the latest 20.
    Always say how many messages there are. When the user asks you to read the
    messages, or to read all of them, read every one out in order, saying who
    wrote it: do not turn them into a summary unless a summary is what they ask
    for.

    If the name fits several conversations, nothing is read and they are returned
    as `chats_matching`: ask the user which one, then call again with its id. If
    none fit, say no chat by that name was found. A person's name on its own
    means the direct message with them.

    When there are earlier messages than the ones returned, `older_than` is set:
    tell the user there are earlier ones. To go further back, call again with the
    same chat_id and that older_than.

    Args:
        chat: The conversation as the user named it: the name of a space or group,
            or the person for a direct message.
        chat_id: The id of the conversation, when a tool result has already given
            it. Use it instead of the name whenever you have it.
        kind: "direct" when the user means their one-to-one chat with a person,
            "group" for a group chat, "space" for a named space. Leave it as
            "any" when they did not say.
        since: The first day to read, as the user said it: "today", "yesterday",
            a weekday for the most recent one, or a date as YYYY-MM-DD. Leave
            empty when the user named no day.
        until: The last day to read, in the same form. For what was said on one
            day, give that day as both since and until. Leave empty for up to now.
        older_than: The older_than of the previous result, to read what came
            before it.
        max_results: Leave at 0 unless the user asks for a number of messages:
            then that number, up to 200.
    """
    session = utils.http_context.http_session()
    name = chat.strip()
    if not chat_id and not name:
        raise ToolError("Say which conversation: give chat or chat_id.")
    after = _day(since) if since.strip() else None
    before = _day(until) + timedelta(days=1) if until.strip() else None
    if after and before and before <= after:
        raise ToolError("until is before since. Check the days with the user.")
    if older_than.strip():
        try:
            reached = datetime.fromisoformat(older_than.strip().replace("Z", "+00:00"))
        except ValueError as e:
            raise ToolError(
                "older_than must be the older_than value of a previous chat_read."
            ) from e
        before = min(before, reached) if before else reached

    with reading("Google Chat"):
        chats = await gchat.conversations(session)
        if chat_id:
            found = next((c for c in chats if c["id"] == chat_id), None)
            if found is None:
                raise ToolError(
                    "No conversation has that id. Use chat_conversations to get "
                    "a current one."
                )
        else:
            matches = gchat.matching(chats, name, "" if kind == "any" else kind)
            found = _chat_meant(matches)
            if found is None:
                offered = matches[:MAX_CHATS_OFFERED]
                if offered:
                    await stage.show_list(
                        "found:chat",
                        "Chats",
                        "chat",
                        _chat_rows(offered),
                        tag=f"{len(matches)} chats",
                    )
                return {"chat": None, "chats_matching": offered}
        # The user named the days, so they get the days: all of it, not a sample.
        fitting = MAX_CHAT_MESSAGES if after or until.strip() else RECENT_CHAT_MESSAGES
        read = await gchat.messages(
            session,
            found["id"],
            after=after,
            before=before,
            limit=_clamp(max_results or fitting, MAX_CHAT_MESSAGES),
        )
    messages = read["messages"]
    await stage.show_list(
        "found:chat",
        found["name"],
        "chat",
        [
            stage.row(m["sender"], m["text"], "", m["sent"], whole=True)
            for m in messages
        ],
        tag=f"{len(messages)}{'+' if read['older_than'] else ''} messages",
        whole=True,
    )
    return {
        "chat": {key: found[key] for key in ("id", "name", "kind")},
        "messages": [{**m, "text": _for_model(m["text"])} for m in messages],
        "count": len(messages),
        "older_than": read["older_than"],
    }


def _for_model(text: str) -> str:
    """A message on one line, cut where the model has enough to say what it is."""
    text = " ".join(text.split())
    if len(text) > CHAT_TEXT_FOR_MODEL:
        return text[:CHAT_TEXT_FOR_MODEL] + "…"
    return text


@function_tool()
async def drive_search(
    context: RunContext, text: str = "", max_results: int = 5
) -> dict[str, Any]:
    """Find files in the user's Google Drive.

    Use this for any question about the user's documents, sheets, slides or files.
    Call it before saying anything about what is in Drive. It finds files by their
    words, wherever they are kept: to see what is inside a folder, use
    drive_list_folder instead.

    Each file comes with its `link`, the address that opens it. The links are on
    the user's screen too: a file's name on the card opens it, and a button there
    copies its link.

    Args:
        text: Words to look for in file names and contents. Leave empty for the
            most recently changed files.
        max_results: How many files to return, from 1 to 10.
    """
    with reading("Google Drive"):
        files = await gdrive.search(
            utils.http_context.http_session(), text, _clamp(max_results, MAX_RESULTS)
        )
    await stage.show_list(
        "found:drive",
        "Drive",
        "file",
        _file_rows(files),
        tag=f"{len(files)} found",
        every_row=True,
    )
    return {"files": files, "count": len(files)}


def _meant(found: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The one folder or file this name points to, or None when it is none or several."""
    exact = [f for f in found if f["name"].casefold() == name.casefold()]
    if len(exact) == 1:
        return exact[0]
    if not exact and len(found) == 1:
        return found[0]
    return None


def _file_rows(files: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        stage.row(
            file["name"],
            file["kind"],
            file["owner"] or "",
            file["modified"],
            link=file.get("link"),
            # A file can be shown in a card of its own, and a sheet as a table of
            # its cells. A folder is listed instead.
            file_id=None
            if file["kind"] in ("folder", gdrive.SHEET_KIND)
            else file["id"],
            sheet_id=file["id"] if file["kind"] == gdrive.SHEET_KIND else None,
        )
        for file in files
    ]


@function_tool()
async def drive_list_folder(
    context: RunContext, folder: str = "", folder_id: str = "", max_results: int = 20
) -> dict[str, Any]:
    """List what is directly inside a folder in the user's Google Drive.

    Use this when the user asks what is in a folder, asks to open or look inside
    one, or wants the link to a folder. The items come back with their folders
    first; to go into one of those, call this again with its id.

    If several folders match the name, nothing is listed and they are returned as
    `folders_matching`: ask the user which one, then call again with its id. If
    none match, say no folder by that name was found.

    The folder and each item in it come with a `link`, the address that opens it.
    The links are on the user's screen too.

    Args:
        folder: The folder's name as the user said it, without the word "folder".
            Leave this and folder_id empty for the top level of the user's Drive.
        folder_id: The id of the folder, when a tool result has already given it.
            Use it instead of the name whenever you have it.
        max_results: How many items to return, from 1 to 30.
    """
    session = utils.http_context.http_session()
    name = folder.strip()
    with reading("Google Drive"):
        if folder_id:
            target = await gdrive.describe(session, folder_id)
            if target["kind"] != "folder":
                raise ToolError(
                    f"That id is a {target['kind']}, not a folder. Use drive_read "
                    "to read it."
                )
        elif name:
            folders = await gdrive.folders_named(session, name)
            found = _meant(folders, name)
            if found is None:
                if folders:
                    await stage.show_list(
                        "found:drive",
                        "Drive",
                        "file",
                        _file_rows(folders),
                        tag=f"{len(folders)} folders",
                        every_row=True,
                    )
                return {"folder": None, "folders_matching": folders}
            target = found
        else:
            target = {
                "id": gdrive.TOP_LEVEL,
                "name": "My Drive",
                "link": gdrive.TOP_LEVEL_LINK,
            }
        listing = await gdrive.children(
            session, target["id"], _clamp(max_results, MAX_FOLDER_ITEMS)
        )
    items, more = listing["items"], listing["more"]
    await stage.show_list(
        "found:drive",
        target["name"],
        "file",
        _file_rows(items),
        tag=f"{len(items)}{'+' if more else ''} inside",
        every_row=True,
        link=target.get("link"),
        link_label="Open folder in Drive",
    )
    return {
        "folder": {key: target.get(key) for key in ("id", "name", "link")},
        "items": items,
        "count": len(items),
        "more_not_listed": more,
    }


@function_tool()
async def drive_read(context: RunContext[OrbitState], file_id: str) -> dict[str, Any]:
    """Open one Google Drive file: put it on the user's screen and read what it says.

    Use this after drive_search or drive_list_folder, when the user wants to see,
    open, show or read a specific file, or asks what it says. It works for
    documents, sheets, slides, text and Markdown files, PDFs and pictures.

    The file itself is shown to the user. You get its text as `content` when it
    has text Orbit can read: Google Docs, Sheets and Slides, and text files. A
    picture or a PDF has none you can read: `content` is then empty and `note`
    says so, and you must not describe what is in it.

    Args:
        file_id: The id of the file, as returned by drive_search.
    """
    session = utils.http_context.http_session()
    try:
        with reading("Google Drive"):
            file = await gdrive.read(session, file_id, sheets_as_text=False)
    except gdrive.UnreadableFileError as e:
        if str(e) == "folder":
            raise ToolError(
                "That is a folder, not a file. Call drive_list_folder with this id "
                "to see what is inside it."
            ) from e
        raise ToolError(
            f"That file is a {e}, which Orbit can neither read nor show. Tell the "
            "user they can open it in Drive from its link on their screen."
        ) from e
    if file["kind"] == gdrive.SHEET_KIND:
        with reading("Google Sheets"):
            try:
                sheet = await gsheets.read(
                    session, file_id, context.userdata.sheet_tabs.get(file_id, "")
                )
            except GoogleApiError as e:
                if not e.api_not_enabled:
                    raise
                sheet = None
        if sheet is not None:
            return await _opened_sheet(context, sheet)
        # Without the Sheets API a sheet is read as Drive gives it: its first tab,
        # as text, and on screen as a PDF.
        with reading("Google Drive"):
            file = await gdrive.read(session, file_id)
    await stage.show_drive_file(file_id, file["name"], file["kind"])
    opened = {**file, "on_screen": True}
    if file["content"] is None:
        opened["note"] = _unseen(file["kind"])
    return opened


@function_tool()
async def sheets_read(
    context: RunContext[OrbitState],
    spreadsheet: str = "",
    spreadsheet_id: str = "",
    tab: str = "",
    cells: str = "",
    matching: str = "",
) -> dict[str, Any]:
    """Read a Google Sheet: put it on the user's screen and get its rows.

    Use this for any question about a spreadsheet, and before changing one. It
    finds the spreadsheet by name itself. Each row comes with its number and each
    column with its letter, as the sheet numbers them: a cell is its column letter
    and its row number, such as D7.

    If the name fits several spreadsheets, nothing is read and they are returned as
    `spreadsheets_matching`: ask the user which one, then call again with its id.

    Args:
        spreadsheet: The spreadsheet's name as the user said it, without the word
            sheet or spreadsheet.
        spreadsheet_id: Its id, when a tool result has already given it. Use it
            instead of the name whenever you have it.
        tab: The tab to read, by name. Leave empty for the tab read last, or else
            the first.
        cells: Only these cells, such as "A2:D50". Leave empty for the whole tab.
        matching: Only the rows with these words in a cell. Leave empty for all.
    """
    session = utils.http_context.http_session()
    name = spreadsheet.strip()
    if not spreadsheet_id and not name:
        raise ToolError("Say which spreadsheet: give spreadsheet or spreadsheet_id.")
    with reading("Google Sheets"):
        if not spreadsheet_id:
            found = await gdrive.spreadsheets_named(session, name)
            meant = _meant(found, name)
            if meant is None:
                offered = found[:MAX_SHEETS_OFFERED]
                if offered:
                    await stage.show_list(
                        "found:drive",
                        "Drive",
                        "file",
                        _file_rows(offered),
                        tag=f"{len(found)} sheets",
                        every_row=True,
                    )
                return {"spreadsheet": None, "spreadsheets_matching": offered}
            spreadsheet_id = meant["id"]
        tab = tab.strip() or context.userdata.sheet_tabs.get(spreadsheet_id, "")
        sheet = await gsheets.read(session, spreadsheet_id, tab, cells, matching)
    return await _opened_sheet(context, sheet, cells, matching)


async def _opened_sheet(
    context: RunContext[OrbitState],
    sheet: dict[str, Any],
    cells: str = "",
    matching: str = "",
) -> dict[str, Any]:
    """A sheet that was read: remembered for this conversation, put on the user's
    screen, and given to the model as far as it should have it."""
    context.userdata.sheet_tabs[sheet["id"]] = sheet["tab"]
    await stage.show_sheet(
        sheet["id"], sheet["title"], sheet["tab"], cells=cells, matching=matching
    )
    return _sheet_for_model(sheet, matching)


def _sheet_for_model(sheet: dict[str, Any], matching: str = "") -> dict[str, Any]:
    """The rows of a sheet as the model is given them: all of them, unless there are
    more than it should take in at once. It is then told where it can read on."""
    left = sheet["first_column"]
    rows = sheet["rows"]
    width = len(rows[0]["cells"]) if rows else len(sheet["headers"])
    headers = sheet["headers"] or [""] * width
    given: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        size += len(json.dumps(row, ensure_ascii=False))
        if len(given) == SHEET_ROWS_FOR_MODEL or size > SHEET_CHARS_FOR_MODEL:
            break
        given.append(row)
    told: dict[str, Any] = {
        "spreadsheet": {key: sheet[key] for key in ("id", "title", "link")},
        "tabs": sheet["tabs"],
        "tab": sheet["tab"],
        "columns": [
            {"column": gsheets.letter(left + n), "header": header}
            for n, header in enumerate(headers)
        ],
        "header_row": sheet["header_row"],
        "rows": given,
        "count": len(rows),
        "on_screen": True,
    }
    if matching.strip():
        told["matching"] = matching.strip()
    cells = [*sheet["headers"], *(cell for row in given for cell in row["cells"])]
    if any(_AMOUNT.fullmatch(cell.strip()) for cell in cells) and not any(
        _CURRENCY.search(cell) for cell in cells
    ):
        told["amounts"] = NO_CURRENCY
    if left_out := rows[len(given) :]:
        first, last = left_out[0]["row"], left_out[-1]["row"]
        rest = f"{gsheets.letter(left)}{first}:{gsheets.letter(left + width - 1)}{last}"
        told["not_included"] = (
            f"Rows {first} to {last} are not here. To read them, call sheets_read "
            f"again with cells {rest}"
            + (" and the same matching." if matching else ".")
        )
    if not sheet["complete"]:
        told["not_read"] = (
            f"Only the first {gsheets.MAX_READ_ROWS} rows of this tab were read."
        )
    return told


@function_tool()
async def waiting_on_me(context: RunContext) -> dict[str, Any]:
    """What is waiting on the user right now.

    Use this when the user asks what needs them, what they have missed, or for a
    briefing. It returns mail that Orbit has read, saying which emails want an
    answer and whether a reply is already drafted, invitations they have not
    answered, and reminders that have come due.
    """
    session = utils.http_context.http_session()
    now = datetime.now().astimezone()
    with reading("Gmail and Google Calendar"):
        mail, events = await asyncio.gather(
            waiting_mail(session),
            gcal.raw_events(session, now, now + LOOKAHEAD, limit=40),
        )
    due = [r for r in reminders.open_reminders() if r.due_at <= now.timestamp()]
    await stage.show({"id": "needs", "kind": "needs"})
    return {
        "emails": [
            {
                "id": item.id,
                "from": item.sender,
                "subject": item.subject,
                "what_it_asks": item.gist,
                "needs_reply": item.needs_reply,
                "reply_drafted": bool(item.draft and item.draft.status == "suggested"),
            }
            for item in mail.items
        ],
        "still_reading_mail": mail.reading,
        "invitations_not_answered": [
            {
                "event_id": event["id"],
                "title": event.get("summary", "(no title)"),
                "from": event.get("organizer", {}).get("email"),
                "starts": event["start"].get("dateTime") or event["start"]["date"],
            }
            for event in unanswered(events)
        ],
        "reminders_due": [{"id": r.id, "title": r.title} for r in due],
    }


GOOGLE_TOOLS = [
    waiting_on_me,
    gmail_search,
    gmail_read,
    gmail_open_attachment,
    calendar_events,
    chat_recent_messages,
    chat_conversations,
    chat_read,
    drive_search,
    drive_list_folder,
    drive_read,
    sheets_read,
]
