"""What each kind of action actually does once it is approved."""

import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import aiohttp

import attachments
import writing
from integrations.google import gcal, gdrive, gmail, gsheets

from .gate import Action

Effect = Callable[[aiohttp.ClientSession, dict[str, Any]], Awaitable[dict[str, Any]]]


class AttachmentGoneError(Exception):
    """A file the draft was to carry is no longer in the store."""


def attached_files(payload: dict[str, Any]) -> list[gmail.File]:
    """The files a draft carries, as they were copied when it was made."""
    carried: list[gmail.File] = []
    for attachment_id in payload.get("attachments") or ():
        kept = attachments.find(attachment_id)
        if kept is None:
            raise AttachmentGoneError(attachment_id)
        carried.append((kept.name, kept.mime_type, attachments.read(kept)))
    return carried


def clear_up(action: Action) -> None:
    """Drops the copies a draft held once the draft is over, however it ended.

    A file the user handed to Orbit is dropped too once an email has carried it:
    it is kept no longer than it is needed.
    """
    if action.status in ("awaiting_approval", "running"):
        return
    for attachment_id in action.payload.get("attachments") or ():
        attachments.drop(attachment_id)
    if action.status == "done":
        for attachment_id in action.payload.get("from_tray") or ():
            attachments.drop(attachment_id)


def _email(payload: dict[str, Any]) -> str:
    """A drafted email as the message Gmail is given, with the files it carries."""
    # A formatted email goes out as the HTML the user was shown, with its words
    # beside it for mail programs that show no formatting.
    html = payload.get("html")
    return gmail.build_raw(
        payload["to"],
        payload["subject"],
        payload["body"],
        html=writing.page(payload["subject"], html) if html else None,
        attachments=attached_files(payload),
        in_reply_to=payload.get("in_reply_to"),
        references=payload.get("references"),
    )


async def _send_email(
    session: aiohttp.ClientSession, payload: dict[str, Any]
) -> dict[str, Any]:
    return await gmail.send(session, _email(payload), payload.get("thread_id"))


async def keep_in_gmail(
    session: aiohttp.ClientSession, payload: dict[str, Any]
) -> dict[str, Any]:
    """Puts a drafted email in the user's Gmail drafts, for them to finish there.
    Nothing is sent. With ORBIT_DRY_RUN set, nothing leaves Orbit."""
    if os.getenv("ORBIT_DRY_RUN"):
        return {"dry_run": True}
    return await gmail.save_draft(session, _email(payload), payload.get("thread_id"))


async def _create_event(
    session: aiohttp.ClientSession, payload: dict[str, Any]
) -> dict[str, Any]:
    return await gcal.create_event(
        session,
        title=payload["title"],
        start=datetime.fromisoformat(payload["start"]),
        end=datetime.fromisoformat(payload["end"]),
        attendees=payload["attendees"],
        location=payload["location"],
        description=payload["description"],
    )


async def _respond_to_invitation(
    session: aiohttp.ClientSession, payload: dict[str, Any]
) -> dict[str, Any]:
    return await gcal.respond(session, payload["event_id"], payload["response"])


async def _create_document(
    session: aiohttp.ClientSession, payload: dict[str, Any]
) -> dict[str, Any]:
    html = payload.get("html")
    return await gdrive.create_document(
        session,
        payload["title"],
        payload["content"],
        html=writing.page(payload["title"], html) if html else None,
    )


async def _create_file(
    session: aiohttp.ClientSession, payload: dict[str, Any]
) -> dict[str, Any]:
    return await gdrive.create_file(
        session, payload["name"], payload["content"], payload["mime_type"]
    )


async def _add_to_document(
    session: aiohttp.ClientSession, payload: dict[str, Any]
) -> dict[str, Any]:
    return await gdrive.append_text(session, payload["file_id"], payload["text"])


async def _add_rows(
    session: aiohttp.ClientSession, payload: dict[str, Any]
) -> dict[str, Any]:
    return await gsheets.add_rows(
        session,
        payload["spreadsheet_id"],
        payload["tab_id"],
        payload["columns"],
        payload["rows"],
    )


async def _set_cells(
    session: aiohttp.ClientSession, payload: dict[str, Any]
) -> dict[str, Any]:
    return await gsheets.set_cells(
        session,
        payload["spreadsheet_id"],
        payload["tab_id"],
        payload["cells"],
        payload["values"],
        payload["before"],
    )


async def _delete_rows(
    session: aiohttp.ClientSession, payload: dict[str, Any]
) -> dict[str, Any]:
    return await gsheets.delete_rows(
        session,
        payload["spreadsheet_id"],
        payload["tab_id"],
        payload["rows"],
        payload["before"],
    )


async def _create_sheet(
    session: aiohttp.ClientSession, payload: dict[str, Any]
) -> dict[str, Any]:
    return await gsheets.create(
        session, payload["title"], payload["columns"], payload["rows"]
    )


EFFECTS: dict[str, Effect] = {
    "gmail.send_reply": _send_email,
    "gmail.send_email": _send_email,
    "calendar.create_event": _create_event,
    "calendar.respond": _respond_to_invitation,
    "drive.create_document": _create_document,
    "drive.create_file": _create_file,
    "drive.add_to_document": _add_to_document,
    "sheets.add_rows": _add_rows,
    "sheets.set_cells": _set_cells,
    "sheets.delete_rows": _delete_rows,
    "sheets.create": _create_sheet,
}


async def perform(
    session: aiohttp.ClientSession, kind: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Carries out an approved action. With ORBIT_DRY_RUN set, nothing leaves Orbit."""
    if os.getenv("ORBIT_DRY_RUN"):
        return {"dry_run": True}
    return await EFFECTS[kind](session, payload)
