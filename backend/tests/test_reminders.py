import time
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from livekit.agents.llm import ToolError

from agent_tools.reminders import (
    reminder_add,
    reminder_cancel,
    reminder_change,
    reminder_done,
    reminder_list,
)
from api import reminders as api
from reminders import store
from reminders.when import WhenError, in_words, resolve

NOW = datetime(2026, 10, 3, 23, 0).astimezone()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path))


def in_hours(hours: float) -> float:
    return time.time() + hours * 3600


def at(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute).astimezone()


def test_a_delay_is_counted_from_now():
    assert resolve("", "", 20, NOW) == NOW + timedelta(minutes=20)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        # NOW is Saturday 3 October 2026, 23:00.
        ("tomorrow", at(2026, 10, 4, 17)),
        ("Tomorrow ", at(2026, 10, 4, 17)),
        ("sunday", at(2026, 10, 4, 17)),
        ("friday", at(2026, 10, 9, 17)),
        # Today's five o'clock has gone, so this Saturday means the next one.
        ("saturday", at(2026, 10, 10, 17)),
        ("2026-12-01", at(2026, 12, 1, 17)),
        # No day: the next time the clock shows five.
        ("", at(2026, 10, 4, 17)),
    ],
)
def test_the_clock_works_out_the_date_from_the_day_as_it_was_said(day, expected):
    assert resolve(day, "17:00", 0, NOW) == expected


def test_tomorrow_is_the_next_calendar_day_even_just_after_midnight():
    just_after_midnight = at(2026, 10, 4, 0, 7)

    assert resolve("tomorrow", "17:00", 0, just_after_midnight) == at(2026, 10, 5, 17)
    assert resolve("today", "17:00", 0, just_after_midnight) == at(2026, 10, 4, 17)


def test_a_time_alone_later_today_means_today():
    assert resolve("", "23:30", 0, NOW) == at(2026, 10, 3, 23, 30)


def test_a_day_without_a_time_is_sent_back_to_be_asked_about():
    with pytest.raises(WhenError, match="what time"):
        resolve("friday", "", 0, NOW)


def test_a_time_that_has_passed_is_refused():
    with pytest.raises(WhenError, match="already passed"):
        resolve("today", "17:00", 0, NOW)


@pytest.mark.parametrize(
    ("day", "clock", "in_minutes"), [("", "", 0), ("tomorrow", "17:00", 20)]
)
def test_the_time_is_given_in_exactly_one_way(day, clock, in_minutes):
    with pytest.raises(WhenError, match="one of them"):
        resolve(day, clock, in_minutes, NOW)


@pytest.mark.parametrize(
    ("day", "clock", "complaint"),
    [("someday", "17:00", "day must be"), ("tomorrow", "5pm", "24-hour")],
)
def test_a_day_or_time_in_another_form_is_sent_back(day, clock, complaint):
    with pytest.raises(WhenError, match=complaint):
        resolve(day, clock, 0, NOW)


def test_a_time_years_away_is_questioned():
    with pytest.raises(WhenError, match="two years"):
        resolve("2030-01-01", "09:00", 0, NOW)


@pytest.mark.parametrize(
    ("moment", "words"),
    [
        (at(2026, 10, 3, 23, 30), "today, Saturday 3 October, at 23:30"),
        (at(2026, 10, 4, 17), "tomorrow, Sunday 4 October, at 17:00"),
        (at(2026, 10, 9, 9), "on Friday 9 October at 09:00"),
        (at(2027, 1, 2, 9), "on Saturday 2 January 2027 at 09:00"),
    ],
)
def test_a_moment_is_said_back_relative_to_today(moment, words):
    assert in_words(moment, NOW) == words


def test_open_reminders_come_soonest_first():
    store.add("Later", in_hours(5))
    store.add("Sooner", in_hours(1))

    assert [r.title for r in store.open_reminders()] == ["Sooner", "Later"]


def test_done_and_cancelled_reminders_leave_the_open_list():
    done = store.add("Call Asha", in_hours(1))
    cancelled = store.add("Buy milk", in_hours(2))
    store.add("Send report", in_hours(3))

    store.set_status(done.id, "done", only_from="open")
    store.set_status(cancelled.id, "cancelled", only_from="open")

    assert [r.title for r in store.open_reminders()] == ["Send report"]
    assert [r.title for r in store.done_since(0)] == ["Call Asha"]


def test_a_reminder_is_only_marked_done_once():
    reminder = store.add("Call Asha", in_hours(1))

    assert store.set_status(reminder.id, "done", only_from="open") is not None
    assert store.set_status(reminder.id, "done", only_from="open") is None


def test_changing_the_time_leaves_the_wording_alone():
    reminder = store.add("Call Asha", in_hours(1), note="About the invoice")
    later = in_hours(6)

    changed = store.change(reminder.id, due_at=later)

    assert changed.title == "Call Asha"
    assert changed.note == "About the invoice"
    assert changed.due_at == later


def test_a_finished_reminder_cannot_be_changed():
    reminder = store.add("Call Asha", in_hours(1))
    store.set_status(reminder.id, "done", only_from="open")

    assert store.change(reminder.id, title="Call Rahul") is None
    assert store.get(reminder.id).title == "Call Asha"


async def test_the_agent_saves_a_reminder_and_gets_back_what_was_saved():
    result = await reminder_add(None, "Check the oven", in_minutes=20, note="  ")

    (saved,) = store.open_reminders()
    assert saved.title == "Check the oven"
    assert saved.note is None
    assert abs(saved.due_at - in_hours(1 / 3)) < 5
    assert result["saved"]["id"] == saved.id
    assert not result["saved"]["overdue"]


async def test_a_reminder_for_a_time_that_has_passed_is_not_saved():
    with pytest.raises(ToolError, match="already passed"):
        await reminder_add(None, "Call Asha", day="2020-01-01", time="09:00")

    assert store.open_reminders() == []


async def test_a_reminder_without_words_is_not_saved():
    with pytest.raises(ToolError):
        await reminder_add(None, "   ", in_minutes=5)

    assert store.open_reminders() == []


async def test_the_agent_changes_only_what_it_is_given():
    reminder = store.add("Call Asha", in_hours(1))

    await reminder_change(None, reminder.id, title="Call Asha about the invoice")

    changed = store.get(reminder.id)
    assert changed.title == "Call Asha about the invoice"
    assert changed.due_at == reminder.due_at

    with pytest.raises(ToolError, match="Nothing to change"):
        await reminder_change(None, reminder.id)


async def test_an_unknown_reminder_is_reported_not_invented():
    for tool in (reminder_done, reminder_cancel):
        with pytest.raises(ToolError, match="no open reminder"):
            await tool(None, "nope")


async def test_the_list_says_what_is_overdue():
    store.add("Was due", in_hours(-2))
    store.add("Coming up", in_hours(2))

    listed = await reminder_list(None)

    assert [(r["title"], r["overdue"]) for r in listed["reminders"]] == [
        ("Was due", True),
        ("Coming up", False),
    ]
    assert listed["count"] == 2


def test_the_app_lists_open_reminders_then_what_was_just_done():
    done = store.add("Call Asha", in_hours(1))
    store.add("Send report", in_hours(3))
    api.mark_done(done.id)

    listed = api.reminders()

    assert [(r.title, r.status) for r in listed] == [
        ("Send report", "open"),
        ("Call Asha", "done"),
    ]
    assert datetime.fromisoformat(listed[0].due_at).utcoffset() is not None


def test_a_slip_on_done_can_be_undone():
    reminder = store.add("Call Asha", in_hours(1))
    api.mark_done(reminder.id)

    assert api.reopen(reminder.id).status == "open"
    assert [r.title for r in store.open_reminders()] == ["Call Asha"]


def test_the_app_is_told_when_a_reminder_is_gone_or_already_handled():
    reminder = store.add("Call Asha", in_hours(1))
    api.cancel(reminder.id)

    with pytest.raises(HTTPException) as handled:
        api.mark_done(reminder.id)
    with pytest.raises(HTTPException) as unknown:
        api.mark_done("nope")

    assert handled.value.status_code == 409
    assert unknown.value.status_code == 404
    assert api.reminders() == []
