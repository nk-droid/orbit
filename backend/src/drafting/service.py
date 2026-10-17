"""Keeps Orbit's read of the waiting mail up to date without making anyone wait for it."""

import asyncio
import logging
import time

import aiohttp

from integrations.google import api, gmail

from . import store
from .triage import ModelError, Triage, triage

logger = logging.getLogger("orbit.drafting")

_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# One email per request: the model judges each on its own, and never skips one the
# way it sometimes does in a batch.
EMAILS_PER_REQUEST = 1
REQUESTS_AT_ONCE = 3
# After a failed read the email is tried again, waiting four times longer each time.
RETRY_SECONDS = 60
LONGEST_RETRY_SECONDS = 6 * 60 * 60

COULD_NOT_READ = "Orbit could not read some of your mail. It will try again."

# Emails being read right now, so a second request does not start a second read.
_in_progress: set[str] = set()
# For each email that could not be read: when (monotonic) to try again, why, and how
# many times it has failed.
_failed: dict[str, tuple[float, str, int]] = {}
# asyncio keeps only weak references to tasks; holding them here stops them vanishing.
_tasks: set[asyncio.Task[None]] = set()
_names: tuple[str, str] | None = None


async def _user_names(session: aiohttp.ClientSession) -> tuple[str, str]:
    """The user's full and given name, for drafting replies in their voice."""
    global _names
    if _names is None:
        profile = await api.get_json(session, _USERINFO_URL)
        name = profile.get("name") or profile.get("email", "the user")
        _names = (name, profile.get("given_name") or name)
    return _names


def _put_off(message_ids: list[str], reason: str) -> None:
    now = time.monotonic()
    for message_id in message_ids:
        failures = _failed.get(message_id, (0, "", 0))[2]
        wait = min(RETRY_SECONDS * 4**failures, LONGEST_RETRY_SECONDS)
        _failed[message_id] = (now + wait, reason, failures + 1)


async def _fetch(session: aiohttp.ClientSession, message_id: str) -> tuple[dict, bool]:
    """The email, and whether the user has already replied in its thread."""
    email, answered = await asyncio.gather(
        gmail.read(session, message_id), gmail.answered(session, message_id)
    )
    return email, answered


async def _read_batch(message_ids: list[str]) -> None:
    reason = COULD_NOT_READ
    try:
        async with aiohttp.ClientSession() as session:
            name, given_name = await _user_names(session)
            fetched = await asyncio.gather(
                *(_fetch(session, message_id) for message_id in message_ids),
                return_exceptions=True,
            )
        # One email that cannot be fetched must not hold up the rest.
        readable = [
            (message_id, *result)
            for message_id, result in zip(message_ids, fetched, strict=True)
            if not isinstance(result, BaseException)
        ]
        results = (
            await triage(
                [email for _, email, _ in readable], name=name, given_name=given_name
            )
            if readable
            else []
        )
        for (message_id, _, answered), result in zip(readable, results, strict=True):
            if result is None:
                continue
            if answered:
                # The user already replied in this thread: keep the gist, drop the draft.
                result = Triage(gist=result.gist, needs_reply=False)
            store.put(message_id, result)
            _failed.pop(message_id, None)
    except ModelError as e:
        logger.warning("%d emails not read: %s", len(message_ids), e.reason)
        reason = f"Orbit could not read some of your mail: {e.reason}"
    except Exception:
        logger.exception("could not read %d emails", len(message_ids))
    finally:
        # Whatever was not recorded is left for a later attempt.
        recorded = store.get_many(message_ids)
        _put_off([m for m in message_ids if m not in recorded], reason)
        _in_progress.difference_update(message_ids)


async def _read_all(message_ids: list[str]) -> None:
    requests = [
        message_ids[start : start + EMAILS_PER_REQUEST]
        for start in range(0, len(message_ids), EMAILS_PER_REQUEST)
    ]
    for start in range(0, len(requests), REQUESTS_AT_ONCE):
        together = requests[start : start + REQUESTS_AT_ONCE]
        await asyncio.gather(*(_read_batch(batch) for batch in together))


def start_reading(message_ids: list[str]) -> None:
    """Begins reading, in the background, any of these emails Orbit has not read yet."""
    known = store.get_many(message_ids)
    now = time.monotonic()
    unread = [
        message_id
        for message_id in message_ids
        if message_id not in known
        and message_id not in _in_progress
        and _failed.get(message_id, (0, "", 0))[0] <= now
    ]
    if not unread:
        return
    _in_progress.update(unread)
    task = asyncio.create_task(_read_all(unread))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def still_reading(message_ids: list[str]) -> bool:
    return any(message_id in _in_progress for message_id in message_ids)


def trouble(message_ids: list[str]) -> str | None:
    """Why some of these emails have not been read, if Orbit tried and failed."""
    known = store.get_many(message_ids)
    reasons = [
        _failed[message_id][1]
        for message_id in message_ids
        if message_id in _failed and message_id not in known
    ]
    # The specific reason, when there is one, is more use than the general one.
    return next(
        (r for r in reasons if r != COULD_NOT_READ), reasons[0] if reasons else None
    )
