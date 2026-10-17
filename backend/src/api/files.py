"""Files for the app to show: Drive files, email attachments, and the files kept
to go out with an email.

For each file there are two routes. One says what the file is and how the app can
show it, and carries its text if it is text. The other is the file itself, as
bytes a browser can draw: a picture, or a PDF.

What a file contains is somebody else's writing, so it is never handed to the
browser as something it would run. Text goes out as plain text whatever the file
calls itself, and a picture is sandboxed in case it is opened as a page.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import quote

import aiohttp
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

import attachments
import files
from integrations.google import gdrive, gmail
from integrations.google.api import GoogleApiError, NotConnectedError, TooLargeError
from integrations.google.auth import ReconnectRequiredError
from integrations.google.client import ClientNotConfiguredError

router = APIRouter()


class NotShownError(Exception):
    """The file is a kind the app has no way to show."""


class FileInfo(BaseModel):
    name: str
    # What to call it: "PDF", "Google Doc", "picture".
    kind: str
    view: files.View
    # The picture shown is Drive's preview of the first page, not the file itself.
    preview: bool = False
    # The whole text, for a file shown as text.
    text: str | None = None
    # Clean HTML to draw, for a Markdown or an HTML file (writing.py makes it).
    html: str | None = None
    truncated: bool = False
    # Where the file opens in full, such as in Drive.
    link: str | None = None


@asynccontextmanager
async def _google() -> AsyncIterator[aiohttp.ClientSession]:
    """A session for Google, with whatever goes wrong turned into an answer the app can show."""
    try:
        async with aiohttp.ClientSession() as session:
            yield session
    except (NotConnectedError, ClientNotConfiguredError, ReconnectRequiredError) as e:
        raise HTTPException(503, "Orbit is not connected to Google.") from e
    except GoogleApiError as e:
        raise HTTPException(e.status, e.message) from e
    except TooLargeError as e:
        raise HTTPException(413, "This file is too large to show here.") from e
    except gmail.NoSuchAttachmentError as e:
        raise HTTPException(404, "This email has no such attachment.") from e
    except (gdrive.UnreadableFileError, NotShownError) as e:
        raise HTTPException(415, "Orbit cannot show this kind of file.") from e
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise HTTPException(502, "Google could not be reached.") from e


def _bytes(name: str, media_type: str, data: bytes) -> Response:
    """A file's bytes, for the browser to draw and nothing more."""
    headers = {
        # The browser must take our word for the type, not guess one from the bytes.
        "X-Content-Type-Options": "nosniff",
        # Only the app itself may put the file on a page.
        "Cross-Origin-Resource-Policy": "same-origin",
        "Cache-Control": "private, max-age=300",
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(name, safe='')}",
    }
    # A PDF is drawn by the browser's own viewer, which a sandbox would switch off.
    # Anything else, such as an SVG picture opened in a tab of its own, runs nothing.
    if media_type != files.PDF:
        headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; style-src 'unsafe-inline'"
        )
    return Response(data, media_type=media_type, headers=headers)


@router.get("/files/drive/{file_id}", response_model=FileInfo)
async def drive_file(file_id: str) -> FileInfo:
    async with _google() as session:
        file = await gdrive.look(session, file_id)
    return FileInfo(
        name=file["name"],
        kind=file["kind"],
        view=file["view"],
        preview=file["preview"],
        text=file["text"],
        html=file["html"],
        truncated=file["text_truncated"],
        link=file["link"],
    )


@router.get("/files/drive/{file_id}/content")
async def drive_file_content(file_id: str) -> Response:
    async with _google() as session:
        name, media_type, data = await gdrive.shown(session, file_id)
    return _bytes(name, media_type, data)


@router.get("/files/mail/{message_id}/{part_id}", response_model=FileInfo)
async def attachment(message_id: str, part_id: str) -> FileInfo:
    async with _google() as session:
        message = await gmail.raw_message(session, message_id)
        part = gmail.file_part(message, part_id)
        name, mime_type = part["filename"], part.get("mimeType", "")
        view = files.view_of(name, mime_type)
        shown = {"text": None, "html": None, "text_truncated": False}
        # Only text travels with this answer. A picture or a PDF is fetched when shown.
        if view in files.WRITTEN:
            data = await gmail.part_bytes(session, message_id, part)
            shown = gdrive.written(view, data)
    return FileInfo(
        name=name,
        kind=files.kind_of(name, mime_type),
        view=view,
        text=shown["text"],
        html=shown["html"],
        truncated=shown["text_truncated"],
    )


@router.get("/files/mail/{message_id}/{part_id}/content")
async def attachment_content(message_id: str, part_id: str) -> Response:
    async with _google() as session:
        message = await gmail.raw_message(session, message_id)
        part = gmail.file_part(message, part_id)
        name = part["filename"]
        media_type = files.served_as(name, part.get("mimeType", ""))
        if media_type is None:
            raise NotShownError
        data = await gmail.part_bytes(session, message_id, part)
    if media_type == files.PLAIN_TEXT:
        data = files.decode(data).encode()
    return _bytes(name, media_type, data)


def _kept(attachment_id: str) -> tuple[attachments.Attached, bytes]:
    """A file kept to go out with an email, and its bytes."""
    file = attachments.find(attachment_id)
    if file is not None:
        try:
            return file, attachments.read(file)
        except FileNotFoundError:
            pass
    raise HTTPException(404, "That file is no longer here.")


# Plain `def`s, so FastAPI reads the file in a worker thread.
@router.get("/files/attached/{attachment_id}", response_model=FileInfo)
def attached(attachment_id: str) -> FileInfo:
    file, data = _kept(attachment_id)
    view = files.view_of(file.name, file.mime_type)
    shown = {"text": None, "html": None, "text_truncated": False}
    if view in files.WRITTEN:
        shown = gdrive.written(view, data)
    return FileInfo(
        name=file.name,
        kind=files.kind_of(file.name, file.mime_type),
        view=view,
        text=shown["text"],
        html=shown["html"],
        truncated=shown["text_truncated"],
    )


@router.get("/files/attached/{attachment_id}/content")
def attached_content(attachment_id: str) -> Response:
    file, data = _kept(attachment_id)
    media_type = files.served_as(file.name, file.mime_type)
    if media_type is None:
        raise HTTPException(415, "Orbit cannot show this kind of file.")
    if media_type == files.PLAIN_TEXT:
        data = files.decode(data).encode()
    return _bytes(file.name, media_type, data)
