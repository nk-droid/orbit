"""Files the user hands to Orbit to send with an email.

A file from their computer arrives as the body of a request, one file a request.
It waits in the tray, where Orbit can take it for a draft, or goes straight onto
the reply waiting on an email's card (api/mail.py). The tray also holds the files
Orbit wrote.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import attachments
import files
from attachments import Attached

from .guard import require_app

router = APIRouter()

TOO_MUCH = "That is more than one email can carry. Gmail takes 25 MB of files in all."
TOO_MANY = f"One email can carry {attachments.MAX_FILES} files at most."


class AttachedFile(BaseModel):
    id: str
    name: str
    # What to call it: "PDF", "picture".
    kind: str
    # How the app can show it, if at all (api/files.py).
    view: files.View
    # As people say it: "340 KB".
    size: str
    # Where it came from, in words: "from your computer", "written by Orbit".
    source: str


def shown(kept: list[Attached]) -> list[AttachedFile]:
    return [AttachedFile(**file) for file in attachments.listed(kept)]


async def received(request: Request, owner: str, name: str) -> Attached:
    """Keeps the file that is a request's body, for `owner`.

    It is read no further than an email could carry, so nothing larger is ever
    held in memory or written down.
    """
    held = attachments.held_by(owner)
    if len(held) >= attachments.MAX_FILES:
        raise HTTPException(413, TOO_MANY)
    room = attachments.MAX_TOTAL_BYTES - sum(file.size for file in held)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > room:
        raise HTTPException(413, TOO_MUCH)
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > room:
            raise HTTPException(413, TOO_MUCH)
    if not data:
        raise HTTPException(422, "That file is empty.")
    claimed = request.headers.get("content-type", "")
    try:
        # Writing a large file down takes a moment, which other requests need not wait for.
        return await asyncio.to_thread(
            attachments.keep,
            owner,
            name,
            claimed,
            bytes(data),
            attachments.FROM_THE_USER,
        )
    except attachments.TooMuchError as e:
        raise HTTPException(413, TOO_MUCH) from e


# A plain `def`, so FastAPI runs the SQLite read in a worker thread.
@router.get("/attachments", response_model=list[AttachedFile])
def waiting() -> list[AttachedFile]:
    """The files in the tray, oldest first. What was left there too long goes."""
    attachments.clear_out()
    return shown(attachments.held_by(attachments.TRAY))


@router.post(
    "/attachments",
    response_model=AttachedFile,
    dependencies=[Depends(require_app)],
)
async def add(name: str, request: Request) -> AttachedFile:
    """Puts a file from the user's computer in the tray."""
    (added,) = shown([await received(request, attachments.TRAY, name)])
    return added


@router.delete("/attachments/{attachment_id}", dependencies=[Depends(require_app)])
def remove(attachment_id: str) -> dict[str, bool]:
    """Takes a file out of the tray, or off a reply waiting on its card."""
    file = attachments.find(attachment_id)
    if file is None:
        raise HTTPException(404, "That file is no longer here.")
    # What Orbit read out is what the user agreed to, so it is not changed under them.
    if file.owner.startswith("draft:"):
        raise HTTPException(
            409,
            "That file is on a draft Orbit read to you. Ask Orbit to draft it again.",
        )
    attachments.drop(attachment_id)
    return {"removed": True}
