"""What the command deck shows: what is waiting on the user, and what is coming up."""

import asyncio
from datetime import datetime, timedelta
from typing import Any, Literal

import aiohttp
from fastapi import APIRouter
from pydantic import BaseModel

from integrations.google import gcal
from integrations.google.api import GoogleApiError, NotConnectedError
from integrations.google.auth import ReconnectRequiredError
from integrations.google.client import ClientNotConfiguredError

from .mail import MailItem, waiting_mail

router = APIRouter()

LOOKAHEAD = timedelta(days=7)


class WaitingItem(BaseModel):
    kind: Literal["email", "invitation"]
    title: str
    detail: str
    # ISO 8601: when the email arrived, or when the event starts.
    at: str
    # For emails: its id, Orbit's one-line read of it, and whether a reply is drafted.
    message_id: str | None = None
    gist: str | None = None
    has_draft: bool = False


class DeckEvent(BaseModel):
    title: str
    # ISO 8601 with an offset, or a plain date for all-day events.
    start: str
    end: str
    all_day: bool
    place: str | None = None


class Deck(BaseModel):
    connected: bool
    waiting: list[WaitingItem] = []
    # Orbit is still reading some of the mail; its gists and drafts are on their way.
    reading: bool = False
    events: list[DeckEvent] = []
    # One short reason per service that could not be read.
    problems: list[str] = []


def email_item(mail: MailItem) -> WaitingItem:
    return WaitingItem(
        kind="email",
        title=f"{mail.sender}: {mail.subject}",
        detail=mail.snippet,
        at=mail.received_at,
        message_id=mail.id,
        gist=mail.gist,
        has_draft=mail.draft is not None and mail.draft.status == "suggested",
    )


def _start(event: dict[str, Any]) -> str:
    return event["start"].get("dateTime") or event["start"]["date"]


def awaits_reply(event: dict[str, Any]) -> bool:
    """An invitation the user has not answered yet."""
    return any(
        attendee.get("self") and attendee.get("responseStatus") == "needsAction"
        for attendee in event.get("attendees", [])
    )


def unanswered(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Invitations awaiting a reply, with a recurring series counted once (its next occurrence)."""
    seen: set[str] = set()
    result = []
    for event in events:
        series = event.get("recurringEventId") or event["id"]
        if awaits_reply(event) and series not in seen:
            seen.add(series)
            result.append(event)
    return result


def invitation_item(event: dict[str, Any]) -> WaitingItem:
    organizer = event.get("organizer", {})
    sender = organizer.get("displayName") or organizer.get("email") or "someone"
    return WaitingItem(
        kind="invitation",
        title=event.get("summary", "(no title)"),
        detail=f"Invitation from {sender}, not answered yet",
        at=_start(event),
    )


def deck_event(event: dict[str, Any]) -> DeckEvent:
    return DeckEvent(
        title=event.get("summary", "(no title)"),
        start=_start(event),
        end=event["end"].get("dateTime") or event["end"]["date"],
        all_day="date" in event["start"],
        place=event.get("location")
        or ("Google Meet" if event.get("hangoutLink") else None),
    )


def _problem(service: str, error: BaseException) -> str:
    if isinstance(error, GoogleApiError) and error.api_not_enabled:
        return f"{service} is not enabled in your Google Cloud project."
    if isinstance(error, ReconnectRequiredError):
        return "The Google sign-in has expired. Connect Google again."
    return f"{service} could not be read."


@router.get("/deck", response_model=Deck)
async def deck() -> Deck:
    now = datetime.now().astimezone()
    async with aiohttp.ClientSession() as session:
        # Gathered with return_exceptions so one failing service leaves the other usable.
        mail, events = await asyncio.gather(
            waiting_mail(session),
            gcal.raw_events(session, now, now + LOOKAHEAD, limit=40),
            return_exceptions=True,
        )
    not_connected = (NotConnectedError, ClientNotConfiguredError)
    if isinstance(mail, not_connected) or isinstance(events, not_connected):
        return Deck(connected=False)

    result = Deck(connected=True)
    if isinstance(mail, BaseException):
        result.problems.append(_problem("Gmail", mail))
    else:
        result.reading = mail.reading
        result.waiting.extend(email_item(item) for item in mail.items)
        if mail.trouble:
            result.problems.append(mail.trouble)
    if isinstance(events, BaseException):
        result.problems.append(_problem("Google Calendar", events))
    else:
        result.waiting.extend(invitation_item(e) for e in unanswered(events))
        result.events = [deck_event(e) for e in events]
    return result
