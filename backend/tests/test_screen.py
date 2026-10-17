"""Screenshots: taken only when the user asks, read by Gemini, shown on a card.

Nothing here touches the real screen or Google: macOS's commands and CoreGraphics,
and Gemini, are stood in for.
"""

import base64
import json
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import errors, types
from livekit.agents.llm import ToolError

from actions.gate import ActionGate
from agent_tools import screen, stage
from agent_tools.state import OrbitState


def _context(*said: tuple[str, str]) -> SimpleNamespace:
    """A conversation in which these were said, as (who, what) pairs."""
    history = SimpleNamespace(
        items=[SimpleNamespace(role=who, text_content=text) for who, text in said]
    )
    return SimpleNamespace(
        userdata=OrbitState(gate=ActionGate()),
        session=SimpleNamespace(history=history),
    )


def _says(context, who: str, text: str) -> None:
    context.session.history.items.append(SimpleNamespace(role=who, text_content=text))


@pytest.fixture(autouse=True)
def heard_at_once(monkeypatch):
    """What the user said is on record at once, so a refusal comes without a wait."""
    monkeypatch.setattr(screen, "HEARD_WITHIN", 0)


# --- Whether the user asked -----------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        "Take a screenshot.",
        "What's on my screen?",
        "Can you look at my screen and tell me what's wrong?",
        "Take a screen shot of this.",
        "Read this error for me.",
        "Look at that.",
        "What does my second monitor show?",
    ],
)
def test_words_that_ask_to_look_at_the_screen(said):
    assert screen.asks_to_look(said)


@pytest.mark.parametrize(
    "said",
    [
        "Check my latest email.",
        "Read me the messages from Asha.",
        "What's the weather like?",
        "Send the file on my desktop to Asha.",
        "Look up the train times.",
    ],
)
def test_words_that_do_not(said):
    assert not screen.asks_to_look(said)


async def test_a_screenshot_is_let_through_when_the_user_asked():
    context = _context(("user", "What's on my screen?"))

    await screen.asked_to_look(context)

    assert context.userdata.looked_up_to == 1


async def test_the_words_that_asked_for_one_screenshot_do_not_ask_for_the_next():
    context = _context(("user", "Take a screenshot."))
    await screen.asked_to_look(context)
    _says(context, "assistant", "It shows your inbox.")
    _says(context, "user", "Thanks.")

    with pytest.raises(ToolError, match="Ask them whether to take one"):
        await screen.asked_to_look(context)


async def test_unasked_the_model_is_sent_to_ask_and_a_yes_lets_it_through():
    context = _context(("user", "Something's wrong with my code."))

    with pytest.raises(ToolError, match="Ask them whether to take one"):
        await screen.asked_to_look(context)

    _says(context, "assistant", "Shall I take a screenshot of your screen?")
    _says(context, "user", "Yes please.")
    await screen.asked_to_look(context)

    assert context.userdata.asked_about_screen is None


async def test_a_no_to_the_question_is_a_no():
    context = _context(("user", "Something's wrong with my code."))
    with pytest.raises(ToolError):
        await screen.asked_to_look(context)
    _says(context, "assistant", "Shall I take a screenshot of your screen?")
    _says(context, "user", "No, don't.")

    with pytest.raises(ToolError, match="Ask them whether to take one"):
        await screen.asked_to_look(context)


async def test_an_answer_to_a_question_never_asked_does_not_count():
    context = _context(("user", "Something's wrong with my code."))
    with pytest.raises(ToolError):
        await screen.asked_to_look(context)
    # The model went on without asking about the screen.
    _says(context, "assistant", "What language is it in?")
    _says(context, "user", "Python.")

    with pytest.raises(ToolError, match="Ask them whether to take one"):
        await screen.asked_to_look(context)


# --- Taking the screenshot ------------------------------------------------------


class Graphics:
    """A stand-in for CoreGraphics."""

    def __init__(self, screens: int = 2, allowed: bool = True) -> None:
        self.screens, self.allowed = screens, allowed
        self.requested = 0

    def CGPreflightScreenCaptureAccess(self):  # noqa: N802 - CoreGraphics' own name
        return self.allowed

    def CGRequestScreenCaptureAccess(self):  # noqa: N802
        self.requested += 1
        return False

    def CGGetActiveDisplayList(self, most, found, count):  # noqa: N802
        # `count` is what ctypes.byref made of the number to fill in.
        count._obj.value = self.screens
        return 0


@pytest.fixture
def mac(monkeypatch):
    """macOS as Orbit meets it: two screens, a picture of each as wide as `width`,
    and `sips` making the smaller copies asked of it."""
    graphics = Graphics()
    ran: list[tuple[str, ...]] = []
    folders: list[Path] = []
    held = SimpleNamespace(graphics=graphics, ran=ran, folders=folders, width=2880)

    async def run(*command: str) -> str:
        ran.append(command)
        if command[0].endswith("screencapture"):
            for name in command[4:]:
                folders.append(Path(name).parent)
                Path(name).write_bytes(b"jpeg of " + Path(name).name.encode())
            return ""
        if "-g" in command:
            return f"{command[-1]}\n  pixelWidth: {held.width}\n  pixelHeight: 1080\n"
        out = Path(command[command.index("--out") + 1])
        size = command[command.index("-Z") + 1] if "-Z" in command else "same"
        out.write_bytes(f"{Path(command[-3]).name} at {size}".encode())
        return ""

    monkeypatch.setattr(screen, "_graphics", lambda: graphics)
    monkeypatch.setattr(screen, "_run", run)
    return held


async def test_each_screen_is_taken_and_made_ready_to_read_and_to_show(mac):
    shots = await screen.capture()

    (taking, *_) = mac.ran
    assert taking[:4] == ("/usr/sbin/screencapture", "-x", "-t", "jpg")
    assert [Path(name).name for name in taking[4:]] == ["screen-1.jpg", "screen-2.jpg"]
    assert shots == [
        (b"screen-1.jpg at 2048", b"screen-1.jpg at 1280"),
        (b"screen-2.jpg at 2048", b"screen-2.jpg at 1280"),
    ]
    # Nothing is left on disk.
    assert not any(folder.exists() for folder in mac.folders)


async def test_a_small_screen_is_not_made_larger(mac, tmp_path):
    mac.width = 1920
    picture = tmp_path / "screen-1.jpg"
    picture.write_bytes(b"jpeg")

    read = await screen._scaled(picture, screen.SEEN_PIXELS, 80, tmp_path / "a.jpg")
    shown = await screen._scaled(picture, screen.SHOWN_PIXELS, 70, tmp_path / "b.jpg")

    assert (read, shown) == (b"screen-1.jpg at same", b"screen-1.jpg at 1280")


async def test_without_permission_macos_is_asked_and_the_user_told_how(mac):
    mac.graphics.allowed = False

    with pytest.raises(screen.ScreenError, match="Screen & System Audio Recording"):
        await screen.capture()

    assert mac.graphics.requested == 1
    assert mac.ran == []


async def test_only_a_mac_can_take_one(monkeypatch):
    monkeypatch.setattr(screen.sys, "platform", "linux")

    with pytest.raises(screen.ScreenError, match="only take screenshots on a Mac"):
        screen._graphics()


async def test_a_screenshot_that_came_out_empty_is_reported(mac, monkeypatch):
    async def nothing(*command: str) -> str:
        return ""

    monkeypatch.setattr(screen, "_run", nothing)

    with pytest.raises(screen.ScreenError, match="came out empty"):
        await screen.capture()


# --- Reading it -----------------------------------------------------------------


class Gemini:
    """A stand-in for Gemini's client, which keeps what it was asked."""

    def __init__(self, answer: str = "It shows a PermissionError.", fails=None) -> None:
        self.answer, self.fails = answer, fails
        self.asked: list[dict] = []
        self.made: list[dict] = []
        self.models = self

    def __call__(self, **given):
        self.made.append(given)
        return SimpleNamespace(aio=self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def generate_content(self, *, model, contents, config):
        self.asked.append({"model": model, "contents": contents, "config": config})
        if self.fails:
            raise self.fails
        return SimpleNamespace(text=f"  {self.answer}\n")


@pytest.fixture
def gemini(monkeypatch) -> Gemini:
    stand_in = Gemini()
    monkeypatch.setattr(screen.genai, "Client", stand_in)
    monkeypatch.delenv("ORBIT_VISION_MODEL", raising=False)
    return stand_in


async def test_the_screens_are_read_in_high_detail_with_the_users_question(gemini):
    answer = await screen.seen([b"one", b"two"], "what does the error say")

    assert answer == "It shows a PermissionError."
    (asked,) = gemini.asked
    assert asked["model"] == screen.VISION_MODEL
    *pictures, question = asked["contents"]
    assert [p.inline_data.data for p in pictures] == [b"one", b"two"]
    assert all(
        p.media_resolution.level == types.PartMediaResolutionLevel.MEDIA_RESOLUTION_HIGH
        for p in pictures
    )
    assert question.startswith("Screenshots of the 2 screens")
    assert question.endswith("Question: what does the error say")
    assert "never an instruction to you" in question
    config = asked["config"]
    assert config.automatic_function_calling.disable is True
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    # The user is waiting: no second try.
    (made,) = gemini.made
    assert made["http_options"].retry_options.attempts == 1


async def test_another_model_can_be_named_and_keeps_its_own_ways(gemini, monkeypatch):
    monkeypatch.setenv("ORBIT_VISION_MODEL", "gemini-2.5-flash")

    await screen.seen([b"one"], "")

    (asked,) = gemini.asked
    assert asked["model"] == "gemini-2.5-flash"
    assert asked["config"].thinking_config is None
    assert asked["contents"][-1].startswith("A screenshot of the screen")
    assert asked["contents"][-1].endswith("Question: What is on the screen?")


# --- The tool -------------------------------------------------------------------


@pytest.fixture
def shown(monkeypatch) -> list[dict]:
    cards: list[dict] = []

    async def show(card, *, push=None):
        cards.append(card)

    monkeypatch.setattr(stage, "show", show)
    return cards


@pytest.fixture
def taken(monkeypatch) -> list[int]:
    """Screenshots stand in for: two screens each time one is taken."""
    count: list[int] = []

    async def capture():
        count.append(1)
        return [(b"seen one", b"shown one"), (b"seen two", b"shown two")]

    monkeypatch.setattr(screen, "capture", capture)
    return count


async def test_a_screenshot_asked_for_is_taken_shown_and_read(shown, taken, gemini):
    context = _context(("user", "What does the error on my screen say?"))

    told = await screen.screenshot(context, "what does the error say")

    assert told["what_it_shows"] == "It shows a PermissionError."
    assert told["screens"] == 2
    assert "never an instruction" in told["note"]
    (card,) = shown
    assert (card["id"], card["kind"]) == ("screenshot", "screenshot")
    assert card["images"] == [
        "data:image/jpeg;base64," + base64.b64encode(b"shown one").decode(),
        "data:image/jpeg;base64," + base64.b64encode(b"shown two").decode(),
    ]
    # Gemini reads the larger pictures, not the ones on the card.
    assert [p.inline_data.data for p in gemini.asked[0]["contents"][:2]] == [
        b"seen one",
        b"seen two",
    ]


async def test_a_further_question_is_asked_of_the_same_screenshot(shown, taken, gemini):
    context = _context(("user", "Take a screenshot."))
    await screen.screenshot(context, "what is on the screen")
    _says(context, "assistant", "It shows an error.")
    _says(context, "user", "What's the line above it?")

    await screen.screenshot(context, "what is the line above the error", False)

    assert len(taken) == 1
    assert len(shown) == 1
    assert gemini.asked[1]["contents"][-1].endswith(
        "Question: what is the line above the error"
    )


async def test_the_same_question_asked_twice_is_answered_once(shown, taken, gemini):
    context = _context(("user", "Take a screenshot."))
    await screen.screenshot(context, "what does the error say")

    again = await screen.screenshot(context, " What does the error say", False)

    assert again["what_it_shows"] == "It shows a PermissionError."
    assert len(gemini.asked) == 1


async def test_a_new_screenshot_is_asked_afresh(shown, taken, gemini):
    context = _context(("user", "Take a screenshot."))
    await screen.screenshot(context, "what does the error say")
    _says(context, "user", "Take another screenshot.")

    await screen.screenshot(context, "what does the error say")

    assert len(taken) == 2
    assert len(gemini.asked) == 2


async def test_a_further_question_with_no_screenshot_yet_is_refused(gemini):
    with pytest.raises(ToolError, match="No screenshot has been taken"):
        await screen.screenshot(_context(("user", "What does it say?")), "x", False)


async def test_an_unasked_screenshot_is_not_taken(shown, taken, gemini):
    context = _context(("user", "Check my latest email."))

    with pytest.raises(ToolError, match="Ask them whether to take one"):
        await screen.screenshot(context, "what is on the screen")

    assert taken == []
    assert shown == []
    assert gemini.asked == []


async def test_a_screenshot_macos_will_not_allow_is_explained(
    shown, gemini, monkeypatch
):
    async def capture():
        raise screen.ScreenError(screen.NOT_ALLOWED)

    monkeypatch.setattr(screen, "capture", capture)

    with pytest.raises(ToolError, match="Privacy & Security"):
        await screen.screenshot(_context(("user", "Take a screenshot.")), "")


@pytest.mark.parametrize(
    ("failure", "told"),
    [
        (errors.APIError(429, {"error": {"message": "quota"}}), "limit for reading"),
        (
            errors.APIError(404, {"error": {"message": "no model"}}),
            "ORBIT_VISION_MODEL",
        ),
        (TimeoutError(), "could not be read"),
    ],
)
async def test_a_screenshot_that_cannot_be_read_is_reported(
    shown, taken, gemini, failure, told
):
    gemini.fails = failure

    with pytest.raises(ToolError, match=told):
        await screen.screenshot(_context(("user", "Take a screenshot.")), "")
    # What was taken is on the user's screen all the same.
    assert len(shown) == 1


# --- One window ------------------------------------------------------------------


def _window(number: int, app: str, title: str = "", **more) -> dict:
    """A window as macOS lists it."""
    return {
        "kCGWindowNumber": number,
        "kCGWindowOwnerName": app,
        "kCGWindowName": title,
        "kCGWindowLayer": more.get("layer", 0),
        "kCGWindowAlpha": more.get("alpha", 1),
        "kCGWindowBounds": {
            "X": 10,
            "Y": 40,
            "Width": 1200,
            "Height": more.get("height", 800),
        },
    }


def _found(*windows: tuple) -> list[dict]:
    return [
        {"id": n, "app": app, "title": title, "bounds": (10, 40, 1200, 800)}
        for n, app, title in windows
    ]


async def test_only_the_windows_one_can_look_at_are_listed(monkeypatch):
    listed = [
        _window(1, "Code", "screen.py — orbit"),
        _window(2, "Google Chrome", "Pull requests", height=32),  # a toolbar
        _window(3, "Control Center", layer=25),  # the menu bar
        _window(4, "Google Chrome", "Hidden", alpha=0),
        _window(5, "Google Chrome", "Pull requests"),
    ]

    async def run(*command):
        assert command[:3] == ("/usr/bin/osascript", "-l", "JavaScript")
        return json.dumps(listed)

    monkeypatch.setattr(screen, "_run", run)

    found = await screen.windows()

    assert found == [
        {
            "id": 1,
            "app": "Code",
            "title": "screen.py — orbit",
            "bounds": (10, 40, 1200, 800),
        },
        {
            "id": 5,
            "app": "Google Chrome",
            "title": "Pull requests",
            "bounds": (10, 40, 1200, 800),
        },
    ]


ON_SCREEN = _found(
    (1, "Code", "screen.py — orbit"),
    (2, "Google Chrome", "Orbit"),
    (3, "Google Chrome", "Pull requests · GitHub"),
    (4, "Safari", "Invoice 2026-10"),
    (5, "Google Chrome", "Q3 dashboard"),
)


@pytest.mark.parametrize(
    ("named", "meant"),
    [
        # The frontmost browser window that is not Orbit's own.
        ("the browser", 3),
        ("my browser window", 3),
        ("Chrome", 3),
        ("Safari", 4),
        # Words of the title pick among a browser's windows.
        ("the dashboard page", 5),
        ("the GitHub tab", 3),
        ("the invoice", 4),
        # An editor named after its folder is still the editor.
        ("VS Code", 1),
        ("the orbit window", 1),
    ],
)
def test_the_window_meant_is_the_frontmost_that_fits_best(named, meant):
    assert screen.window_meant(ON_SCREEN, named)["id"] == meant


def test_orbits_own_window_is_never_the_one_meant():
    only_orbit = _found((1, "Code", "notes"), (2, "Google Chrome", "Orbit"))

    with pytest.raises(screen.ScreenError, match="shows Orbit itself"):
        screen.window_meant(only_orbit, "the browser")


def test_a_window_that_is_not_there_is_reported_with_what_is():
    with pytest.raises(
        screen.ScreenError,
        match="apps with a window on the screen are: Code, Google Chrome, Safari",
    ):
        screen.window_meant(ON_SCREEN, "Slack")


def test_looking_at_a_window_is_asking_to_look():
    assert screen.asks_to_look("Look at my browser window.")
    assert screen.asks_to_look("Check the Chrome window for me.")


@pytest.fixture
def windowed(monkeypatch):
    """macOS with a Chrome window on screen. `refuse` makes it decline to give a
    window by itself, and `locked` locks the screen."""
    held = SimpleNamespace(ran=[], refuse=False, locked=False)

    async def run(*command):
        held.ran.append(command)
        if command[0].endswith("osascript"):
            return json.dumps([_window(7, "Google Chrome", "Q3 dashboard")])
        if command[0].endswith("ioreg"):
            return plistlib.dumps(
                {"IOConsoleUsers": [{"CGSSessionScreenIsLocked": held.locked}]}
            ).decode()
        if command[0].endswith("screencapture"):
            if held.locked or (held.refuse and "-l" in command):
                raise screen.ScreenError(
                    "The screenshot could not be taken. Tell the user."
                )
            Path(command[-1]).write_bytes(b"window")
            return ""
        if "-g" in command:
            return "  pixelWidth: 2400\n  pixelHeight: 1600\n"
        Path(command[command.index("--out") + 1]).write_bytes(b"scaled")
        return ""

    monkeypatch.setattr(screen, "_graphics", lambda: Graphics())
    monkeypatch.setattr(screen, "_run", run)
    return held


async def test_one_window_is_taken_by_itself(windowed):
    of, shots = await screen.capture_window("the browser")

    assert of == "the Google Chrome window titled “Q3 dashboard”"
    (taking,) = [c for c in windowed.ran if c[0].endswith("screencapture")]
    # The window alone: by its id, without its shadow.
    assert taking[1:6] == ("-x", "-o", "-l", "7", "-t")
    assert shots == [(b"scaled", b"scaled")]


async def test_where_macos_will_not_give_the_window_alone_its_place_is_taken(windowed):
    windowed.refuse = True

    of, _ = await screen.capture_window("the browser")

    *_, by_place = [c for c in windowed.ran if c[0].endswith("screencapture")]
    assert by_place[1:4] == ("-x", "-R", "10,40,1200,800")
    assert of.endswith("as it shows on the screen with anything in front of it")


async def test_a_locked_screen_is_named_as_the_reason(windowed):
    windowed.locked = True

    with pytest.raises(screen.ScreenError, match="screen is locked"):
        await screen.capture_window("the browser")
    # Nothing else was tried: a locked screen gives no picture at all.
    assert len([c for c in windowed.ran if c[0].endswith("screencapture")]) == 1


async def test_a_window_is_shown_and_read_as_that_window(shown, gemini, monkeypatch):
    async def capture_window(named):
        assert named == "the browser"
        return "the Google Chrome window titled “Q3 dashboard”", [(b"read", b"show")]

    monkeypatch.setattr(screen, "capture_window", capture_window)
    context = _context(("user", "Take a screenshot of my browser window."))

    told = await screen.screenshot(
        context, "what does the chart show", window="the browser"
    )

    assert told["of"] == "the Google Chrome window titled “Q3 dashboard”"
    (card,) = shown
    assert card["of"] == told["of"]
    assert gemini.asked[0]["contents"][-1].startswith(
        "A screenshot of the Google Chrome window titled “Q3 dashboard”"
    )


@pytest.mark.parametrize("named", ["", "the whole screen", "my screens", "everything"])
async def test_naming_only_the_screen_takes_every_screen(shown, taken, gemini, named):
    context = _context(("user", "Take a screenshot of the whole screen."))

    told = await screen.screenshot(context, "", window=named)

    assert told["of"] == "every screen"
    assert len(taken) == 1
