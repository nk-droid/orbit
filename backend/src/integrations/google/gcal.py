"""Reading Google Calendar. Returns compact facts for the agent."""

from datetime import datetime
from typing import Any

import aiohttp

from . import api

_API = "https://www.googleapis.com/calendar/v3"


def _when(moment: dict[str, Any]) -> str:
    # All-day events carry a date; timed events carry a dateTime with an offset.
    if "date" in moment:
        return moment["date"]
    return datetime.fromisoformat(moment["dateTime"]).strftime("%a %d %b %H:%M")


def _event(event: dict[str, Any]) -> dict[str, Any]:
    attendees = event.get("attendees", [])
    me = next((a for a in attendees if a.get("self")), None)
    return {
        "id": event["id"],
        "title": event.get("summary", "(no title)"),
        "start": _when(event["start"]),
        "end": _when(event["end"]),
        "all_day": "date" in event["start"],
        "location": event.get("location"),
        "meeting_link": event.get("hangoutLink"),
        "organizer": event.get("organizer", {}).get("email"),
        "attendees": [
            a.get("displayName") or a.get("email")
            for a in attendees
            if not a.get("self")
        ],
        # accepted, declined, tentative or needsAction; None when you are the only one.
        "your_response": me.get("responseStatus") if me else None,
    }


async def raw_events(
    session: aiohttp.ClientSession, start: datetime, end: datetime, limit: int = 20
) -> list[dict[str, Any]]:
    """Google's own event objects between two aware datetimes, soonest first."""
    data = await api.get_json(
        session,
        f"{_API}/calendars/primary/events",
        {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            # Expand recurring events into the individual occurrences in range.
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": limit,
        },
    )
    return [item for item in data.get("items", []) if item.get("status") != "cancelled"]


async def events(
    session: aiohttp.ClientSession, start: datetime, end: datetime, limit: int = 20
) -> list[dict[str, Any]]:
    """Events on the primary calendar as compact facts for the agent."""
    return [_event(item) for item in await raw_events(session, start, end, limit)]


async def get_event(session: aiohttp.ClientSession, event_id: str) -> dict[str, Any]:
    return await api.get_json(session, f"{_API}/calendars/primary/events/{event_id}")


async def create_event(
    session: aiohttp.ClientSession,
    *,
    title: str,
    start: datetime,
    end: datetime,
    attendees: list[str],
    location: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Creates an event on the primary calendar and emails invitations to attendees."""
    body: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "attendees": [{"email": address} for address in attendees],
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    created = await api.send_json(
        session,
        "POST",
        f"{_API}/calendars/primary/events",
        body,
        {"sendUpdates": "all"},
    )
    return _event(created)


async def respond(
    session: aiohttp.ClientSession, event_id: str, response: str
) -> dict[str, Any]:
    """Answers an invitation: accepted, declined or tentative. The organiser is told."""
    event = await get_event(session, event_id)
    attendees = event.get("attendees", [])
    for attendee in attendees:
        if attendee.get("self"):
            attendee["responseStatus"] = response
    # PATCH replaces the whole attendee list, so every attendee is sent back.
    updated = await api.send_json(
        session,
        "PATCH",
        f"{_API}/calendars/primary/events/{event_id}",
        {"attendees": attendees},
        {"sendUpdates": "all"},
    )
    return _event(updated)
