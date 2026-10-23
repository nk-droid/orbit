"""One task done for every item of a list, such as accepting each of 75 invitations,
in one go. The list, its count and its end are kept in code, not left to the model.

Asked to accept every collaborator invitation on GitHub, the model accepted one each
time the user said "next", and twice said all were accepted while some 75 waited. A
voice model ends its turn after a step or two, and nothing told it that the task went
on, how many were left, or that it was not finished.

So the model gathers every item first and hands them over with the task, and the
user says yes once, to the whole list. Then each answer the model gets is the next
item, and when it stops to talk instead, Orbit tells it to carry on, unless the user
has spoken. Words such as "stop" or "wait" pause the list at once. Only this code
says the list is finished. An item counts as done only once something was changed
for it: a click or typing in the browser, an approved action, a reminder. The model's
word for an item with nothing to show is taken the second time, and marked unchecked.
A list is kept as it goes, so one cut short by the user, by failures or by a broken
voice session is carried on later from the next item.

The one yes also covers the Google actions an item needs that do only what the task
says, such as answering an invitation or changing a row. New writing, such as an
email for each item, is kept, and shown all together once every item is done, for one
more yes before anything is sent.
"""

import asyncio
import json
import logging
import re
import time
from collections import Counter
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import aiohttp
from livekit.agents import RunContext, function_tool, utils
from livekit.agents.llm import ToolError, Toolset

from actions.effects import perform
from actions.gate import Action, NotDoneError
from lists import store
from lists.store import TaskList

from . import stage
from .browser import PERSON_CHECK, BrowserToolset
from .heard import LOOK_EVERY, given_by_tools, said_by_the_user
from .state import OrbitState, note_change

logger = logging.getLogger("orbit.lists")

# The kinds of draft that do a list and send what it kept.
RUN = "list.run"
SEND = "list.send"
# The most items one list takes. Any beyond are said, never dropped unsaid.
MOST_ITEMS = 500
# The most pages of a list in the browser read through its Next links.
MOST_PAGES = 40
# How long the user may take over the yes to a list: there is a lot to look at.
EXPIRY_SECONDS = 600
# A click called together with item_done may still be under way: it is waited for
# this long, in seconds.
ACTED_WITHIN = 5.0
# The list pauses once this many items in a row could not be done: something is
# wrong for all of them, such as a sign-in that ran out.
FAILED_IN_A_ROW = 5
# How long Orbit waits, once the model has gone quiet in the middle of a list, before
# telling it to carry on: the first, second and third time for one item. Once the
# last has passed again with nothing done, the list pauses and the user is told.
NUDGES = (3.0, 10.0, 25.0)
# An unfinished list is offered again for this long.
OFFER_SECONDS = 7 * 24 * 60 * 60
# The most items the card shows at once: those around the one being done.
MOST_ROWS = 150
MOST_SAID = 200
# Where the model is when it gives no page to read the list from.
HERE = "here"
# Google actions an item may need that do only what the task says. The user's yes
# to the list covers them.
COVERED = frozenset(
    {
        "calendar.respond",
        "calendar.create_event",
        "sheets.add_rows",
        "sheets.set_cells",
        "sheets.delete_rows",
    }
)
# New writing, kept to be shown with the others before any is sent.
WRITTEN = frozenset(
    {
        "gmail.send_reply",
        "gmail.send_email",
        "drive.create_document",
        "drive.create_file",
        "drive.add_to_document",
    }
)
# What the user says to stop a list, or hold it.
_STOPPING = re.compile(
    r"\b(stop|stopp?ed|pause|wait|hold on|hold it|cancel|enough|halt|abort|quit)\b",
    re.IGNORECASE,
)
_LINK = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
# Parts of an address that say where a visit came from, not what it is for, such as
# GitHub's notification_referrer_id: one item linked from two pages is one item.
_FROM_WHERE = re.compile(
    r"^(utm_\w+|fbclid|gclid|mc_[ce]id|ref|ref_src|ref_url|referr?er|\w*_referr?er_id)$",
    re.IGNORECASE,
)

NO_TASK = (
    "Say what to do for each item, in words that fit every one, such as "
    '"Accept the collaborator invitation".'
)
NO_ITEMS = (
    "Give every item of the list, or the page in the browser that lists them, with "
    "list_page and matching."
)
NO_MATCHING = (
    "Give matching: words every item's link has, in its text or its address, such "
    'as "invitations".'
)
NO_BROWSER = (
    "There is no browser in use to read the list from. Start it and open the page "
    "that lists the items first."
)
BROWSER_BUSY = "The browser is busy {what}. Wait until that is done."
NO_MATCH = (
    "No link on {where} has all of {words!r} in its words or its address. Look at "
    "how the items are written there, with browser_find, and call this again with "
    "words every item's link has."
)
NOT_FROM_TOOLS = (
    "These items did not come up in what tools gave or what the user said: {items}. "
    "Give each item as a tool gave it, with its link or id."
)
BUSY = (
    "A list is already being done: {done} of {total}. Stop it with for_each_stop "
    "first, or let it finish."
)
LEFT_FOR_LATER = (
    "{count} more items were found than one list takes, and are left out. Tell the "
    "user, and offer to do them as a second list once this one is done."
)
MORE_PAGES = (
    "The list goes on past {pages} pages, and only those were read. Tell the user."
)
ASK = (
    "Tell the user how many items there are and what will be done for each, and ask "
    "whether to go ahead with all of them. Call confirm_action only after they "
    "clearly agree."
)
DO_NOW = (
    "Do the task for this item now with your tools, then call item_done. Go straight "
    "on from one item to the next without asking the user whether to go on. Speak "
    "only if something needs them, and call for_each_wait before you ask them."
)
NOT_DONE_YET = (
    "Nothing has been changed for item {number} yet: no click or typing in the "
    "browser, no approved action, no reminder. Do the task for it with your tools "
    "first, then call item_done. If it cannot be done, call item_done with done "
    "false and say why."
)
NOT_CONFIRMED = (
    "The draft for item {number} has not been carried out yet. The user's yes to the "
    "list covers it: call confirm_action with action_id {action_id} now, then call "
    "item_done."
)
COVERED_NEXT = (
    "This is for the list's item, and the user's yes to the list covers it: call "
    "confirm_action with action_id {action_id} now, without asking them, then call "
    "item_done."
)
KEPT_NEXT = (
    "This is for the list's item. Do not read it out or ask about it: call item_done. "
    "It is kept with the others, and all are shown to the user together once every "
    "item is done."
)
WITH_FILES = (
    "That draft carries files, so it goes out only with the user's own yes: read it "
    "to them, ask, call confirm_action with action_id {action_id} once they agree, "
    "then call item_done."
)
COULD_NOT = "could not be done"
PAUSED_FAILING = (
    "The list is paused: the last {count} items could not be done ({why}). Tell the "
    "user what went wrong, and call for_each_continue if they want to go on."
)
FINISHED = "The list is finished: {summary}. Tell the user in a sentence or two."
KEPT_READY = (
    "Every item is done. {count} written drafts are kept and now on the user's "
    "screen. Tell them how many, and ask whether to send them all. Call "
    "confirm_action with the action_id only after they clearly agree."
)
KEPT_OUTCOME = "Kept, to be sent with the others once you say yes."
CARRY_ON = (
    "Carry on with the list: item {number} of {total}: {item}. The task: {task}. Do "
    "it now with your tools, then call item_done. Do not ask the user whether to go "
    "on."
)
STUCK = (
    "The list is paused at item {number} of {total}, as it was not carried on. Tell "
    "the user in a sentence, and call for_each_continue if they want to go on."
)
STOPPED = (
    "Stopped: {done} of {total} done, {left} left. What is left is kept: call "
    "for_each_continue when the user wants to go on."
)
WAIT = (
    "Ask the user now. The list waits for their answer, and you carry on once you "
    "have it."
)
NOT_RUNNING = "No list is being done."
PAUSED = (
    "The list is paused: {done} of {total} done. Call for_each_continue if the user "
    "wants to go on."
)
NOTHING_TO_CONTINUE = "There is no unfinished list."
GONE = "That list is no longer waiting. Draft it again with for_each."
# What the model is told when the conversation starts, if a list was left unfinished.
NOTE = (
    "A list from an earlier conversation is unfinished: {task!r}, {done} of {total} "
    "done, {left} left. If the user wants it finished, call for_each_continue."
)

# A page of a list, once it has stopped growing: scrolled to its end again and again,
# as a site may add rows only once they come into view, for some seconds at most.
_SETTLE = r"""async () => {
  const wait = (ms) => new Promise((done) => setTimeout(done, ms));
  let last = -1;
  for (let step = 0, still = 0; step < 24 && still < 3; step++) {
    window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
    await wait(250);
    const height = document.body ? document.body.scrollHeight : 0;
    still = height === last ? still + 1 : 0;
    last = height;
  }
  window.scrollTo(0, 0);
  return last;
}"""
# The links on a page with all of some words in their words or their address, each
# once, and the next page of the list: a link marked as the next one, or one that
# says Next.
_LINKS = r"""(matching) => {
  const words = matching.toLowerCase().split(/\s+/).filter(Boolean);
  const said = (el) => ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).replace(/\s+/g, ' ').trim();
  const shown = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const plain = (href) => { try { return decodeURI(href); } catch (e) { return href; } };
  const items = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href.split('#')[0];
    if (!/^https?:/i.test(href) || seen.has(href) || !shown(a)) continue;
    const name = said(a);
    const text = (name + ' ' + plain(href)).toLowerCase();
    if (!words.every((word) => text.includes(word))) continue;
    seen.add(href);
    items.push({name: name.slice(0, 200), href});
  }
  let next = '';
  const marked = document.querySelector('a[rel~="next"][href], link[rel~="next"][href]');
  if (marked) next = marked.href;
  for (const a of document.querySelectorAll('a[href]')) {
    if (next) break;
    if (!shown(a) || a.getAttribute('aria-disabled') === 'true') continue;
    const text = said(a);
    if (/^(next|next page|older|\u203a|\u00bb|\u2192)$/i.test(text) || /^next\b/i.test(text)) next = a.href;
  }
  return {items, next: next.split('#')[0], url: location.href};
}"""

_SHOWN = {
    store.WAITING: "",
    store.DONE: "Done",
    store.FAILED: "Not done",
    store.KEPT: "Drafted",
}


def _plain(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _link_in(text: str) -> str:
    found = _LINK.search(text)
    return found[0].rstrip(").,;:'\"") if found else ""


def _words_of(item: str) -> str:
    """An item as the card shows it: its words, its link opening from them."""
    return _LINK.sub("", item).strip(" -\u2013\u2014|:") or item


def _key(link: str) -> str:
    """What tells one item's address from another's: without what comes after a #,
    a slash at its end, or the parts that only say where the visit came from."""
    parts = urlsplit(link.split("#")[0])
    query = urlencode(
        [
            (name, value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if not _FROM_WHERE.match(name)
        ]
    )
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc.casefold(), path, query, ""))


def _came_up(item: str, sources: list[str], plains: list[str]) -> bool:
    """Whether an item came up in what a tool gave or the user said: each of its
    links, whole or as a page's address gives it, and its words, all together or
    every word of them in one place."""
    for link in _LINK.findall(item):
        link = link.rstrip(").,;:'\"")
        path = urlparse(link).path
        if not any(
            link in source or (len(path) > 1 and path in source) for source in sources
        ):
            return False
    words = _plain(_LINK.sub(" ", item))
    if not words or any(words in plain for plain in plains):
        return True
    tokens = [word for word in words.split() if len(word) > 2]
    return bool(tokens) and any(
        all(token in plain for token in tokens) for plain in plains
    )


def _not_from_tools(context: Any, items: list[str]) -> list[str]:
    """The items that came up nowhere in what tools gave or the user said."""
    sources = [gave for _, _, gave in given_by_tools(context)]
    sources += said_by_the_user(context)
    plains = [_plain(source) for source in sources]
    return [item for item in items if not _came_up(item, sources, plains)]


def _deduped(found: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Each item once: the same link, or the same words without one, is one item."""
    seen: set[str] = set()
    unique = []
    for item, link in found:
        key = _key(link) if link else _plain(item)
        if key and key not in seen:
            seen.add(key)
            unique.append((item, link))
    return unique


def _summary(listed: TaskList) -> str:
    done = [item for item in listed.items if item.state == store.DONE]
    failed = [item for item in listed.items if item.state == store.FAILED]
    said = f"{len(done)} of {listed.total} done"
    if unchecked := sum(1 for item in done if not item.checked):
        said += f" ({unchecked} said to be done with nothing Orbit could check)"
    if kept := listed.counted(store.KEPT):
        said += f"; {kept} drafted, to be sent once the user says yes"
    if failed:
        why = Counter(item.said for item in failed).most_common(3)
        said += f"; {len(failed)} not: " + "; ".join(
            f"{reason} ({count})" for reason, count in why
        )
    return said


def _not_done(listed: TaskList) -> list[str]:
    return [
        f"{item.number}: {_words_of(item.item)}: {item.said}"
        for item in listed.items
        if item.state == store.FAILED
    ][:20]


def _tag(listed: TaskList) -> str:
    parts = [f"{listed.counted(store.DONE)} of {listed.total} done"]
    if failed := listed.counted(store.FAILED):
        parts.append(f"{failed} not")
    if kept := listed.counted(store.KEPT):
        parts.append(f"{kept} drafted")
    tag = ", ".join(parts)
    return f"Paused \u00b7 {tag}" if listed.state == store.PAUSED else tag


def _preview(listed: TaskList) -> dict[str, Any]:
    """The list as the user is asked about it: the task, and every item."""
    return {
        "what": listed.task,
        "for": f"each of {listed.total} items",
        "table": {
            "columns": ["#", "Item"],
            "rows": [[str(item.number), item.item] for item in listed.items],
        },
    }


def _kept_preview(listed: TaskList) -> dict[str, Any]:
    """The written drafts a list kept, as the user is asked to send them all."""
    rows = []
    for item in listed.items:
        if item.state != store.KEPT:
            continue
        shown = item.draft.get("preview") or {}
        to = shown.get("to") or shown.get("document") or shown.get("title") or ""
        text = shown.get("body") or shown.get("content") or shown.get("add") or ""
        rows.append(
            [str(item.number), str(to), str(shown.get("subject") or ""), str(text)]
        )
    return {
        "what": f"Send the {len(rows)} drafts written for the list",
        "for": listed.task,
        "table": {"columns": ["#", "To", "Subject", "Writing"], "rows": rows},
    }


def _http() -> aiohttp.ClientSession:
    return utils.http_context.http_session()


def _last_tool_answer(session: Any) -> str:
    """What the tool called last in the conversation gave back."""
    try:
        items = session.history.items
    except (AttributeError, RuntimeError):
        return ""
    for item in reversed(items):
        if getattr(item, "type", None) == "function_call_output":
            return str(getattr(item, "output", "") or "")
    return ""


async def _say(session: Any, instructions: str) -> None:
    """Has the model take a turn of its own, as it is told."""
    try:
        await session.generate_reply(instructions=instructions)
    except RuntimeError:
        logger.info("the conversation ended before the list could go on")
    except Exception:
        logger.warning("the list could not tell the model to go on", exc_info=True)


def lists_note() -> str:
    """What the model is told when the conversation starts, if a list was left
    unfinished. Empty when none was."""
    listed = store.unfinished(time.time() - OFFER_SECONDS)
    if listed is None:
        return ""
    return NOTE.format(
        task=listed.task,
        done=listed.counted(store.DONE),
        total=listed.total,
        left=listed.left,
    )


class ListToolset(Toolset):
    """Doing one task for every item of a list, in one go. One per conversation: a
    list from an earlier one is carried on from the store. Items that are links on
    a page are read from `browser`, through every page of the list."""

    def __init__(self, browser: BrowserToolset | None = None) -> None:
        super().__init__(id="lists")
        self._browser = browser
        # The list being done in this conversation, and the item being done. The
        # last list stays known once it has paused.
        self._list_id: str | None = None
        self._last_id: str | None = None
        self._current: int | None = None
        # The conversation's state, and what had been changed and the time, on the
        # gate's clock, when the item being done began.
        self._state: OrbitState | None = None
        self._began: tuple[int, float] = (0, 0.0)
        # Items the model said were done although nothing could be seen changed:
        # told so once, its word is taken the second time, marked unchecked.
        self._claimed: set[int] = set()
        # Why each of the latest items in a row could not be done.
        self._failing: list[str] = []
        # The voice session, told to carry on when the model goes quiet mid-list.
        self._session: Any = None
        self._listening: list[tuple[str, Any]] = []
        self._timer: asyncio.Task | None = None
        self._nudges = 0
        # The model asked the user something it needs: no nudge until they answer.
        self._waiting = False
        self._tools = [
            function_tool(
                self._for_each,
                name="for_each",
                description=(
                    "Do one task for every item of a list, such as accepting each "
                    "invitation, deleting each old reminder or replying to each "
                    "email, when the user wants it done for all of them. Gather every "
                    "item first, from every page. A task that changes or sends "
                    "anything waits for the user's yes to the whole list, once. Then "
                    "you are given the items one at a time."
                ),
            ),
            function_tool(
                self._item_done,
                name="item_done",
                description=(
                    "Say the item you were given is done, or could not be done. You "
                    "are given the next item, or told the list is finished."
                ),
            ),
            function_tool(
                self._for_each_wait,
                name="for_each_wait",
                description=(
                    "Call before you ask the user something you need for the item "
                    "you are on, such as signing in. The list waits for their answer."
                ),
            ),
            function_tool(
                self._for_each_stop,
                name="for_each_stop",
                description=(
                    "Stop the list being done, when the user asks. What is left is "
                    "kept, to carry on later."
                ),
            ),
            function_tool(
                self._for_each_continue,
                name="for_each_continue",
                description=(
                    "Carry on with an unfinished list, when the user asks: one that "
                    "was stopped or paused, or one left from an earlier conversation."
                ),
            ),
        ]

    # The tools.

    async def _for_each(
        self,
        context: RunContext[OrbitState],
        task: str,
        items: list[str] | None = None,
        changes: bool = True,
        list_page: str = "",
        matching: str = "",
    ) -> dict[str, Any]:
        """
        Args:
            task: What to do for each item, in words that fit every one, such as
                "Accept the collaborator invitation".
            items: Every item, each as a tool gave it, with what tells it apart,
                such as its link or id. Leave empty to take the items from links in
                the browser, with list_page and matching.
            changes: True when doing the task changes or sends anything, such as
                accepting, deleting, sending or buying. False when it only reads.
            list_page: To take the items from links in the browser: the address of
                the page that lists them, or "here" for the page open now. Every
                page of the list is read, through its Next links.
            matching: With list_page: words every item's link has, in its text or
                its address, such as "invitations".
        """
        running = store.get(self._list_id) if self._list_id else None
        if running is not None and running.state == store.RUNNING:
            raise ToolError(
                BUSY.format(done=running.counted(store.DONE), total=running.total)
            )
        task = " ".join(task.split())
        if not task:
            raise ToolError(NO_TASK)
        given = [" ".join(str(item).split()) for item in items or []]
        given = [item for item in given if item]
        if missing := _not_from_tools(context, given):
            raise ToolError(NOT_FROM_TOOLS.format(items="; ".join(missing[:5])))
        found = [(item, _link_in(item)) for item in given]
        notes: list[str] = []
        if list_page.strip():
            if not matching.strip():
                raise ToolError(NO_MATCHING)
            gathered, notes = await self._gather(
                context, list_page.strip(), matching.strip()
            )
            found += gathered
        if not found:
            raise ToolError(NO_ITEMS)
        unique = _deduped(found)
        if len(unique) > MOST_ITEMS:
            notes.append(LEFT_FOR_LATER.format(count=len(unique) - MOST_ITEMS))
            unique = unique[:MOST_ITEMS]
        listed = store.create(task, changes, unique)
        if not changes:
            # Only reading: there is nothing to agree to.
            told = await self._start(context, listed)
        else:
            state = context.userdata
            state.handlers[RUN] = self._approved
            action = state.gate.propose(
                RUN, {"list_id": listed.id}, _preview(listed), expiry=EXPIRY_SECONDS
            )
            told = {
                "action_id": action.id,
                "status": action.status,
                "items": listed.total,
                "first": [item.item for item in listed.items[:3]],
                "next": ASK,
            }
        if notes:
            told["notes"] = notes
        return told

    async def _item_done(
        self, context: RunContext[OrbitState], done: bool, what_happened: str = ""
    ) -> dict[str, Any]:
        """
        Args:
            done: True if the task was done for the item, false if it could not be.
            what_happened: In a few words, what came of it, such as "accepted", or
                why it could not be done.
        """
        listed = self._list()
        number = self._current
        assert number is not None
        said = " ".join(what_happened.split())[:MOST_SAID]
        if not done:
            store.mark(listed.id, number, store.FAILED, said or COULD_NOT)
            self._failing.append(said or COULD_NOT)
            if len(self._failing) >= FAILED_IN_A_ROW:
                why = "; ".join(dict.fromkeys(self._failing))
                return await self._pause(
                    PAUSED_FAILING.format(count=len(self._failing), why=why)
                )
            return await self._next(context)
        if (kept := self._kept()) is not None:
            self._failing = []
            store.mark(listed.id, number, store.KEPT, said or "drafted", draft=kept)
            return await self._next(context)
        acted = not listed.changes or await self._acted()
        if not acted and number not in self._claimed:
            self._claimed.add(number)
            awaiting = self._state.gate.awaiting() if self._state else None
            if awaiting is not None and self._covers(awaiting):
                raise ToolError(
                    NOT_CONFIRMED.format(number=number, action_id=awaiting.id)
                )
            raise ToolError(NOT_DONE_YET.format(number=number))
        self._failing = []
        store.mark(listed.id, number, store.DONE, said or "done", checked=acted)
        return await self._next(context)

    async def _for_each_wait(
        self, context: RunContext[OrbitState], question: str = ""
    ) -> str:
        """
        Args:
            question: What you are about to ask the user, in a few words.
        """
        self._list()
        self._waiting = True
        self._cancel_timer()
        return WAIT

    async def _for_each_stop(self, context: RunContext[OrbitState]) -> dict[str, Any]:
        # Paused already, as the user's words that stop it do at once.
        last = store.get(self._last_id) if self._last_id else None
        if last is None or last.state not in (store.RUNNING, store.PAUSED):
            raise ToolError(NOT_RUNNING)
        store.set_state(last.id, store.PAUSED)
        self._end()
        stopped = store.get(last.id)
        assert stopped is not None
        await self._show(stopped)
        return {
            "stopped": STOPPED.format(
                done=stopped.counted(store.DONE),
                total=stopped.total,
                left=stopped.left,
            )
        }

    async def _for_each_continue(
        self, context: RunContext[OrbitState]
    ) -> dict[str, Any]:
        if self._list_id is not None:
            # Already going: the item being done is given again.
            return self._given(self._list())
        listed = store.unfinished(time.time() - OFFER_SECONDS)
        if listed is None:
            raise ToolError(NOTHING_TO_CONTINUE)
        if listed.left == 0:
            # Every item is done but for the written drafts it kept: those are shown
            # to the user again.
            self._state = context.userdata
            return await self._finish(context, listed)
        return await self._start(context, listed)

    # Doing the list.

    async def _approved(
        self, context: RunContext[OrbitState], action: Action
    ) -> dict[str, Any]:
        """The user said yes to the list: it begins."""
        listed = store.get(str(action.payload.get("list_id")))
        if listed is None or listed.state != store.DRAFTED:
            raise NotDoneError(GONE)
        told = await self._start(context, listed)
        return {
            "told": told,
            "outcome": f"Going through all {listed.total}",
            # Nothing of the user's is changed yet: the first item has only begun.
            "changed": False,
        }

    async def _start(
        self, context: RunContext[OrbitState], listed: TaskList
    ) -> dict[str, Any]:
        store.set_state(listed.id, store.RUNNING)
        self._list_id = self._last_id = listed.id
        self._failing = []
        self._claimed = set()
        self._state = context.userdata
        self._state.covered = self._covers
        self._state.after_draft = self._after_draft
        self._state.trace.doing_list()
        self._wire(getattr(context, "session", None))
        return await self._next(context)

    async def _next(self, context: RunContext[OrbitState]) -> dict[str, Any]:
        """Begins the next item, or finishes the list when none is left."""
        listed = self._list()
        item = listed.next_item()
        if item is None:
            return await self._finish(context, listed)
        assert self._state is not None
        self._current = item.number
        self._began = (self._state.changed, self._state.gate.now())
        self._nudges = 0
        self._waiting = False
        await self._show(listed)
        return self._given(listed)

    def _given(self, listed: TaskList) -> dict[str, Any]:
        assert self._current is not None
        item = listed.item(self._current)
        return {
            "item": {"number": item.number, "of": listed.total, "item": item.item},
            "task": listed.task,
            "do": DO_NOW,
        }

    async def _finish(
        self, context: RunContext[OrbitState], listed: TaskList
    ) -> dict[str, Any]:
        """Every item is done. What the list wrote is shown for one more yes; with
        nothing written, the list is finished."""
        self._end()
        if listed.counted(store.KEPT):
            store.set_state(listed.id, store.PAUSED)
            state = context.userdata
            state.handlers[SEND] = self._send
            action = state.gate.propose(
                SEND,
                {"list_id": listed.id},
                _kept_preview(listed),
                expiry=EXPIRY_SECONDS,
            )
            paused = store.get(listed.id)
            assert paused is not None
            await self._show(paused)
            return {
                "done": _summary(paused),
                "action_id": action.id,
                "status": action.status,
                "next": KEPT_READY.format(count=paused.counted(store.KEPT)),
            }
        store.set_state(listed.id, store.FINISHED)
        finished = store.get(listed.id)
        assert finished is not None
        await self._show(finished)
        told: dict[str, Any] = {"finished": FINISHED.format(summary=_summary(finished))}
        if not_done := _not_done(finished):
            told["not_done"] = not_done
        return told

    async def _send(
        self, context: RunContext[OrbitState], action: Action
    ) -> dict[str, Any]:
        """The user said yes to the drafts a list kept: each is sent, in order."""
        listed = store.get(str(action.payload.get("list_id")))
        if listed is None:
            raise NotDoneError(GONE)
        sent = failed = 0
        session = _http()
        for item in listed.items:
            if item.state != store.KEPT:
                continue
            try:
                await perform(session, item.draft["kind"], item.draft["payload"])
            except Exception:
                logger.warning("a draft kept by a list was not sent", exc_info=True)
                store.mark(
                    listed.id, item.number, store.FAILED, "Google did not take it"
                )
                failed += 1
                continue
            store.mark(listed.id, item.number, store.DONE, item.said or "sent")
            note_change(context)
            sent += 1
        store.set_state(listed.id, store.FINISHED)
        finished = store.get(listed.id)
        assert finished is not None
        await self._show(finished)
        told: dict[str, Any] = {"finished": FINISHED.format(summary=_summary(finished))}
        if not_done := _not_done(finished):
            told["not_done"] = not_done
        # Each draft sent was counted as it went.
        return {
            "told": told,
            "outcome": f"Sent {sent} of {sent + failed}",
            "changed": False,
        }

    async def _pause(self, said: str) -> dict[str, Any]:
        assert self._list_id is not None
        list_id = self._list_id
        store.set_state(list_id, store.PAUSED)
        self._end()
        paused = store.get(list_id)
        assert paused is not None
        await self._show(paused)
        return {"paused": said}

    def _end(self) -> None:
        """Stops working on the list in this conversation: no more nudges, and the
        user's yes to it covers nothing more."""
        self._cancel_timer()
        self._unwire()
        if self._state is not None:
            if self._state.covered == self._covers:
                self._state.covered = None
            if self._state.after_draft == self._after_draft:
                self._state.after_draft = None
        self._list_id = None
        self._current = None

    def _list(self) -> TaskList:
        """The list being done, or ToolError when none is."""
        listed = store.get(self._list_id) if self._list_id else None
        if listed is not None and listed.state == store.RUNNING:
            return listed
        self._end()
        last = store.get(self._last_id) if self._last_id else None
        if last is not None and last.state == store.PAUSED:
            raise ToolError(
                PAUSED.format(done=last.counted(store.DONE), total=last.total)
            )
        raise ToolError(NOT_RUNNING)

    async def _acted(self) -> bool:
        """Whether something was changed since the item began. A click called with
        item_done may still be under way, so it is given a moment."""
        assert self._state is not None
        waited = 0.0
        while self._state.changed <= self._began[0]:
            if waited >= ACTED_WITHIN:
                return False
            await asyncio.sleep(LOOK_EVERY)
            waited += LOOK_EVERY
        return True

    def _kept(self) -> dict[str, Any] | None:
        """A written draft made for the item being done, taken from the gate to be
        sent with the others once the user has seen them all. None if there is none."""
        assert self._state is not None
        gate = self._state.gate
        action = gate.awaiting()
        if (
            action is None
            or action.kind not in WRITTEN
            or action.created_at < self._began[1]
        ):
            return None
        if action.payload.get("attachments") or action.payload.get("from_tray"):
            raise ToolError(WITH_FILES.format(action_id=action.id))
        gate.set_aside(action.id, {"outcome": KEPT_OUTCOME})
        return {
            "kind": action.kind,
            "payload": action.payload,
            "preview": action.preview,
        }

    def _covers(self, action: Action) -> bool:
        """Whether the user's yes to the list covers a draft: one made for the item
        being done, that does only what the task says."""
        return (
            self._list_id is not None
            and self._current is not None
            and action.kind in COVERED
            and action.created_at >= self._began[1]
        )

    def _after_draft(self, action: Action) -> str:
        """What the model is told to do with a draft made for the item being done, in
        place of asking the user. Empty for any other draft."""
        if self._covers(action):
            return COVERED_NEXT.format(action_id=action.id)
        if (
            self._list_id is not None
            and action.kind in WRITTEN
            and not action.payload.get("attachments")
            and not action.payload.get("from_tray")
        ):
            return KEPT_NEXT
        return ""

    # Reading a list from the browser.

    async def _gather(
        self, context: RunContext[OrbitState], page: str, matching: str
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """The links of a list in the browser with all of `matching` in their words
        or address, from every page of it, each with its address; and what the model
        should know of what was left out."""
        browser = self._browser
        if browser is None or not browser.in_use:
            raise ToolError(NO_BROWSER)
        if what := browser.holding[0]:
            raise ToolError(BROWSER_BUSY.format(what=what))
        notes: list[str] = []
        found: dict[str, tuple[str, str]] = {}
        browser.hold("reading every page of a list")
        try:
            if page.casefold() == HERE:
                await browser.run_script(context, _SETTLE)
            else:
                await browser.open_page(context, page, _SETTLE)
            read: list[str] = []
            while True:
                got = await browser.run_script(
                    context, f"() => ({_LINKS})({json.dumps(matching)})"
                )
                read.append(str(got.get("url") or ""))
                for link in got.get("items") or []:
                    href = str(link["href"])
                    found.setdefault(_key(href), (href, str(link.get("name") or "")))
                following = str(got.get("next") or "")
                if not following or following in read or len(found) > MOST_ITEMS:
                    break
                if len(read) >= MOST_PAGES:
                    notes.append(MORE_PAGES.format(pages=MOST_PAGES))
                    break
                await browser.open_page(context, following, _SETTLE)
        finally:
            browser.release()
        if not found:
            where = "the page open now" if page.casefold() == HERE else page
            raise ToolError(NO_MATCH.format(words=matching, where=where))
        items = [
            (f"{name} \u2014 {href}" if name else href, href)
            for href, name in found.values()
        ]
        return items, notes

    # The card.

    async def _show(self, listed: TaskList) -> None:
        """The list on the user's screen: each item, and what came of it."""
        items = listed.items
        if len(items) > MOST_ROWS:
            at = max(0, (self._current or 1) - 1 - MOST_ROWS // 5)
            items = items[at : at + MOST_ROWS]
        rows = []
        for item in items:
            meta = _SHOWN[item.state]
            if item.state == store.WAITING and item.number == self._current:
                meta = "Now"
            elif item.state == store.DONE and not item.checked:
                meta = "Unchecked"
            rows.append(
                stage.row(_words_of(item.item), item.said, meta=meta, link=item.link)
            )
        card: dict[str, Any] = {
            "id": f"list:{listed.id}",
            "kind": "list",
            "title": listed.task,
            "icon": "repeat",
            "tag": _tag(listed),
            "rows": rows,
        }
        if listed.total > len(rows):
            card["more"] = listed.total - len(rows)
        await stage.show(card)

    # Keeping the model at it.

    def _wire(self, session: Any) -> None:
        if session is None or not hasattr(session, "on"):
            return
        self._unwire()
        self._session = session
        self._listening = [
            ("agent_state_changed", self._on_agent_state),
            ("user_input_transcribed", self._on_transcript),
            ("conversation_item_added", self._on_item),
            ("close", self._on_close),
        ]
        for event, handler in self._listening:
            session.on(event, handler)

    def _unwire(self) -> None:
        if self._session is not None:
            for event, handler in self._listening:
                self._session.off(event, handler)
        self._session = None
        self._listening = []

    def _on_agent_state(self, event: Any) -> None:
        if event.new_state == "listening":
            self._schedule()
        else:
            self._cancel_timer()

    def _on_transcript(self, event: Any) -> None:
        if event.transcript.strip():
            self._heard(event.transcript)

    def _on_item(self, event: Any) -> None:
        if getattr(event.item, "role", None) == "user":
            self._heard(event.item.text_content or "")

    def _on_close(self, event: Any) -> None:
        if self._list_id is not None:
            store.set_state(self._list_id, store.PAUSED)
        self._end()

    def _heard(self, words: str) -> None:
        """The user said something while the list is being done. Words that stop
        it pause it at once: Orbit must never carry on against them."""
        self._cancel_timer()
        self._waiting = False
        if self._list_id is not None and _STOPPING.search(words):
            list_id = self._list_id
            store.set_state(list_id, store.PAUSED)
            self._end()
            if (paused := store.get(list_id)) is not None:
                asyncio.get_running_loop().create_task(self._show(paused))

    def _schedule(self) -> None:
        self._cancel_timer()
        if self._list_id is None or self._current is None or self._waiting:
            return
        delay = NUDGES[min(self._nudges, len(NUDGES) - 1)]
        self._timer = asyncio.get_running_loop().create_task(self._nudge(delay))

    def _cancel_timer(self) -> None:
        if self._timer is not None and self._timer is not asyncio.current_task():
            self._timer.cancel()
        self._timer = None

    async def _nudge(self, delay: float) -> None:
        """Tells the model to carry on with the list, once it has been quiet for a
        while in the middle of it; or, once that has not helped, pauses the list."""
        await asyncio.sleep(delay)
        session = self._session
        if (
            session is None
            or self._list_id is None
            or self._current is None
            or self._waiting
            or getattr(session, "agent_state", "listening") != "listening"
            or getattr(session, "current_speech", None) is not None
            # A page asked for a check only the user may do: they are doing it.
            or PERSON_CHECK.strip()[:60] in _last_tool_answer(session)
        ):
            return
        listed = store.get(self._list_id)
        if listed is None or listed.state != store.RUNNING:
            return
        item = listed.item(self._current)
        self._timer = None
        if self._nudges >= len(NUDGES):
            await self._pause(STUCK)
            await _say(session, STUCK.format(number=item.number, total=listed.total))
            return
        self._nudges += 1
        await _say(
            session,
            CARRY_ON.format(
                number=item.number, total=listed.total, item=item.item, task=listed.task
            ),
        )

    async def aclose(self) -> None:
        if self._list_id is not None:
            store.set_state(self._list_id, store.PAUSED)
        self._end()
        await super().aclose()
