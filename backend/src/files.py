"""How the app can show a file: as a picture, a PDF, text, a drawn page, or not at all.

Decided here from a file's type and name, for Drive files and email attachments
alike, so the agent and the app always agree on it.
"""

from pathlib import PurePosixPath
from typing import Literal

View = Literal["image", "pdf", "text", "markdown", "html", "none"]
# The views whose file is text, which Orbit can read as well as show.
WRITTEN = ("text", "markdown", "html")

PDF = "application/pdf"
# Text and Markdown are only ever handed to a browser as plain text, never as a page.
PLAIN_TEXT = "text/plain; charset=utf-8"

# The picture formats a browser draws. Others, such as TIFF or HEIC, it does not.
_IMAGE_SUFFIXES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}
_IMAGE_TYPES = {*_IMAGE_SUFFIXES.values(), "image/vnd.microsoft.icon"}

_MARKDOWN_TYPES = {"text/markdown", "text/x-markdown"}
_MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd"}
_HTML_TYPES = {"text/html", "application/xhtml+xml"}
_HTML_SUFFIXES = {".html", ".htm"}

# Types that are text although their names do not start with "text/".
_TEXT_TYPES = {
    "application/json",
    "application/ld+json",
    "application/x-ndjson",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/toml",
    "application/javascript",
    "application/x-javascript",
    "application/x-sh",
    "application/sql",
}
_TEXT_SUFFIXES = {
    *(".txt", ".text", ".log", ".csv", ".tsv", ".srt", ".vtt", ".rst", ".tex"),
    *(".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml"),
    *(".css", ".js", ".jsx", ".ts", ".tsx", ".py", ".rb", ".go", ".rs", ".java"),
    *(".kt", ".swift", ".c", ".h", ".cpp", ".cs", ".sh", ".sql"),
}

# Types that say nothing about the file. Its name is then all there is to go on.
_UNTYPED = {"", "application/octet-stream", "binary/octet-stream"}


def _type(mime_type: str) -> str:
    return mime_type.split(";")[0].strip().lower()


def _suffix(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def view_of(name: str, mime_type: str) -> View:
    """How the app can show a file with this name and type."""
    mime_type, suffix = _type(mime_type), _suffix(name)
    plain = mime_type.startswith("text/") or mime_type in _TEXT_TYPES
    untyped = mime_type in _UNTYPED

    if mime_type in _MARKDOWN_TYPES:
        return "markdown"
    # A Markdown file is often stored as plain text, or with no type at all.
    if suffix in _MARKDOWN_SUFFIXES and (plain or untyped):
        return "markdown"
    if mime_type in _HTML_TYPES or (untyped and suffix in _HTML_SUFFIXES):
        return "html"
    if mime_type in _IMAGE_TYPES or (untyped and suffix in _IMAGE_SUFFIXES):
        return "image"
    if mime_type == PDF or (untyped and suffix == ".pdf"):
        return "pdf"
    if plain or (untyped and suffix in _TEXT_SUFFIXES):
        return "text"
    return "none"


def served_as(name: str, mime_type: str) -> str | None:
    """The type a file's bytes are given to the browser as. None if it is not shown.

    Never the type the file claims for itself when that is something a browser
    would run, such as HTML: text of every kind goes out as plain text.
    """
    view = view_of(name, mime_type)
    if view == "image":
        mime_type = _type(mime_type)
        return (
            mime_type if mime_type in _IMAGE_TYPES else _IMAGE_SUFFIXES[_suffix(name)]
        )
    if view == "pdf":
        return PDF
    if view in WRITTEN:
        return PLAIN_TEXT
    return None


def kind_of(name: str, mime_type: str) -> str:
    """What to call a file of this type when talking about it: "picture", "PDF"."""
    view = view_of(name, mime_type)
    if view == "none":
        return _type(mime_type) or "file"
    return {
        "image": "picture",
        "pdf": "PDF",
        "markdown": "Markdown file",
        "html": "HTML file",
        "text": "text file",
    }[view]


def decode(data: bytes) -> str:
    """A text file's bytes as text."""
    # Files written on Windows are often UTF-16, and say so in their first two bytes.
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    # Bytes that are not valid UTF-8 come out as marks, not dropped silently.
    return data.decode("utf-8-sig", errors="replace")


def as_text(data: bytes, limit: int) -> tuple[str, bool]:
    """A text file's bytes as text, cut at `limit` characters. Says if it was cut."""
    text = decode(data)
    return text[:limit], len(text) > limit
