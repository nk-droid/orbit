import base64
from email import message_from_bytes

import pytest

from actions.gate import EXPIRY_SECONDS, ActionGate, GateError
from actions.store import recent, save
from integrations.google.gmail import build_raw


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float = 1.0) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def gate(clock: Clock) -> ActionGate:
    return ActionGate(clock=clock)


def draft(gate: ActionGate):
    return gate.propose("gmail.send_email", {"to": ["a@example.com"]}, {"what": "x"})


def reviewed_and_answered(gate: ActionGate, clock: Clock):
    action = draft(gate)
    clock.tick()
    gate.mark_reviewed(action.id)
    clock.tick()
    gate.note_user_input()
    return action


def test_a_reviewed_draft_runs_after_the_user_answers(gate, clock):
    action = reviewed_and_answered(gate, clock)

    assert gate.approve(action.id) is action
    assert action.status == "running"


def test_a_draft_cannot_run_before_the_user_has_heard_it(gate, clock):
    action = draft(gate)
    clock.tick()
    gate.note_user_input()

    with pytest.raises(GateError, match="not been told"):
        gate.approve(action.id)
    assert action.status == "awaiting_approval"


def test_the_speech_that_drafted_an_action_does_not_count_as_reading_it_out(
    gate, clock
):
    speech_began = clock.now
    clock.tick()
    action = draft(gate)
    clock.tick()
    gate.note_agent_spoke(speech_began)
    clock.tick()
    # The user's own request, transcribed late, arrives after the tool call.
    gate.note_user_input()

    with pytest.raises(GateError, match="not been told"):
        gate.approve(action.id)


def test_a_draft_is_heard_once_later_speech_finishes(gate, clock):
    action = draft(gate)
    clock.tick()
    read_back_began = clock.now
    clock.tick()
    gate.note_agent_spoke(read_back_began)

    with pytest.raises(GateError, match="not answered"):
        gate.approve(action.id)

    clock.tick()
    gate.note_user_input()

    assert gate.approve(action.id) is action


def test_what_the_user_said_before_the_review_is_not_an_answer(gate, clock):
    gate.note_user_input()
    clock.tick()
    action = draft(gate)
    clock.tick()
    gate.mark_reviewed(action.id)

    with pytest.raises(GateError, match="has not answered"):
        gate.approve(action.id)


def test_an_action_runs_only_once(gate, clock):
    action = reviewed_and_answered(gate, clock)
    gate.approve(action.id)
    gate.complete(action.id, {"id": "sent"})

    with pytest.raises(GateError, match="already done"):
        gate.approve(action.id)


def test_a_second_approval_while_running_is_refused(gate, clock):
    action = reviewed_and_answered(gate, clock)
    gate.approve(action.id)

    with pytest.raises(GateError, match="running"):
        gate.approve(action.id)


def test_a_stale_draft_expires(gate, clock):
    action = reviewed_and_answered(gate, clock)
    clock.tick(EXPIRY_SECONDS)

    with pytest.raises(GateError, match="expired"):
        gate.approve(action.id)
    assert action.status == "expired"


def test_a_new_draft_withdraws_the_one_before(gate, clock):
    first = reviewed_and_answered(gate, clock)
    second = draft(gate)

    assert first.status == "cancelled"
    with pytest.raises(GateError, match="cancelled"):
        gate.approve(first.id)
    # The earlier "yes" was for the first draft; it does not carry over.
    with pytest.raises(GateError, match="not been told"):
        gate.approve(second.id)


def test_a_cancelled_draft_cannot_run(gate, clock):
    action = reviewed_and_answered(gate, clock)
    gate.cancel(action.id)

    with pytest.raises(GateError, match="cancelled"):
        gate.approve(action.id)


def test_a_failed_action_is_not_reported_as_done(gate, clock):
    action = reviewed_and_answered(gate, clock)
    gate.approve(action.id)
    gate.fail(action.id, "403")

    assert action.status == "failed"
    with pytest.raises(GateError, match="failed"):
        gate.approve(action.id)


def test_an_unknown_id_is_refused(gate):
    with pytest.raises(GateError, match="no draft"):
        gate.approve("nope")


def test_every_change_is_logged_for_the_app(tmp_path, monkeypatch, clock):
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path))
    gate = ActionGate(clock=clock, on_change=save)

    action = reviewed_and_answered(gate, clock)
    gate.approve(action.id)
    gate.complete(action.id, {"id": "sent"})

    [record] = recent()
    assert record["id"] == action.id
    assert record["status"] == "done"
    assert record["preview"] == {"what": "x"}


def test_a_reply_is_addressed_and_threaded():
    raw = build_raw(
        ["asha@example.com"],
        "Re: Q3 invoice",
        "Thanks, received.",
        in_reply_to="<abc@mail.example.com>",
        references="<root@mail.example.com> <abc@mail.example.com>",
    )

    email = message_from_bytes(base64.urlsafe_b64decode(raw))
    assert email["To"] == "asha@example.com"
    assert email["Subject"] == "Re: Q3 invoice"
    assert email["In-Reply-To"] == "<abc@mail.example.com>"
    assert email["References"].endswith("<abc@mail.example.com>")
    assert email.get_payload().strip() == "Thanks, received."


def test_the_gate_tells_whether_the_user_has_spoken_since_a_moment():
    moment = [10.0]
    gate = ActionGate(clock=lambda: moment[0])
    asked_at = gate.now()

    assert not gate.heard_from_user_since(asked_at)
    gate.note_user_input()
    # At the very moment of asking does not count as an answer.
    assert not gate.heard_from_user_since(asked_at)
    moment[0] = 12.0
    gate.note_user_input()

    assert gate.heard_from_user_since(asked_at)
    assert not gate.heard_from_user_since(12.0)
