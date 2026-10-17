from datetime import date, datetime, timedelta
from typing import Any

import pytest
from livekit.agents.llm import ToolError

from agent_tools import google
from agent_tools.google import chat_conversations, chat_read
from integrations.google import api, gchat
from reminders.when import WhenError, earlier

ME = "users/100"
ASHA = {"name": "users/201", "displayName": "Asha Verma"}
RAHUL = {"name": "users/202", "displayName": "Rahul Shah"}
MEERA = {"name": "users/203", "displayName": "Meera Iyer"}
MYSELF = {"name": ME, "displayName": "Nidhish Kumar"}


def _sent(sender: dict, minutes_ago: int, text: str = "hello") -> dict:
    stamp = datetime(2026, 10, 6, 12, 0) - timedelta(minutes=minutes_ago)
    return {"sender": sender, "createTime": f"{stamp.isoformat()}Z", "text": text}


class StandInChat:
    """Google Chat as Orbit sees it: the user's conversations and what is in them.
    Every request made is kept, to see what was and was not asked."""

    def __init__(self) -> None:
        self.spaces = [
            {
                "name": "spaces/design",
                "displayName": "Design team",
                "spaceType": "SPACE",
                "lastActiveTime": "2026-10-06T10:00:00Z",
            },
            {
                "name": "spaces/dm-asha",
                "spaceType": "DIRECT_MESSAGE",
                "lastActiveTime": "2026-10-06T11:00:00Z",
            },
            {
                "name": "spaces/trio",
                "spaceType": "GROUP_CHAT",
                "lastActiveTime": "2026-10-05T09:00:00Z",
            },
            {
                "name": "spaces/dm-quiet",
                "spaceType": "DIRECT_MESSAGE",
                "lastActiveTime": "2026-09-01T09:00:00Z",
            },
        ]
        self.messages = {
            "spaces/design": [_sent(RAHUL, 5), _sent(MYSELF, 9)],
            "spaces/dm-asha": [_sent(MYSELF, 1), _sent(ASHA, 2), _sent(ASHA, 3)],
            "spaces/trio": [_sent(MEERA, 4), _sent(MYSELF, 6), _sent(ASHA, 7)],
            # Only the user has written here, so nothing says who it is with.
            "spaces/dm-quiet": [_sent(MYSELF, 50000)],
        }
        self.asked: list[tuple[str, dict]] = []
        self.pages: dict[str, dict[str, Any]] = {}

    async def get_json(self, session, url: str, params=None) -> dict[str, Any]:
        params = dict(params or {})
        self.asked.append((url, params))
        if url == gchat._USERINFO:
            return {"sub": "100"}
        if url == f"{gchat._API}/spaces":
            return self.pages.get(params.get("pageToken", ""), {"spaces": self.spaces})
        space = url.removeprefix(f"{gchat._API}/").removesuffix("/messages")
        return {"messages": self.messages[space][: params["pageSize"]]}

    def asked_about(self) -> list[str]:
        """The conversations whose messages were fetched."""
        return [
            url.removeprefix(f"{gchat._API}/").removesuffix("/messages")
            for url, _ in self.asked
            if url.endswith("/messages")
        ]


@pytest.fixture
def chat(tmp_path, monkeypatch) -> StandInChat:
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path))
    # Each test is its own conversation with Orbit: nothing is kept from another.
    monkeypatch.setattr(gchat, "_kept", None)
    stand_in = StandInChat()
    monkeypatch.setattr(api, "get_json", stand_in.get_json)
    return stand_in


def _a_later_conversation_with_orbit() -> None:
    """Orbit starts again, with only what it wrote down."""
    gchat._kept = None


async def test_each_conversation_has_a_name_to_call_it_by(chat):
    found = await gchat.conversations(None)

    assert [(c["id"], c["name"], c["kind"]) for c in found] == [
        # The most recently active first.
        ("spaces/dm-asha", "Asha Verma", "direct"),
        ("spaces/design", "Design team", "space"),
        # A group nobody named: the people who write in it, the latest first.
        ("spaces/trio", "Meera Iyer, Asha Verma", "group"),
        ("spaces/dm-quiet", "Direct message", "direct"),
    ]
    assert found[0]["last_active"].startswith("Tue 06 Oct")


async def test_only_conversations_without_a_name_are_asked_who_is_in_them(chat):
    await gchat.conversations(None)

    assert sorted(chat.asked_about()) == [
        "spaces/dm-asha",
        "spaces/dm-quiet",
        "spaces/trio",
    ]


async def test_who_is_in_a_conversation_is_remembered_for_the_next_time(chat):
    first = await gchat.conversations(None)
    chat.asked.clear()
    _a_later_conversation_with_orbit()

    again = await gchat.conversations(None)

    assert again == first
    assert chat.asked_about() == []
    # The list itself was asked for again: only the people in it were remembered.
    assert chat.asked


async def test_the_list_is_not_fetched_again_while_a_chat_is_being_read(chat):
    first = await gchat.conversations(None)
    chat.asked.clear()

    again = await gchat.conversations(None)

    assert again == first
    assert chat.asked == []


async def test_a_direct_message_is_not_asked_about_again_when_it_goes_on(chat):
    await gchat.conversations(None)
    chat.asked.clear()
    _a_later_conversation_with_orbit()
    for space in chat.spaces:
        space["lastActiveTime"] = "2026-10-07T08:00:00Z"

    found = await gchat.conversations(None)

    # The group may have new people in it. Who a direct message is with never
    # changes, unless it was never found out.
    assert sorted(chat.asked_about()) == ["spaces/dm-quiet", "spaces/trio"]
    assert {c["id"]: c["name"] for c in found}["spaces/dm-asha"] == "Asha Verma"


async def test_a_long_list_of_people_is_cut_short(chat):
    others = [{"name": f"users/3{n}", "displayName": f"Person {n}"} for n in range(6)]
    chat.messages["spaces/trio"] = [_sent(other, n) for n, other in enumerate(others)]

    found = await gchat.conversations(None)

    name = {c["id"]: c["name"] for c in found}["spaces/trio"]
    assert name == "Person 0, Person 1, Person 2, Person 3 and 2 more"


async def test_every_page_of_conversations_is_read(chat):
    later = {"name": "spaces/old", "displayName": "Archive", "spaceType": "SPACE"}
    chat.pages = {
        "": {"spaces": chat.spaces, "nextPageToken": "page-2"},
        "page-2": {"spaces": [later]},
    }

    found = await gchat.conversations(None)

    assert found[-1] == {
        "id": "spaces/old",
        "name": "Archive",
        "kind": "space",
        "last_active": "",
    }


CHATS = [
    {"id": "spaces/1", "name": "Asha Verma", "kind": "direct", "last_active": ""},
    {"id": "spaces/2", "name": "Natasha Rao", "kind": "direct", "last_active": ""},
    {"id": "spaces/3", "name": "Design team", "kind": "space", "last_active": ""},
    {
        "id": "spaces/4",
        "name": "Meera Iyer, Asha Verma",
        "kind": "group",
        "last_active": "",
    },
    {"id": "spaces/5", "name": "Design", "kind": "space", "last_active": ""},
]


@pytest.mark.parametrize(
    ("said", "kind", "ids"),
    [
        ("Design team", "", ["spaces/3"]),
        ("the design team space", "", ["spaces/3"]),
        # A name that is one conversation's whole name means that one.
        ("design", "", ["spaces/5"]),
        ("desi", "", ["spaces/3", "spaces/5"]),
        # Words count from their first letters: Natasha is not Asha.
        ("my chat with Asha", "", ["spaces/1", "spaces/4"]),
        ("Asha", "direct", ["spaces/1"]),
        ("Asha", "group", ["spaces/4"]),
        ("asha and meera", "", ["spaces/4"]),
        ("verma asha", "", ["spaces/1", "spaces/4"]),
        ("Rahul", "", []),
        ("the chat", "", []),
    ],
)
def test_a_name_finds_the_conversations_it_can_mean(said, kind, ids):
    assert [c["id"] for c in gchat.matching(CHATS, said, kind)] == ids


async def test_the_latest_messages_between_two_moments_come_in_the_order_written(
    chat,
):
    after = datetime(2026, 10, 1).astimezone()
    before = datetime(2026, 10, 3).astimezone()

    read = await gchat.messages(
        None, "spaces/dm-asha", after=after, before=before, limit=2
    )

    url, params = chat.asked[-1]
    assert url == f"{gchat._API}/spaces/dm-asha/messages"
    assert params["pageSize"] == 2
    assert params["orderBy"] == "createTime desc"
    assert params["filter"] == (
        f'createTime > "{after.isoformat()}" AND createTime < "{before.isoformat()}"'
    )
    # The two latest, asked for newest first so the limit keeps those, and given
    # back as a conversation reads.
    assert [m["sender"] for m in read["messages"]] == ["Asha Verma", "Nidhish Kumar"]
    assert set(read["messages"][0]) == {"sender", "sent", "text"}
    # Nothing says there are more, so there is nowhere further back to go.
    assert read["older_than"] is None


async def test_with_no_moments_given_no_filter_is_sent(chat):
    await gchat.messages(None, "spaces/dm-asha")

    assert "filter" not in chat.asked[-1][1]


async def test_older_messages_are_reached_from_the_oldest_one_read(chat, monkeypatch):
    oldest = _sent(ASHA, 30)

    async def more_to_come(session, url, params=None):
        return {"messages": [_sent(ASHA, 10), oldest], "nextPageToken": "next"}

    monkeypatch.setattr(api, "get_json", more_to_come)

    read = await gchat.messages(None, "spaces/dm-asha", limit=2)

    assert read["older_than"] == oldest["createTime"]


async def test_a_message_is_kept_as_it_was_written(chat):
    chat.messages["spaces/dm-asha"] = [
        _sent(ASHA, 1, "word " * 1000),
        {**_sent(ASHA, 2, ""), "attachment": [{"contentName": "plan.pdf"}]},
        _sent({"name": "users/999"}, 3, "  spread\nover lines  "),
    ]

    unnamed, file, long = (await gchat.messages(None, "spaces/dm-asha"))["messages"]

    assert unnamed == {
        "sender": "unknown sender",
        "sent": unnamed["sent"],
        "text": "spread\nover lines",
    }
    assert file["text"] == "[attached: plan.pdf]"
    # Only something far longer than a message is cut.
    assert len(long["text"]) == gchat.MAX_TEXT_CHARS + 1


TODAY = date(2026, 10, 8)  # a Thursday


@pytest.mark.parametrize(
    ("said", "meant"),
    [
        ("today", date(2026, 10, 8)),
        ("Yesterday", date(2026, 10, 7)),
        ("friday", date(2026, 10, 2)),
        ("last Friday", date(2026, 10, 2)),
        ("wednesday", date(2026, 10, 7)),
        # Said on a Thursday, "Thursday" is the one before.
        ("thursday", date(2026, 10, 1)),
        ("2026-09-15", date(2026, 9, 15)),
    ],
)
def test_a_day_that_has_been_is_worked_out_from_how_it_was_said(said, meant):
    assert earlier(said, TODAY) == meant


@pytest.mark.parametrize("said", ["2026-10-09", "the other week", ""])
def test_a_day_that_cannot_be_meant_is_refused(said):
    with pytest.raises(WhenError):
        earlier(said, TODAY)


@pytest.fixture
def shown(monkeypatch) -> list[dict]:
    """What the tools asked the app to put on screen."""
    asked: list[dict] = []

    async def show(card, *, push=None):
        asked.append(card)

    monkeypatch.setattr(google.stage, "show", show)
    monkeypatch.setattr(google.utils.http_context, "http_session", lambda: None)
    return asked


@pytest.fixture
def chats(monkeypatch) -> list[dict]:
    """The user's conversations as the tools get them, and every read that is made."""
    reads: list[dict] = []

    async def conversations(session):
        return CHATS

    async def messages(session, conversation, *, after=None, before=None, limit=20):
        reads.append(
            {"chat": conversation, "after": after, "before": before, "limit": limit}
        )
        return {
            "messages": [
                {"sender": "Asha Verma", "sent": "Tue 06 Oct 11:58", "text": "hello"}
            ],
            "older_than": "2026-10-06T06:28:00.123Z",
        }

    monkeypatch.setattr(gchat, "conversations", conversations)
    monkeypatch.setattr(gchat, "messages", messages)
    return reads


async def test_a_space_named_by_the_user_is_found_and_read(shown, chats):
    read = await chat_read(None, chat="Design team")

    assert read["chat"] == {"id": "spaces/3", "name": "Design team", "kind": "space"}
    assert read["count"] == 1
    assert read["older_than"] == "2026-10-06T06:28:00.123Z"
    assert chats == [{"chat": "spaces/3", "after": None, "before": None, "limit": 20}]
    assert shown[-1]["title"] == "Design team"
    assert shown[-1]["tag"] == "1+ messages"


async def test_a_persons_name_means_the_direct_message_with_them(shown, chats):
    read = await chat_read(None, chat="Asha")

    assert read["chat"]["id"] == "spaces/1"


async def test_asked_for_a_group_the_direct_message_is_left_alone(shown, chats):
    read = await chat_read(None, chat="Asha", kind="group")

    assert read["chat"]["id"] == "spaces/4"


async def test_a_name_that_fits_several_is_offered_and_none_is_read(shown, chats):
    read = await chat_read(None, chat="desi")

    assert read["chat"] is None
    assert [c["id"] for c in read["chats_matching"]] == ["spaces/3", "spaces/5"]
    assert chats == []
    assert [row["title"] for row in shown[-1]["rows"]] == ["Design team", "Design"]


async def test_a_name_no_conversation_has_reads_and_shows_nothing(shown, chats):
    read = await chat_read(None, chat="Rahul")

    assert read == {"chat": None, "chats_matching": []}
    assert chats == []
    assert shown == []


async def test_a_conversation_is_read_by_the_id_a_result_gave(shown, chats):
    read = await chat_read(None, chat_id="spaces/4")

    assert read["chat"]["name"] == "Meera Iyer, Asha Verma"


async def test_an_id_no_conversation_has_is_refused(shown, chats):
    with pytest.raises(ToolError, match="No conversation has that id"):
        await chat_read(None, chat_id="spaces/gone")

    with pytest.raises(ToolError, match="which conversation"):
        await chat_read(None)


async def test_days_the_user_names_are_read_from_start_to_end(shown, chats):
    await chat_read(None, chat="Design team", since="2026-10-01", until="2026-10-02")

    assert chats[0]["after"] == datetime(2026, 10, 1).astimezone()
    # The whole of the last day is included.
    assert chats[0]["before"] == datetime(2026, 10, 3).astimezone()


async def test_going_further_back_reads_what_came_before(shown, chats):
    first = await chat_read(None, chat="Design team", until="2026-10-06")

    await chat_read(
        None, chat_id="spaces/3", until="2026-10-06", older_than=first["older_than"]
    )

    assert chats[1]["before"] == datetime.fromisoformat("2026-10-06T06:28:00.123+00:00")
    assert chats[1]["chat"] == "spaces/3"


@pytest.mark.parametrize(
    ("days", "why"),
    [
        ({"since": "2026-10-05", "until": "2026-10-01"}, "until is before since"),
        ({"since": "2099-01-01"}, "has not come yet"),
        ({"since": "a while back"}, "day must be"),
        ({"older_than": "earlier"}, "older_than must be"),
    ],
)
async def test_days_that_make_no_sense_are_refused_before_anything_is_read(
    shown, chats, days, why
):
    with pytest.raises(ToolError, match=why):
        await chat_read(None, chat="Design team", **days)

    assert chats == []


async def test_the_number_of_messages_is_kept_within_bounds(shown, chats):
    await chat_read(None, chat="Design team", max_results=500)
    await chat_read(None, chat="Design team", max_results=5)

    assert [read["limit"] for read in chats] == [google.MAX_CHAT_MESSAGES, 5]


@pytest.mark.parametrize(
    ("days", "limit"),
    [
        # No day named: the latest few.
        ({}, 20),
        # A day named: the day, not a sample of it.
        ({"since": "today", "until": "today"}, 200),
        ({"since": "2026-10-01"}, 200),
        ({"until": "2026-10-01"}, 200),
    ],
)
async def test_days_the_user_names_are_read_whole(shown, chats, days, limit):
    await chat_read(None, chat="Design team", **days)

    assert chats[0]["limit"] == limit


async def test_every_message_is_on_the_card_in_full_and_the_model_gets_the_gist(
    shown, chats, monkeypatch
):
    long = "First line.\nSecond line. " + "and more " * 60
    day = [
        {
            "sender": "Asha Verma",
            "sent": f"Thu 08 Oct 10:{n:02}",
            "text": f"message {n}",
        }
        for n in range(64)
    ]
    day[0]["text"] = long

    async def a_busy_day(session, conversation, *, after=None, before=None, limit=20):
        return {"messages": day, "older_than": None}

    monkeypatch.setattr(gchat, "messages", a_busy_day)

    read = await chat_read(None, chat="Asha", since="today", until="today")

    card = shown[-1]
    assert card["whole"] is True
    assert card["more"] == 0
    assert [row["line"] for row in card["rows"][1:]] == [
        f"message {n}" for n in range(1, 64)
    ]
    # As it was written, line breaks and all.
    assert card["rows"][0]["line"] == long.strip()
    assert card["tag"] == "64 messages"
    assert read["count"] == 64
    assert read["older_than"] is None
    told = read["messages"][0]["text"]
    assert told.startswith("First line. Second line.")
    assert len(told) == google.CHAT_TEXT_FOR_MODEL + 1


async def test_conversations_are_listed_for_the_user_to_choose_from(shown, chats):
    listed = await chat_conversations(None, max_results=2)

    assert [c["name"] for c in listed["chats"]] == ["Asha Verma", "Natasha Rao"]
    assert listed["count"] == 2
    assert listed["more_not_listed"] is True
    assert shown[-1]["tag"] == "5 chats"
    assert shown[-1]["rows"][0] == {"title": "Asha Verma", "line": "Direct message"}


async def test_conversations_are_narrowed_by_kind_and_by_name(shown, chats):
    direct = await chat_conversations(None, kind="direct")
    with_asha = await chat_conversations(None, matching="asha")
    groups_with_asha = await chat_conversations(None, matching="asha", kind="group")

    assert [c["id"] for c in direct["chats"]] == ["spaces/1", "spaces/2"]
    assert [c["id"] for c in with_asha["chats"]] == ["spaces/1", "spaces/4"]
    assert [c["id"] for c in groups_with_asha["chats"]] == ["spaces/4"]
    assert direct["more_not_listed"] is False
