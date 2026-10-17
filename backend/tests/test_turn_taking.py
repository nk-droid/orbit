# Whether Orbit lets someone finish what they are saying. Typed input has no pauses
# in it, so the last test speaks to the real agent model with a recorded voice.

import asyncio
import os
import time
from pathlib import Path

import pytest
from voice_stub import RATE, Microphone, Speaker, recording

from agent import Assistant, create_session, end_of_speech

# "What is seven times ... eight?", with 1.3 seconds of silence where the dots are:
# longer than Gemini waits by itself, shorter than Orbit tells it to. Said by the
# Mac's own voice (say -v Samantha), with the silence at both ends cut off.
SEVEN_TIMES_EIGHT = Path(__file__).parent / "recordings" / "seven-times-eight.wav"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Nothing is written to the real data folder, and the wait is Orbit's own."""
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ORBIT_SILENCE_SECONDS", raising=False)


def _silence_ms() -> int | None:
    return end_of_speech().automatic_activity_detection.silence_duration_ms


def test_orbit_waits_out_a_second_and_a_half_of_silence() -> None:
    assert _silence_ms() == 1500


def test_the_wait_can_be_set_in_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("ORBIT_SILENCE_SECONDS", "2.2")

    assert _silence_ms() == 2200


def test_an_empty_setting_means_orbits_own_wait(monkeypatch) -> None:
    monkeypatch.setenv("ORBIT_SILENCE_SECONDS", "")

    assert _silence_ms() == 1500


async def _until(happened, by: float) -> None:
    while not happened() and time.time() < by:
        await asyncio.sleep(0.1)


@pytest.mark.skipif(
    not os.getenv("GOOGLE_API_KEY"), reason="needs GOOGLE_API_KEY in .env.local"
)
async def test_a_pause_in_the_middle_of_a_sentence_is_not_taken_for_its_end() -> None:
    question = recording(SEVEN_TIMES_EIGHT)
    answers_begun: list[float] = []
    said: list[str] = []

    async with create_session() as session:
        microphone, speaker = Microphone(), Speaker()
        session.input.audio = microphone
        session.output.audio = speaker
        session.on("speech_created", lambda event: answers_begun.append(time.time()))

        @session.on("conversation_item_added")
        def _on_item(event) -> None:
            if getattr(event.item, "role", None) == "assistant":
                said.append(event.item.text_content or "")

        try:
            await session.start(Assistant())
            finished = await microphone.play(question)
            await _until(lambda: said, by=finished + 20)
        finally:
            await microphone.aclose()

    began = finished - len(question) / RATE
    # Gemini starts on an answer the moment it takes the user to have finished,
    # whether or not any of it is heard before they go on.
    assert not [at - began for at in answers_begun if began <= at < finished]
    assert not [at - began for at in speaker.began if began <= at < finished]
    # Both halves were heard as the one question.
    assert any("56" in text or "fifty-six" in text.lower() for text in said), said
