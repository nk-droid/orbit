"""Reading Gmail. Returns compact facts for the agent, never Google's raw payloads."""

import asyncio
import base64
import html
import re
from collections.abc import Sequence
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any
from urllib.parse import quote

import aiohttp

from . import api

_API = "https://gmail.googleapis.com/gmail/v1/users/me"
# How many ids one page of a search holds, at most.
_PAGE = 500
# Where Gmail takes a message too large for an ordinary call, as an upload.
_UPLOAD = "https://gmail.googleapis.com/upload/gmail/v1/users/me"
# An ordinary call carries a message inside a piece of JSON, which is not made for
# megabytes. Past this many characters of it, the message is uploaded instead.
_UPLOAD_OVER_CHARS = 3_000_000
# A file to send along: its name, what kind of file it is, and its bytes.
File = tuple[str, str, bytes]
_SUMMARY_HEADERS = ("From", "To", "Subject")

# Enough for the agent to answer about an email without flooding its context.
MAX_BODY_CHARS = 6000
# Gmail carries attachments of up to 25 MB.
MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024
# The part id of a message that is itself the file. Gmail gives that one no id.
_WHOLE_MESSAGE = "-"


class NoSuchAttachmentError(Exception):
    """The email has no file in that part."""


def header(message: dict[str, Any], name: str) -> str:
    for header in message.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def received_at(message: dict[str, Any]) -> datetime:
    """When Gmail received the message, in the machine's local time zone."""
    millis = int(message.get("internalDate", "0"))
    return datetime.fromtimestamp(millis / 1000).astimezone()


def snippet(message: dict[str, Any]) -> str:
    return html.unescape(message.get("snippet", ""))


def _summary(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": message["id"],
        "from": header(message, "From"),
        "subject": header(message, "Subject") or "(no subject)",
        "received": received_at(message).strftime("%a %d %b %H:%M"),
        "unread": "UNREAD" in message.get("labelIds", []),
        "snippet": snippet(message),
    }


def _bytes(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode(data: str) -> str:
    return _bytes(data).decode("utf-8", errors="replace")


def _find_part(payload: dict[str, Any], mime_type: str) -> str | None:
    if payload.get("mimeType") == mime_type and payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])
    for part in payload.get("parts", []):
        found = _find_part(part, mime_type)
        if found is not None:
            return found
    return None


def _strip_html(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _body(payload: dict[str, Any]) -> str:
    plain = _find_part(payload, "text/plain")
    if plain is not None:
        return plain.strip()
    markup = _find_part(payload, "text/html")
    return _strip_html(markup) if markup is not None else ""


def _file_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The parts of a message that are files, wherever they are nested."""
    found = [payload] if payload.get("filename") else []
    for part in payload.get("parts", []):
        found.extend(_file_parts(part))
    return found


def file_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    """The parts of a raw message that hold its files, in the order they come."""
    return _file_parts(message.get("payload", {}))


def attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    """The files sent with a message: each one's name and type, and the part it is in."""
    return [
        {
            "part": part.get("partId") or _WHOLE_MESSAGE,
            "name": part["filename"],
            "mime_type": part.get("mimeType", ""),
        }
        for part in file_parts(message)
    ]


async def search_ids(
    session: aiohttp.ClientSession, query: str = "", limit: int = 5
) -> list[str]:
    """The ids of the messages matching a Gmail search query, newest first.

    No query means the inbox.
    """
    params: list[tuple[str, Any]] = [("maxResults", limit)]
    if query:
        params.append(("q", query))
    else:
        params.append(("labelIds", "INBOX"))
    listing = await api.get_json(session, f"{_API}/messages", params)
    return [item["id"] for item in listing.get("messages", [])]


async def raw_messages(
    session: aiohttp.ClientSession, message_ids: list[str]
) -> list[dict[str, Any]]:
    """Gmail's own message objects (headers only) for these ids, in the same order."""
    details = [("format", "metadata")]
    details.extend(("metadataHeaders", name) for name in _SUMMARY_HEADERS)
    messages = await asyncio.gather(
        *(
            api.get_json(session, f"{_API}/messages/{message_id}", details)
            for message_id in message_ids
        )
    )
    return list(messages)


async def every_id(
    session: aiohttp.ClientSession, query: str, limit: int
) -> tuple[list[str], bool]:
    """The ids of the messages matching a Gmail search, newest first, page after page,
    up to `limit`. Also says whether there were more."""
    ids: list[str] = []
    token = ""
    while len(ids) < limit:
        params: list[tuple[str, Any]] = [("maxResults", min(_PAGE, limit - len(ids)))]
        params.append(("q", query) if query else ("labelIds", "INBOX"))
        if token:
            params.append(("pageToken", token))
        listing = await api.get_json(session, f"{_API}/messages", params)
        ids += [item["id"] for item in listing.get("messages", [])]
        token = listing.get("nextPageToken", "")
        if not token:
            break
    return ids[:limit], bool(token)


async def headers_of(
    session: aiohttp.ClientSession, message_ids: list[str], at_once: int = 20
) -> list[dict[str, Any]]:
    """Gmail's own message objects (headers only) for many ids, a few at a time:
    Gmail answers only so many at once."""
    found: list[dict[str, Any]] = []
    for start in range(0, len(message_ids), at_once):
        found += await raw_messages(session, message_ids[start : start + at_once])
    return found


async def raw_search(
    session: aiohttp.ClientSession, query: str = "", limit: int = 5
) -> list[dict[str, Any]]:
    """Gmail's own message objects (headers only) for a search query, newest first."""
    return await raw_messages(session, await search_ids(session, query, limit))


async def search(
    session: aiohttp.ClientSession, query: str = "", limit: int = 5
) -> list[dict[str, Any]]:
    """Emails matching a Gmail search query, as compact facts for the agent."""
    return [_summary(message) for message in await raw_search(session, query, limit)]


async def raw_message(
    session: aiohttp.ClientSession, message_id: str
) -> dict[str, Any]:
    """Gmail's own message object, complete with its body."""
    return await api.get_json(
        session, f"{_API}/messages/{message_id}", {"format": "full"}
    )


def full(message: dict[str, Any]) -> dict[str, Any]:
    """A raw message as compact facts: who, when, and the text of its body."""
    payload = message.get("payload", {})
    body = _body(payload)
    return {
        **_summary(message),
        "to": header(message, "To"),
        "body": body[:MAX_BODY_CHARS],
        "body_truncated": len(body) > MAX_BODY_CHARS,
        "attachments": [file["name"] for file in attachments(message)],
    }


async def read(session: aiohttp.ClientSession, message_id: str) -> dict[str, Any]:
    """One email in full, as compact facts for the agent."""
    return full(await raw_message(session, message_id))


def passed_on(message: dict[str, Any]) -> dict[str, str]:
    """An email as it is quoted when it is forwarded: who, when, and all of its words."""
    return {
        "from": header(message, "From"),
        "to": header(message, "To"),
        "date": header(message, "Date") or f"{received_at(message):%a, %d %b %Y %H:%M}",
        "subject": header(message, "Subject") or "(no subject)",
        "body": _body(message.get("payload", {})),
    }


def file_part(message: dict[str, Any], part_id: str) -> dict[str, Any]:
    """The part of a raw message that holds one of its files.

    A file is named by the part it is in, which stays the same. Gmail's own id
    for an attachment changes from one request to the next.
    """
    for part in file_parts(message):
        if (part.get("partId") or _WHOLE_MESSAGE) == part_id:
            return part
    raise NoSuchAttachmentError


async def part_bytes(
    session: aiohttp.ClientSession, message_id: str, part: dict[str, Any]
) -> bytes:
    """The bytes of one file of an email."""
    body = part.get("body", {})
    if body.get("size", 0) > MAX_ATTACHMENT_BYTES:
        raise api.TooLargeError
    # A small file comes with the message. A larger one is fetched separately.
    data = body.get("data")
    if data is None:
        fetched = await api.get_json(
            session,
            f"{_API}/messages/{message_id}/attachments/{body['attachmentId']}",
            timeout=api.DOWNLOAD_TIMEOUT,
        )
        data = fetched["data"]
    return _bytes(data)


async def answered(session: aiohttp.ClientSession, message_id: str) -> bool:
    """True if the user has sent something in this email's thread since it arrived."""
    minimal = {"format": "metadata", "metadataHeaders": "From"}
    message = await api.get_json(session, f"{_API}/messages/{message_id}", minimal)
    thread = await api.get_json(
        session, f"{_API}/threads/{message['threadId']}", minimal
    )
    arrived = int(message["internalDate"])
    return any(
        "SENT" in later.get("labelIds", ()) and int(later["internalDate"]) > arrived
        for later in thread.get("messages", [])
    )


def _reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


async def reply_target(
    session: aiohttp.ClientSession, message_id: str
) -> dict[str, Any]:
    """Who a reply to this email goes to, and the headers that keep it in its thread."""
    details: list[tuple[str, Any]] = [("format", "metadata")]
    details.extend(
        ("metadataHeaders", name)
        for name in ("From", "Reply-To", "Subject", "Message-ID", "References")
    )
    message = await api.get_json(session, f"{_API}/messages/{message_id}", details)
    message_ref = header(message, "Message-ID")
    _, address = parseaddr(header(message, "Reply-To") or header(message, "From"))
    return {
        "to": [address],
        "subject": _reply_subject(header(message, "Subject") or "(no subject)"),
        "thread_id": message["threadId"],
        "in_reply_to": message_ref,
        "references": f"{header(message, 'References')} {message_ref}".strip(),
    }


def build_raw(
    to: list[str],
    subject: str,
    body: str,
    *,
    html: str | None = None,
    attachments: Sequence[File] = (),
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """An RFC 822 message, base64url-encoded the way Gmail's send call wants it.

    With `html`, a whole page of clean HTML, the message carries both: mail
    programs show the formatted one, and `body` is there for those that do not.
    The `attachments` go along as files, each under its own name.
    """
    email = EmailMessage()
    email["To"] = ", ".join(to)
    email["Subject"] = subject
    if in_reply_to:
        email["In-Reply-To"] = in_reply_to
        email["References"] = references or in_reply_to
    email.set_content(body)
    if html:
        email.add_alternative(html, subtype="html")
    for name, mime_type, data in attachments:
        kind, _, form = mime_type.partition("/")
        email.add_attachment(
            data,
            maintype=kind or "application",
            subtype=form or "octet-stream",
            filename=name,
        )
    return base64.urlsafe_b64encode(email.as_bytes()).decode()


async def _upload(
    session: aiohttp.ClientSession, to: str, raw: str, described: dict[str, Any]
) -> dict[str, Any]:
    """Hands Gmail a large message as an upload: what it is, then the message itself."""
    with aiohttp.MultipartWriter("related") as body:
        body.append_json(described)
        body.append(_bytes(raw), {"Content-Type": "message/rfc822"})
    return await api.send(
        session,
        "POST",
        f"{_UPLOAD}/{to}",
        body,
        {"uploadType": "multipart"},
        timeout=api.DOWNLOAD_TIMEOUT,
    )


async def send(
    session: aiohttp.ClientSession, raw: str, thread_id: str | None = None
) -> dict[str, Any]:
    """Sends a message built by build_raw. Gmail fills in the From address."""
    body: dict[str, Any] = {"threadId": thread_id} if thread_id else {}
    if len(raw) > _UPLOAD_OVER_CHARS:
        sent = await _upload(session, "messages/send", raw, body)
    else:
        sent = await api.send_json(
            session, "POST", f"{_API}/messages/send", {"raw": raw, **body}
        )
    return {"id": sent["id"], "thread_id": sent.get("threadId")}


def draft_link(message_id: str, account: str = "") -> str:
    """Where a draft saved in Gmail opens: in the Gmail of the account it was
    saved in, whichever account the browser has in front."""
    whose = f"?authuser={quote(account)}" if account else ""
    opened = f"?compose={quote(message_id)}" if message_id else ""
    return f"https://mail.google.com/mail/{whose}#drafts{opened}"


async def save_draft(
    session: aiohttp.ClientSession, raw: str, thread_id: str | None = None
) -> dict[str, Any]:
    """Puts a message built by build_raw in the user's Gmail Drafts. Nothing is sent.

    It is there for the user to finish and send from Gmail itself.
    """
    message: dict[str, Any] = {"threadId": thread_id} if thread_id else {}
    if len(raw) > _UPLOAD_OVER_CHARS:
        saved = await _upload(session, "drafts", raw, {"message": message})
    else:
        saved = await api.send_json(
            session, "POST", f"{_API}/drafts", {"message": {"raw": raw, **message}}
        )
    return {"id": saved["id"], "message_id": saved.get("message", {}).get("id")}
