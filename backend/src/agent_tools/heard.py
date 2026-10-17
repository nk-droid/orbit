"""What the user has said in this conversation, for tools that must not take the
model's word for it.

A tool that needs the user's own wish, such as putting a file on an email, asks the
model to pass on what the user said and then looks for those words here.
"""

from livekit.agents import RunContext

from session_memory import EARLIER_ID

# What the user has just said aloud reaches the record of the conversation a moment
# after the model has heard it. Their words are looked for this long, in seconds.
HEARD_WITHIN = 1.6
LOOK_EVERY = 0.2


def said_so_far(context: RunContext) -> list[tuple[str, str]]:
    """What has been said in this conversation, as far as it is on record, in
    order: who said it, "user" or "assistant", and what.

    What was said in an earlier conversation of the session is left out. Words
    that asked for something then do not ask for it now.
    """
    try:
        messages = context.session.history.items
    except (AttributeError, RuntimeError):
        return []
    return [
        (message.role, message.text_content or "")
        for message in messages
        if getattr(message, "role", None) in ("user", "assistant")
        and not str(getattr(message, "id", "")).startswith(EARLIER_ID)
    ]


def said_by_the_user(context: RunContext) -> list[str]:
    """What the user has said in this conversation, as far as it is on record."""
    return [text for who, text in said_so_far(context) if who == "user"]
