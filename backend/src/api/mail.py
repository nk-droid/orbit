"""The mail waiting on the user, Orbit's read of each one, and its suggested replies.

A reply can carry files, which the user puts on it from their computer. They are
kept as they were when added (attachments.py), and go out with the reply.
"""

import asyncio
import time
import uuid
from email.utils import parseaddr
from typing import Any

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import attachments
import files
from actions import store as action_log
from actions.gate import Action
from drafting import service, store
from drafting.store import DraftStatus
from integrations.google import gmail, tokens
from integrations.google.api import GoogleApiError, NotConnectedError
from integrations.google.client import ClientNotConfiguredError
from integrations.google.scopes import access_level

from .attachments import AttachedFile, received, shown
from .guard import require_app

router = APIRouter()

# Unread mail Gmail marks important is listed until it is read, however old it is.
UNREAD_QUERY = "in:inbox is:unread is:important"
UNREAD_LIMIT = 5
# Recent mail, read or not. Orbit reads these and lists the ones still owed a reply.
RECENT_QUERY = "in:inbox newer_than:14d -from:me -category:promotions -category:social"
RECENT_LIMIT = 40

# Headers of the mail already fetched. An email's sender, subject and date never
# change, so each is fetched from Gmail once, not on every refresh: Gmail allows
# only so many requests a minute.
_fetched: dict[str, dict[str, Any]] = {}
_FETCHED_LIMIT = 500


class Draft(BaseModel):
    body: str
    status: DraftStatus
    # The files the reply carries, and where it opens in Gmail once it has been
    # put in the drafts there. Given with one email, left out of a list of them.
    attachments: list[AttachedFile] = []
    link: str | None = None


class MailItem(BaseModel):
    id: str
    sender: str
    address: str
    subject: str
    # ISO 8601.
    received_at: str
    snippet: str
    # None while Orbit is still reading the email.
    gist: str | None = None
    needs_reply: bool | None = None
    draft: Draft | None = None


class Attachment(BaseModel):
    name: str
    # Which part of the email the file is in: how the app asks for it (api/files.py).
    part: str
    view: files.View


class MailDetail(MailItem):
    to: str
    body: str
    body_truncated: bool
    attachments: list[Attachment]


class MailList(BaseModel):
    connected: bool
    # Orbit is still reading some of these; ask again shortly.
    reading: bool = False
    items: list[MailItem] = []
    problem: str | None = None


class DraftBody(BaseModel):
    body: str
    # For sending: the ids of the files the user was looking at on the reply.
    attachments: list[str] = []


def mail_item(message: dict[str, Any], record: dict[str, Any] | None) -> MailItem:
    name, address = parseaddr(gmail.header(message, "From"))
    item = MailItem(
        id=message["id"],
        sender=name or address,
        address=address,
        subject=gmail.header(message, "Subject") or "(no subject)",
        received_at=gmail.received_at(message).isoformat(),
        snippet=gmail.snippet(message),
    )
    if record:
        item.gist = record["gist"]
        item.needs_reply = bool(record["needs_reply"])
        if record["reply"] and record["status"] != "none":
            item.draft = Draft(body=record["reply"], status=record["status"])
    return item


def _carried(message_id: str) -> list[attachments.Attached]:
    """The files on the reply to this email."""
    return attachments.held_by(attachments.reply(message_id))


def _draft(message_id: str, record: dict[str, Any]) -> Draft:
    """The reply to one email as its card is given it, with its files."""
    link = None
    if record["status"] == "in_gmail":
        account = (tokens.load() or {}).get("account", "")
        link = gmail.draft_link(record.get("sent_message_id") or "", account)
    return Draft(
        body=record["reply"],
        status=record["status"],
        attachments=shown(_carried(message_id)),
        link=link,
    )


def owed_reply(record: dict[str, Any] | None) -> bool:
    """Orbit drafted a reply to this email and the user has not dealt with it yet."""
    return bool(record and record["reply"]) and record["status"] in (
        "suggested",
        "sending",
    )


class Waiting(BaseModel):
    items: list[MailItem]
    # Orbit is still reading some of the mail; ask again shortly.
    reading: bool
    # Why some of it has not been read, if Orbit tried and failed.
    trouble: str | None


async def _messages(
    session: aiohttp.ClientSession, message_ids: list[str]
) -> list[dict[str, Any]]:
    """Gmail's message objects (headers only) for these ids, fetching only new ones."""
    if len(_fetched) > _FETCHED_LIMIT:
        _fetched.clear()
    missing = [i for i in message_ids if i not in _fetched]
    for message in await gmail.raw_messages(session, missing):
        _fetched[message["id"]] = message
    return [_fetched[i] for i in message_ids]


async def waiting_mail(session: aiohttp.ClientSession) -> Waiting:
    """The mail waiting on the user with Orbit's read of each, newest first."""
    unread_ids, recent_ids = await asyncio.gather(
        gmail.search_ids(session, UNREAD_QUERY, UNREAD_LIMIT),
        gmail.search_ids(session, RECENT_QUERY, RECENT_LIMIT),
    )
    ids = unread_ids + [i for i in recent_ids if i not in unread_ids]
    service.start_reading(ids)
    records = store.get_many(ids)

    # Recent mail is listed only while a reply is owed, so only that is fetched.
    owed = [i for i in ids if i not in unread_ids and owed_reply(records.get(i))]
    waiting = await _messages(session, unread_ids + owed)
    waiting.sort(key=gmail.received_at, reverse=True)
    return Waiting(
        items=[mail_item(message, records.get(message["id"])) for message in waiting],
        reading=service.still_reading(ids),
        trouble=service.trouble(ids),
    )


def _suggested(message_id: str) -> dict[str, Any]:
    record = store.get(message_id)
    if record is None or not record["reply"]:
        raise HTTPException(404, "Orbit has no draft for this email.")
    if record["status"] == "sent":
        raise HTTPException(409, "This reply has already been sent.")
    if record["status"] == "in_gmail":
        raise HTTPException(409, "This reply is in your Gmail drafts. Finish it there.")
    if record["status"] != "suggested":
        raise HTTPException(409, f"This draft is {record['status']}.")
    return record


@router.get("/mail/waiting", response_model=MailList)
async def waiting() -> MailList:
    try:
        async with aiohttp.ClientSession() as session:
            found = await waiting_mail(session)
    except (NotConnectedError, ClientNotConfiguredError):
        return MailList(connected=False)
    except GoogleApiError as e:
        return MailList(connected=True, problem=f"Gmail could not be read: {e.message}")
    return MailList(
        connected=True, reading=found.reading, items=found.items, problem=found.trouble
    )


@router.get("/mail/{message_id}", response_model=MailDetail)
async def message(message_id: str) -> MailDetail:
    try:
        async with aiohttp.ClientSession() as session:
            raw = await gmail.raw_message(session, message_id)
    except GoogleApiError as e:
        raise HTTPException(e.status, e.message) from e
    full = gmail.full(raw)
    record = store.get(message_id)
    item = mail_item(raw, record)
    if item.draft:
        item.draft = _draft(message_id, record)
    return MailDetail(
        **item.model_dump(),
        to=full["to"],
        body=full["body"],
        body_truncated=full["body_truncated"],
        attachments=[
            Attachment(
                name=file["name"],
                part=file["part"],
                view=files.view_of(file["name"], file["mime_type"]),
            )
            for file in gmail.attachments(raw)
        ],
    )


@router.put(
    "/mail/{message_id}/draft",
    response_model=Draft,
    dependencies=[Depends(require_app)],
)
def edit_draft(message_id: str, request: DraftBody) -> Draft:
    record = _suggested(message_id)
    if not request.body.strip():
        raise HTTPException(422, "A reply cannot be empty.")
    store.edit(message_id, request.body)
    return _draft(message_id, {**record, "reply": request.body})


@router.post(
    "/mail/{message_id}/draft/attachments",
    response_model=list[AttachedFile],
    dependencies=[Depends(require_app)],
)
async def attach(message_id: str, name: str, request: Request) -> list[AttachedFile]:
    """Puts a file from the user's computer on the reply. Answers with all its files."""
    _suggested(message_id)
    await received(request, attachments.reply(message_id), name)
    return shown(_carried(message_id))


def _agreed_to(message_id: str, request: DraftBody) -> dict[str, Any]:
    """The reply, if it is the one the user was looking at when they pressed the
    button: the same words and the same files. The go-ahead is for nothing else."""
    record = _suggested(message_id)
    carried = [file.id for file in _carried(message_id)]
    if request.body != record["reply"] or request.attachments != carried:
        raise HTTPException(409, "The draft has changed. Review it again.")
    granted = (tokens.load() or {}).get("scopes", ())
    if access_level("gmail", granted) != "write":
        raise HTTPException(403, "Orbit only has read access to Gmail.")
    return record


async def _reply(
    session: aiohttp.ClientSession, message_id: str, record: dict[str, Any]
) -> tuple[dict[str, Any], str, list[attachments.Attached]]:
    """The reply as the message Gmail is given: who it goes to, the message, and
    the files in it."""
    target = await gmail.reply_target(session, message_id)
    carried = _carried(message_id)
    raw = gmail.build_raw(
        target["to"],
        target["subject"],
        record["reply"],
        attachments=[
            (file.name, file.mime_type, attachments.read(file)) for file in carried
        ],
        in_reply_to=target["in_reply_to"],
        references=target["references"],
    )
    return target, raw, carried


@router.post(
    "/mail/{message_id}/draft/send",
    response_model=Draft,
    dependencies=[Depends(require_app)],
)
async def send_draft(message_id: str, request: DraftBody) -> Draft:
    record = _agreed_to(message_id, request)
    # Claimed before Gmail is called, so two clicks cannot send two emails.
    if not store.reserve(message_id):
        raise HTTPException(409, "This reply is already being sent.")

    try:
        async with aiohttp.ClientSession() as session:
            if await gmail.answered(session, message_id):
                store.release(message_id)
                store.discard(message_id)
                raise HTTPException(
                    409, "Not sent. You have already replied in this thread."
                )
            target, raw, carried = await _reply(session, message_id, record)
            sent = await gmail.send(session, raw, target["thread_id"])
    except HTTPException:
        raise
    except Exception as e:
        store.release(message_id)
        detail = (
            e.message if isinstance(e, GoogleApiError) else "Gmail did not accept it."
        )
        raise HTTPException(502, f"Not sent. {detail}") from e

    store.mark_sent(message_id, sent["id"])
    # The log says which files went. The copies themselves are done with.
    went = {"attachments": attachments.listed(carried)} if carried else {}
    attachments.drop_all(attachments.reply(message_id))
    now = time.time()
    action_log.save(
        Action(
            id=uuid.uuid4().hex[:12],
            kind="gmail.send_reply",
            payload={},
            preview={
                "what": "Reply to email",
                "to": target["to"],
                "subject": target["subject"],
                "body": record["reply"],
                **went,
            },
            created_at=now,
            updated_at=now,
            status="done",
        )
    )
    return Draft(body=record["reply"], status="sent")


@router.post(
    "/mail/{message_id}/draft/to-gmail",
    response_model=Draft,
    dependencies=[Depends(require_app)],
)
async def to_gmail(message_id: str, request: DraftBody) -> Draft:
    """Puts the reply in the user's Gmail drafts, files and all, for them to finish
    there: to add what Orbit cannot, or to send it later. Nothing is sent."""
    record = _agreed_to(message_id, request)
    if not store.reserve(message_id):
        raise HTTPException(409, "This reply is already being sent.")
    try:
        async with aiohttp.ClientSession() as session:
            target, raw, _ = await _reply(session, message_id, record)
            saved = await gmail.save_draft(session, raw, target["thread_id"])
    except Exception as e:
        store.release(message_id)
        detail = (
            e.message if isinstance(e, GoogleApiError) else "Gmail did not accept it."
        )
        raise HTTPException(502, f"Not saved. {detail}") from e

    store.hand_over(message_id, saved["message_id"] or "")
    attachments.drop_all(attachments.reply(message_id))
    return _draft(message_id, store.get(message_id))


@router.post(
    "/mail/{message_id}/draft/discard",
    response_model=Draft,
    dependencies=[Depends(require_app)],
)
def discard_draft(message_id: str) -> Draft:
    record = _suggested(message_id)
    store.discard(message_id)
    attachments.drop_all(attachments.reply(message_id))
    return Draft(body=record["reply"], status="discarded")
