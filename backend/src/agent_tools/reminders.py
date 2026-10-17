"""The agent's tools for the user's reminders.

A reminder is the user's own note to themselves, kept on this machine, so it is
saved straight away with no approval step. The model decides what the user meant;
the checks on the time live in reminders/when.py.
"""

from datetime import datetime
from typing import Any

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from reminders import store
from reminders.store import Reminder
from reminders.when import WhenError, in_words, resolve

from . import stage

MAX_TITLE_CHARS = 200
# The app keeps this card up to date from the store itself.
REMINDERS_CARD = {"id": "reminders", "kind": "reminders"}
NOT_FOUND = (
    "There is no open reminder with that id. Call reminder_list to find the right one."
)


def _now() -> datetime:
    return datetime.now().astimezone()


def _facts(reminder: Reminder, now: datetime) -> dict[str, Any]:
    due = datetime.fromtimestamp(reminder.due_at).astimezone()
    return {
        "id": reminder.id,
        "title": reminder.title,
        "note": reminder.note,
        # Worked out by the clock here. Say it to the user as it is written.
        "due": in_words(due, now),
        "status": reminder.status,
        "overdue": reminder.status == "open" and due < now,
    }


def _due(day: str, time: str, in_minutes: int, now: datetime) -> float:
    try:
        return resolve(day, time, in_minutes, now).timestamp()
    except WhenError as e:
        raise ToolError(str(e)) from e


def _title(title: str) -> str:
    title = title.strip()
    if not title:
        raise ToolError("A reminder needs to say what it is for. Ask the user.")
    return title[:MAX_TITLE_CHARS]


@function_tool()
async def reminder_add(
    context: RunContext,
    title: str,
    day: str = "",
    time: str = "",
    in_minutes: int = 0,
    note: str = "",
) -> dict[str, Any]:
    """Log a reminder for the user. It is saved straight away.

    Use this whenever the user asks to be reminded of something. Give the time in
    exactly one of two ways: a day and time, or a delay. Pass the day the way the
    user said it and do not work out the date yourself. If the user named a day but
    no time, ask what time before calling this.

    Args:
        title: What to be reminded of, in a few words, as the user put it.
        day: The day, as the user said it: today, tomorrow, a weekday such as friday, or a date as YYYY-MM-DD.
            Leave empty if they gave only a time, or when using in_minutes.
        time: The time on the user's clock, as a 24-hour HH:MM. Leave empty when
            using in_minutes.
        in_minutes: For a delay, such as "in twenty minutes" or "in two hours":
            how many minutes from now. Leave at 0 when giving a day and time.
        note: Any extra detail the user gave. Leave empty if there was none.
    """
    now = _now()
    reminder = store.add(
        _title(title), _due(day, time, in_minutes, now), note.strip() or None
    )
    await stage.show(REMINDERS_CARD)
    return {"saved": _facts(reminder, now)}


@function_tool()
async def reminder_list(
    context: RunContext, include_done_today: bool = False
) -> dict[str, Any]:
    """The user's reminders, soonest first.

    Use this for any question about what the user has to remember, and to find a
    reminder's id before changing, completing or cancelling it.

    Args:
        include_done_today: Also return the reminders marked done in the last day.
    """
    now = _now()
    reminders = store.open_reminders()
    if include_done_today:
        reminders += store.done_since(now.timestamp() - 24 * 60 * 60)
    await stage.show(REMINDERS_CARD)
    return {
        "now": f"{now:%A %d %B %Y %H:%M}",
        "reminders": [_facts(reminder, now) for reminder in reminders],
        "count": len(reminders),
    }


@function_tool()
async def reminder_change(
    context: RunContext,
    reminder_id: str,
    title: str = "",
    day: str = "",
    time: str = "",
    in_minutes: int = 0,
) -> dict[str, Any]:
    """Change what an open reminder says, when it is due, or both.

    Whatever you leave out stays as it is. A new time is given as in reminder_add:
    a day and time, or a delay.

    Args:
        reminder_id: The id of the reminder, from reminder_list or reminder_add.
        title: The new wording. Leave empty to keep the current one.
        day: The new day, as the user said it: today, tomorrow, a weekday such as friday, or a date as YYYY-MM-DD.
        time: The new time on the user's clock, as a 24-hour HH:MM.
        in_minutes: The new time as minutes from now. Leave at 0 otherwise.
    """
    now = _now()
    new_title = _title(title) if title.strip() else None
    moved = day.strip() or time.strip() or in_minutes
    new_due = _due(day, time, in_minutes, now) if moved else None
    if new_title is None and new_due is None:
        raise ToolError("Nothing to change: give a new title, a new time, or both.")
    reminder = store.change(reminder_id, title=new_title, due_at=new_due)
    if reminder is None:
        raise ToolError(NOT_FOUND)
    await stage.show(REMINDERS_CARD)
    return {"saved": _facts(reminder, now)}


@function_tool()
async def reminder_done(context: RunContext, reminder_id: str) -> dict[str, Any]:
    """Mark a reminder as done, when the user says they have dealt with it.

    Args:
        reminder_id: The id of the reminder, from reminder_list or reminder_add.
    """
    reminder = store.set_status(reminder_id, "done", only_from="open")
    if reminder is None:
        raise ToolError(NOT_FOUND)
    await stage.show(REMINDERS_CARD)
    return {"done": _facts(reminder, _now())}


@function_tool()
async def reminder_cancel(context: RunContext, reminder_id: str) -> dict[str, Any]:
    """Remove a reminder the user no longer wants. Use reminder_done instead when
    the thing was actually dealt with.

    Args:
        reminder_id: The id of the reminder, from reminder_list or reminder_add.
    """
    reminder = store.set_status(reminder_id, "cancelled", only_from="open")
    if reminder is None:
        raise ToolError(NOT_FOUND)
    await stage.show(REMINDERS_CARD)
    return {"cancelled": _facts(reminder, _now())}


REMINDER_TOOLS = [
    reminder_add,
    reminder_list,
    reminder_change,
    reminder_done,
    reminder_cancel,
]
