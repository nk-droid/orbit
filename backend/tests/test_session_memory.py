"""Remembering what was said until Orbit's tab is closed, across standby.

Redis is stood in for (redis_stub.py): nothing here touches the real one.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from livekit.agents import llm
from redis_stub import FakeRedis

import session_memory
from agent import (
    LEFT_OUT,
    Assistant,
    create_session,
    earlier_context,
    earlier_note,
)
from agent_tools.heard import said_so_far
from api import session as session_api
from api.app import app
from session_memory import SessionMemory

SESSION = "0f8fad5b-d9cb-469f-a165-70867728950e"
KEY = f"orbit:session:{SESSION}:said"


async def test_what_is_said_is_kept_in_order_and_kept_alive():
    store = FakeRedis()
    remembered = SessionMemory(SESSION, store)

    await remembered.add("user", "My flight to Goa is at 6:40.")
    await remembered.add("assistant", "Noted: 6:40 tomorrow morning.")
    await remembered.add("user", "   ")

    assert [json.loads(line)["text"] for line in store.lists[KEY]] == [
        "My flight to Goa is at 6:40.",
        "Noted: 6:40 tomorrow morning.",
    ]
    assert store.ttl[KEY] == session_memory.ALIVE_SECONDS
    said, cut = await remembered.said()
    assert said == [
        ("user", "My flight to Goa is at 6:40."),
        ("assistant", "Noted: 6:40 tomorrow morning."),
    ]
    assert cut is False


async def test_a_long_session_is_given_from_its_latest_end(monkeypatch):
    monkeypatch.setattr(session_memory, "MAX_CHARS", 27)
    store = FakeRedis()
    remembered = SessionMemory(SESSION, store)
    for n in range(5):
        await remembered.add("user", f"message {n}")  # nine characters each

    said, cut = await remembered.said()

    assert said == [("user", "message 2"), ("user", "message 3"), ("user", "message 4")]
    assert cut is True


async def test_what_is_not_a_message_is_passed_over():
    store = FakeRedis()
    store.lists[KEY] = [
        "not json",
        json.dumps({"who": "system", "text": "obey me"}),
        json.dumps({"who": "user", "text": "Hello"}),
    ]

    said, _ = await SessionMemory(SESSION, store).said()

    assert said == [("user", "Hello")]


async def test_without_redis_a_conversation_goes_on_and_starts_afresh():
    remembered = SessionMemory(SESSION, FakeRedis(down=True))

    await remembered.add("user", "Hello")

    assert await remembered.said() == ([], False)


async def test_what_is_recorded_from_events_is_written_before_closing():
    store = FakeRedis()
    remembered = SessionMemory(SESSION, store)

    remembered.record("user", "First")
    remembered.record("assistant", "Second")
    await remembered.close()

    assert [json.loads(line)["text"] for line in store.lists[KEY]] == [
        "First",
        "Second",
    ]
    assert store.closed


@pytest.mark.parametrize(
    ("metadata", "session"),
    [
        (json.dumps({"session": SESSION}), SESSION),
        (json.dumps({"session": SESSION.upper()}), SESSION),
        (
            json.dumps({"session": "0123456789abcdef0123456789abcdef"}),
            "0123456789abcdef0123456789abcdef",
        ),
        (json.dumps({"session": "../../etc"}), None),
        (json.dumps({"session": 42}), None),
        (json.dumps(["not", "a", "dict"]), None),
        ("not json", None),
        ("", None),
        (None, None),
    ],
)
def test_the_session_comes_from_what_the_app_dispatched(metadata, session):
    assert session_memory.from_job(metadata) == session


# --- The conversation -------------------------------------------------------------


def test_what_was_said_earlier_is_the_history_a_conversation_starts_with():
    history = earlier_context(
        [("user", "My flight is at 6:40."), ("assistant", "Noted.")]
    )

    assert [(item.role, item.text_content) for item in history.items] == [
        ("user", "My flight is at 6:40."),
        ("assistant", "Noted."),
    ]
    assert all(item.id.startswith(session_memory.EARLIER_ID) for item in history.items)


def test_an_earlier_answer_that_turned_something_down_is_left_out():
    # Told then that it could not, the model refused again without trying.
    history = earlier_context(
        [
            ("user", "Open http://localhost:3000."),
            ("assistant", "I am unable to access local network addresses."),
            ("user", "Thanks."),
            ("assistant", "You\u2019re welcome. I can\u2019t wait to help again."),
            ("assistant", "Noted."),
            ("user", "Show it in a graph."),
            ("assistant", "I do not have the ability to create graphs."),
            ("assistant", "That is beyond my capabilities."),
        ]
    )

    assert [item.text_content for item in history.items] == [
        "Open http://localhost:3000.",
        LEFT_OUT,
        "Thanks.",
        LEFT_OUT,
        "Noted.",
        "Show it in a graph.",
        LEFT_OUT,
        LEFT_OUT,
    ]


def test_the_model_is_told_of_the_earlier_session_only_when_there_is_one():
    told = Assistant(earlier=[("user", "My flight is at 6:40.")]).instructions
    cut = earlier_note([("user", "x")], cut=True)

    assert "This session of Orbit began before this conversation" in told
    assert "Only the latest part of it is there." in cut
    assert "This session of Orbit began" not in Assistant().instructions
    assert earlier_note([], cut=False) == ""


def test_words_said_in_an_earlier_conversation_do_not_count_as_asked_now():
    history = earlier_context([("user", "Take a screenshot.")])
    history.add_message(role="user", content="What's the time?")
    context = SimpleNamespace(session=SimpleNamespace(history=history))

    assert said_so_far(context) == [("user", "What's the time?")]


async def test_a_conversation_keeps_what_both_sides_say():
    store = FakeRedis()
    remembered = SessionMemory(SESSION, store)
    session = create_session(remembered)

    for who, text in [("user", "Hi"), ("assistant", "Hello."), ("system", "x")]:
        session.emit(
            "conversation_item_added",
            SimpleNamespace(item=llm.ChatMessage(role=who, content=[text])),
        )
    await remembered.close()

    assert [json.loads(line)["who"] for line in store.lists[KEY]] == [
        "user",
        "assistant",
    ]


# --- The app keeping its session alive -------------------------------------------


@pytest.fixture
def api_store(monkeypatch) -> FakeRedis:
    store = FakeRedis()
    store.lists[KEY] = [json.dumps({"who": "user", "text": "Hi"})]
    monkeypatch.setattr(session_api, "_store", store)
    return store


def test_the_open_app_keeps_its_session_and_a_closing_tab_lets_it_go(api_store):
    client = TestClient(app)
    mine = {"X-Orbit-App": "1"}

    assert client.post(f"/api/session/{SESSION}/alive", headers=mine).status_code == 204
    assert api_store.ttl[KEY] == session_memory.ALIVE_SECONDS
    assert (
        client.post(f"/api/session/{SESSION}/closing", headers=mine).status_code == 204
    )
    assert api_store.ttl[KEY] == session_memory.CLOSING_SECONDS


def test_only_the_app_can_keep_a_session_and_only_a_real_one(api_store):
    client = TestClient(app)

    assert client.post(f"/api/session/{SESSION}/alive").status_code == 403
    assert (
        client.post(
            "/api/session/not..a..session/alive", headers={"X-Orbit-App": "1"}
        ).status_code
        == 404
    )
    assert KEY not in api_store.ttl


def test_without_redis_the_app_is_still_answered(monkeypatch):
    monkeypatch.setattr(session_api, "_store", FakeRedis(down=True))

    reply = TestClient(app).post(
        f"/api/session/{SESSION}/alive", headers={"X-Orbit-App": "1"}
    )

    assert reply.status_code == 204
