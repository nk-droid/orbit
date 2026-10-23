"""The approval gate: nothing is done in the user's name until they agree to it.

The model drafts an action and, after the user answers, asks for it to be carried
out. This module is the part that does not trust the model: it records what was
drafted, whether the user was actually told about it, and whether they have said
anything since. An action runs once, only for the exact draft that was reviewed.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

# A draft the user has not acted on in this long has to be drafted again.
EXPIRY_SECONDS = 300

Status = Literal[
    "awaiting_approval", "running", "done", "failed", "cancelled", "expired"
]


class GateError(Exception):
    """The action may not proceed. The message tells the model what to do instead."""


class NotDoneError(Exception):
    """An approved action that Orbit carries out itself, such as filling in a form in
    the browser, stopped before it was done. The message says why, for the model to
    tell the user."""


@dataclass
class Action:
    id: str
    kind: str
    # Exactly what will be executed. Fixed when drafted; a change is a new action.
    payload: dict[str, Any]
    # The same facts in the form the user is told.
    preview: dict[str, Any]
    created_at: float
    status: Status = "awaiting_approval"
    # When the user finished hearing the draft. None until then.
    reviewed_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    updated_at: float = field(default=0.0)
    # How long it waits for the user's answer, in seconds.
    expiry: float = EXPIRY_SECONDS


class ActionGate:
    """One per conversation. Holds the drafts and decides when one may run."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        on_change: Callable[[Action], None] | None = None,
    ) -> None:
        self._clock = clock
        self._on_change = on_change
        self._actions: dict[str, Action] = {}
        self._last_user_input_at: float | None = None

    def get(self, action_id: str) -> Action | None:
        return self._actions.get(action_id)

    def awaiting(self) -> Action | None:
        """The draft waiting for the user's answer, if there is one."""
        for action in self._actions.values():
            if action.status == "awaiting_approval":
                return action
        return None

    def _changed(self, action: Action) -> None:
        action.updated_at = self._clock()
        if self._on_change:
            self._on_change(action)

    def _require(self, action_id: str) -> Action:
        action = self._actions.get(action_id)
        if action is None:
            raise GateError("There is no draft with that id. Draft the action again.")
        return action

    def propose(
        self,
        kind: str,
        payload: dict[str, Any],
        preview: dict[str, Any],
        expiry: float = EXPIRY_SECONDS,
    ) -> Action:
        """Records a draft. Any earlier draft still waiting is withdrawn, so there is
        only ever one thing the user's next "yes" could mean. A draft the user takes
        longer to read, such as a job application, may wait longer than most."""
        for other in self._actions.values():
            if other.status == "awaiting_approval":
                other.status = "cancelled"
                self._changed(other)
        action = Action(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            payload=payload,
            preview=preview,
            created_at=self._clock(),
            expiry=expiry,
        )
        self._actions[action.id] = action
        self._changed(action)
        return action

    def mark_reviewed(self, action_id: str) -> None:
        """The agent finished telling the user about this draft, uninterrupted."""
        action = self._actions.get(action_id)
        if action and action.status == "awaiting_approval":
            action.reviewed_at = self._clock()

    def note_agent_spoke(self, began_at: float) -> None:
        """The agent finished saying something, uninterrupted. A draft counts as
        read out only if it already existed when that speech began: the speech that
        made the draft cannot also have read it to the user."""
        for action in list(self._actions.values()):
            if action.reviewed_at is None and action.created_at <= began_at:
                self.mark_reviewed(action.id)

    def note_user_input(self) -> None:
        """The user said or typed something. Called from the session's own events."""
        self._last_user_input_at = self._clock()

    def now(self) -> float:
        """The moment it is, on the clock the gate keeps time by."""
        return self._clock()

    def heard_from_user_since(self, moment: float) -> bool:
        """Whether the user has said or typed anything after that moment."""
        return self._last_user_input_at is not None and (
            self._last_user_input_at > moment
        )

    def approve(self, action_id: str) -> Action:
        """Checks that the draft may run now and reserves it, or raises GateError."""
        action = self._require(action_id)
        if action.status == "done":
            raise GateError("That was already done. Do not do it again.")
        if action.status != "awaiting_approval":
            raise GateError(
                f"That draft is {action.status}. Draft the action again if it is "
                "still wanted."
            )
        if self._clock() - action.created_at > action.expiry:
            action.status = "expired"
            self._changed(action)
            raise GateError("That draft has expired. Draft the action again.")
        if action.reviewed_at is None:
            raise GateError(
                "The user has not been told the full draft yet. Read it to them, "
                "ask whether to go ahead, and wait for their answer."
            )
        if (
            self._last_user_input_at is None
            or self._last_user_input_at <= action.reviewed_at
        ):
            raise GateError(
                "The user has not answered since hearing the draft. Ask whether to "
                "go ahead and wait for their answer."
            )
        # Reserved before the effect starts, so a repeated call cannot run it twice.
        action.status = "running"
        self._changed(action)
        return action

    def complete(self, action_id: str, result: dict[str, Any]) -> None:
        action = self._require(action_id)
        action.status = "done"
        action.result = result
        self._changed(action)

    def fail(self, action_id: str, error: str) -> None:
        action = self._require(action_id)
        action.status = "failed"
        action.error = error
        self._changed(action)

    def set_aside(self, action_id: str, result: dict[str, Any]) -> None:
        """An approved draft was not carried out but put somewhere for the user to
        finish themselves. It is withdrawn, and `result` says where it is now."""
        action = self._require(action_id)
        action.status = "cancelled"
        action.result = result
        self._changed(action)

    def cancel(self, action_id: str) -> Action:
        action = self._require(action_id)
        if action.status != "awaiting_approval":
            raise GateError(f"That draft is {action.status} and cannot be cancelled.")
        action.status = "cancelled"
        self._changed(action)
        return action
