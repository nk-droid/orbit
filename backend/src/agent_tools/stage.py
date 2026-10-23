"""What is on the user's screen: putting cards there, and moving or closing them.

The app's home shows only Orbit. When a tool has something worth looking at, it
asks the app to pull up a card for it. The request reaches the browser as a LiveKit
text stream on the topic below; outside a room (tests, the text debugger) nothing
is sent, and a failure to send never fails the tool.
See https://docs.livekit.io/transport/data/text-streams/

Moving and closing cards is something the user asks for, so the agent needs to know
it happened. That goes through a remote procedure call, which the app answers with
what it changed and which cards are now there.
See https://docs.livekit.io/transport/data/rpc/
"""

import json
import logging
import re
import time
from collections.abc import Iterable
from typing import Any, Literal

from livekit.agents import RunContext, function_tool, get_job_context
from livekit.agents.llm import ToolError

import writing

logger = logging.getLogger("orbit.stage")

TOPIC = "orbit.stage"
# The function the app registers for moving and closing cards. Must match
# frontend/src/voice/VoiceProvider.tsx.
CARDS_METHOD = "orbit.cards"
_ANSWER_SECONDS = 5
# A card is something to glance at, not the whole result.
MAX_ROWS = 8
MAX_FIELD_CHARS = 220
# Something Orbit writes for the screen is a page or two, not a book.
MAX_WRITTEN_CHARS = 20_000


async def show(card: dict[str, Any], *, push: dict[str, str] | None = None) -> None:
    """Asks the app to open the card, or bring it forward if it is already open.

    A card with the same id replaces what that card showed. `push` instead adds one
    entry to a list the card already holds, such as the next step of a task.
    """
    message: dict[str, Any] = {"type": "show", "card": card}
    if push:
        message["push"] = push
    await _tell_app(message)


async def standing_by() -> None:
    """Tells the app the conversation is about to end because the user asked.

    Without this the app cannot tell a goodbye from a lost agent, and reports the
    agent leaving as a failure.
    """
    await _tell_app({"type": "standby"})


async def _tell_app(message: dict[str, Any]) -> None:
    """Sends the app a message. Outside a room, or if it cannot be sent, nothing happens."""
    job = get_job_context(required=False)
    if job is None:
        return
    try:
        await job.room.local_participant.send_text(json.dumps(message), topic=TOPIC)
    except Exception:
        logger.warning("could not tell the app: %s", message["type"], exc_info=True)


def _link(address: str | None) -> str:
    """An address the app may offer to open: a secure web address, or nothing."""
    return address if address and address.startswith("https://") else ""


def row(
    title: str,
    line: str = "",
    detail: str = "",
    meta: str = "",
    *,
    whole: bool = False,
    link: str | None = None,
    file_id: str | None = None,
    sheet_id: str | None = None,
) -> dict[str, str]:
    """One entry of a list card: a title, a second line, small print, and a time.

    `whole` keeps the second line as it was written, however long and over however
    many lines, for a card that is there to be read. `link` is where the entry
    opens: the app makes its title open it and offers to copy it. `file_id` is the
    Drive file the entry stands for: the app offers to show that file in a card.
    `sheet_id` is a Google Sheet's instead, which is shown as a table of its cells.
    """
    fields = {"title": title, "line": line, "detail": detail, "meta": meta}
    entry = {
        name: " ".join(str(value).split())[:MAX_FIELD_CHARS]
        for name, value in fields.items()
        if value
    }
    if whole and line.strip():
        entry["line"] = line.strip()
    if _link(link):
        entry["link"] = _link(link)
    if file_id:
        entry["file_id"] = file_id
    if sheet_id:
        entry["sheet_id"] = sheet_id
    return entry


async def show_drive_file(file_id: str, name: str, kind: str = "") -> None:
    """A card showing one Drive file itself: a picture, a PDF, or a document.

    The app fetches the file from the backend (api/files.py). Only which file it
    is travels here.
    """
    await show(
        {
            "id": f"file:{file_id}",
            "kind": "file",
            "from": "drive",
            "file_id": file_id,
            "title": name,
            "tag": kind,
        }
    )


async def show_sheet(
    spreadsheet_id: str,
    title: str,
    tab: str = "",
    *,
    cells: str = "",
    matching: str = "",
    changed_rows: Iterable[int] = (),
    changed_cells: Iterable[str] = (),
) -> None:
    """A card showing one tab of a spreadsheet as a table, or the part of it read.

    The app fetches the cells from the backend (api/sheets.py), so the card shows the
    sheet as it is now. `changed_rows` and `changed_cells` are marked on it after a
    change.
    """
    await show(
        {
            "id": f"sheet:{spreadsheet_id}",
            "kind": "sheet",
            "spreadsheet_id": spreadsheet_id,
            "title": title,
            "tab": tab,
            "cells": cells,
            "matching": matching,
            "changed_rows": list(changed_rows),
            "changed_cells": list(changed_cells),
            # The same tab shown again after a change is fetched again.
            "shown_at": round(time.time() * 1000),
        }
    )


async def show_chart(chart_id: str, title: str, tag: str = "") -> None:
    """A card drawing a chart. The app fetches the chart, worked out, from the backend
    (api/charts.py). The same chart shown again after a change is fetched again."""
    await show(
        {
            "id": f"chart:{chart_id}",
            "kind": "chart",
            "chart_id": chart_id,
            "title": title,
            "tag": tag,
            "shown_at": round(time.time() * 1000),
        }
    )


async def show_profile() -> None:
    """The card of the user's job profile. The app fetches it from the backend
    (api/jobs.py), so it is fetched again each time it is shown after a change."""
    await show(
        {
            "id": "jobs:profile",
            "kind": "profile",
            "shown_at": round(time.time() * 1000),
        }
    )


async def show_attachment(
    message_id: str, part: str, name: str, kind: str = ""
) -> None:
    """A card showing one file that came with an email."""
    await show(
        {
            "id": f"file:{message_id}:{part}",
            "kind": "file",
            "from": "mail",
            "message_id": message_id,
            "part": part,
            "title": name,
            "tag": kind,
        }
    )


async def show_list(
    card_id: str,
    title: str,
    icon: str,
    rows: list[dict[str, str]],
    tag: str = "",
    *,
    whole: bool = False,
    every_row: bool = False,
    link: str | None = None,
    link_label: str = "",
) -> None:
    """A card listing what a search found.

    Most lists are something to glance at, and show their first few entries. With
    `every_row` the card shows them all, a line each, and with `whole` it shows
    every entry in full, for the user to read through. `link` is where the whole
    list opens, such as the folder it is the contents of.
    """
    shown = rows if whole or every_row else rows[:MAX_ROWS]
    await show(
        {
            "id": card_id,
            "kind": "list",
            "title": title,
            "icon": icon,
            "tag": tag,
            "rows": shown,
            "more": len(rows) - len(shown),
            "whole": whole,
            **opens(link, link_label),
        }
    )


def opens(link: str | None, label: str = "") -> dict[str, str]:
    """What a card is given so that it can be opened where it lives."""
    return {"link": _link(link), "link_label": label} if _link(link) else {}


async def _ask_app(context: RunContext, request: dict[str, str]) -> dict[str, Any]:
    """Calls the app and returns its answer."""
    job = get_job_context(required=False)
    try:
        user = context.session.room_io.linked_participant
    except Exception:
        user = None
    if job is None or user is None:
        raise ToolError(
            "The user's screen cannot be reached in this conversation. Tell them."
        )
    try:
        answer = await job.room.local_participant.perform_rpc(
            destination_identity=user.identity,
            method=CARDS_METHOD,
            payload=json.dumps(request),
            response_timeout=_ANSWER_SECONDS,
        )
        return json.loads(answer)
    except Exception as e:
        logger.warning(
            "the app did not answer a request about its cards", exc_info=True
        )
        raise ToolError(
            "The user's screen did not answer, so nothing was moved or closed. "
            "Tell them."
        ) from e


@function_tool()
async def arrange_cards(
    context: RunContext,
    action: Literal["background", "bring_back", "close", "list"],
    which: str = "all",
) -> dict[str, Any]:
    """Move or close the cards on the user's screen, or see which cards are there.

    Use this when the user asks you to clear or tidy the screen, hide or minimise
    something, bring a card back, or close a card. Clearing the screen or the stage
    means moving everything to the background, not closing it, unless the user says
    close.

    Args:
        action: "background" moves cards to the background shelf, where they stay
            within reach. "bring_back" puts a card from the background back in
            front. "close" removes cards. "list" only reports which cards there are.
        which: Which cards: "all", "front" for the card in front, or a word from the
            card's title, such as "reminders", "needs you", "reply" or "web".
    """
    result = await _ask_app(
        context, {"action": action, "which": which.strip() or "all"}
    )
    if action != "list" and not result.get("changed"):
        result["note"] = (
            "No card matched, so nothing changed. The cards on screen are listed: "
            "use a word from the title of the one the user means."
        )
    return result


@function_tool()
async def write_on_screen(
    context: RunContext,
    title: str,
    content: str,
    written_in: Literal["markdown", "html"] = "markdown",
) -> dict[str, Any]:
    """Put something you have written on the user's screen, with its formatting.

    Use this when an answer has a shape that is easier to look at than to listen
    to: a table, a comparison, steps to follow, a checklist, or a summary with
    headings. Write it in Markdown, or in HTML if the user asks for HTML. Then say
    the gist in a sentence or two: do not read the whole of it out.

    Do not use it for an answer of a sentence or two, and do not use it to show
    what another tool already put on the screen.

    Args:
        title: A few words naming what it is, such as "Trip checklist".
        content: What to show, written in Markdown or HTML: headings, lists,
            tables, bold text and links.
        written_in: "markdown" or "html".
    """
    if len(content) > MAX_WRITTEN_CHARS:
        raise ToolError(
            "That is too long to put on the screen in one piece. Shorten it, or "
            "offer to save it as a document instead."
        )
    html = writing.to_html(writing.as_meant(content), written_in)
    if not title.strip() or not html or not writing.words(html):
        raise ToolError("There is nothing to show. Write a title and the content.")
    # The same title again is the same card, with what it shows replaced.
    name = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "note"
    await show({"id": f"note:{name}", "kind": "note", "title": title, "html": html})
    return {"on_screen": title}


STAGE_TOOLS = [arrange_cards, write_on_screen]
