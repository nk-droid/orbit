"""What was said in this session of Orbit, kept in Redis until the session closes.

A session is Orbit open in one browser tab, from opening the app until the tab is
closed. Within it the user may talk to Orbit many times and put it on standby in
between, and each time the model starts a new conversation. Everything the user and
Orbit say is kept here under the session's id, and each new conversation starts with
it, so Orbit remembers what was discussed until the tab is closed.

The app keeps its session alive while it is open (api/session.py). When the tab
closes, what was said is let go a minute later: a reload of the tab keeps the
session, and keeps it alive again before then. If the app goes without a word, as
when the browser quits, it is let go after ALIVE_SECONDS.

Redis is reached at REDIS_URL. Without it Orbit works as before, and remembers a
conversation only while it lasts.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Literal

import redis.asyncio as redis
from redis.exceptions import RedisError

logger = logging.getLogger("orbit.session")

DEFAULT_URL = "redis://localhost:6379/0"
# How long what was said is kept after the app was last heard from. The app says it
# is still open every few minutes (frontend/src/lib/appSession.ts).
ALIVE_SECONDS = 15 * 60
# How long it is kept once the app says its tab is closing: long enough for a reload
# to say it is open again.
CLOSING_SECONDS = 60
# What a new conversation is given of what was said before, at most: the latest of
# it. A busy half hour of talk is a small part of this.
MAX_CHARS = 60_000

Who = Literal["user", "assistant"]

# What a message carried over from earlier in the session has its id begin with. The
# checks that look for the user's own words in this conversation pass these over.
EARLIER_ID = "earlier_"

# A session's id as the app makes it: a UUID, or random hex where the browser has no
# crypto.randomUUID.
_ID = re.compile(r"^[0-9a-f-]{16,64}$")
_PREFIX = "orbit:session:"


def session_id(raw: object) -> str | None:
    """A session id the app sent, if it is one."""
    if isinstance(raw, str) and _ID.match(raw.strip().casefold()):
        return raw.strip().casefold()
    return None


def from_job(metadata: str | None) -> str | None:
    """The session a conversation belongs to, from what the app put in the dispatch."""
    try:
        sent = json.loads(metadata or "{}")
    except ValueError:
        return None
    return session_id(sent.get("session")) if isinstance(sent, dict) else None


def _key(session: str) -> str:
    return f"{_PREFIX}{session}:said"


def client() -> redis.Redis:
    """A Redis client that gives up quickly, so a missing Redis never holds up a reply."""
    return redis.from_url(
        os.getenv("REDIS_URL") or DEFAULT_URL,
        socket_connect_timeout=1,
        socket_timeout=2,
        decode_responses=True,
    )


class SessionMemory:
    """What was said in one session of Orbit. Any trouble with Redis is logged and
    passed over: the conversation goes on without it."""

    def __init__(self, session: str, store: redis.Redis | None = None) -> None:
        self.session = session
        self._store = store if store is not None else client()
        # Each message is written in the order it was said.
        self._writing = asyncio.Lock()
        self._pending: set[asyncio.Task] = set()

    async def said(self) -> tuple[list[tuple[Who, str]], bool]:
        """What was said in the session so far, oldest first, and whether its
        beginning had to be left out to stay within MAX_CHARS."""
        try:
            kept = await self._store.lrange(_key(self.session), 0, -1)
        except (RedisError, OSError):
            logger.warning("Redis could not be read; this conversation starts afresh")
            return [], False
        lines: list[tuple[Who, str]] = []
        for raw in kept:
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if (
                line.get("who") in ("user", "assistant")
                and str(line.get("text")).strip()
            ):
                lines.append((line["who"], str(line["text"])))
        given, size = [], 0
        for who, text in reversed(lines):
            size += len(text)
            if size > MAX_CHARS:
                break
            given.append((who, text))
        given.reverse()
        return given, len(given) < len(lines)

    async def add(self, who: Who, text: str) -> None:
        """Keeps one thing said, and keeps the session alive."""
        if not text.strip():
            return
        line = json.dumps({"who": who, "text": text, "at": round(time.time())})
        async with self._writing:
            try:
                async with self._store.pipeline(transaction=True) as pipe:
                    pipe.rpush(_key(self.session), line)
                    pipe.expire(_key(self.session), ALIVE_SECONDS)
                    await pipe.execute()
            except (RedisError, OSError):
                logger.warning("Redis could not be written; this is not remembered")

    def record(self, who: Who, text: str) -> None:
        """Keeps one thing said, from a session's event, which cannot wait for it."""
        task = asyncio.create_task(self.add(who, text))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def close(self) -> None:
        """Finishes what is being written and lets the connection go."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        await self._store.aclose()


async def keep(store: redis.Redis, session: str, seconds: int) -> None:
    """Keeps what was said in a session for this many seconds more, if anything was."""
    await store.expire(_key(session), seconds)
