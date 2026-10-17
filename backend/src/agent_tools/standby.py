"""Going to standby: ending the conversation when the user says they are done.

This is LiveKit's prebuilt end-call tool. Once the model calls it, it says a last
few words, then the session closes and the room is deleted. Deleting the room
disconnects the app, which is what puts it back in standby.
See https://docs.livekit.io/agents/prebuilt/tools/end-call-tool/
"""

from livekit.agents.beta.tools import EndCallTool
from livekit.agents.llm import Toolset

from . import stage

# Added to the tool's own description, which speaks of calls and of not ending one
# when asked to pause.
WHEN = """\
For Orbit, ending the call is called going to standby: the conversation ends and
you stop listening until the user presses the orb again. Call this when the user
tells you to go to standby, go to sleep or stop listening, or says goodbye or that
they are done for now. A thank-you on its own is not a reason to call it."""

LAST_WORDS = "Say in a few words that you are standing by."


async def _tell_app(event: Toolset.ToolCalledEvent) -> None:
    await stage.standing_by()


def standby_tool() -> EndCallTool:
    """One per agent: it keeps track of the conversation it is ending."""
    return EndCallTool(
        extra_description=WHEN, end_instructions=LAST_WORDS, on_tool_called=_tell_app
    )
