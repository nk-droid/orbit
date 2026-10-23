"""Tasks the user asked Orbit to remember, as workflows: offering to remember one,
the user's yes, what is kept, and doing one again.

A workflow keeps how a task is done, never what it was done for. Its steps are
written with a {name} where each part that changes goes, such as the song or the
message, and those come from what the user asks for next time, or Orbit asks.
The only values kept are those the user calls theirs, such as the address they
call "my test email". What can be decided in code is decided here:

- What the user asked for the first time must be nowhere in what is saved, its
  name included. A workflow named after "work music" was run for "work" again
  when the user only asked for some music.
- Only what the user called theirs ("my ...") is kept, and only a value that
  came up in the conversation, from them or from a tool.
- In a task in the browser, each part asked for has to be the user's own words
  this time: it goes into an address and is acted on, with nothing read back. In
  any other task the model writes it from what they said, and whatever sends or
  changes something is drafted and read back first, as always.

The model never offered to remember a task of its own accord, so the offer is
made in code: once the browser has opened a page and done something on it, and
once an action is carried out for something the user calls theirs. The tool that
did it tells the model to offer when it has finished. That happens once in a
conversation, and not in one where a workflow was run. Browser steps count for
the whole conversation: what the user says reaches the record too late to tell
where a request began, and it landed between the first two steps of the very
task to be offered.

A workflow is saved only when the user wants it: their own words ask for it, or
Orbit asked them and their answer did not turn it down. Like the checks on
attaching files, these are checks of words, and can be wrong both ways. Saving
tells the user what stays the same and what is asked for each time, so that a
wrong guess is heard at once.
"""

import asyncio
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, quote_plus, unquote_plus, urlparse

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError, Toolset

import writing
from session_memory import EARLIER_ID
from workflows import store
from workflows.store import Workflow

from . import stage
from .heard import (
    HEARD_WITHIN,
    LOOK_EVERY,
    latest_request,
    said_by_the_user,
    said_so_far,
)

if TYPE_CHECKING:
    from .browser import BrowserToolset

# A task in the browser is offered to be remembered once it took this many steps,
# one opening a page and one doing something on it. Every answer shows the page,
# so playing a song can take two: opening the search results, clicking the video.
OFFER_AFTER = 2
_ACTING = frozenset(
    {"browser_click", "browser_type", "browser_press_key", "browser_select_option"}
)
# Actions that answer one message or one invitation: not done again.
_ANSWERING = frozenset({"gmail.send_reply", "calendar.respond"})
# The tools that remember, which are not part of any task.
_REMEMBERING = frozenset({"run_workflow", "save_workflow", "forget_workflow"})
MAX_STEPS = 10
MAX_STEP_CHARS = 300
MAX_NAME_CHARS = 80
# Where a part goes in a workflow's steps and address: {song}, {test email}.
_PART = re.compile(r"\{([^{}]{1,40})\}")

OFFER_TEXT = (
    "Once you have done what the user asked, ask them in a few words whether to "
    "remember how you did it, so that next time it takes one step. If they say "
    "yes, call save_workflow."
)
OFFER = f"\n\n[{OFFER_TEXT}]"
NOT_AGREED = (
    "The user has not said they want this remembered. Ask them whether to "
    "remember how you did it for next time, and call save_workflow again only if "
    "they say yes."
)
NOTHING_DONE = (
    "Nothing has been done in this conversation, so there is nothing to remember."
)
NO_NAME = (
    "Name the workflow in a few words that fit any time it is done, such as "
    '"play music on YouTube".'
)
NO_STEPS = (
    "Write what to do as steps, or give the address a task in the browser begins at."
)
TOO_MANY_STEPS = f"Write the steps in at most {MAX_STEPS} short instructions."
NOT_WRITTEN = 'Write each part as "name: value", such as "song: Hotel California".'
NOT_LISTED = (
    "The steps have {parts} in them, which {is_are} not in asks or keeps. Put each "
    "part that changes in asks, and each that stays the same in keeps."
)
NOT_USED = "Write {parts} in the steps or start_url, where {it_they} {goes_go}."
BOTH = '"{part}" is in both asks and keeps: a part changes each time or stays the same.'
NO_VALUE = 'Give the value kept for "{part}".'
NOT_THEIRS = (
    'Only what the user calls theirs, such as "my test email", stays the same, and '
    'they have not called "{part}" theirs. Name it as they do, or put it in asks, '
    "to be taken from what they ask for each time."
)
NOT_FROM_HERE = (
    'What is kept for "{part}" did not come up in this conversation. Keep only '
    "what the user said, or what a tool gave, in this conversation."
)
STILL_IN = (
    'Write {{{part}}} in place of "{value}" wherever it appears, the name '
    "included, so that the workflow fits whatever the user asks for next time."
)
NOT_VISITED = (
    "A task in the browser begins on a page you opened in this conversation, and "
    "that address is not on any of the sites you opened: {sites}. Give the "
    "address it began at."
)
NO_WORKFLOWS = "The user has no saved workflows."
NO_SUCH_WORKFLOW = (
    "No saved workflow is called {asked!r}. The saved ones are: {names}. Ask the "
    "user which one they mean."
)
SEVERAL_WORKFLOWS = "{asked!r} fits several saved workflows: {names}. Ask which one."
NEEDS = (
    "This workflow needs {parts}. Take {it_them} from what the user asked for now, "
    "or ask them, then run it again."
)
NOT_SAID_NOW = (
    'The user did not ask for "{words}" this time. Run it with what they asked for '
    "now, in their words, or ask them what they want."
)
NO_BROWSER_HERE = (
    "That workflow is done in the browser, and there is no browser in this "
    "conversation."
)
NEXT = (
    "Do steps_left now, in order, with your tools. If they no longer fit, do what "
    "the user asked another way."
)
DONE = "The page is open, and the workflow has no other steps."
# A workflow done once is one item. Asked for every one, the model ran it once.
EVERY = (
    "If the user asked for this for every item of a list, gather the items and do "
    "it for each with for_each, rather than running this once for one item."
)
_EVERY = re.compile(r"\b(all|every|each)\b", re.IGNORECASE)
SAVED = (
    "Tell the user in a sentence what stays the same and what you will take from "
    "what they ask for each time."
)
# What the model is told when the conversation starts, if workflows are saved.
WORKFLOWS = """\
The user asked you to remember how to do these tasks: {listing}. When they ask for one, call run_workflow straight away, with what they ask for this time and never what they asked for before."""

_WORD = re.compile(r"[a-z0-9']+")
# Words that ask for something to be kept, and words of a task's being done, which
# together ask for the task to be remembered: "remember how you did that", or
# "save it as a workflow".
_KEEPING = frozenset(
    {
        "remember", "remembering", "save", "saving", "keep", "store", "memorise",
        "memorize",
    }
)  # fmt: skip
_TASK = frozenset(
    {"how", "way", "steps", "workflow", "shortcut", "routine", "next", "again"}
)
# Words that turn it down.
_REFUSING = frozenset({"no", "nope", "nah", "not", "don't", "dont", "never", "cancel"})
# Words with which the user calls something theirs.
_THEIRS = frozenset({"my", "our"})
# Words a search may have that do not change what it is for.
_FILLER = frozenset(
    {
        "a", "an", "the", "some", "any", "by", "of", "and", "or", "for", "to", "in",
        "on", "at", "with", "from", "my", "our", "me", "please", "this", "that",
        "it",
    }
)  # fmt: skip


def _plain(text: str) -> str:
    """Text without accents, which a transcript may or may not write."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _word_list(text: str) -> list[str]:
    return _WORD.findall(_plain(text.casefold().replace("\u2019", "'")))


def _words(text: str) -> set[str]:
    return set(_word_list(text))


def _part(name: str) -> str:
    """A part's name as it is compared: "My Test Email" is "test email"."""
    name = " ".join(name.casefold().split())
    return re.sub(r"^(the|a|an|my|our|your) ", "", name)


def _listed(names: list[str]) -> str:
    quoted = [f'"{name}"' for name in names]
    if len(quoted) == 1:
        return quoted[0]
    return f"{', '.join(quoted[:-1])} and {quoted[-1]}"


def asks_to_remember(text: str) -> bool:
    """Whether these words of the user's ask for how a task was done to be
    remembered."""
    words = _words(text)
    return bool(words & _KEEPING and words & _TASK and not words & _REFUSING)


def site(url: str) -> str:
    """The site an address is on, such as youtube.com for www.youtube.com."""
    return urlparse(url).netloc.casefold().removeprefix("www.")


def _mentions(text: str, value: str) -> bool:
    """Whether the value is written in the text, in whole words."""
    whole = rf"(?<![a-z0-9]){re.escape(value.casefold())}(?![a-z0-9])"
    return re.search(whole, unquote_plus(text).casefold()) is not None


def parts_in(text: str) -> set[str]:
    """The parts a step or an address has a place for."""
    return {_part(name) for name in _PART.findall(text)}


def fill(text: str, values: dict[str, str], *, address: bool = False) -> str:
    """Text with each part's value in its place. In an address, a value is written
    as part of a query where it stands in one, and as part of a path otherwise,
    unless it is the whole address."""
    whole = _PART.fullmatch(text.strip())
    if address and whole and _part(whole.group(1)) in values:
        return values[_part(whole.group(1))]

    def put(found: re.Match) -> str:
        value = values.get(_part(found.group(1)))
        if value is None:
            return found.group(0)
        if not address:
            return value
        before = text[: found.start()]
        if "?" in before or "#" in before:
            return quote_plus(value)
        return quote(value, safe="")

    return _PART.sub(put, text)


def begins_on(workflow: Workflow) -> str:
    """The site a task in the browser begins on. Empty for any other task."""
    if not workflow.start_url:
        return ""
    anything = dict.fromkeys(workflow.asks, "x")
    return site(fill(workflow.start_url, {**anything, **workflow.keeps}, address=True))


def parts(written: list[str]) -> dict[str, str]:
    """Parts written as "name: value", by name."""
    found: dict[str, str] = {}
    for item in written:
        name, colon, value = str(item).partition(":")
        if not colon or not _part(name) or _part(name) in ("http", "https"):
            raise ToolError(NOT_WRITTEN)
        found[_part(name)] = " ".join(value.split())
    return found


@dataclass(frozen=True)
class Done:
    """One step done in the browser."""

    tool: str
    # The page the step was on, or opened.
    page: str
    # Whether the step opened that page.
    opened: bool
    shown: bool
    profile: str | None


class Trace:
    """What was done in the browser in one conversation, and where the user was
    offered to have a task remembered. Kept with the conversation's state."""

    def __init__(self) -> None:
        self.done: list[Done] = []
        # How much had been said when the model was first told to offer. None if
        # it has not been.
        self._offered_at: int | None = None
        # A saved workflow was run in this conversation.
        self._ran_workflow = False
        # A task was done for every item of a list in this conversation.
        self._listing = False
        # How much had been said when the model was told to offer, or sent to ask:
        # what the user says after that answers it. None if it has not been.
        self.asked_from: int | None = None
        # How much had been said when a workflow was last saved. Words that saved
        # one do not save the next.
        self.saved_up_to = 0

    def note(
        self, tool: str, page: str, opened: bool, shown: bool, profile: str | None
    ) -> None:
        """Keeps a step that was just done in the browser."""
        self.done.append(Done(tool, page, opened, shown, profile))

    def ran_workflow(self) -> None:
        """A saved workflow is being run: this conversation is not offered."""
        self._ran_workflow = True

    def doing_list(self) -> None:
        """A task is being done for every item of a list: this conversation is not
        offered. Its steps are the list's, and an offer would stop it midway."""
        self._listing = True

    def offer(self, said: list[tuple[str, str]]) -> str:
        """What to add to a browser step's answer to have the model offer to
        remember the task, once it is worth remembering. Empty otherwise.

        `said` is what has been said so far, by "user" or "assistant". Once
        offered, each further step carries it until the model has spoken, since
        it asks only when it has finished.
        """
        if self._listing:
            return ""
        if self._offered_at is not None:
            spoken = any(who == "assistant" for who, _ in said[self._offered_at :])
            return "" if spoken else OFFER
        if self._ran_workflow or len(self.done) < OFFER_AFTER:
            return ""
        if not any(step.opened for step in self.done):
            return ""
        if not any(step.tool in _ACTING for step in self.done):
            return ""
        # Not on a site a saved workflow is for: that one is remembered already.
        last = next((step.page for step in reversed(self.done) if step.page), "")
        saved = {begins_on(workflow) for workflow in store.listed()}
        if site(last) in saved:
            return ""
        self._offered_at = self.asked_from = len(said)
        return OFFER

    def offer_after(self, kind: str, said: list[tuple[str, str]]) -> str:
        """What to tell the model after an action of this kind was carried out, to
        have it offer to remember the task: only when the user asked for it with
        something they call theirs, as in "email my test address". Empty
        otherwise."""
        if (
            self._listing
            or self._offered_at is not None
            or self._ran_workflow
            or kind in _ANSWERING
        ):
            return ""
        if not any(who == "user" and _words(text) & _THEIRS for who, text in said):
            return ""
        self._offered_at = self.asked_from = len(said)
        return OFFER_TEXT

    def sites(self) -> list[str]:
        """The sites the browser was on in this conversation, in the order first
        seen."""
        found = [site(done.page) for done in self.done if done.page]
        return list(dict.fromkeys(name for name in found if name))

    def last_on(self, name: str) -> Done | None:
        """The latest step done on a site."""
        steps = (done for done in reversed(self.done) if done.page)
        return next((done for done in steps if name and site(done.page) == name), None)


def trace_of(context: Any) -> Trace | None:
    """The trace kept with a conversation's state. None outside a conversation."""
    return getattr(getattr(context, "userdata", None), "trace", None)


def agreed(said: list[tuple[str, str]], trace: Trace) -> bool:
    """Whether the user wants the task remembered: they asked for it themselves, or
    Orbit asked them and their latest answer did not turn it down.

    `said` is what has been said in this conversation, by "user" or "assistant".
    """
    fresh = said[trace.saved_up_to :]
    if any(who == "user" and asks_to_remember(text) for who, text in fresh):
        return True
    if trace.asked_from is None:
        return False
    asked, answer = False, False
    for who, text in said[max(trace.asked_from, trace.saved_up_to) :]:
        words = _words(text)
        if who == "assistant":
            # What Orbit says of anything else is not this question.
            asked = asked or bool(words & (_KEEPING | {"workflow"}))
        elif asked and text.strip():
            answer = not words & _REFUSING
    return answer


def done_here(context: Any) -> tuple[list[str], list[str]]:
    """The tools called in this conversation, and every value they were given or
    gave back, as text."""
    try:
        items = context.session.history.items
    except (AttributeError, RuntimeError):
        return [], []
    called: list[str] = []
    values: list[str] = []
    for item in items:
        if str(getattr(item, "id", "")).startswith(EARLIER_ID):
            continue
        kind, name = getattr(item, "type", None), getattr(item, "name", "")
        if name in _REMEMBERING:
            continue
        if kind == "function_call":
            called.append(name)
            values.extend(_leaves(item.arguments))
        elif kind == "function_call_output":
            values.append(str(item.output))
    return called, values


def _leaves(arguments: str) -> list[str]:
    """Every value in a tool call's arguments, as text."""
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return [str(arguments)]
    found: list[str] = []
    waiting = [parsed]
    while waiting:
        value = waiting.pop()
        if isinstance(value, dict):
            waiting.extend(value.values())
        elif isinstance(value, list):
            waiting.extend(value)
        elif value is not None:
            found.append(str(value))
    return found


def _called_theirs(part: str, said: list[str]) -> bool:
    """Whether the user called this theirs, as in "my test email" for "test email"."""
    named = set(_part(part).split())
    for text in said:
        words = _word_list(text)
        for at, word in enumerate(words):
            if word in _THEIRS and named & set(words[at + 1 : at + 4]):
                return True
    return False


def _came_up(value: str, said: list[str], given: list[str]) -> bool:
    """Whether a value came up in this conversation: a tool was given it or gave it,
    or the user said it."""
    meant = _plain(value.casefold())
    if any(meant in _plain(text.casefold()) for text in given):
        return True
    words = _words(value)
    return bool(words) and any(words <= _words(text) for text in said)


@dataclass(frozen=True)
class Recipe:
    """A workflow as it is to be saved."""

    name: str
    steps: list[str]
    asks: list[str]
    keeps: dict[str, str]
    start_url: str
    # The latest step on the site a task in the browser begins on. None for any
    # other task.
    began: Done | None


def checked(
    trace: Trace,
    said: list[tuple[str, str]],
    done: tuple[list[str], list[str]],
    name: str,
    steps: list[str],
    asks: list[str],
    keeps: list[str],
    start_url: str,
) -> Recipe:
    """A workflow to save, made sure of. Raises ToolError for one that cannot be.

    `said` is what has been said in this conversation and `done` what done_here()
    found: the tools called, and the values they were given or gave back."""
    called, given = done
    if not called and not trace.done:
        raise ToolError(NOTHING_DONE)
    name = " ".join(name.split())
    if not name or len(name) > MAX_NAME_CHARS:
        raise ToolError(NO_NAME)
    start_url = start_url.strip()
    steps = [" ".join(str(step).split())[:MAX_STEP_CHARS] for step in steps]
    steps = [step for step in steps if step]
    if not steps and not start_url:
        raise ToolError(NO_STEPS)
    if len(steps) > MAX_STEPS:
        raise ToolError(TOO_MANY_STEPS)
    asked, kept = parts(asks), parts(keeps)
    if both := sorted(set(asked) & set(kept)):
        raise ToolError(BOTH.format(part=both[0]))
    placed = set().union(*(parts_in(text) for text in [start_url, *steps]))
    if unlisted := sorted(placed - set(asked) - set(kept)):
        names = ", ".join(f"{{{part}}}" for part in unlisted)
        is_are = "is" if len(unlisted) == 1 else "are"
        raise ToolError(NOT_LISTED.format(parts=names, is_are=is_are))
    if unused := sorted((set(asked) | set(kept)) - placed):
        names = ", ".join(f"{{{part}}}" for part in unused)
        one_part = len(unused) == 1
        raise ToolError(
            NOT_USED.format(
                parts=names,
                it_they="it" if one_part else "they",
                goes_go="goes" if one_part else "go",
            )
        )
    theirs = [text for who, text in said if who == "user"]
    for part, value in kept.items():
        if not value:
            raise ToolError(NO_VALUE.format(part=part))
        if not _called_theirs(part, theirs):
            raise ToolError(NOT_THEIRS.format(part=part))
        if not _came_up(value, theirs, given):
            raise ToolError(NOT_FROM_HERE.format(part=part))
    written = [name, start_url, *steps, *kept.values()]
    for part, value in asked.items():
        if value and any(_mentions(text, value) for text in written):
            raise ToolError(STILL_IN.format(part=part, value=value))
    began = None
    if start_url:
        anything = dict.fromkeys(asked, "x")
        began = trace.last_on(site(fill(start_url, {**anything, **kept}, address=True)))
        if began is None:
            sites = ", ".join(trace.sites()) or "none"
            raise ToolError(NOT_VISITED.format(sites=sites))
    return Recipe(name, steps, list(asked), kept, start_url, began)


def one(asked: str) -> Workflow:
    """The saved workflow a name means. Raises ToolError when it is not clear."""
    found = store.matching(asked)
    if len(found) == 1:
        return found[0]
    if found:
        names = ", ".join(workflow.name for workflow in found)
        raise ToolError(SEVERAL_WORKFLOWS.format(asked=asked, names=names))
    saved = store.listed()
    if not saved:
        raise ToolError(NO_WORKFLOWS)
    names = ", ".join(workflow.name for workflow in saved)
    raise ToolError(NO_SUCH_WORKFLOW.format(asked=asked, names=names))


def given_for(workflow: Workflow, written: list[str]) -> dict[str, str]:
    """What a workflow is run with, by part. A value written without its name is
    for the one part a workflow asks for."""
    given: dict[str, str] = {}
    for item in written:
        name, colon, value = str(item).partition(":")
        named = bool(colon) and _part(name) not in ("http", "https")
        if named and _part(name) in workflow.asks:
            given[_part(name)] = " ".join(value.split())
        elif len(workflow.asks) == 1:
            # Under another name, or none: still the one part it asks for.
            given[workflow.asks[0]] = " ".join((value if named else str(item)).split())
        else:
            raise ToolError(NOT_WRITTEN)
    return given


async def _check_said_now(context: Any, values: list[str]) -> None:
    """Lets values into a task in the browser only if they are the user's own words
    in this conversation. What they said may not be on record yet, so it is given
    a moment to get there."""
    waited = 0.0
    while True:
        theirs = set().union(*(_words(text) for text in said_by_the_user(context)))
        missing = [
            word
            for value in values
            for word in _word_list(value)
            if word not in _FILLER and word not in theirs
        ]
        if not missing:
            return
        if waited >= HEARD_WITHIN:
            break
        await asyncio.sleep(LOOK_EVERY)
        waited += LOOK_EVERY
    raise ToolError(NOT_SAID_NOW.format(words=" ".join(dict.fromkeys(missing))))


async def _check_agreed(context: Any, trace: Trace) -> None:
    """Lets a workflow be saved only when the user wants it. What they said may not
    be on record yet, so it is given a moment to get there. Failing that, the
    model is sent to ask them, and what they say next is their answer."""
    waited = 0.0
    while True:
        said = said_so_far(context)
        if agreed(said, trace):
            # The words that saved this workflow do not also save the next.
            trace.saved_up_to, trace.asked_from = len(said), None
            return
        if waited >= HEARD_WITHIN:
            break
        await asyncio.sleep(LOOK_EVERY)
        waited += LOOK_EVERY
    trace.asked_from = len(said)
    raise ToolError(NOT_AGREED)


def _card(workflow: Workflow) -> dict[str, Any]:
    """A card for a workflow just saved: what is done, what is asked for each time,
    and what stays the same."""
    named = {part: f"*{part}*" for part in [*workflow.asks, *workflow.keeps]}
    lines = []
    if workflow.start_url:
        how = "a browser you can see" if workflow.shown else "a hidden browser"
        if workflow.profile:
            how += f", in your {workflow.profile} profile"
        then = ", then:" if workflow.steps else "."
        lines.append(f"Opens **{begins_on(workflow)}** in {how}{then}\n")
    lines += [
        f"{number}. {fill(step, named)}"
        for number, step in enumerate(workflow.steps, 1)
    ]
    if workflow.asks:
        lines.append(f"\nEach time, from what you ask for: {', '.join(workflow.asks)}.")
    if workflow.keeps:
        kept = "; ".join(f"{part}: {value}" for part, value in workflow.keeps.items())
        lines.append(f"\nStays the same: {kept}.")
    return {
        "id": f"workflow:{workflow.name.casefold()}",
        "kind": "note",
        "title": f"Remembered: {workflow.name}",
        "html": writing.to_html("\n".join(lines), "markdown") or "",
    }


def listing() -> str:
    """What the model is told of the saved workflows, or nothing if there are none."""
    saved = store.listed()
    if not saved:
        return ""
    each = []
    for workflow in saved:
        about = []
        if workflow.asks:
            about.append(f"asks for {', '.join(workflow.asks)}")
        if workflow.keeps:
            kept = ", ".join(f"{part} = {v}" for part, v in workflow.keeps.items())
            about.append(f"keeps {kept}")
        each.append(f'"{workflow.name}"' + (f" ({'; '.join(about)})" if about else ""))
    return WORKFLOWS.format(listing="; ".join(each))


class WorkflowToolset(Toolset):
    """Remembering how a task was done, doing it again, and forgetting it. A task
    in the browser is done again in `browser`."""

    def __init__(self, browser: "BrowserToolset | None" = None) -> None:
        super().__init__(id="workflows")
        self._browser = browser
        self._tools = [
            function_tool(
                self._run_workflow,
                name="run_workflow",
                description=(
                    "Do a task the user asked you to remember, with what they ask "
                    "for this time. A task in the browser opens the browser the way "
                    "it was saved, without browser_start."
                ),
            ),
            function_tool(
                self._save_workflow,
                name="save_workflow",
                description=(
                    "Remember how you did a task, so that next time run_workflow "
                    "does it in one step. Call it only when the user has said yes "
                    "to that, or asked for it."
                ),
            ),
            function_tool(
                self._forget_workflow,
                name="forget_workflow",
                description="Forget a task the user asked you to remember.",
            ),
        ]

    @property
    def rules(self) -> str:
        """What the model is told of the saved workflows when the conversation
        starts. Empty when there are none."""
        return listing()

    async def _run_workflow(
        self, context: RunContext, name: str, values: list[str] | None = None
    ) -> dict[str, Any]:
        """
        Args:
            name: The workflow's name, as listed.
            values: What it asks for, from what the user asked for now, each as
                "name: value", such as "song: Bohemian Rhapsody".
        """
        workflow = one(name)
        given = given_for(workflow, values or [])
        if missing := [part for part in workflow.asks if not given.get(part)]:
            it_them = "it" if len(missing) == 1 else "them"
            raise ToolError(NEEDS.format(parts=_listed(missing), it_them=it_them))
        if workflow.start_url:
            if self._browser is None:
                raise ToolError(NO_BROWSER_HERE)
            await _check_said_now(context, [given[part] for part in workflow.asks])
        if (trace := trace_of(context)) is not None:
            trace.ran_workflow()
        filled = {**workflow.keeps, **given}
        steps = [fill(step, filled) for step in workflow.steps]
        ran: dict[str, Any] = {
            "workflow": workflow.name,
            "steps_left": steps,
            "next": NEXT if steps else DONE,
        }
        if _EVERY.search(" ".join(latest_request(said_so_far(context)))):
            ran["every"] = EVERY
        if workflow.start_url and self._browser is not None:
            browser, page = await self._browser.open_saved(
                context,
                fill(workflow.start_url, filled, address=True),
                bool(workflow.shown),
                workflow.profile,
            )
            ran.update(browser=browser, page=page)
        store.used(workflow.id)
        return ran

    async def _save_workflow(
        self,
        context: RunContext,
        name: str,
        steps: list[str],
        asks: list[str] | None = None,
        keeps: list[str] | None = None,
        start_url: str = "",
    ) -> dict[str, Any]:
        """
        Args:
            name: What the task is, in words that fit any time it is done, such as
                "play music on YouTube". Never what the user asked for this time.
            steps: What to do, in order, one short instruction each, with {name}
                where a part goes, such as "Draft an email to {test email}
                saying {message}".
            asks: The parts that change, to be taken from what the user asks for
                each time, each as "name: what it was this time", such as
                "message: Running late".
            keeps: The parts that stay the same, only ones the user calls theirs,
                each as "name: value", such as "test email: me@example.com".
            start_url: For a task in the browser, the address it began at, with
                {name} where a part goes.
        """
        trace = trace_of(context) or Trace()
        recipe = checked(
            trace,
            said_so_far(context),
            done_here(context),
            name,
            steps,
            asks or [],
            keeps or [],
            start_url,
        )
        await _check_agreed(context, trace)
        replaced = any(
            w.name.casefold() == recipe.name.casefold() for w in store.listed()
        )
        began = recipe.began
        workflow = store.save(
            recipe.name,
            recipe.steps,
            recipe.asks,
            recipe.keeps,
            recipe.start_url,
            began.shown if began else None,
            began.profile if began else None,
        )
        await stage.show(_card(workflow))
        saved: dict[str, Any] = {
            "saved": workflow.name,
            "asked_for_each_time": workflow.asks,
            "stays_the_same": workflow.keeps,
            "next": SAVED,
        }
        if began:
            saved["browser"] = {
                "shown": began.shown,
                "profile": began.profile or "none",
            }
        if replaced:
            saved["replaced"] = "the workflow saved before under that name"
        return saved

    async def _forget_workflow(self, context: RunContext, name: str) -> dict[str, Any]:
        """
        Args:
            name: The workflow's name, as listed.
        """
        workflow = one(name)
        store.remove(workflow.id)
        return {"forgotten": workflow.name}
