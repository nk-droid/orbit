"""Files that are to go out with an email, kept exactly as they were when they were chosen.

An attachment is copied here the moment it is put on a draft: from Drive, from
another email, from the user's computer, or from what Orbit wrote. The email is
built from that copy, so what goes out is what the user was shown and agreed to,
whatever happens to the original in between.

Each copy belongs to an owner: a draft Orbit made in a conversation, a reply
waiting on its card, or the tray, which holds the files the user has handed to
Orbit and not yet put on anything. The agent and the API are separate processes
that share this store, as they share the rest of the database.
"""

import mimetypes
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import files
from storage import connect, data_dir

# Gmail carries 25 MB of attachments on one email, counted before it encodes them.
MAX_TOTAL_BYTES = 25_000_000
MAX_FILES = 20
# The files the user has handed to Orbit, waiting to be put on a draft.
TRAY = "tray"
# Where a file handed over by the user is said to be from.
FROM_THE_USER = "from your computer"

# A copy on a draft Orbit made is done with once the draft is: a draft lasts five
# minutes. One left behind, by a conversation that ended first, is cleared out.
_DRAFT_KEPT_SECONDS = 60 * 60
# A file handed over and never used is not kept for good.
_TRAY_KEPT_SECONDS = 24 * 60 * 60
# Nor is one put on a reply that was never sent.
_REPLY_KEPT_SECONDS = 30 * 24 * 60 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    source TEXT NOT NULL,
    created_at REAL NOT NULL
)
"""

_UNSAFE_IN_A_NAME = re.compile(r"[\x00-\x1f\x7f/\\]+")
_MAX_NAME_CHARS = 150
_ID = re.compile(r"[0-9a-f]{32}")
# What a type has to look like to be put in an email's headers as it is.
_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*")
_NO_TYPE = "application/octet-stream"


class TooMuchError(Exception):
    """The files together are more than one email can carry."""


@dataclass(frozen=True)
class Attached:
    id: str
    # What it belongs to: "draft:<key>", "reply:<message id>" or the tray.
    owner: str
    name: str
    mime_type: str
    size: int
    # Where it came from, in words for the user: "from Drive", "from your
    # computer", "written by Orbit".
    source: str
    created_at: float


def draft(key: str) -> str:
    """The owner name of a draft Orbit made in a conversation."""
    return f"draft:{key}"


def reply(message_id: str) -> str:
    """The owner name of the reply waiting on an email's card."""
    return f"reply:{message_id}"


def _folder() -> Path:
    folder = data_dir() / "attachments"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def _row(row: Any) -> Attached:
    return Attached(**dict(row))


def safe_name(name: str) -> str:
    """A file name with nothing in it that could reach outside a folder or a header."""
    name = _UNSAFE_IN_A_NAME.sub(" ", name).strip(" .")
    return name[:_MAX_NAME_CHARS] or "attachment"


def _usable(mime_type: str) -> bool:
    """Whether an email can carry a file under this type as it is. Not if it is no
    type at all, and not if it says the file is itself an email or made of parts:
    the email it went into would then be built wrongly, or not at all."""
    return bool(_TYPE.fullmatch(mime_type)) and not mime_type.startswith(
        ("multipart/", "message/")
    )


def type_of(name: str, claimed: str = "") -> str:
    """What kind of file this is: what it says it is, or else what its name says."""
    claimed = claimed.split(";")[0].strip().lower()
    if claimed != _NO_TYPE and _usable(claimed):
        return claimed
    guessed = mimetypes.guess_type(name)[0] or ""
    return guessed if _usable(guessed) else _NO_TYPE


def human_size(size: int) -> str:
    """A size as people say it: "340 KB", "1.2 MB"."""
    if size < 1000:
        return f"{size} bytes"
    if size < 1_000_000:
        return f"{round(size / 1000)} KB"
    return f"{size / 1_000_000:.1f} MB"


def keep(owner: str, name: str, mime_type: str, data: bytes, source: str) -> Attached:
    """Copies a file in. Raises TooMuchError if its owner would then hold too much."""
    held = held_by(owner)
    if (
        len(held) >= MAX_FILES
        or sum(a.size for a in held) + len(data) > MAX_TOTAL_BYTES
    ):
        raise TooMuchError
    name = safe_name(name)
    kept = Attached(
        id=uuid.uuid4().hex,
        owner=owner,
        name=name,
        mime_type=type_of(name, mime_type),
        size=len(data),
        source=source,
        created_at=time.time(),
    )
    path = _folder() / kept.id
    path.write_bytes(data)
    path.chmod(0o600)
    with connect(_SCHEMA) as connection:
        connection.execute(
            "INSERT INTO attachments VALUES (:id, :owner, :name, :mime_type, :size, "
            ":source, :created_at)",
            asdict(kept),
        )
    return kept


def held_by(owner: str) -> list[Attached]:
    """An owner's files, in the order they were added."""
    with connect(_SCHEMA) as connection:
        rows = connection.execute(
            "SELECT * FROM attachments WHERE owner = ? ORDER BY created_at, rowid",
            (owner,),
        ).fetchall()
    return [_row(row) for row in rows]


def find(attachment_id: str) -> Attached | None:
    with connect(_SCHEMA) as connection:
        row = connection.execute(
            "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
    return _row(row) if row else None


def read(attached: Attached) -> bytes:
    """The file's bytes, as they were when it was copied in."""
    return (_folder() / attached.id).read_bytes()


def copy_to(attached: Attached, owner: str) -> Attached:
    """A second copy of a file, for another owner. The first stays where it is."""
    return keep(
        owner, attached.name, attached.mime_type, read(attached), attached.source
    )


def drop(attachment_id: str) -> None:
    # Only ever a name this store gave: never a path to somewhere else.
    if not _ID.fullmatch(attachment_id):
        return
    with connect(_SCHEMA) as connection:
        connection.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
    (_folder() / attachment_id).unlink(missing_ok=True)


def drop_all(owner: str) -> None:
    for attached in held_by(owner):
        drop(attached.id)


def listed(kept: list[Attached]) -> list[dict[str, str]]:
    """Files as a card shows them: what each is called, what it is, how large it
    is and where it came from."""
    return [
        {
            "id": file.id,
            "name": file.name,
            "kind": files.kind_of(file.name, file.mime_type),
            "view": files.view_of(file.name, file.mime_type),
            "size": human_size(file.size),
            "source": file.source,
        }
        for file in kept
    ]


def clear_out() -> int:
    """Removes what has been left behind: copies on drafts long gone, files handed
    over and never used, and files on replies never sent. Returns how many went."""
    now = time.time()
    with connect(_SCHEMA) as connection:
        rows = connection.execute(
            "SELECT id FROM attachments WHERE (owner LIKE 'draft:%' AND created_at < ?)"
            " OR (owner = ? AND created_at < ?)"
            " OR (owner LIKE 'reply:%' AND created_at < ?)",
            (
                now - _DRAFT_KEPT_SECONDS,
                TRAY,
                now - _TRAY_KEPT_SECONDS,
                now - _REPLY_KEPT_SECONDS,
            ),
        ).fetchall()
    for row in rows:
        drop(row["id"])
    return len(rows)
