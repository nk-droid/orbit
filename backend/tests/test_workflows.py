import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from livekit.agents.llm import ToolError
from test_browser import StubBrowser, tool

import agent_tools.actions as actions
from actions.gate import ActionGate
from agent_tools import browser as browser_tools
from agent_tools import stage, workflows
from agent_tools.heard import said_so_far
from agent_tools.workflows import (
    OFFER,
    OFFER_TEXT,
    Trace,
    WorkflowToolset,
    agreed,
    asks_to_remember,
    fill,
)
from storage import data_dir
from workflows import store

RESULTS = "https://www.youtube.com/results?search_query={what to play}"
ON_YOUTUBE = (
    "### Page\n- Page URL: https://www.youtube.com/results?search_query=work+music"
    "\n- Page Title: work music - YouTube"
)
ASKED = "Want me to remember how to do that next time?"
TEST_EMAIL = "nidhish.test@example.com"


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Workflows are saved to a throwaway folder, never the real one."""
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture(autouse=True)
def apps_answer(monkeypatch):
    """Whatever address on this computer a test opens, something answers there."""

    async def answering(ports):
        return list(ports)

    monkeypatch.setattr(browser_tools, "answering", answering)


@pytest.fixture
async def browser(tmp_path):
    stub = StubBrowser(tmp_path)
    await stub.toolset.setup()
    yield stub
    await stub.toolset.aclose()


@pytest.fixture
def remembering(browser) -> WorkflowToolset:
    return WorkflowToolset(browser.toolset)


@pytest.fixture
def no_waiting(monkeypatch):
    """The user's words are either on record or not: nothing is on its way."""
    monkeypatch.setattr(workflows, "HEARD_WITHIN", 0.0)


@pytest.fixture
def shown(monkeypatch) -> list[dict]:
    """The cards the tools asked the app to put on screen."""
    cards: list[dict] = []

    async def show(card, *, push=None):
        cards.append(card)

    monkeypatch.setattr(stage, "show", show)
    return cards


class Conversation:
    """A conversation as the tools see it: what was said and which tools were
    called, on record, and the state Orbit keeps with it."""

    def __init__(self, *said: str) -> None:
        self.items: list[SimpleNamespace] = []
        self.state = SimpleNamespace(gate=ActionGate(), trace=Trace(), handlers={})
        for text in said:
            self.user(text)

    def _add(self, **item) -> "Conversation":
        self.items.append(SimpleNamespace(id=f"item_{len(self.items)}", **item))
        return self

    def user(self, text: str) -> "Conversation":
        return self._add(type="message", role="user", text_content=text)

    def orbit(self, text: str) -> "Conversation":
        return self._add(type="message", role="assistant", text_content=text)

    def called(self, name: str, output: str = "", **arguments) -> "Conversation":
        self._add(type="function_call", name=name, arguments=json.dumps(arguments))
        return self._add(type="function_call_output", name=name, output=output)

    @property
    def context(self) -> SimpleNamespace:
        return SimpleNamespace(
            session=SimpleNamespace(history=SimpleNamespace(items=self.items)),
            userdata=self.state,
            disallow_interruptions=lambda: None,
        )


def remember(remembering: WorkflowToolset, name: str):
    return next(t for t in remembering.tools if t.id == name)


async def _play_work_music(browser: StubBrowser, talk: Conversation) -> list[str]:
    """Plays work music on YouTube, as the model did: the search results, then
    the first video. Returns what each step answered."""
    browser.answers("browser_navigate", ON_YOUTUBE)
    await tool(browser, "browser_start")(None, show_browser=True, said="show it")
    url = "https://www.youtube.com/results?search_query=work+music"
    return [
        await tool(browser, "browser_navigate")(talk.context, {"url": url}),
        await tool(browser, "browser_click")(
            talk.context, {"element": "first video", "target": "e10"}
        ),
    ]


async def _save_music(remembering, talk: Conversation, **changed) -> dict:
    asked = {
        "name": "play music on YouTube",
        "start_url": RESULTS,
        "steps": ["Click the first video in the results"],
        "asks": ["what to play: work music"],
        **changed,
    }
    return await remember(remembering, "save_workflow")(talk.context, **asked)


def _saved_music(**changed) -> store.Workflow:
    kept = {
        "name": "play music on YouTube",
        "steps": ["Click the first video in the results"],
        "asks": ["what to play"],
        "keeps": {},
        "start_url": RESULTS,
        "shown": True,
        "profile": None,
        **changed,
    }
    return store.save(**kept)


def _saved_email() -> store.Workflow:
    return store.save(
        "email my test address",
        ["Draft an email to {test email} saying {message}"],
        ["message"],
        {"test email": TEST_EMAIL},
    )


def _emailed_my_test_address() -> Conversation:
    """A conversation in which the user had an email sent to their test address,
    which Orbit had to ask for."""
    talk = Conversation("Send an email to my test email id saying hi.")
    talk.orbit("What is your test email address?")
    talk.user(f"It's {TEST_EMAIL}.")
    talk.called("gmail_draft_email", to=[TEST_EMAIL], subject="Hi", body="hi")
    return talk


# The store.


def test_a_workflow_is_kept_with_what_it_asks_for_and_what_stays_the_same():
    _saved_email()

    (workflow,) = store.listed()
    assert workflow.asks == ["message"]
    assert workflow.keeps == {"test email": TEST_EMAIL}
    assert (workflow.start_url, workflow.shown, workflow.profile) == ("", None, None)
    assert (workflow.uses, workflow.used_at) == (0, None)


def test_saving_under_a_name_in_use_replaces_the_workflow():
    _saved_music()
    _saved_music(name="Play music on YouTube", steps=["Click the second video"])

    (workflow,) = store.listed()
    assert workflow.steps == ["Click the second video"]


def test_a_workflow_is_found_by_its_name_or_part_of_it():
    _saved_music()
    _saved_music(name="play a playlist on YouTube")

    assert [w.name for w in store.matching("Play music on YouTube")] == [
        "play music on YouTube"
    ]
    assert len(store.matching("play")) == 2
    assert [w.name for w in store.matching("playlist")] == [
        "play a playlist on YouTube"
    ]
    assert store.matching("order a pizza") == []


def test_a_workflow_counts_its_uses_and_can_be_removed():
    workflow = _saved_music()

    store.used(workflow.id)
    store.used(workflow.id)
    (used,) = store.listed()
    assert used.uses == 2 and used.used_at is not None

    store.remove(workflow.id)
    assert store.listed() == []


def test_a_workflow_saved_by_the_first_version_is_upgraded():
    folder = data_dir()
    folder.mkdir(parents=True)
    first = sqlite3.connect(folder / "orbit.db")
    first.execute(
        "CREATE TABLE workflows (id TEXT PRIMARY KEY, name TEXT NOT NULL, value_name "
        "TEXT NOT NULL, start_url TEXT NOT NULL, steps TEXT NOT NULL, shown INTEGER "
        "NOT NULL, profile TEXT, created_at REAL NOT NULL, used_at REAL, uses "
        "INTEGER NOT NULL DEFAULT 0)"
    )
    first.execute(
        "INSERT INTO workflows VALUES ('a1', 'play work music', 'the music', "
        "'https://www.youtube.com/results?search_query={value}', "
        "'[\"Click the first video\"]', 1, 'Work', 1.0, 2.0, 3)"
    )
    first.commit()
    first.close()

    (workflow,) = store.listed()

    assert workflow.start_url == "https://www.youtube.com/results?search_query={music}"
    assert workflow.asks == ["music"]
    assert workflow.keeps == {}
    assert (workflow.shown, workflow.profile, workflow.uses) == (True, "Work", 3)


# Writing parts in.


@pytest.mark.parametrize(
    ("text", "values", "filled"),
    [
        (
            RESULTS,
            {"what to play": "some music"},
            "https://www.youtube.com/results?search_query=some+music",
        ),
        (
            "https://example.com/wiki/{topic}",
            {"topic": "Ada Lovelace"},
            "https://example.com/wiki/Ada%20Lovelace",
        ),
        ("https://example.com/#q={q}", {"q": "a&b"}, "https://example.com/#q=a%26b"),
        # A kept address is the whole address, as it is.
        (
            "{my playlist}",
            {"playlist": "https://x.example.com/list?id=1"},
            "https://x.example.com/list?id=1",
        ),
    ],
)
def test_parts_are_written_into_an_address(text, values, filled):
    assert fill(text, values, address=True) == filled


def test_parts_are_written_into_steps_as_they_are():
    step = "Draft an email to {Test Email} saying {message}"

    filled = fill(step, {"test email": TEST_EMAIL, "message": "The build passed."})

    assert filled == f"Draft an email to {TEST_EMAIL} saying The build passed."


@pytest.mark.parametrize(
    ("said", "asks"),
    [
        ("Remember how you did that.", True),
        ("Save it as a workflow.", True),
        ("Keep this for next time.", True),
        ("Remember that I like jazz.", False),
        ("Don't save how you did it.", False),
        ("What workflows do I have?", False),
        ("Yes.", False),
    ],
)
def test_the_user_asks_for_a_task_to_be_remembered(said, asks):
    assert asks_to_remember(said) is asks


def _offered(trace: Trace, said_before: int = 0) -> Trace:
    """A trace in which the model was told to offer, after this much was said."""
    trace.asked_from = said_before
    return trace


@pytest.mark.parametrize(
    ("said", "agrees"),
    [
        ([("assistant", ASKED), ("user", "Yes please.")], True),
        ([("assistant", "Should I save it as a workflow?"), ("user", "Sure.")], True),
        ([("assistant", ASKED), ("user", "No thanks.")], False),
        ([("assistant", ASKED), ("user", "Not now.")], False),
        # It never asked, so the user's yes was to something else.
        ([("assistant", "It's playing."), ("user", "Yes, thanks.")], False),
        # The latest answer is the one that counts.
        ([("assistant", ASKED), ("user", "No."), ("user", "Actually yes.")], True),
        (
            [("assistant", ASKED), ("user", "Yes."), ("assistant", "Saving it now.")],
            True,
        ),
        ([("assistant", ASKED)], False),
    ],
)
def test_the_user_says_yes_to_the_offer(said, agrees):
    assert agreed(said, _offered(Trace())) is agrees


def test_a_yes_before_the_offer_is_not_an_answer_to_it():
    said = [("assistant", ASKED), ("user", "Yes.")]
    assert agreed(said, _offered(Trace(), said_before=2)) is False
    assert agreed(said, Trace()) is False


def test_words_that_saved_one_workflow_do_not_save_the_next():
    trace = Trace()
    said = [("user", "Play Hotel California on YouTube and remember how.")]
    assert agreed(said, trace)

    trace.saved_up_to = len(said)
    assert not agreed(said, trace)
    assert agreed([*said, ("user", "Remember how you did that one too.")], trace)


# Offering to remember.


async def test_a_page_opened_and_used_is_offered_to_be_remembered(browser):
    talk = Conversation("Play work music on YouTube.")

    opened, clicked = await _play_work_music(browser, talk)

    assert OFFER not in opened
    assert clicked.endswith(OFFER)


async def test_the_offer_lasts_until_orbit_has_spoken(browser):
    talk = Conversation("Play work music on YouTube.")
    await _play_work_music(browser, talk)

    still = await tool(browser, "browser_snapshot")(talk.context, {})
    talk.orbit("It's playing. " + ASKED)
    after = await tool(browser, "browser_snapshot")(talk.context, {})

    assert still.endswith(OFFER)
    assert OFFER not in after


async def test_the_offer_is_made_once_a_conversation(browser):
    talk = Conversation("Play work music on YouTube.")
    await _play_work_music(browser, talk)
    talk.orbit(ASKED).user("No. Play some jazz.")

    answers = await _play_work_music(browser, talk)

    assert not any(OFFER in answer for answer in answers)


async def test_reading_pages_alone_is_not_offered(browser):
    talk = Conversation("What does example dot com say?")
    await tool(browser, "browser_start")(None, show_browser=True, said="show it")
    answers = [
        await tool(browser, "browser_navigate")(
            talk.context, {"url": "https://example.com"}
        ),
        await tool(browser, "browser_snapshot")(talk.context, {}),
        await tool(browser, "browser_find")(talk.context, {"text": "Example"}),
    ]

    assert not any(OFFER in answer for answer in answers)


async def test_using_a_page_already_open_is_not_offered(browser):
    talk = Conversation("Pause it.")
    await tool(browser, "browser_start")(None, show_browser=True, said="show it")
    answers = [
        await tool(browser, "browser_click")(
            talk.context, {"element": "Pause", "target": "e5"}
        ),
        await tool(browser, "browser_press_key")(talk.context, {"key": "k"}),
    ]

    assert not any(OFFER in answer for answer in answers)


async def test_a_task_on_a_site_with_a_saved_workflow_is_not_offered(browser):
    _saved_music()

    answers = await _play_work_music(browser, Conversation("Play work music."))

    assert not any(OFFER in answer for answer in answers)


def test_an_action_for_something_of_the_users_is_offered_to_be_remembered():
    said = [("user", "Send an email to my test email id saying hi.")]

    assert Trace().offer_after("gmail.send_email", said) == OFFER_TEXT
    # Not an answer to one email, nor a request with nothing of the user's.
    assert Trace().offer_after("gmail.send_reply", said) == ""
    assert Trace().offer_after("gmail.send_email", [("user", "Email Asha.")]) == ""
    # Once in a conversation, and not after a workflow was run.
    offered = Trace()
    offered.offer_after("gmail.send_email", said)
    assert offered.offer_after("calendar.create_event", said) == ""
    ran = Trace()
    ran.ran_workflow()
    assert ran.offer_after("gmail.send_email", said) == ""


async def test_the_offer_comes_once_the_email_is_sent(monkeypatch):
    sent: list[dict] = []

    async def perform(session, kind, payload):
        sent.append(payload)
        return {"id": "sent-1"}

    monkeypatch.setattr(actions, "_require_write", lambda service: None)
    monkeypatch.setattr(actions, "_http", lambda: None)
    monkeypatch.setattr(actions, "perform", perform)
    talk = _emailed_my_test_address()
    drafted = await actions.gmail_draft_email(talk.context, [TEST_EMAIL], "Hi", "hi")
    gate = talk.state.gate
    gate.note_agent_spoke(gate.now() + 1)
    gate._last_user_input_at = gate.now() + 2

    done = await actions.confirm_action(talk.context, drafted["action_id"])

    assert sent and done["then"] == OFFER_TEXT


# Saving.


async def test_a_task_in_the_browser_is_saved_without_what_was_asked_for(
    browser, remembering, shown
):
    talk = Conversation("Play work music on YouTube.")
    await _play_work_music(browser, talk)
    talk.orbit(ASKED).user("Yes.")

    saved = await _save_music(remembering, talk)

    assert saved["saved"] == "play music on YouTube"
    assert saved["asked_for_each_time"] == ["what to play"]
    assert saved["stays_the_same"] == {}
    assert saved["browser"] == {"shown": True, "profile": "none"}
    (workflow,) = store.listed()
    assert (workflow.start_url, workflow.shown, workflow.profile) == (
        RESULTS,
        True,
        None,
    )
    assert "work" not in json.dumps([workflow.name, workflow.steps, workflow.keeps])
    card = shown[-1]
    assert card["kind"] == "note"
    assert card["title"] == "Remembered: play music on YouTube"
    assert "<strong>youtube.com</strong>" in card["html"]
    assert "what to play" in card["html"]


async def test_what_the_user_asked_for_is_not_kept_in_the_name(browser, remembering):
    # A workflow saved like this went on to search for "work" when the user only
    # asked for some music.
    talk = Conversation("Play work music on YouTube.", "Remember how you did that.")
    await _play_work_music(browser, talk)

    with pytest.raises(ToolError, match=r'in place of "work music" wherever'):
        await _save_music(remembering, talk, name="play work music on YouTube")
    with pytest.raises(ToolError, match=r'in place of "work music" wherever'):
        await _save_music(
            remembering,
            talk,
            start_url="https://www.youtube.com/results?search_query=work+music",
            steps=["Click the first video in the results for {what to play}"],
        )
    assert store.listed() == []


async def test_an_email_keeps_the_users_own_address_and_asks_for_the_message(
    remembering, shown
):
    talk = _emailed_my_test_address()
    # Once the email was sent, Orbit was told to offer, and did.
    said = said_so_far(talk.context)
    assert talk.state.trace.offer_after("gmail.send_email", said) == OFFER_TEXT
    talk.orbit("Sent. " + ASKED).user("Yes, please.")

    saved = await remember(remembering, "save_workflow")(
        talk.context,
        name="email my test address",
        steps=["Draft an email to {test email} saying {message}"],
        asks=["message: hi"],
        keeps=[f"test email: {TEST_EMAIL}"],
    )

    assert saved["asked_for_each_time"] == ["message"]
    assert saved["stays_the_same"] == {"test email": TEST_EMAIL}
    assert "browser" not in saved
    (workflow,) = store.listed()
    assert (workflow.start_url, workflow.shown) == ("", None)
    assert "Stays the same: test email" in shown[-1]["html"]


async def test_a_message_is_never_kept_the_same(remembering):
    talk = _emailed_my_test_address().user("Remember how you did that.")

    with pytest.raises(ToolError, match=r'they have not called "message" theirs'):
        await remember(remembering, "save_workflow")(
            talk.context,
            name="email my test address",
            steps=["Draft an email to {test email} saying {message}"],
            keeps=[f"test email: {TEST_EMAIL}", "message: hi"],
        )
    assert store.listed() == []


async def test_only_what_came_up_in_the_conversation_is_kept(remembering):
    talk = _emailed_my_test_address().user("Remember how you did that.")

    with pytest.raises(ToolError, match=r'kept for "test email" did not come up'):
        await remember(remembering, "save_workflow")(
            talk.context,
            name="email my test address",
            steps=["Draft an email to {test email} saying {message}"],
            asks=["message: hi"],
            keeps=["test email: someone.else@example.com"],
        )


@pytest.mark.parametrize(
    ("changed", "refused"),
    [
        ({"asks": []}, r"\{message\} in them, which is not in asks or keeps"),
        ({"asks": ["message: hi", "subject: Hi"]}, r"Write \{subject\} in the steps"),
        ({"asks": ["message: hi", "test email: x"]}, r'"test email" is in both'),
        ({"asks": ["hi"]}, r'Write each part as "name: value"'),
        ({"name": "  "}, r"Name the workflow"),
        ({"steps": []}, r"Write what to do as steps"),
    ],
)
async def test_each_part_of_a_workflow_has_its_place(remembering, changed, refused):
    talk = _emailed_my_test_address().user("Remember how you did that.")
    asked = {
        "name": "email my test address",
        "steps": ["Draft an email to {test email} saying {message}"],
        "asks": ["message: hi"],
        "keeps": [f"test email: {TEST_EMAIL}"],
        **changed,
    }

    with pytest.raises(ToolError, match=refused):
        await remember(remembering, "save_workflow")(talk.context, **asked)


async def test_a_workflow_is_not_saved_until_the_user_says_yes(
    browser, remembering, no_waiting
):
    talk = Conversation("Play work music on YouTube.")
    await _play_work_music(browser, talk)
    talk.orbit("It's playing.")

    with pytest.raises(ToolError, match="has not said they want this remembered"):
        await _save_music(remembering, talk)
    talk.orbit(ASKED).user("No, thanks.")
    with pytest.raises(ToolError, match="has not said they want this remembered"):
        await _save_music(remembering, talk)
    assert store.listed() == []

    # Sent to ask again, it does, and they have changed their mind.
    talk.orbit(ASKED).user("Yes, do.")
    await _save_music(remembering, talk)
    assert len(store.listed()) == 1


async def test_a_workflow_is_saved_when_the_user_asks_for_it(browser, remembering):
    talk = Conversation("Play work music on YouTube and remember how you did it.")
    await _play_work_music(browser, talk)

    assert (await _save_music(remembering, talk))["saved"] == "play music on YouTube"


async def test_a_workflow_needs_something_done(remembering):
    talk = Conversation("Remember how you did that.")

    with pytest.raises(ToolError, match="Nothing has been done"):
        await _save_music(remembering, talk)


async def test_a_task_in_the_browser_begins_on_a_site_that_was_opened(
    browser, remembering
):
    talk = Conversation("Play work music.", "Remember how you did that.")
    await _play_work_music(browser, talk)

    with pytest.raises(ToolError, match=r"sites you opened: youtube\.com"):
        await _save_music(
            remembering, talk, start_url="https://music.example.com/?q={what to play}"
        )


async def test_saving_again_under_the_same_name_replaces_the_workflow(
    browser, remembering
):
    _saved_music(steps=["Click the second video"])
    talk = Conversation("Play work music.", "Remember how you did that.")
    await _play_work_music(browser, talk)

    saved = await _save_music(remembering, talk)

    assert "replaced" in saved
    (workflow,) = store.listed()
    assert workflow.steps == ["Click the first video in the results"]


# Running.


async def test_a_task_in_the_browser_runs_with_what_the_user_asks_for_now(browser):
    _saved_music()
    await browser.next_conversation()
    remembering = WorkflowToolset(browser.toolset)
    talk = Conversation("Play some music.")

    ran = await remember(remembering, "run_workflow")(
        talk.context, name="play music on youtube", values=["what to play: some music"]
    )

    # No question about seeing the browser: it is started as it was saved.
    assert browser.started == [True]
    assert browser.asked()[-1]["arguments"] == {
        "url": "https://www.youtube.com/results?search_query=some+music"
    }
    assert ran["steps_left"] == ["Click the first video in the results"]
    assert ran["next"] == workflows.NEXT
    assert ran["browser"] == "clean"
    assert store.listed()[0].uses == 1
    started = await tool(browser, "browser_start")(talk.context)
    assert started["status"] == "already running"


async def test_what_the_user_asked_for_before_is_not_asked_for_again(
    browser, remembering, no_waiting
):
    _saved_music()
    talk = Conversation("Play some music.")

    with pytest.raises(ToolError, match=r'did not ask for "work" this time'):
        await remember(remembering, "run_workflow")(
            talk.context, name="play music", values=["what to play: work music"]
        )
    assert browser.started == []


async def test_what_the_user_said_may_reach_the_record_late(browser, remembering):
    _saved_music()
    talk = Conversation()

    async def heard_later():
        await asyncio.sleep(0.3)
        talk.user("Play Bohemian Rhapsody.")

    later = asyncio.create_task(heard_later())
    ran = await remember(remembering, "run_workflow")(
        talk.context, name="play music", values=["Bohemian Rhapsody"]
    )
    await later

    assert ran["workflow"] == "play music on YouTube"


async def test_an_email_runs_with_what_it_keeps_and_the_message_said_now(
    browser, remembering
):
    _saved_email()
    talk = Conversation("Email my test address that the build passed.")

    ran = await remember(remembering, "run_workflow")(
        talk.context,
        name="email my test address",
        values=["message: The build passed."],
    )

    assert ran["steps_left"] == [
        f"Draft an email to {TEST_EMAIL} saying The build passed."
    ]
    assert ran["next"] == workflows.NEXT
    assert "browser" not in ran and browser.started == []


@pytest.mark.parametrize(
    "values", [["Yesterday"], ["song: Yesterday"], ["what to play: Yesterday"]]
)
async def test_a_workflow_asking_for_one_part_takes_it_however_it_is_named(
    browser, remembering, values
):
    _saved_music()

    await remember(remembering, "run_workflow")(
        Conversation("Play Yesterday.").context, name="play music", values=values
    )

    assert browser.asked()[-1]["arguments"] == {
        "url": "https://www.youtube.com/results?search_query=Yesterday"
    }


async def test_a_workflow_needs_what_it_asks_for(remembering):
    _saved_email()

    with pytest.raises(ToolError, match=r'needs "message"\. Take it from'):
        await remember(remembering, "run_workflow")(
            Conversation("Email my test address.").context, name="email my test"
        )


async def test_a_workflow_runs_in_a_copy_of_the_profile_it_was_saved_with(
    browser, remembering
):
    _saved_music(shown=False, profile="Work")

    ran = await remember(remembering, "run_workflow")(
        Conversation("Play Yesterday.").context, name="play music", values=["Yesterday"]
    )

    assert browser.started == [False]
    assert browser.chrome.copied == ["Work"]
    assert ran["browser"] == "Work"


async def test_a_workflow_uses_the_open_browser_of_its_kind(browser, remembering):
    _saved_music()
    await tool(browser, "browser_start")(None, show_browser=True, said="show it")
    await tool(browser, "browser_navigate")(
        None, {"url": "https://www.youtube.com/watch?v=1"}
    )

    await remember(remembering, "run_workflow")(
        Conversation("Play Yesterday.").context, name="play music", values=["Yesterday"]
    )

    assert browser.started == [True]
    # Another song on the same site is meant to replace the one playing.
    assert browser.calls()[-1] == ("shown", "browser_navigate")


async def test_a_workflow_for_another_site_leaves_the_page_playing_alone(
    browser, remembering
):
    _saved_music(
        name="look up a word",
        start_url="https://dictionary.example.com/?q={word}",
        asks=["word"],
    )
    await tool(browser, "browser_start")(None, show_browser=True, said="show it")
    await tool(browser, "browser_navigate")(
        None, {"url": "https://www.youtube.com/watch?v=1"}
    )

    await remember(remembering, "run_workflow")(
        Conversation("Look up orbit.").context, name="look up a word", values=["orbit"]
    )

    assert browser.asked()[-1]["tool"] == "browser_tabs"
    assert browser.asked()[-1]["arguments"] == {
        "action": "new",
        "url": "https://dictionary.example.com/?q=orbit",
    }


async def test_a_hidden_workflow_does_not_take_over_the_browser_on_screen(
    browser, remembering
):
    _saved_music(shown=False)
    await tool(browser, "browser_start")(None, show_browser=True, said="show it")

    await remember(remembering, "run_workflow")(
        Conversation("Play Yesterday.").context, name="play music", values=["Yesterday"]
    )

    assert browser.started == [True, False]


async def test_a_task_run_by_a_workflow_is_not_offered_to_be_remembered(
    browser, remembering
):
    _saved_music(
        name="look up a word",
        start_url="https://dictionary.example.com/?q={word}",
        asks=["word"],
    )
    talk = Conversation("Look up orbit in my dictionary.")

    ran = await remember(remembering, "run_workflow")(
        talk.context, name="look up a word", values=["word: orbit"]
    )
    clicked = await tool(browser, "browser_click")(
        talk.context, {"element": "first entry", "target": "e1"}
    )

    assert OFFER not in ran["page"] and OFFER not in clicked
    said = [("user", "Look up orbit in my dictionary.")]
    assert talk.state.trace.offer_after("gmail.send_email", said) == ""


async def test_a_workflow_with_nothing_to_fill_in_just_opens_its_page(
    browser, remembering
):
    _saved_music(
        name="open my inbox", asks=[], start_url="https://mail.example.com/", steps=[]
    )

    ran = await remember(remembering, "run_workflow")(
        Conversation("Open my inbox.").context, name="open my inbox"
    )

    assert ran["steps_left"] == [] and ran["next"] == workflows.DONE
    assert browser.asked()[-1]["arguments"] == {"url": "https://mail.example.com/"}


async def test_a_workflow_may_begin_at_an_address_on_the_users_computer(
    browser, remembering
):
    # Saved where the user had the browser go, it is one they asked for.
    _saved_music(
        name="open my app", asks=[], start_url="http://localhost:3000/", steps=[]
    )

    await remember(remembering, "run_workflow")(
        Conversation("Open my app.").context, name="open my app"
    )

    assert browser.asked()[-1]["arguments"] == {"url": "http://localhost:3000/"}


async def test_a_workflow_never_opens_orbits_own_app(browser, remembering):
    _saved_music(
        name="open my mail", asks=[], start_url="http://localhost:5173/", steps=[]
    )

    with pytest.raises(ToolError, match="Orbit's own app"):
        await remember(remembering, "run_workflow")(
            Conversation("Open my mail.").context, name="open my mail"
        )
    assert browser.started == []


async def test_an_unknown_workflow_names_the_saved_ones(remembering):
    talk = Conversation("Play some music.")
    with pytest.raises(ToolError, match="The user has no saved workflows"):
        await remember(remembering, "run_workflow")(talk.context, name="play music")

    _saved_music()
    _saved_music(name="play a playlist on YouTube")
    with pytest.raises(ToolError, match="No saved workflow is called 'order a pizza'"):
        await remember(remembering, "run_workflow")(talk.context, name="order a pizza")
    with pytest.raises(ToolError, match="fits several saved workflows"):
        await remember(remembering, "run_workflow")(talk.context, name="play")


async def test_a_profile_the_user_no_longer_has_is_not_guessed(browser, remembering):
    _saved_music(profile="Old")

    with pytest.raises(ToolError, match="no Chrome profile called 'Old'"):
        await remember(remembering, "run_workflow")(
            Conversation("Play Yesterday.").context,
            name="play music",
            values=["Yesterday"],
        )
    assert browser.started == []


async def test_a_workflow_can_be_forgotten(remembering):
    _saved_music()

    forgotten = await remember(remembering, "forget_workflow")(
        Conversation("Forget it.").context, name="play music"
    )

    assert forgotten == {"forgotten": "play music on YouTube"}
    assert store.listed() == []


# What the model is told.


def test_the_workflow_tools_are_there_for_every_conversation():
    assert [t.id for t in WorkflowToolset().tools] == [
        "run_workflow",
        "save_workflow",
        "forget_workflow",
    ]


def test_the_model_is_told_which_workflows_are_saved():
    assert WorkflowToolset().rules == ""

    _saved_music()
    _saved_email()

    rules = WorkflowToolset().rules
    assert '"play music on YouTube" (asks for what to play)' in rules
    assert (
        f'"email my test address" (asks for message; keeps test email = {TEST_EMAIL})'
        in rules
    )
    assert "never what they asked for before" in rules
