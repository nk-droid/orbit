import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from livekit.agents.llm import ToolError
from test_browser import StubBrowser, tool

import agent_tools.actions as action_tools
from actions.gate import ActionGate
from agent import Assistant
from agent_tools import lists as list_tools
from agent_tools import stage, workflows
from agent_tools.lists import ListToolset, lists_note
from agent_tools.reminders import reminder_add
from agent_tools.state import OrbitState, note_change
from lists import store
from workflows import store as workflow_store

TASK = "Accept the collaborator invitation"
INVITES = [
    f"Invitation to join team/SE-SEP2026-Team{n:03} "
    f"https://github.com/team/SE-SEP2026-Team{n:03}/invitations"
    for n in range(1, 4)
]


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Lists are kept in a throwaway folder, never the real one."""
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """Waits are short in tests."""
    monkeypatch.setattr(list_tools, "ACTED_WITHIN", 0.2)
    monkeypatch.setattr(list_tools, "LOOK_EVERY", 0.02)
    monkeypatch.setattr(list_tools, "NUDGES", (0.02, 0.02, 0.02))


@pytest.fixture
def shown(monkeypatch) -> list[dict]:
    cards: list[dict] = []

    async def show(card, *, push=None):
        cards.append(card)

    monkeypatch.setattr(stage, "show", show)
    return cards


@pytest.fixture
def google(monkeypatch) -> list[tuple[str, dict]]:
    """Google, standing in: what was carried out."""
    done: list[tuple[str, dict]] = []

    async def perform(session, kind, payload):
        done.append((kind, payload))
        return {"done": True}

    monkeypatch.setattr(action_tools, "perform", perform)
    monkeypatch.setattr(action_tools, "_http", lambda: None)
    monkeypatch.setattr(list_tools, "perform", perform)
    monkeypatch.setattr(list_tools, "_http", lambda: None)
    return done


class FakeSession:
    """The voice session as a list sees it: its events, what the model was told to
    do of its own accord, and what was said and given in the conversation."""

    def __init__(self, items: list) -> None:
        self.history = SimpleNamespace(items=items)
        self.handlers: dict[str, list] = {}
        self.told: list[str] = []
        self.agent_state = "listening"
        self.current_speech = None

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def off(self, event, handler):
        if handler in self.handlers.get(event, []):
            self.handlers[event].remove(handler)

    def emit(self, event, payload):
        for handler in list(self.handlers.get(event, [])):
            handler(payload)

    async def generate_reply(self, instructions=""):
        self.told.append(instructions)


def _talk(*said: str) -> SimpleNamespace:
    moment = [1000.0]
    items = [SimpleNamespace(type="message", role="user", text_content=t) for t in said]
    return SimpleNamespace(
        session=FakeSession(items),
        userdata=OrbitState(gate=ActionGate(clock=lambda: moment[0])),
        disallow_interruptions=lambda: None,
        moment=moment,
    )


def _gave(talk: SimpleNamespace, name: str, output: str) -> None:
    """A tool the model called gave this back."""
    n = len(talk.session.history.items)
    talk.session.history.items += [
        SimpleNamespace(type="function_call", call_id=f"c{n}", arguments="{}"),
        SimpleNamespace(
            type="function_call_output",
            call_id=f"c{n}",
            name=name,
            output=output,
            is_error=False,
            id=f"o{n}",
        ),
    ]


def _yes(talk: SimpleNamespace) -> str:
    """The user heard the draft waiting, then said yes. Returns its action_id."""
    gate = talk.userdata.gate
    action = gate.awaiting()
    talk.moment[0] += 1
    gate.mark_reviewed(action.id)
    talk.moment[0] += 1
    gate.note_user_input()
    return action.id


def _later(talk: SimpleNamespace) -> None:
    talk.moment[0] += 1


async def _started(talk, items=INVITES, task=TASK) -> tuple[ListToolset, dict]:
    """A list the user said yes to, and what the model was told then."""
    _gave(talk, "browser_read", "\n".join(items))
    tools = ListToolset()
    await tools._for_each(talk, task, items=items)
    done = await action_tools.confirm_action(talk, _yes(talk))
    _later(talk)
    return tools, done


async def _listening(talk: SimpleNamespace, then: float = 0.1) -> None:
    """The model has gone quiet, waiting for the user."""
    talk.session.emit("agent_state_changed", SimpleNamespace(new_state="listening"))
    await asyncio.sleep(then)


def _heard(talk: SimpleNamespace, words: str) -> None:
    talk.session.emit(
        "user_input_transcribed", SimpleNamespace(transcript=words, is_final=True)
    )


def _only_list() -> store.TaskList:
    import sqlite3

    from storage import data_dir

    with sqlite3.connect(data_dir() / "orbit.db") as connection:
        (list_id,) = connection.execute("SELECT id FROM lists").fetchone()
    found = store.get(list_id)
    assert found is not None
    return found


# Asking once.


async def test_a_list_waits_for_one_yes_to_all_of_it(shown, google):
    talk = _talk("Accept all my collaborator invites.")
    _gave(talk, "browser_read", "\n".join(INVITES))
    tools = ListToolset()

    told = await tools._for_each(talk, TASK, items=INVITES)

    action = talk.userdata.gate.awaiting()
    assert action.kind == list_tools.RUN
    assert told == {
        "action_id": action.id,
        "status": "awaiting_approval",
        "items": 3,
        "first": INVITES,
        "next": list_tools.ASK,
    }
    # Every item is on the draft the user says yes to.
    assert [row[1] for row in action.preview["table"]["rows"]] == INVITES
    assert google == []
    with pytest.raises(ToolError, match="has not been told"):
        await action_tools.confirm_action(talk, action.id)

    done = await action_tools.confirm_action(talk, _yes(talk))

    assert done["item"] == {"number": 1, "of": 3, "item": INVITES[0]}
    assert "call item_done" in done["do"]
    assert shown[-1]["tag"] == "0 of 3 done"
    assert shown[-1]["rows"][0]["meta"] == "Now"


async def test_each_item_is_given_in_turn_until_the_list_is_finished(shown, google):
    talk = _talk("Accept all my collaborator invites.")
    tools, done = await _started(talk)

    given = []
    for _ in INVITES:
        given.append(done["item"]["number"])
        note_change(talk)  # The model clicked Accept invitation.
        done = await tools._item_done(talk, True, "accepted")

    assert given == [1, 2, 3]
    assert done == {"finished": list_tools.FINISHED.format(summary="3 of 3 done")}
    assert _only_list().state == store.FINISHED
    assert shown[-1]["tag"] == "3 of 3 done"
    assert [row["meta"] for row in shown[-1]["rows"]] == ["Done"] * 3
    # Each row opens its own invitation.
    assert shown[-1]["rows"][0]["link"].endswith("Team001/invitations")
    with pytest.raises(ToolError, match="No list is being done"):
        await tools._item_done(talk, True, "accepted")


async def test_an_item_is_not_done_until_something_was_changed_for_it(shown, google):
    talk = _talk("Accept all my collaborator invites.")
    tools, _ = await _started(talk)

    # Said to be accepted, with nothing clicked: told so.
    with pytest.raises(ToolError, match="Nothing has been changed for item 1"):
        await tools._item_done(talk, True, "accepted")
    # Said again, its word is taken, marked as unchecked.
    told = await tools._item_done(talk, True, "accepted")

    assert told["item"]["number"] == 2
    first = _only_list().item(1)
    assert (first.state, first.checked) == (store.DONE, False)
    assert shown[-1]["rows"][0]["meta"] == "Unchecked"
    for _ in range(2):
        note_change(talk)
        told = await tools._item_done(talk, True, "accepted")
    assert "1 said to be done with nothing Orbit could check" in told["finished"]


async def test_a_click_made_alongside_item_done_is_waited_for(shown, google):
    talk = _talk("Accept all my collaborator invites.")
    tools, _ = await _started(talk)

    async def click_lands():
        await asyncio.sleep(0.05)
        note_change(talk)

    clicking = asyncio.create_task(click_lands())
    told = await tools._item_done(talk, True, "accepted")
    await clicking

    assert told["item"]["number"] == 2
    assert _only_list().item(1).checked


# What a list holds.


async def test_items_that_never_came_up_are_refused():
    talk = _talk("Accept all my collaborator invites.")
    _gave(talk, "browser_read", INVITES[0])

    with pytest.raises(ToolError, match="did not come up") as refused:
        await ListToolset()._for_each(talk, TASK, items=INVITES[:2])

    assert "Team002" in str(refused.value)
    assert "Team001" not in str(refused.value)


async def test_an_item_may_come_from_a_page_or_from_the_user():
    talk = _talk("Remind me to call Asha, Ben and Chitra tomorrow.")
    # A snapshot gives a link's address as the page has it, without its site.
    _gave(
        talk,
        "browser_snapshot",
        '- link "Invitation to join team/SE-SEP2026-Team001" [ref=e5]:\n'
        "  - /url: /team/SE-SEP2026-Team001/invitations",
    )
    tools = ListToolset()

    told = await tools._for_each(
        talk,
        "Set a reminder",
        items=[INVITES[0], "Call Asha", "Call Ben", "Call Chitra"],
    )

    assert told["items"] == 4


async def test_the_same_item_twice_is_one_item():
    talk = _talk("Accept all my collaborator invites.")
    _gave(talk, "browser_read", "\n".join(INVITES))

    # The same link, given with fewer of its words.
    shorter = "team/SE-SEP2026-Team001 " + _link(1)

    told = await ListToolset()._for_each(
        talk, TASK, items=[INVITES[0], shorter, INVITES[0], INVITES[1]]
    )

    assert told["items"] == 2


async def test_more_items_than_one_list_takes_are_said(monkeypatch):
    monkeypatch.setattr(list_tools, "MOST_ITEMS", 2)
    talk = _talk("Accept all my collaborator invites.")
    _gave(talk, "browser_read", "\n".join(INVITES))

    told = await ListToolset()._for_each(talk, TASK, items=INVITES)

    assert told["items"] == 2
    assert told["notes"] == [list_tools.LEFT_FOR_LATER.format(count=1)]


async def test_a_list_that_only_reads_starts_at_once(shown):
    talk = _talk("Summarise each of these pages.")
    _gave(talk, "browser_read", "\n".join(INVITES))
    tools = ListToolset()

    told = await tools._for_each(
        talk, "Summarise the page", items=INVITES, changes=False
    )

    assert talk.userdata.gate.awaiting() is None
    assert told["item"]["number"] == 1
    # Reading changes nothing, and nothing is asked for.
    told = await tools._item_done(talk, True, "about a team")
    assert told["item"]["number"] == 2


# Reading a list from the browser.


class ListPages:
    """The browser in use, holding a list over two pages."""

    def __init__(self) -> None:
        self.in_use = True
        self.opened: list[str] = []
        self.held: list[str | None] = []
        self.holding = (None, False)
        first = [
            {"name": "Invitation to join team/SE-SEP2026-Team001", "href": _link(1)},
            {"name": "Invitation to join team/SE-SEP2026-Team002", "href": _link(2)},
        ]
        # GitHub links the same invitation from each page with a mark of its own.
        second = [
            {
                "name": "Invitation to join team/SE-SEP2026-Team002",
                "href": _link(2) + "?notification_referrer_id=NT_kwDOB",
            },
            {"name": "Invitation to join team/SE-SEP2026-Team003", "href": _link(3)},
        ]
        self.pages = {
            NOTIFICATIONS: {"items": first, "next": NOTIFICATIONS + "?after=Y3", "url": NOTIFICATIONS},
            NOTIFICATIONS + "?after=Y3": {"items": second, "next": "", "url": NOTIFICATIONS + "?after=Y3"},
        }  # fmt: skip

    def hold(self, what, showing=False):
        self.held.append(what)

    def release(self):
        self.held.append(None)

    async def open_page(self, context, url, settle=""):
        self.opened.append(url)
        return "### Page"

    async def run_script(self, context, function):
        if "(matching)" not in function:
            return 0
        # The links with every one of the words, as the page's script finds them.
        words = json.loads(re.search(r"\)\((\"[^\"]*\")\)$", function)[1]).split()
        page = self.pages[self.opened[-1]]
        return {
            **page,
            "items": [
                link
                for link in page["items"]
                if all(
                    word.lower() in (link["name"] + " " + link["href"]).lower()
                    for word in words
                )
            ],
        }


NOTIFICATIONS = "https://github.com/notifications"


def _link(n: int) -> str:
    return f"https://github.com/team/SE-SEP2026-Team{n:03}/invitations"


async def test_a_list_in_the_browser_is_read_from_every_page():
    browser = ListPages()
    talk = _talk("Accept all my collaborator invites.")
    tools = ListToolset(browser)

    told = await tools._for_each(
        talk, TASK, list_page=NOTIFICATIONS, matching="invitations"
    )

    assert browser.opened == [NOTIFICATIONS, NOTIFICATIONS + "?after=Y3"]
    assert told["items"] == 3
    rows = talk.userdata.gate.awaiting().preview["table"]["rows"]
    assert [row[1] for row in rows] == [
        f"Invitation to join team/SE-SEP2026-Team{n:03} — {_link(n)}" for n in (1, 2, 3)
    ]
    # The browser was the list's while it was read, and is free again.
    assert browser.held == ["reading every page of a list", None]


async def test_a_list_in_the_browser_with_nothing_matching_says_so():
    browser = ListPages()
    talk = _talk("Accept all my collaborator invites.")

    with pytest.raises(ToolError, match=r"No link on .* has all of 'decline'"):
        await ListToolset(browser)._for_each(
            talk, TASK, list_page=NOTIFICATIONS, matching="decline"
        )

    assert talk.userdata.gate.awaiting() is None


# Going on, and stopping.


async def test_when_the_model_goes_quiet_it_is_told_to_carry_on(shown, google):
    talk = _talk("Accept all my collaborator invites.")
    await _started(talk)

    await _listening(talk)

    assert talk.session.told == [
        list_tools.CARRY_ON.format(number=1, total=3, item=INVITES[0], task=TASK)
    ]


async def test_the_model_is_not_hurried_while_the_user_speaks(shown, google):
    talk = _talk("Accept all my collaborator invites.")
    await _started(talk)

    talk.session.emit("agent_state_changed", SimpleNamespace(new_state="listening"))
    _heard(talk, "How many are there?")
    await asyncio.sleep(0.1)
    assert talk.session.told == []

    # Once the model has answered them, the list goes on.
    await _listening(talk)
    assert len(talk.session.told) == 1


async def test_the_users_stop_pauses_the_list_at_once(shown, google):
    talk = _talk("Accept all my collaborator invites.")
    tools, _ = await _started(talk)

    _heard(talk, "Wait, stop that.")
    await _listening(talk)

    assert talk.session.told == []
    assert _only_list().state == store.PAUSED
    with pytest.raises(ToolError, match="The list is paused: 0 of 3 done"):
        await tools._item_done(talk, True, "accepted")
    # Stopping what is already stopped says where it stopped.
    stopped = await tools._for_each_stop(talk)
    assert stopped["stopped"].startswith("Stopped: 0 of 3 done, 3 left")


async def test_a_list_not_carried_on_pauses_and_the_user_is_told(shown, google):
    talk = _talk("Accept all my collaborator invites.")
    await _started(talk)

    for _ in range(len(list_tools.NUDGES) + 1):
        await _listening(talk)

    assert (
        talk.session.told[:3]
        == [list_tools.CARRY_ON.format(number=1, total=3, item=INVITES[0], task=TASK)]
        * 3
    )
    assert talk.session.told[3] == list_tools.STUCK.format(number=1, total=3)
    assert _only_list().state == store.PAUSED
    assert shown[-1]["tag"].startswith("Paused")


async def test_a_question_for_the_user_holds_the_list_until_they_answer(shown, google):
    talk = _talk("Accept all my collaborator invites.")
    tools, _ = await _started(talk)

    await tools._for_each_wait(talk, "Sign in to GitHub?")
    await _listening(talk)
    assert talk.session.told == []

    _heard(talk, "Done, I've signed in.")
    await _listening(talk)
    assert len(talk.session.told) == 1


async def test_a_check_only_the_user_may_do_is_not_hurried(shown, google):
    talk = _talk("Accept all my collaborator invites.")
    await _started(talk)
    _gave(talk, "browser_navigate", "### Page" + list_tools.PERSON_CHECK)

    await _listening(talk)

    assert talk.session.told == []


async def test_failing_items_are_kept_and_five_in_a_row_pause_the_list(shown, google):
    talk = _talk("Accept all my collaborator invites.")
    invites = [
        f"Invitation to join team/T{n} https://github.com/team/T{n}/invitations"
        for n in range(7)
    ]
    tools, _ = await _started(talk, invites)

    for _ in range(4):
        told = await tools._item_done(talk, False, "GitHub asked to sign in")
    assert told["item"]["number"] == 5
    told = await tools._item_done(talk, False, "GitHub asked to sign in")

    assert told == {
        "paused": list_tools.PAUSED_FAILING.format(
            count=5, why="GitHub asked to sign in"
        )
    }
    stopped = _only_list()
    assert stopped.state == store.PAUSED
    assert [item.said for item in stopped.items[:5]] == ["GitHub asked to sign in"] * 5


async def test_a_stopped_list_is_carried_on_in_a_later_conversation(shown, google):
    talk = _talk("Accept all my collaborator invites.")
    tools, _ = await _started(talk)
    note_change(talk)
    await tools._item_done(talk, True, "accepted")

    stopped = await tools._for_each_stop(talk)
    assert stopped["stopped"].startswith("Stopped: 1 of 3 done, 2 left")
    assert lists_note() == list_tools.NOTE.format(task=TASK, done=1, total=3, left=2)

    later = _talk("Carry on accepting the invites.")
    told = await ListToolset()._for_each_continue(later)

    assert told["item"] == {"number": 2, "of": 3, "item": INVITES[1]}
    assert _only_list().state == store.RUNNING


async def test_a_conversation_that_ends_leaves_its_list_paused(shown, google):
    talk = _talk("Accept all my collaborator invites.")
    await _started(talk)

    talk.session.emit("close", SimpleNamespace())

    assert _only_list().state == store.PAUSED
    assert "unfinished" in lists_note()


async def test_a_running_list_must_be_stopped_before_another(shown, google):
    talk = _talk("Accept all my collaborator invites.")
    tools, _ = await _started(talk)

    with pytest.raises(ToolError, match="already being done: 0 of 3"):
        await tools._for_each(talk, TASK, items=INVITES)


# Google actions inside a list.


async def test_the_lists_yes_covers_an_action_that_does_only_what_it_says(
    shown, google
):
    talk = _talk("Accept all the calendar invitations from Asha.")
    events = [f"Standup {day} (event e{day})" for day in ("Mon", "Tue")]
    tools, _ = await _started(talk, events, "Accept the invitation")

    drafted = action_tools._draft(
        talk, "calendar.respond", {"event_id": "eMon"}, {"what": "Answer invitation"}
    )
    assert drafted["next"] == list_tools.COVERED_NEXT.format(
        action_id=drafted["action_id"]
    )
    # No yes of its own: the yes to the list covers it.
    await action_tools.confirm_action(talk, drafted["action_id"])
    told = await tools._item_done(talk, True, "accepted")

    assert google == [("calendar.respond", {"event_id": "eMon"})]
    assert told["item"]["number"] == 2


async def test_a_draft_for_the_item_not_yet_carried_out_is_named(shown, google):
    talk = _talk("Accept all the calendar invitations from Asha.")
    events = [f"Standup {day} (event e{day})" for day in ("Mon", "Tue")]
    tools, _ = await _started(talk, events, "Accept the invitation")
    drafted = action_tools._draft(
        talk, "calendar.respond", {"event_id": "eMon"}, {"what": "Answer invitation"}
    )

    with pytest.raises(ToolError, match="call confirm_action with action_id"):
        await tools._item_done(talk, True, "accepted")

    assert drafted["action_id"] in str(talk.userdata.gate.awaiting().id)


async def test_an_action_drafted_before_the_item_needs_its_own_yes(shown, google):
    talk = _talk("Accept all the calendar invitations from Asha.")
    early = action_tools._draft(
        talk, "calendar.respond", {"event_id": "e0"}, {"what": "Answer invitation"}
    )
    _later(talk)
    events = [f"Standup {day} (event e{day})" for day in ("Mon", "Tue")]
    await _started(talk, events, "Accept the invitation")

    with pytest.raises(ToolError, match="That draft is cancelled"):
        await action_tools.confirm_action(talk, early["action_id"])
    assert google == []


async def test_written_drafts_are_kept_and_sent_together_after_one_yes(shown, google):
    talk = _talk("Reply to each of these recruiters that I'm not looking.")
    mails = [f"Recruiter mail from {name} (message m{name})" for name in ("Ana", "Bo")]
    tools, _ = await _started(talk, mails, "Reply that I'm not looking")

    for name in ("Ana", "Bo"):
        drafted = action_tools._draft(
            talk,
            "gmail.send_reply",
            {"message_id": f"m{name}", "body": "Thanks, not looking."},
            {
                "what": "Reply",
                "to": f"{name}@example.com",
                "body": "Thanks, not looking.",
            },
        )
        assert drafted["next"] == list_tools.KEPT_NEXT
        told = await tools._item_done(talk, True, "drafted")

    # Nothing is sent yet: the drafts are shown together, for one more yes.
    assert google == []
    action = talk.userdata.gate.awaiting()
    assert action.kind == list_tools.SEND
    assert told["next"] == list_tools.KEPT_READY.format(count=2)
    assert told["done"] == "0 of 2 done; 2 drafted, to be sent once the user says yes"
    assert [row[1] for row in action.preview["table"]["rows"]] == [
        "Ana@example.com",
        "Bo@example.com",
    ]

    sent = await action_tools.confirm_action(talk, _yes(talk))

    assert [payload["message_id"] for _, payload in google] == ["mAna", "mBo"]
    assert sent["finished"] == list_tools.FINISHED.format(summary="2 of 2 done")
    assert _only_list().state == store.FINISHED


async def test_a_draft_with_files_goes_with_its_own_yes(shown, google):
    talk = _talk("Send each of them my CV.")
    mails = [f"Recruiter mail from {name} (message m{name})" for name in ("Ana", "Bo")]
    tools, _ = await _started(talk, mails, "Reply with my CV")
    drafted = action_tools._draft(
        talk,
        "gmail.send_reply",
        {"message_id": "mAna", "body": "CV attached.", "attachments": ["a1"]},
        {"what": "Reply", "to": "Ana@example.com"},
    )

    assert drafted["next"] == action_tools.NEXT_STEP
    with pytest.raises(ToolError, match="carries files"):
        await tools._item_done(talk, True, "drafted")


# What counts as a change.


@pytest.fixture
async def browser(tmp_path):
    stub = StubBrowser(tmp_path)
    await stub.toolset.setup()
    yield stub
    await stub.toolset.aclose()


async def test_a_click_or_typing_in_the_browser_counts_as_a_change(browser):
    talk = _talk("Accept the invitation.")
    await tool(browser, "browser_start")(None, show_browser=True)

    await tool(browser, "browser_snapshot")(talk, {})
    assert talk.userdata.changed == 0
    await tool(browser, "browser_click")(talk, {"element": "Accept", "target": "e5"})
    await tool(browser, "browser_type")(
        talk, {"element": "Note", "target": "e6", "text": "hi"}
    )

    assert talk.userdata.changed == 2


async def test_the_steps_of_a_list_are_not_offered_as_a_workflow(
    browser, shown, google
):
    async def accept(talk):
        await tool(browser, "browser_navigate")(
            talk, {"url": "https://github.com/team/x/invitations"}
        )
        return await tool(browser, "browser_click")(
            talk, {"element": "Accept invitation", "target": "e5"}
        )

    await tool(browser, "browser_start")(None, show_browser=True)
    # Done once, the task is offered to be remembered.
    assert workflows.OFFER_TEXT in await accept(_talk("Accept the invite."))

    talk = _talk("Accept all my collaborator invites.")
    await _started(talk)

    # In a list it is not: the offer would stop the list midway.
    assert workflows.OFFER_TEXT not in await accept(talk)


async def test_a_reminder_set_counts_as_a_change(shown):
    talk = _talk("Remind me to call Asha in ten minutes.")

    await reminder_add(talk, "Call Asha", in_minutes=10)

    assert talk.userdata.changed == 1


# What the model is told.


def test_the_model_is_told_to_use_for_each_for_a_whole_list():
    instructions = Assistant().instructions

    assert "use for_each" in instructions
    assert "Never say all of them are done" in instructions


async def test_a_saved_workflow_asked_for_every_item_points_to_for_each():
    workflow_store.save(
        "Accept GitHub collaborator invite",
        ["Go to notifications", "Click accept invitation"],
        [],
        {},
    )
    talk = _talk("Accept all the collaborator invites.")

    ran = await workflows.WorkflowToolset()._run_workflow(
        talk, "Accept GitHub collaborator invite"
    )

    assert ran["every"] == workflows.EVERY
    one = await workflows.WorkflowToolset()._run_workflow(
        _talk("Accept the invite from Asha."), "Accept GitHub collaborator invite"
    )
    assert "every" not in one
