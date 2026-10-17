"""Reading Google Chat. Returns compact facts for the agent."""

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any

import aiohttp

from storage import connect

from . import api

_API = "https://chat.googleapis.com/v1"
_USERINFO = "https://www.googleapis.com/oauth2/v3/userinfo"

# Chat has no "latest messages across everything" call, so each space is asked in turn.
SPACES_CHECKED = 15
MESSAGES_PER_SPACE = 3

# Google lists a thousand conversations at most in one answer.
_PAGE = 1000
MAX_CONVERSATIONS = 3 * _PAGE
# A direct message has no name of its own, nor has a group nobody named. Each is
# called by the people who write in it, found among its latest messages.
PEOPLE_LOOKED_FOR_IN = 50
PEOPLE_NAMED = 4
_AT_ONCE = 40
# Only what Orbit uses is asked for, which keeps a long history quick to fetch.
_SENDERS = "messages(sender(name,displayName))"
_MESSAGES = (
    "messages(createTime,text,sender(displayName),attachment(contentName)),"
    "nextPageToken"
)
# Longer than any message someone means to be read.
MAX_TEXT_CHARS = 4000
# What the user says around a name, which is not part of it.
_NOT_THE_NAME = frozenset(
    [
        "a",
        "an",
        "the",
        "my",
        "our",
        "chat",
        "chats",
        "space",
        "spaces",
        "group",
        "groups",
        "dm",
        "dms",
        "direct",
        "message",
        "messages",
        "conversation",
        "thread",
        "with",
        "and",
        "in",
        "from",
        "on",
    ]
)

# Reading a chat and then going further back in it asks for the list of
# conversations each time. Within a minute the list is not fetched again.
KEPT_SECONDS = 60
_kept: tuple[float, list[dict[str, Any]]] | None = None

KINDS = {"SPACE": "space", "GROUP_CHAT": "group", "DIRECT_MESSAGE": "direct"}
_UNNAMED = {"space": "Chat", "group": "Group chat", "direct": "Direct message"}

# Who writes in each unnamed conversation. Finding out takes a call per conversation,
# a hundred of them for a busy account, so the answers are kept between Orbit's
# conversations and asked for again only when there is something new to learn.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_people (
    space TEXT PRIMARY KEY,
    people TEXT NOT NULL,
    active TEXT NOT NULL
)
"""


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _said(moment: datetime) -> str:
    """A time the way it is put on a card, with the year once it is not this one."""
    moment = moment.astimezone()
    year = "" if moment.year == datetime.now().year else " %Y"
    return moment.strftime(f"%a %d %b{year} %H:%M")


def _space_name(space: dict[str, Any]) -> str:
    if space.get("displayName"):
        return space["displayName"]
    return "Direct message" if space.get("spaceType") == "DIRECT_MESSAGE" else "Chat"


def _message(space: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    sent = _when(message["createTime"])
    return {
        "space": _space_name(space),
        # With read-only scopes Google often omits the sender's name.
        "sender": message.get("sender", {}).get("displayName") or "unknown sender",
        "sent": sent.astimezone().strftime("%a %d %b %H:%M"),
        "text": message.get("text", ""),
        "_sent": sent,
    }


async def _space_messages(
    session: aiohttp.ClientSession, space: dict[str, Any]
) -> list[dict[str, Any]]:
    data = await api.get_json(
        session,
        f"{_API}/{space['name']}/messages",
        {"pageSize": MESSAGES_PER_SPACE, "orderBy": "createTime desc"},
    )
    return [_message(space, message) for message in data.get("messages", [])]


async def recent_messages(
    session: aiohttp.ClientSession, limit: int = 10
) -> list[dict[str, Any]]:
    """The newest messages across the user's spaces and direct messages."""
    listing = await api.get_json(
        session, f"{_API}/spaces", {"pageSize": SPACES_CHECKED}
    )
    per_space = await asyncio.gather(
        *(_space_messages(session, space) for space in listing.get("spaces", []))
    )
    messages = sorted(
        (message for batch in per_space for message in batch),
        key=lambda message: message["_sent"],
        reverse=True,
    )
    return [
        {key: value for key, value in message.items() if key != "_sent"}
        for message in messages[:limit]
    ]


async def _spaces(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    """Every conversation the user is in, as Google describes them."""
    spaces: list[dict[str, Any]] = []
    params: dict[str, Any] = {"pageSize": _PAGE}
    while len(spaces) < MAX_CONVERSATIONS:
        page = await api.get_json(session, f"{_API}/spaces", params)
        spaces += page.get("spaces", [])
        if not page.get("nextPageToken"):
            break
        params["pageToken"] = page["nextPageToken"]
    return spaces


async def _people(
    session: aiohttp.ClientSession, space: str, me: str, limit: asyncio.Semaphore
) -> list[str]:
    """Who else has written in a conversation lately, the latest to write first."""
    async with limit:
        data = await api.get_json(
            session,
            f"{_API}/{space}/messages",
            {
                "pageSize": PEOPLE_LOOKED_FOR_IN,
                "orderBy": "createTime desc",
                "fields": _SENDERS,
            },
        )
    senders = (message.get("sender", {}) for message in data.get("messages", []))
    named = [
        sender["displayName"]
        for sender in senders
        if sender.get("displayName") and sender.get("name") != me
    ]
    return list(dict.fromkeys(named))


def _remembered() -> dict[str, tuple[list[str], str]]:
    """The people found before in each conversation, and how recent its last
    message was at the time."""
    with connect(_SCHEMA) as connection:
        rows = connection.execute("SELECT * FROM chat_people").fetchall()
    return {row["space"]: (json.loads(row["people"]), row["active"]) for row in rows}


def _remember(found: dict[str, tuple[list[str], str]]) -> None:
    with connect(_SCHEMA) as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO chat_people (space, people, active) VALUES (?, ?, ?)",
            [
                (space, json.dumps(people), active)
                for space, (people, active) in found.items()
            ],
        )


def _still_true(known: tuple[list[str], str] | None, space: dict[str, Any]) -> bool:
    """Whether what was found before still holds. The other person in a direct
    message never changes. Otherwise it holds until someone writes again."""
    if known is None:
        return False
    people, active = known
    if people and space.get("spaceType") == "DIRECT_MESSAGE":
        return True
    return active == space.get("lastActiveTime", "")


def _called(kind: str, people: list[str]) -> str:
    """What to call a conversation that has no name of its own."""
    if not people:
        return _UNNAMED[kind]
    if kind == "direct":
        return people[0]
    more = len(people) - PEOPLE_NAMED
    return ", ".join(people[:PEOPLE_NAMED]) + (f" and {more} more" if more > 0 else "")


async def conversations(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    """All the user's conversations, the most recently active first.

    Each has a name to call it by. A space has its own. A direct message is called
    by the other person, and a group nobody named by the people in it, as far as
    they have written lately: Google gives neither a name.
    """
    global _kept
    if _kept is not None and time.monotonic() - _kept[0] < KEPT_SECONDS:
        return _kept[1]
    spaces, about_me = await asyncio.gather(
        _spaces(session), api.get_json(session, _USERINFO)
    )
    # The same number Chat uses for the user as a sender.
    me = f"users/{about_me.get('sub')}"
    limit = asyncio.Semaphore(_AT_ONCE)
    known = _remembered()
    unknown = [
        space
        for space in spaces
        if not space.get("displayName")
        and not _still_true(known.get(space["name"]), space)
    ]
    asked = await asyncio.gather(
        *(_people(session, space["name"], me, limit) for space in unknown)
    )
    learned = {
        space["name"]: (people, space.get("lastActiveTime", ""))
        for space, people in zip(unknown, asked, strict=True)
    }
    if learned:
        _remember(learned)
    people = {space: found for space, (found, _) in {**known, **learned}.items()}
    found = []
    for space in spaces:
        kind = KINDS.get(space.get("spaceType", ""), "space")
        active = space.get("lastActiveTime")
        found.append(
            {
                "id": space["name"],
                "name": space.get("displayName")
                or _called(kind, people[space["name"]]),
                "kind": kind,
                "last_active": _said(_when(active)) if active else "",
                "_active": active or "",
            }
        )
    found.sort(key=lambda conversation: conversation["_active"], reverse=True)
    listed = [
        {key: value for key, value in conversation.items() if key != "_active"}
        for conversation in found
    ]
    _kept = (time.monotonic(), listed)
    return listed


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def matching(
    found: list[dict[str, Any]], said: str, kind: str = ""
) -> list[dict[str, Any]]:
    """The conversations a name can mean, the most recently active first: the one
    called exactly that, or else every one whose name has all its words, a word
    counting from its first letters. `kind` narrows it to spaces, groups or direct
    messages."""
    if kind:
        found = [conversation for conversation in found if conversation["kind"] == kind]
    asked = [word for word in _words(said) if word not in _NOT_THE_NAME]
    if not asked:
        return []
    named = [(conversation, _words(conversation["name"])) for conversation in found]
    exact = [conversation for conversation, words in named if words == asked]
    if len(exact) == 1:
        return exact
    return [
        conversation
        for conversation, words in named
        if all(any(word.startswith(a) for word in words) for a in asked)
    ]


async def messages(
    session: aiohttp.ClientSession,
    conversation: str,
    *,
    after: datetime | None = None,
    before: datetime | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Messages of one conversation in the order they were written, optionally
    between two moments. When there are more than `limit`, they are the latest.

    `older_than` is where to go on from for the messages before these, or None
    when these are the earliest there are in that stretch.
    """
    between = []
    if after is not None:
        between.append(f'createTime > "{after.isoformat()}"')
    if before is not None:
        between.append(f'createTime < "{before.isoformat()}"')
    params: dict[str, Any] = {
        "pageSize": limit,
        "orderBy": "createTime desc",
        "fields": _MESSAGES,
    }
    if between:
        params["filter"] = " AND ".join(between)
    data = await api.get_json(session, f"{_API}/{conversation}/messages", params)
    # Asked for newest first, so that a limit keeps the latest ones.
    listed = data.get("messages", [])[::-1]
    return {
        "messages": [_line(message) for message in listed],
        "older_than": listed[0]["createTime"]
        if listed and data.get("nextPageToken")
        else None,
    }


def _line(message: dict[str, Any]) -> dict[str, str]:
    text = message.get("text", "").strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "…"
    files = [a.get("contentName") or "a file" for a in message.get("attachment", [])]
    if files:
        text = f"{text} [attached: {', '.join(files)}]".strip()
    return {
        "sender": message.get("sender", {}).get("displayName") or "unknown sender",
        "sent": _said(_when(message["createTime"])),
        "text": text or "[no text]",
    }
