import asyncio
from types import SimpleNamespace

from livekit.agents import llm
from livekit.agents.voice.speech_handle import SpeechHandle

import agent
from agent import FAILED_REPLIES, Answering, create_session


def _answering() -> tuple[Answering, list[str]]:
    ended: list[str] = []
    return Answering(lambda: ended.append("ended")), ended


def _timed_out() -> llm.RealtimeError:
    return llm.RealtimeError(
        "generate_reply timed out waiting for generation_created event."
    )


def test_a_model_that_stops_answering_ends_the_conversation_once():
    answering, ended = _answering()

    for _ in range(FAILED_REPLIES + 2):
        answering.finished(_timed_out(), said_something=False)

    assert ended == ["ended"]


def test_a_reply_said_in_between_starts_the_count_again():
    answering, ended = _answering()

    for _ in range(3):
        for _ in range(FAILED_REPLIES - 1):
            answering.finished(_timed_out(), said_something=False)
        answering.finished(None, said_something=True)

    assert ended == []


def test_a_reply_that_ends_otherwise_is_no_failed_one():
    answering, ended = _answering()

    for _ in range(FAILED_REPLIES + 1):
        # Interrupted by the user, or a tool's turn with nothing said.
        answering.finished(None, said_something=False)

    assert ended == []


async def test_the_session_ends_a_conversation_whose_replies_keep_timing_out(
    monkeypatch,
):
    ended = []
    monkeypatch.setattr(agent, "_end_lost", ended.append)
    session = create_session()

    for _ in range(FAILED_REPLIES):
        # As LiveKit ends a reply that Gemini never began.
        handle = SpeechHandle.create()
        session.emit("speech_created", SimpleNamespace(speech_handle=handle))
        handle._mark_done(error=_timed_out())
        await asyncio.sleep(0)

    assert ended == [session]
