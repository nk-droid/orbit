"""A microphone that plays a recording and a loudspeaker that plays nothing.

For the tests that have to speak to Orbit, since typed input has no pauses in it.
Set them on a session before it starts:

    session.input.audio = microphone
    session.output.audio = speaker

After the stand-ins in LiveKit's own tests (tests/fake_io.py).
"""

import array
import asyncio
import random
import time
import wave
from pathlib import Path

from livekit import rtc
from livekit.agents import utils
from livekit.agents.voice.io import AudioInput, AudioOutput, AudioOutputCapabilities

RATE = 16000
_FRAME = RATE // 50  # 20 ms
# A quiet room is never digital silence.
_HISS = 12


def recording(path: Path) -> array.array:
    """The samples of a 16 kHz mono recording."""
    with wave.open(str(path)) as file:
        assert (file.getframerate(), file.getnchannels()) == (RATE, 1), path
        samples = array.array("h")
        samples.frombytes(file.readframes(file.getnframes()))
    return samples


def _hiss(count: int) -> array.array:
    return array.array("h", (random.randint(-_HISS, _HISS) for _ in range(count)))


class Microphone(AudioInput):
    """Sends a quiet room, and what is played into it, at the speed it was spoken."""

    def __init__(self) -> None:
        super().__init__(label="recording")
        self._frames = utils.aio.Chan[rtc.AudioFrame]()
        self._left = array.array("h")
        self._began: asyncio.Future[float] | None = None
        self._sending = asyncio.create_task(self._send())

    async def __anext__(self) -> rtc.AudioFrame:
        return await self._frames.__anext__()

    async def play(self, samples: array.array) -> float:
        """Starts playing `samples`. Returns the moment they will have been said."""
        self._left = array.array("h", samples)
        self._began = asyncio.get_running_loop().create_future()
        return await self._began + len(samples) / RATE

    async def _send(self) -> None:
        due = time.perf_counter()
        while True:
            piece, self._left = self._left[:_FRAME], self._left[_FRAME:]
            if piece and self._began is not None and not self._began.done():
                self._began.set_result(time.time())
            piece = piece + _hiss(_FRAME - len(piece))
            self._frames.send_nowait(
                rtc.AudioFrame(
                    data=piece.tobytes(),
                    sample_rate=RATE,
                    num_channels=1,
                    samples_per_channel=_FRAME,
                )
            )
            due += _FRAME / RATE
            await asyncio.sleep(max(0.0, due - time.perf_counter()))

    async def aclose(self) -> None:
        await utils.aio.cancel_and_wait(self._sending)


class Speaker(AudioOutput):
    """Takes as long over what it is given as a loudspeaker would, and notes when
    each thing said began."""

    def __init__(self) -> None:
        super().__init__(
            label="nowhere", capabilities=AudioOutputCapabilities(pause=False)
        )
        self.began: list[float] = []
        self._given = 0.0
        self._started: float | None = None
        self._finishing: asyncio.TimerHandle | None = None

    def _reset(self) -> None:
        self._given = 0.0
        self._started = None
        if self._finishing:
            self._finishing.cancel()
            self._finishing = None

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        self._given += frame.duration
        if self._started is None:
            self._started = time.time()
            self.began.append(self._started)
            self.on_playback_started(created_at=self._started)

    def flush(self) -> None:
        super().flush()
        if self._started is None:
            return
        given = self._given

        def finished() -> None:
            self._reset()
            self.on_playback_finished(playback_position=given, interrupted=False)

        if self._finishing:
            self._finishing.cancel()
        left = max(given - (time.time() - self._started), 0.0)
        self._finishing = asyncio.get_event_loop().call_later(left, finished)

    def clear_buffer(self) -> None:
        if self._started is None:
            return
        played = min(time.time() - self._started, self._given)
        self._reset()
        self.on_playback_finished(playback_position=played, interrupted=True)
