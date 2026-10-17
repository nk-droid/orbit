"""Putting files on an email Orbit drafts.

The model says which files, by reference. This fetches them, checks that an email
can carry them and that the user asked for them, and copies them into the
attachment store. The draft's card shows them from there and the email takes them
from there, so what goes out is what the user was shown.

A file is how something leaves the user's account for good, and what the model
drafts can be steered by what it has read: an email or a page may say "attach
this and send it there". So a file is put on a draft only when the user asked for
one, and that is decided here from what the user said, not left to the model:

- Their own words must ask for it, and words that have put files on one draft do
  not put files on the next.
- Failing that the model is sent to ask them, and a file goes on only once Orbit
  has spoken to them of attaching and they have not said no.

It is a check of words, so it can be wrong both ways. What it lets through still
has to be read out, file by file, and agreed to.
"""

import asyncio
import mimetypes
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import aiohttp
from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

import attachments
import writing
from attachments import Attached
from integrations.google import gdrive, gmail
from integrations.google.api import GoogleApiError, TooLargeError

from . import stage
from .google import _attachment_meant, reading
from .heard import HEARD_WITHIN, LOOK_EVERY, said_so_far
from .state import OrbitState

HOW_TO_NAME = (
    'Name each attachment as "drive:<file id>" for a file in Drive, '
    '"email:<message id>/<file name>" for a file attached to an email, or '
    '"added:<file name>" for a file the user handed over from their computer: '
    '"added:latest" for the last one, "added:all" for all of them.'
)
TOO_MUCH = (
    "That is more than one email can carry: Gmail takes 25 MB of files in all. "
    "Tell the user. Offer to put the file's Drive link in the email instead, or to "
    "draft it without the file and put it in their Gmail drafts, where they can "
    "add the file themselves."
)
TOO_LONG = (
    "Google cannot turn that document into a file to send: it is too long. Tell "
    "the user, and offer to put its link in the email instead."
)
# What the model is told when a document went as a Word file though a PDF was meant.
AS_OFFICE = (
    "The document is too long for Google to make a PDF of, so it is attached as "
    "{name} instead. Tell the user so."
)
NOT_ASKED = (
    "A file goes on an email only when the user has asked for it themselves, and "
    "they have not. Do not attach anything yet. Ask them whether they want "
    "{files} attached, wait for their answer, and draft again with it only if "
    "they say yes."
)
NOT_ASKED_TO_FORWARD = (
    "An email is passed on only when the user has asked for that themselves, and "
    "they have not. Ask them whether they want this email forwarded, with "
    "{files}, wait for their answer, and draft again only if they say yes."
)
NOTHING_ADDED = (
    "The user has not handed over any file from their computer. Ask for it with "
    "ask_for_file, which shows them where to pick it."
)
# What the model is told when files from the user's computer are waiting already.
ALREADY_HERE = (
    "The user has handed these over already. If one of them is the file meant, "
    "attach it as its attach_as. If not, call ask_for_file again: a window "
    "then opens for them to pick it."
)
# What the model is told once the user has been asked for a file.
ASKED_FOR_IT = (
    "A window to pick the file is on the user's screen. Tell them to choose the "
    "file there, and draft nothing yet. They will say when it is added, and what "
    'it is called: attach it then as "added:<that name>".'
)
NOT_ADDED_YET = (
    "The user has not picked the file you asked for yet. Wait for them to say it "
    "is added. If they no longer want to add one, draft without it."
)
# A file asked for and not handed over within this long is no longer waited for.
ASKED_FOR_SECONDS = 300
MAX_ASKED_CHARS = 120
# Where a file that came with a forwarded email is said to be from.
FROM_IT = "from the email"
# What a file written by Orbit is saved as, for each thing it is written in.
_WRITTEN_AS: dict[writing.Format, tuple[str, str]] = {
    "plain": (".txt", "text/plain"),
    "markdown": (".md", "text/markdown"),
    "html": (".html", "text/html"),
}
MAX_WRITTEN_CHARS = 200_000

_REFERENCE = re.compile(r"^\s*(drive|email|added)\s*:(.*)$", re.DOTALL)
_WORD = re.compile(r"[a-z0-9']+")
# Words that ask for a file to go along.
_ATTACHING = frozenset(
    {
        "attach", "attached", "attaching", "attachment", "attachments", "enclose",
        "enclosed", "enclosing", "include", "included", "including",
    }
)  # fmt: skip
# Words that ask for an email to be passed on.
_FORWARDING = frozenset({"forward", "forwarded", "forwarding", "fwd"})
# Words that ask for something to be sent. On their own they may mean only the
# email, so they count when a word of the file's name is said with them: "send
# Asha the budget" asks for the budget to go to her.
_SENDING = frozenset(
    {"send", "sending", "share", "sharing", "email", "emailing", "mail", "mailing"}
)
# Words that point at the email to send on: "send it on to Ravi".
_POINTING = frozenset({"it", "this", "that", "these", "those", "on", "along"})
# Words that ask for a Word, Excel or PowerPoint file, not a PDF.
_OFFICE = frozenset(
    {
        "word", "docx", "excel", "xlsx", "xls", "powerpoint", "pptx", "office",
        "editable",
    }
)  # fmt: skip
# Words that turn an attachment down.
_REFUSING = frozenset({"no", "nope", "nah", "don't", "dont", "never", "cancel", "stop"})
# Parts of a file's name that say nothing about which file it is.
_NOT_A_NAME = frozenset(
    {
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "png", "jpg", "jpeg",
        "gif", "txt", "csv", "html", "the", "and", "for", "file", "copy", "final",
        "new", "scan", "img", "image",
    }
)  # fmt: skip


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold().replace("\u2019", "'")))


def is_office(name: str) -> bool:
    """Whether a file of this name is a Word, Excel or PowerPoint file."""
    return name.casefold().endswith((".docx", ".xlsx", ".pptx"))


def _fresh(context: RunContext[OrbitState]) -> list[set[str]]:
    """The words of each thing the user has said that has not yet put files on
    a draft."""
    said = said_so_far(context)[context.userdata.asked_up_to :]
    return [_words(text) for who, text in said if who == "user"]


async def wants_office(context: RunContext[OrbitState], was_office: bool) -> bool:
    """Whether a Google Doc, Sheet or Slides file is to go as a Word, Excel or
    PowerPoint file, and not as the PDF it goes as otherwise.

    Only if the user's own words ask for one. This is not left to the model:
    asked to send a sheet, it chose Excel about half the time though nobody had
    said so. `was_office` says the draft being changed carries one already, which
    it keeps unless the user now asks for a PDF. If nothing they have said since
    the last draft is on record yet, their words are given a moment to get there.
    """
    waited = 0.0
    while True:
        fresh = _fresh(context)
        if any(words & _OFFICE for words in fresh):
            return True
        if fresh or waited >= HEARD_WITHIN:
            return was_office and not any("pdf" in words for words in fresh)
        await asyncio.sleep(LOOK_EVERY)
        waited += LOOK_EVERY


def _named_by(name: str) -> set[str]:
    """The words of a file's name that say which file it is."""
    return {word for word in _words(name) if len(word) > 2 and word not in _NOT_A_NAME}


def asks(text: str, fetched: Sequence[str] = (), *, forwarding: bool = False) -> bool:
    """Whether these words of the user's ask for files to go with an email.

    A word of attaching does. A word of sending does when it comes with a word
    from the name of each file Orbit would fetch, as in "send Asha the budget".
    When every file is one the user handed over themselves, so that `fetched` is
    empty, a word of sending does by itself, as in "send this to Asha". And an
    email is passed on for a word of forwarding, or for a word of sending with
    one that points at it, as in "send it on to Ravi".
    """
    words = _words(text)
    if forwarding:
        return bool(words & _FORWARDING or (words & _SENDING and words & _POINTING))
    if words & _ATTACHING:
        return True
    if not words & _SENDING:
        return False
    return all(words & _named_by(name) for name in fetched)


async def asked_for(
    context: RunContext[OrbitState],
    files: Sequence[str],
    fetched: Sequence[str] = (),
    *,
    forwarding: bool = False,
) -> None:
    """Lets files onto a draft only once the user has asked for them. Raises
    ToolError otherwise, which sends the model to ask.

    `files` are the names of all of them, `fetched` of the ones Orbit fetched
    itself from Drive or another email, and `forwarding` says they come with an
    email being passed on.

    Their words may be missing though they did ask: the record of what was heard
    runs behind, words are misheard, and people ask in words this does not know.
    So the record is given a moment to catch up, and failing that the model is
    sent to ask them. A draft made after that is let through once Orbit has
    spoken to them of attaching and they have answered, unless they said no. It
    still has to be read out to them, attachments and all, and agreed to.
    """
    state = context.userdata
    waited = 0.0
    while True:
        said = said_so_far(context)
        fresh = [text for who, text in said[state.asked_up_to :] if who == "user"]
        answer = _answer(context)
        if answer or any(asks(text, fetched, forwarding=forwarding) for text in fresh):
            break
        if waited >= HEARD_WITHIN:
            # They did answer, and what they said never reached the record.
            if answer is None:
                break
            state.asked_about_attaching = (state.gate.now(), len(said))
            refusal = NOT_ASKED_TO_FORWARD if forwarding else NOT_ASKED
            raise ToolError(refusal.format(files=", ".join(files)))
        await asyncio.sleep(LOOK_EVERY)
        waited += LOOK_EVERY
    # The words that put files on this draft do not also put files on the next.
    state.asked_about_attaching = None
    state.asked_up_to = len(said_so_far(context))


def _answer(context: RunContext[OrbitState]) -> bool | None:
    """What came of sending the model to ask the user about attaching.

    True if Orbit then spoke to them of attaching and they answered without
    turning it down. False if it has not asked, or they said no. None if it asked
    and they have spoken since, but what they said is not on record yet.
    """
    state = context.userdata
    if state.asked_about_attaching is None:
        return False
    asked_at, said_then = state.asked_about_attaching
    asked, answer = False, None
    for who, text in said_so_far(context)[said_then:]:
        if who == "assistant":
            # A question that does not speak of attaching is not this question.
            if _words(text) & (_ATTACHING | _FORWARDING):
                asked, answer = True, None
        elif asked and text.strip():
            answer = not _words(text) & _REFUSING
    if not asked:
        return False
    if answer is None and not state.gate.heard_from_user_since(asked_at):
        return False
    return answer


def _added(name: str) -> list[Attached]:
    """The files the user added that this name points to: the last one if no name
    is given, and every one for "all"."""
    tray = attachments.held_by(attachments.TRAY)
    if not tray:
        raise ToolError(NOTHING_ADDED)
    wanted = name.strip().casefold()
    if wanted in ("all", "*", "everything"):
        return tray
    if wanted in ("", "latest", "last"):
        return tray[-1:]
    exact = [file for file in tray if file.name.casefold() == wanted]
    partly = [file for file in tray if wanted in file.name.casefold()]
    if len(exact) == 1 or (not exact and len(partly) == 1):
        return exact or partly
    names = ", ".join(file.name for file in tray)
    raise ToolError(
        f"No one file the user added matches that name. They have added: {names}. "
        "Use one of those names, or ask the user which they mean."
    )


@contextmanager
def _fetching(service: str) -> Iterator[None]:
    """Like `reading`, for a file that is to be sent: one that is too large is
    not a file Orbit cannot open, it is one an email cannot carry."""
    with reading(service):
        try:
            yield
        except TooLargeError as e:
            raise ToolError(TOO_MUCH) from e
        except GoogleApiError as e:
            if e.export_too_large:
                raise ToolError(TOO_LONG) from e
            raise


async def _from_drive(
    session: aiohttp.ClientSession, file_id: str, office: bool
) -> tuple[gmail.File, str | None]:
    """A Drive file as a file to send, and anything the model should say of it."""
    limit = attachments.MAX_TOTAL_BYTES
    try:
        with _fetching("Google Drive"):
            try:
                found = await gdrive.to_attach(
                    session, file_id, office=office, limit=limit
                )
                return found, None
            except GoogleApiError as e:
                if office or not e.export_too_large:
                    raise
            # Google makes no PDF of a document this long, but it does make a Word
            # file of it, which is better than no file.
            found = await gdrive.to_attach(session, file_id, office=True, limit=limit)
            return found, AS_OFFICE.format(name=found[0])
    except gdrive.UnreadableFileError as e:
        # A kind with no name of its own is known only by its type.
        what = "a kind of Google file" if "/" in str(e) else f"a {e}"
        raise ToolError(
            f"That is {what}, which cannot be sent as a file. Tell the user, and "
            "offer to put its link in the email instead."
        ) from e


async def _from_email(
    session: aiohttp.ClientSession, message_id: str, name: str
) -> gmail.File:
    with _fetching("Gmail"):
        message = await gmail.raw_message(session, message_id)
        attached = gmail.attachments(message)
        meant = _attachment_meant(attached, name) if attached else None
        if meant is None:
            names = ", ".join(file["name"] for file in attached) or "none"
            raise ToolError(
                f"No one attachment of that email matches that name. It has: {names}."
            )
        part = gmail.file_part(message, meant["part"])
        data = await gmail.part_bytes(session, message_id, part)
    return meant["name"], meant["mime_type"], data


@dataclass(frozen=True)
class Gathered:
    """The files copied in for a draft."""

    kept: list[Attached]
    # The ids of the tray's files among them, which go once the email has gone.
    from_tray: list[str]
    # The names of the ones Orbit fetched itself, from Drive or another email.
    fetched: list[str]
    # What the model should tell the user about them, if anything.
    notes: tuple[str, ...] = ()


async def gather(
    session: aiohttp.ClientSession, references: list[str], *, office: bool, key: str
) -> Gathered:
    """Fetches the files the model named and copies them in for the draft `key`.
    If any one cannot be had, none is kept."""
    owner = attachments.draft(key)
    from_tray: list[str] = []
    fetched: list[str] = []
    notes: list[str] = []
    try:
        for reference in references:
            named = _REFERENCE.match(reference)
            if not named:
                raise ToolError(HOW_TO_NAME)
            where, what = named[1], named[2].strip()
            if where == "added":
                for added in _added(what):
                    from_tray.append(added.id)
                    attachments.copy_to(added, owner)
                continue
            if where == "drive":
                (name, mime_type, data), note = await _from_drive(session, what, office)
                notes += [note] if note else []
                source = "from Drive"
            else:
                message_id, _, file_name = what.partition("/")
                name, mime_type, data = await _from_email(
                    session, message_id.strip(), file_name
                )
                source = "from an email"
            fetched.append(attachments.keep(owner, name, mime_type, data, source).name)
    except attachments.TooMuchError as e:
        attachments.drop_all(owner)
        raise ToolError(TOO_MUCH) from e
    except BaseException:
        attachments.drop_all(owner)
        raise
    return Gathered(attachments.held_by(owner), from_tray, fetched, tuple(notes))


async def take_all(
    session: aiohttp.ClientSession, message: dict[str, Any], *, key: str
) -> list[Attached]:
    """Copies in every file of an email, for a draft that passes the email on."""
    owner = attachments.draft(key)
    try:
        with _fetching("Gmail"):
            for part in gmail.file_parts(message):
                data = await gmail.part_bytes(session, message["id"], part)
                attachments.keep(
                    owner, part["filename"], part.get("mimeType", ""), data, FROM_IT
                )
    except attachments.TooMuchError as e:
        attachments.drop_all(owner)
        raise ToolError(TOO_MUCH) from e
    except BaseException:
        attachments.drop_all(owner)
        raise
    return attachments.held_by(owner)


def handed_over(context: RunContext[OrbitState]) -> None:
    """Refuses a file from the user's computer while one that Orbit asked for has
    not been handed over. Raises ToolError, which tells the model to wait.

    The model sometimes drafts in the same breath as it asks. A file that was
    waiting from before would then go out in place of the one the user is about
    to pick.
    """
    state = context.userdata
    if state.asked_for_file is None:
        return
    asked_at, waiting_then = state.asked_for_file
    waiting_now = {file.id for file in attachments.held_by(attachments.TRAY)}
    still_asking = state.gate.now() - asked_at <= ASKED_FOR_SECONDS
    if still_asking and waiting_now <= waiting_then:
        raise ToolError(NOT_ADDED_YET)
    state.asked_for_file = None


def let_go(key: str) -> None:
    """Drops the copies made for a draft that is not going to be made after all."""
    attachments.drop_all(attachments.draft(key))


def said_as(file: dict[str, str]) -> str:
    """One of a draft's files as the model is told it, to say to the user."""
    return f"{file['name']} ({file['size']}, {file['source']})"


@function_tool()
async def ask_for_file(context: RunContext[OrbitState], what: str) -> dict[str, Any]:
    """Get a file from the user's own computer. Use it when the file to send is
    there: they say so, they say they have added or dropped it, or it is not in
    their Drive or email. It answers with the files they have handed over, or
    opens a window on their screen for them to pick one.

    Args:
        what: The file, in a few words, such as "the signed contract".
    """
    state = context.userdata
    tray = attachments.held_by(attachments.TRAY)
    # What they have handed over and the model has not been told of comes first:
    # asking for a file that is already here would have them pick it twice.
    unknown = [
        file
        for file in tray
        if file.source == attachments.FROM_THE_USER
        and file.id not in state.told_of_files
    ]
    if unknown:
        state.told_of_files |= {file.id for file in unknown}
        here = [
            {
                "name": file.name,
                "size": attachments.human_size(file.size),
                "attach_as": f"added:{file.name}",
            }
            for file in unknown
        ]
        return {"handed_over_already": here, "next": ALREADY_HERE}
    waiting = frozenset(file.id for file in tray)
    state.asked_for_file = (state.gate.now(), waiting)
    asking = " ".join(what.split())[:MAX_ASKED_CHARS]
    await stage.show({"id": "upload", "kind": "upload", "asking": asking})
    return {"status": "the user has been asked for it", "next": ASKED_FOR_IT}


@function_tool()
async def write_attachment(
    context: RunContext[OrbitState],
    name: str,
    content: str,
    written_in: writing.Format = "plain",
) -> dict[str, Any]:
    """Write a file to send with an email, when the user wants something you
    write to go as a file and not in the email's text. Nothing is sent by this
    tool: pass the attach_as it returns to a draft tool.

    Args:
        name: The file's name, such as "Trip notes".
        content: The whole text of the file.
        written_in: "plain" for a text file, "markdown" or "html" for those.
    """
    text = writing.as_meant(content)
    if not name.strip() or not text.strip():
        raise ToolError("A file needs a name and some text. Ask the user.")
    if len(text) > MAX_WRITTEN_CHARS:
        raise ToolError(
            "That is too long to write as an attachment. Shorten it, or offer to "
            "create it as a document in Drive instead."
        )
    suffix, mime_type = _WRITTEN_AS[written_in]
    name = attachments.safe_name(name)
    # A name that already says what kind of file it is, such as "figures.csv", is
    # kept, and the file is of that kind.
    if mimetypes.guess_type(name)[0]:
        mime_type = ""
    else:
        name += suffix
    if written_in == "html":
        text = writing.page(name, writing.clean(text))
    try:
        kept = attachments.keep(
            attachments.TRAY, name, mime_type, text.encode(), "written by Orbit"
        )
    except attachments.TooMuchError as e:
        raise ToolError(
            "There are too many files waiting already. Ask the user to remove "
            "some from the Ready to attach card."
        ) from e
    await stage.show({"id": "tray", "kind": "tray"})
    return {
        "name": kept.name,
        "size": attachments.human_size(kept.size),
        "attach_as": f"added:{kept.name}",
    }


ATTACHING_TOOLS = [ask_for_file, write_attachment]
