from dataclasses import dataclass, field

from actions.gate import ActionGate

from .workflows import Trace


@dataclass
class OrbitState:
    """Everything Orbit keeps for one conversation. Reached in tools as context.userdata."""

    gate: ActionGate
    # How much of what has been said was already counted as asking for files on
    # an email. Words that put files on one draft do not put files on the next.
    asked_up_to: int = 0
    # When the model was last sent to ask the user whether a file should go on an
    # email, on the gate's clock, and how much had been said by then. None if it
    # has not been, or they have answered.
    asked_about_attaching: tuple[float, int] | None = None
    # When the user was last asked to hand over a file from their computer, on the
    # gate's clock, and which files were waiting already. None once they have.
    asked_for_file: tuple[float, frozenset[str]] | None = None
    # The files from the user's computer that the model has been told are waiting.
    told_of_files: frozenset[str] = frozenset()
    # The spreadsheets read in this conversation, by id, each with the tab last read.
    # A cell or a row is only changed in a sheet that has been read, and a change
    # with no tab named is meant for that tab.
    sheet_tabs: dict[str, str] = field(default_factory=dict)
    # The screenshot taken last in this conversation, one picture for each screen,
    # and when. Further questions about it are asked of these.
    screenshot: list[bytes] | None = None
    screenshot_taken: str = ""
    # Which window it is of, as spoken of, or "" for every screen.
    screenshot_of: str = ""
    # What was found out about that screenshot, by the question asked. The model
    # sometimes asks the same twice: the second time is answered from here.
    screenshot_answers: dict[str, str] = field(default_factory=dict)
    # How much of what has been said was already counted as asking for a
    # screenshot, and how much had been said when the model was sent to ask the
    # user whether to take one. None if it has not been, or they have answered.
    looked_up_to: int = 0
    asked_about_screen: int | None = None
    # What was done in the browser, and where the user was offered to have a task
    # remembered as a workflow.
    trace: Trace = field(default_factory=Trace)
