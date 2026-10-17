"""Turning the day and time the user gave into a moment, checked against the clock.

The model passes the day the way the user said it ("tomorrow", "friday") and the
clock here works out the date. Left to work out dates itself, the model gets
"tomorrow" wrong around midnight, and then tells the user the wrong thing.
"""

from datetime import date, datetime, time, timedelta

MAX_AHEAD = timedelta(days=2 * 366)
WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
DAY_FORMS = '"today", "tomorrow", a weekday such as "friday", or a date as YYYY-MM-DD'
EARLIER_FORMS = (
    '"today", "yesterday", a weekday such as "friday" for the most recent one, or '
    "a date as YYYY-MM-DD"
)


class WhenError(ValueError):
    """The time cannot be used. The message tells the model what to do about it."""


def earlier(day: str, today: date) -> date:
    """A day that has been, or today, from the way the user said it. A weekday is
    the most recent one before today."""
    day = day.strip().lower().removeprefix("last ")
    if day == "today":
        return today
    if day == "yesterday":
        return today - timedelta(days=1)
    if day in WEEKDAYS:
        back = (today.weekday() - WEEKDAYS.index(day)) % 7
        return today - timedelta(days=back or 7)
    try:
        found = date.fromisoformat(day)
    except ValueError as e:
        raise WhenError(f"day must be {EARLIER_FORMS}.") from e
    if found > today:
        raise WhenError(
            f"That day has not come yet: today is {today:%A %d %B %Y}. Check the "
            "date with the user."
        )
    return found


def _on(day: str, clock: time, now: datetime) -> datetime:
    """The moment the clock shows `clock` on `day`."""
    today = now.date()

    def at(on: date) -> datetime:
        # A time without an offset is taken as the machine's local time.
        return datetime.combine(on, clock).astimezone()

    if day == "today":
        return at(today)
    if day == "tomorrow":
        return at(today + timedelta(days=1))
    if day in WEEKDAYS or not day:
        # No day, or a weekday: the next time it comes round, today included.
        ahead = (WEEKDAYS.index(day) - today.weekday()) % 7 if day else 0
        first = at(today + timedelta(days=ahead))
        return (
            first
            if first > now
            else at(today + timedelta(days=ahead + (7 if day else 1)))
        )
    try:
        return at(date.fromisoformat(day))
    except ValueError as e:
        raise WhenError(f"day must be {DAY_FORMS}.") from e


def resolve(day: str, clock: str, in_minutes: int, now: datetime) -> datetime:
    """When something is due.

    Either a day and a time on the user's clock, or a delay from now. `day` is given
    as the user said it: today, tomorrow, a weekday, or a date. An empty day means
    the next time the clock shows that time. `now` is an aware datetime.
    """
    day, clock = day.strip().lower(), clock.strip()
    if in_minutes < 0:
        raise WhenError("in_minutes cannot be negative.")
    if bool(day or clock) == (in_minutes > 0):
        raise WhenError(
            "Give either a day and time, or in_minutes: one of them, not both."
        )

    if in_minutes:
        moment = now + timedelta(minutes=in_minutes)
    else:
        if not clock:
            raise WhenError(
                "A time is needed as well as a day. Ask the user what time."
            )
        try:
            hour = datetime.strptime(clock, "%H:%M").time()
        except ValueError as e:
            raise WhenError("time must be a 24-hour time such as 17:00.") from e
        moment = _on(day, hour, now)
        if moment <= now:
            raise WhenError(
                f"That time has already passed: it is now {now:%A %d %B %Y %H:%M}. "
                "Check the day and time with the user."
            )

    if moment - now > MAX_AHEAD:
        raise WhenError("That is more than two years away. Check the date.")
    return moment


def in_words(moment: datetime, now: datetime) -> str:
    """A moment the way it should be said back: "tomorrow, Monday 5 October, at 17:00"."""
    days = (moment.date() - now.date()).days
    named = f"{moment:%A} {moment.day} {moment:%B}"
    if moment.year != now.year:
        named += f" {moment.year}"
    if days == 0:
        day = f"today, {named},"
    elif days == 1:
        day = f"tomorrow, {named},"
    elif days == -1:
        day = f"yesterday, {named},"
    else:
        day = f"on {named}"
    return f"{day} at {moment:%H:%M}"
