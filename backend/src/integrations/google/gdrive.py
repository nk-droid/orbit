"""Google Drive: finding, reading and showing files, and writing documents of Orbit's own.

Returns compact facts for the agent, and for the app the bytes it can show.
"""

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import aiohttp

import files
import writing

from . import api

_API = "https://www.googleapis.com/drive/v3"
_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
_DOCS = "https://docs.googleapis.com/v1/documents"
_GOOGLE_DOC = "application/vnd.google-apps.document"
_FOLDER = "application/vnd.google-apps.folder"
SPREADSHEET = "application/vnd.google-apps.spreadsheet"
# What a Google Sheet is called when talking about it.
SHEET_KIND = "Google Sheet"
_FILE_FIELDS = "id,name,mimeType,modifiedTime,owners(displayName),webViewLink"

# What the app needs to show a file, on top of what a listing says about it.
_SHOWN_FIELDS = _FILE_FIELDS + ",thumbnailLink"

MAX_CONTENT_CHARS = 6000
# A file is shown in full, unlike what the agent is given to read. Past these sizes
# the app says it is too large and points to Drive.
MAX_SHOWN_BYTES = 50 * 1024 * 1024
MAX_SHOWN_CHARS = 1_000_000
# How large a preview picture to ask for, on its longest side.
_PREVIEW_PIXELS = 1600
# Drive's own name for the top level of the user's My Drive.
TOP_LEVEL = "root"
TOP_LEVEL_LINK = "https://drive.google.com/drive/my-drive"

# Google's own formats have no bytes to download; they are exported as text instead.
_EXPORT_AS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}

# To be looked at, Google's own formats become a PDF, and a drawing a picture.
_SHOW_AS = {
    "application/vnd.google-apps.document": files.PDF,
    "application/vnd.google-apps.presentation": files.PDF,
    "application/vnd.google-apps.spreadsheet": files.PDF,
    "application/vnd.google-apps.drawing": "image/png",
}

# Sent as a file, one of Google's own formats becomes a PDF, or on request the
# Office format people open it with elsewhere.
_OFFICE = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
}

_KINDS = {
    "application/vnd.google-apps.document": "Google Doc",
    SPREADSHEET: SHEET_KIND,
    "application/vnd.google-apps.presentation": "Google Slides",
    "application/vnd.google-apps.drawing": "Google Drawing",
    _FOLDER: "folder",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel workbook",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint",
}


class UnreadableFileError(Exception):
    """The file is a kind Orbit can neither read nor show."""


def _file(item: dict[str, Any]) -> dict[str, Any]:
    modified = datetime.fromisoformat(item["modifiedTime"].replace("Z", "+00:00"))
    mime_type = item["mimeType"]
    return {
        "id": item["id"],
        "name": item["name"],
        "kind": _KINDS.get(mime_type) or files.kind_of(item["name"], mime_type),
        "modified": modified.astimezone().strftime("%a %d %b %Y %H:%M"),
        "owner": next((o.get("displayName") for o in item.get("owners", [])), None),
        # Where it opens in the browser, for whoever is allowed to see it.
        "link": item.get("webViewLink"),
    }


def _quoted(text: str) -> str:
    """The text as a string in Drive's search language."""
    escaped = text.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


async def search(
    session: aiohttp.ClientSession, text: str = "", limit: int = 5
) -> list[dict[str, Any]]:
    """Files whose name or content mentions `text`; with no text, the latest changed."""
    params = {
        "pageSize": limit,
        "fields": f"files({_FILE_FIELDS})",
        "q": "trashed = false",
    }
    if text:
        params["q"] = f"fullText contains {_quoted(text)} and trashed = false"
    else:
        # Drive rejects orderBy together with a fullText search.
        params["orderBy"] = "modifiedTime desc"
    data = await api.get_json(session, f"{_API}/files", params)
    return [_file(item) for item in data.get("files", [])]


async def folders_named(
    session: aiohttp.ClientSession, name: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Folders with these words in their name."""
    params = {
        "pageSize": limit,
        "fields": f"files({_FILE_FIELDS})",
        "q": (
            f"name contains {_quoted(name)} and mimeType = '{_FOLDER}' "
            "and trashed = false"
        ),
    }
    data = await api.get_json(session, f"{_API}/files", params)
    return [_file(item) for item in data.get("files", [])]


async def spreadsheets_named(
    session: aiohttp.ClientSession, name: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Google Sheets with these words in their name, the latest changed first.

    When no name has them, the sheets that have them in their cells: people
    remember a sheet by what it is about as often as by what it is called.
    """
    is_sheet = f"mimeType = '{SPREADSHEET}' and trashed = false"
    listed = {"pageSize": limit, "fields": f"files({_FILE_FIELDS})"}
    data = await api.get_json(
        session,
        f"{_API}/files",
        {
            **listed,
            "q": f"name contains {_quoted(name)} and {is_sheet}",
            "orderBy": "modifiedTime desc",
        },
    )
    if not data.get("files"):
        # Drive rejects orderBy together with a fullText search.
        data = await api.get_json(
            session,
            f"{_API}/files",
            {**listed, "q": f"fullText contains {_quoted(name)} and {is_sheet}"},
        )
    return [_file(item) for item in data.get("files", [])]


async def children(
    session: aiohttp.ClientSession, folder_id: str = TOP_LEVEL, limit: int = 20
) -> dict[str, Any]:
    """What sits directly inside one folder: its folders first, then files by name.

    A search cannot answer this. It matches words wherever a file is kept, so it
    has to be asked for by the folder the items are in.
    """
    params = {
        "pageSize": limit,
        "fields": f"nextPageToken,files({_FILE_FIELDS})",
        "q": f"{_quoted(folder_id)} in parents and trashed = false",
        "orderBy": "folder,name",
    }
    data = await api.get_json(session, f"{_API}/files", params)
    return {
        "items": [_file(item) for item in data.get("files", [])],
        "more": "nextPageToken" in data,
    }


def _preview(item: dict[str, Any]) -> str | None:
    """Where Drive keeps a picture of the file's first page, if it made one."""
    link = item.get("thumbnailLink")
    if not link or item["mimeType"] == _FOLDER:
        return None
    # The link is fetched with the user's Google sign-in, so it must be Google's.
    host = urlsplit(link).hostname or ""
    if not host.endswith((".googleusercontent.com", ".google.com")):
        return None
    # It ends in the size Drive chose, such as "=s220". Ask for one worth looking at.
    return re.sub(r"=s\d+$", f"=s{_PREVIEW_PIXELS}", link)


def _view(item: dict[str, Any]) -> tuple[files.View, bool]:
    """How the app can show a file, and whether that is only a preview of it.

    A file a browser cannot draw, such as a Word document, is shown by the picture
    Drive keeps of its first page.
    """
    mime_type = item["mimeType"]
    if mime_type in _SHOW_AS:
        return ("pdf" if _SHOW_AS[mime_type] == files.PDF else "image"), False
    view = files.view_of(item["name"], mime_type)
    if view == "none" and _preview(item):
        return "image", True
    return view, False


async def _media(session: aiohttp.ClientSession, file_id: str) -> bytes:
    return await api.get_bytes(
        session, f"{_API}/files/{file_id}", {"alt": "media"}, limit=MAX_SHOWN_BYTES
    )


async def read(
    session: aiohttp.ClientSession, file_id: str, *, sheets_as_text: bool = True
) -> dict[str, Any]:
    """One file for the agent: what it is, and its text when it has any.

    Google Docs, Sheets and Slides and text files have text Orbit can read. A
    picture or a PDF has none: `content` is then None, and the file can still be
    shown. Raises UnreadableFileError for a file that can be neither read nor shown.

    Without `sheets_as_text`, a Google Sheet is only described: it is read through
    the Sheets API instead (gsheets.py), which gives every tab and not only the first.
    """
    item = await api.get_json(
        session, f"{_API}/files/{file_id}", {"fields": _SHOWN_FIELDS}
    )
    mime_type = item["mimeType"]
    content: str | None = None
    if mime_type in _EXPORT_AS and (sheets_as_text or mime_type != SPREADSHEET):
        content = await api.get_text(
            session,
            f"{_API}/files/{file_id}/export",
            {"mimeType": _EXPORT_AS[mime_type]},
        )
    elif (kept_as := files.view_of(item["name"], mime_type)) in files.WRITTEN:
        content = readable(kept_as, await _media(session, file_id))
    elif _view(item)[0] == "none":
        raise UnreadableFileError(_file(item)["kind"])
    return {
        **_file(item),
        "content": None if content is None else content[:MAX_CONTENT_CHARS],
        "content_truncated": content is not None and len(content) > MAX_CONTENT_CHARS,
    }


def readable(view: files.View, data: bytes) -> str:
    """A text file's bytes as what Orbit reads: for an HTML file its words, not its tags."""
    text, _ = files.as_text(data, MAX_SHOWN_CHARS)
    return writing.words(writing.clean(text)) if view == "html" else text


def written(view: files.View, data: bytes) -> dict[str, Any]:
    """A text file's bytes as the app shows them: as text, or drawn as a page.

    A Markdown or an HTML file is somebody else's writing. It is drawn from clean
    HTML made here, never from what the file itself says.
    """
    text, cut = files.as_text(data, MAX_SHOWN_CHARS)
    if view == "markdown":
        # A file is read as Markdown is: lines run on into a paragraph.
        return {
            "text": None,
            "html": writing.to_html(text, "markdown", breaks=False),
            "text_truncated": cut,
        }
    if view == "html":
        return {"text": None, "html": writing.clean(text), "text_truncated": cut}
    return {"text": text, "html": None, "text_truncated": cut}


async def look(session: aiohttp.ClientSession, file_id: str) -> dict[str, Any]:
    """One file for the app: what it is and how to show it, with its words if it is text."""
    item = await api.get_json(
        session, f"{_API}/files/{file_id}", {"fields": _SHOWN_FIELDS}
    )
    view, preview = _view(item)
    shown = {"text": None, "html": None, "text_truncated": False}
    if view in files.WRITTEN:
        shown = written(view, await _media(session, file_id))
    return {**_file(item), "view": view, "preview": preview, **shown}


async def shown(session: aiohttp.ClientSession, file_id: str) -> tuple[str, str, bytes]:
    """What the app shows for one file: its name, the bytes, and what type they are.

    Raises UnreadableFileError for a file the app cannot show at all.
    """
    item = await api.get_json(
        session, f"{_API}/files/{file_id}", {"fields": _SHOWN_FIELDS}
    )
    name, mime_type = item["name"], item["mimeType"]
    view, preview = _view(item)
    if view == "none":
        raise UnreadableFileError(_file(item)["kind"])
    if mime_type in _SHOW_AS:
        export = f"{_API}/files/{file_id}/export"
        try:
            data = await api.get_bytes(
                session,
                export,
                {"mimeType": _SHOW_AS[mime_type]},
                limit=MAX_SHOWN_BYTES,
            )
        except api.GoogleApiError as e:
            # Google makes no PDF of a very long document. Its words can still be
            # had, and are better than nothing to look at.
            if not (e.export_too_large and mime_type in _EXPORT_AS):
                raise
            data = await api.get_bytes(
                session,
                export,
                {"mimeType": _EXPORT_AS[mime_type]},
                limit=MAX_SHOWN_BYTES,
            )
            return name, files.PLAIN_TEXT, files.decode(data).encode()
        return name, _SHOW_AS[mime_type], data
    if preview:
        link = _preview(item)
        assert link is not None  # _view only reports a preview when there is one
        # Drive does not say what format the picture is in. They are PNG or JPEG;
        # a browser tells the two apart by looking, so either name does.
        data = await api.get_bytes(session, link, limit=MAX_SHOWN_BYTES)
        return name, "image/png" if data.startswith(b"\x89PNG") else "image/jpeg", data
    served = files.served_as(name, mime_type)
    assert served is not None  # the view is not "none", so there is a type for it
    data = await _media(session, file_id)
    if served == files.PLAIN_TEXT:
        data = files.decode(data).encode()
    return name, served, data


async def describe(session: aiohttp.ClientSession, file_id: str) -> dict[str, Any]:
    """What one file is, without its contents."""
    item = await api.get_json(
        session, f"{_API}/files/{file_id}", {"fields": _FILE_FIELDS}
    )
    return _file(item)


async def _upload(
    session: aiohttp.ClientSession,
    described: dict[str, str],
    content: str,
    sent_as: str,
) -> dict[str, Any]:
    # One upload carries the file's description and its contents, so a file can
    # never be left created but empty.
    with aiohttp.MultipartWriter("related") as body:
        body.append_json(described)
        body.append(content, {"Content-Type": f"{sent_as}; charset=utf-8"})
    created = await api.send(
        session,
        "POST",
        _UPLOAD,
        body,
        {"uploadType": "multipart", "fields": "id,name,webViewLink"},
    )
    return {
        "id": created["id"],
        "name": created["name"],
        "link": created.get("webViewLink"),
    }


async def to_attach(
    session: aiohttp.ClientSession, file_id: str, *, office: bool, limit: int
) -> tuple[str, str, bytes]:
    """One Drive file as a file to send with an email: its name, type and bytes.

    A file with bytes of its own goes as it is. A Google Doc, Sheet or Slides file
    has none, and is turned into a PDF, or with `office` into a Word, Excel or
    PowerPoint file. Raises UnreadableFileError for a folder and for the Google
    formats that cannot be turned into a file, such as a form.
    """
    item = await api.get_json(
        session, f"{_API}/files/{file_id}", {"fields": _FILE_FIELDS + ",size"}
    )
    name, mime_type = item["name"], item["mimeType"]
    if mime_type in _SHOW_AS:
        sent_as, suffix = files.PDF, ".pdf"
        if office and mime_type in _OFFICE:
            sent_as, suffix = _OFFICE[mime_type]
        data = await api.get_bytes(
            session,
            f"{_API}/files/{file_id}/export",
            {"mimeType": sent_as},
            limit=limit,
        )
        named = name if name.lower().endswith(suffix) else name + suffix
        return named, sent_as, data
    if mime_type.startswith("application/vnd.google-apps."):
        raise UnreadableFileError(_file(item)["kind"])
    if int(item.get("size") or 0) > limit:
        raise api.TooLargeError
    data = await api.get_bytes(
        session, f"{_API}/files/{file_id}", {"alt": "media"}, limit=limit
    )
    return name, mime_type, data


async def create_document(
    session: aiohttp.ClientSession, title: str, text: str, *, html: str | None = None
) -> dict[str, Any]:
    """Creates a Google Doc holding this text, at the top level of the user's Drive.

    With `html`, a whole page of clean HTML, the document is made from that and
    keeps its headings, lists and tables. Drive does the turning into a Google Doc.
    """
    described = {"name": title, "mimeType": _GOOGLE_DOC}
    if html:
        return await _upload(session, described, html, "text/html")
    return await _upload(session, described, text, "text/plain")


async def create_spreadsheet(
    session: aiohttp.ClientSession, title: str, rows_as_csv: str
) -> dict[str, Any]:
    """Creates a Google Sheet from rows written as CSV, at the top level of Drive.

    Drive turns the CSV into a spreadsheet as Sheets imports a file: numbers become
    numbers, dates dates, and a value starting with "=" a formula.
    """
    described = {"name": title, "mimeType": SPREADSHEET}
    return await _upload(session, described, rows_as_csv, "text/csv")


async def create_file(
    session: aiohttp.ClientSession, name: str, content: str, mime_type: str
) -> dict[str, Any]:
    """Creates a file kept as it is written, such as notes.md, at the top of Drive."""
    return await _upload(
        session, {"name": name, "mimeType": mime_type}, content, mime_type
    )


async def append_text(
    session: aiohttp.ClientSession, file_id: str, text: str
) -> dict[str, Any]:
    """Adds text to the end of a Google Doc, leaving what is already there untouched.

    Goes through the Google Docs API, which must be enabled in the Cloud project.
    Re-uploading the file through Drive instead would discard its formatting.
    """
    insert = {"insertText": {"endOfSegmentLocation": {}, "text": f"\n{text}"}}
    await api.send_json(
        session, "POST", f"{_DOCS}/{file_id}:batchUpdate", {"requests": [insert]}
    )
    return {"id": file_id}
