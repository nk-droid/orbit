"""The show_chart tool: a chart on the user's screen, made once and pulled after.

Every number a chart shows is worked out in charts/, never typed by the model. The
model names the data and how to chart it. The only numbers it may pass itself are
ones from the conversation, which the user said or a tool gave, such as on a web page
the browser read. That is checked here, against the record of the conversation.

A chart is made only when the user asks for one, and it is saved into Google Sheets
when it is made: into Orbit charts, a spreadsheet Orbit makes the first time with
the user's yes and adds to after that without asking, since it is Orbit's own and
nothing in it goes anywhere. Asked for again, by its name or by what it measures, a
saved chart is pulled from its tab and shown in the same card. When Google cannot
be reached, the chart is shown without being saved, and the user is told why.
"""

import asyncio
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from livekit.agents import RunContext, function_tool, utils
from livekit.agents.llm import ToolError

from charts import saving, sources, store
from charts.build import (
    KINDS,
    ChartError,
    Request,
    build,
    family,
    gist,
    key,
    with_kind,
)
from charts.data import number
from integrations.google.api import GoogleApiError, NotConnectedError

from . import stage
from .browser import CUT_SHORT_MARK
from .google import reading
from .heard import (
    HEARD_WITHIN,
    LOOK_EVERY,
    given_by_tools,
    latest_request,
    said_by_the_user,
    said_so_far,
)
from .state import OrbitState

SAY = (
    "Say the gist in a sentence or two, with numbers only from gist. The chart is on "
    "the user's screen."
)
NOT_SAVED = (
    "It could not be saved into Google Sheets: {why} Tell the user, in a few words, "
    "that it is on screen but not kept."
)
OLDER = "These are the numbers as they were on {when}. Fresh ones need fresh as true."
NO_SUCH_CHART = "No saved chart is called {asked!r}. The saved ones are: {names}."
NO_CHARTS = "There are no saved charts. Make one with kind and source."
SEVERAL_CHARTS = "{asked!r} fits several saved charts: {names}. Ask which one."
OTHER_FAMILY = (
    "That chart is drawn as a {was}, and a {kind} would measure something else. Make "
    "a new chart for it, with source and columns."
)
FIXED = (
    "That chart is of numbers from a conversation, with no source to read again. Get "
    "the numbers again where they came from, such as the same web page, and pass "
    "them as points, with chart and fresh."
)
NOT_GIVEN = (
    "Not said by the user or given by a tool in this conversation, for its label: "
    "{values}. A chart takes only values that were said or given, each with the "
    "label it was given for, written as it was: a level in words, such as "
    "Academic, too. Find any others with a tool, such as the browser or web_search, "
    "or ask the user for them."
)
WHAT_WAS_GIVEN = '{point} (what was given for {label}: "{given}")'
NOT_ALL = (
    "The user asked for all of them, and the page lists {count} like these, of "
    "which {given} were given. Not given: {missing}. Show the chart again with every "
    "one, each with its value as it was given."
)
CUT_SHORT = (
    "The page these came from was cut short where they were read, so it may hold "
    "more. If the user asked for all of them, read it whole with browser_read and "
    "show the chart again with every one."
)
REORDER = "To order the levels otherwise, pass levels, lowest first."
NO_KIND = "Say which kind of chart: {kinds}."
# What the model is told when the conversation starts, if charts are saved.
CHARTS = """\
Charts the user has saved in Google Sheets: {listing}. To show one again, call show_chart with its name as chart."""
# How many saved charts are named at the start, the latest.
LISTED = 30

# A number in digits as it is written: a currency before it, a percent sign after
# it, and a scale after that: "₹79,999", "Rs. 1,200", "45%", "4.5 million", "3.4M".
_WRITTEN = re.compile(
    r"((?:[$€£₹¥₩₽]|\b(?:rs|inr|usd|eur|gbp|jpy|krw|rub)\b\.?)\s?)?"
    r"(\d[\d,]*(?:\.\d+)?)(\s?%)?"
    r"(?:\s?(k|m|mn|b|bn|t|tn|cr|thousand|million|billion|trillion|lakhs?|crores?)\b)?",
    re.IGNORECASE,
)
_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}  # fmt: skip
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}  # fmt: skip
_SCALES = {
    "hundred": 100, "thousand": 1000, "lakh": 100_000, "million": 1_000_000,
    "crore": 10_000_000, "billion": 10**9, "trillion": 10**12,
}  # fmt: skip
_SHORT_SCALES = {
    "k": 1000, "m": 1_000_000, "mn": 1_000_000, "b": 10**9, "bn": 10**9,
    "t": 10**12, "tn": 10**12, "cr": 10_000_000,
}  # fmt: skip
# A page the browser read, as its server writes it (see browser.py).
_PAGE_URL = re.compile(r"^- Page URL: (\S+)", re.MULTILINE)
# Where numbers a tool gave came from, by the start of its name, for a chart's label.
_PLACES = {
    "web_search": "a web search",
    "gmail": "Gmail",
    "calendar": "Google Calendar",
    "chat": "Google Chat",
    "drive": "Google Drive",
    "sheets": "Google Sheets",
    "screenshot": "your screen",
    "capture_window": "your screen",
    "reminder": "your reminders",
    "browser": "the browser",
}


def numbers_in(text: str) -> list[float]:
    """The numbers in what was said or given: in digits, with a scale such as
    "4.5 million" or "3.4M", or in words, such as "twenty five" or "two lakh"."""
    return [value for value, _ in _in_digits(text)] + _in_words(text)


def _in_digits(text: str) -> Iterator[tuple[float, str]]:
    """Each number in digits, and how it is written, with the currency before it and
    the percent sign after it: "₹79,999", "45%". One with a scale after it is
    given scaled too."""
    for currency, digits, percent, scale in _WRITTEN.findall(text):
        if (value := number(digits)) is None:
            continue
        yield value, f"{currency}{digits}{percent}"
        if scale:
            word = scale.casefold()
            value *= _SHORT_SCALES.get(word) or _SCALES[word.rstrip("s")]
            # Written out in full, such as 3,400,000 for 3.4M.
            yield value, currency + f"{value:,.2f}".rstrip("0").rstrip(".")


def _in_words(text: str) -> list[float]:
    """Each number in words, such as "five", "twenty five" or "two hundred and fifty"."""
    found: list[float] = []
    total, part, last = 0.0, 0.0, ""

    def said() -> None:
        nonlocal total, part
        if last:
            found.append(total + part)
        total, part = 0.0, 0.0

    for word in re.findall(r"[a-z]+", text.casefold()):
        if word.endswith("s") and word[:-1] in _SCALES:
            word = word[:-1]
        if word in _ONES or word in _TENS:
            kind = "ones" if word in _ONES else "tens"
            # "five six" is two numbers, and "twenty five" one.
            if last in ("ones", "tens") and not (last == "tens" and kind == "ones"):
                said()
            part += _ONES.get(word, 0) + _TENS.get(word, 0)
            last = kind
        elif word in _SCALES and last:
            if _SCALES[word] == 100:
                part *= 100
            else:
                total, part = total + part * _SCALES[word], 0.0
            last = "scale"
        elif word == "and" and last == "scale":
            last = "and"
        else:
            said()
            last = ""
    said()
    return found


def _size(value: float) -> float:
    """A number as numbers are matched: its size, to twelve figures. A sign is left
    out, since a dash before a number is as often a range as a minus."""
    return float(f"{abs(value):.12g}")


def _place(name: str, gave: str) -> str:
    """Where a tool's numbers came from, as the user knows it: the site of a page
    the browser read, or else the service."""
    pages = _PAGE_URL.findall(gave)
    if pages and (site := urlsplit(pages[-1]).netloc.removeprefix("www.")):
        return site
    starts = [place for start, place in _PLACES.items() if name.startswith(start)]
    return starts[0] if starts else "this conversation"


def _written_in(text: str) -> dict[float, str]:
    """The numbers in a text by size, each as it is first written there: in digits
    with its unit, such as "₹79,999", or "" for one in words."""
    found: dict[float, str] = {}
    for value, written in _in_digits(text):
        found.setdefault(_size(value), written)
    for value in _in_words(text):
        found.setdefault(_size(value), "")
    return found


@dataclass(frozen=True)
class _Place:
    """Where values came from in this conversation, and what was there."""

    # As the user knows it: "you", a site, or a service.
    name: str
    # What was there, and what the model sent the tool that gave it.
    text: str
    sent: str
    # A page that was cut short.
    cut: bool

    def gives(self, label: str, part: str) -> "_Found | None":
        """How this place gave a value for a label: within the user's words, or for
        a tool, on the label's row or near the label, and not as one the model sent
        it. None if it gave no such value."""
        n = number(part)
        # The user's numbers are taken from anywhere in their words, as speech runs.
        chunks = [self.text] if self.name == "you" else _near(self.text, label)
        for chunk in chunks:
            written = _value_in(chunk, part, n)
            if written is None or _value_in(self.sent, part, n) is not None:
                continue
            row = _row_of(chunk)
            at = next(
                (
                    i
                    for i, cell in enumerate(row)
                    if _value_in(cell, part, n) is not None
                ),
                -1,
            )
            return _Found(self, written or part.strip(), row, at)
        return None


@dataclass(frozen=True)
class _Found:
    """A value as a place gave it: as written there, and on which row and in which
    of its parts, when it was on a row of a page read as text."""

    place: _Place
    written: str
    row: list[str]
    at: int


def _value_in(text: str, part: str, n: float | None) -> str | None:
    """A value as a text writes it, or None if it is not there: a number in digits
    or words, or a level in words."""
    if n is not None:
        return _written_in(text).get(_size(n))
    return part.strip() if _has_word(text, part) else None


def _row_of(chunk: str) -> list[str]:
    """The parts of a row of a page read as text; none for other text."""
    if chunk.startswith("- ") and " | " in chunk:
        return [cell.strip() for cell in chunk[2:].split(" | ")]
    return []


def _has_word(text: str, word: str) -> bool:
    return (
        re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.IGNORECASE) is not None
    )


# How far from a label its value is looked for, where a text has no rows: a sentence
# or two either way.
_NEAR = 240


# Words that join a label's parts, written in one place one way and in another,
# another: "PostgreSQL + pgvector", "PostgreSQL and pgvector".
_JOINING = frozenset({"and", "plus", "&", "+", "the"})


def _key(label: str) -> str:
    """A label as labels are matched: its words, without those that join them."""
    words = re.findall(r"[a-z0-9.#+&]+", label.casefold())
    return " ".join(word for word in words if word not in _JOINING)


def _near(text: str, label: str) -> list[str]:
    """Where a label is in a text: the rows it is on, in a page read as text, or
    where it has none, the words around it. A label with a row of its own has its
    values there, whatever is written near it elsewhere."""
    label = " ".join(label.split())
    if not label:
        return []
    # A short label, such as C or Go, only as a word of its own.
    pattern = (
        rf"(?<!\w){re.escape(label)}(?!\w)" if len(label) < 4 else re.escape(label)
    )
    own: list[str] = []
    others: list[str] = []
    for found in re.finditer(pattern, text, re.IGNORECASE):
        start = text.rfind("\n", 0, found.start()) + 1
        end = text.find("\n", found.end())
        line = text[start : end if end >= 0 else len(text)]
        if line.startswith("- ") and " | " in line:
            parts = {part.strip().casefold() for part in line[2:].split(" | ")}
            (own if label.casefold() in parts else others).append(line)
        else:
            others.append(text[max(0, found.start() - _NEAR) : found.end() + _NEAR])
    if not own and not others:
        # Written another way, as "PostgreSQL and pgvector" for "PostgreSQL + pgvector".
        key = _key(label)
        own = [
            line
            for line in text.splitlines()
            if line.startswith("- ")
            and " | " in line
            and key in {_key(part) for part in line[2:].split(" | ")}
        ]
    return own or others


def _places(context: RunContext[OrbitState]) -> list[_Place]:
    """Each place values came from in this conversation: the user's words first,
    then what tools gave, the latest first. A value the model sent a tool is not
    one that tool gave, though it may give it back."""
    places = [_Place("you", " ".join(said_by_the_user(context)), "", False)]
    for name, asked, gave in reversed(given_by_tools(context)):
        places.append(_Place(_place(name, gave), gave, asked, CUT_SHORT_MARK in gave))
    return places


def _label(places: list[str]) -> str:
    """A chart's source as the user knows it: where its numbers came from."""
    named = list(dict.fromkeys(place for place in places if place != "you"))
    if not named:
        return "Numbers you said"
    if len(named) > 3:
        named = [*named[:2], f"{len(named) - 2} more places"]
    if len(named) == 1:
        return f"From {named[0]}"
    return f"From {', '.join(named[:-1])} and {named[-1]}"


@dataclass(frozen=True)
class _Given:
    """Values from the conversation, as found there."""

    # Where they came from, as the user knows it.
    label: str
    # Each point's values as written where they were given, for unit and decimals.
    written: list[list[str]]
    # Some were read on a page that was cut short.
    cut: bool


async def _given(context: RunContext[OrbitState], points: list[str]) -> _Given:
    """Lets values into a chart only if the user said them, or a tool gave them for
    their label, in this conversation: numbers, and levels in words such as 5y+.
    What the user said may not be on record yet, so it is given a moment to get
    there. Asked for all of a list on a page, every row of it must be there."""
    rows = [sources.split_point(item) for item in points]
    waited = 0.0
    while True:
        places = _places(context)
        found = [
            [
                next((f for p in places if (f := p.gives(label, part))), None)
                for part in parts
            ]
            for label, parts in rows
        ]
        # Where on its row each column's values were, as most of them were found.
        columns = max((len(parts) for _, parts in rows), default=0)
        value_at = [
            Counter(
                at[j].at for at in found if j < len(at) and at[j] and at[j].at >= 0
            ).most_common(1)
            for j in range(columns)
        ]
        places_at = [most[0][0] if most else -1 for most in value_at]
        missing = [
            _unsaid(places, label, part, places_at[j])
            for (label, parts), at in zip(rows, found, strict=True)
            for j, part in enumerate(parts)
            if at[j] is None
        ]
        if not missing:
            break
        if waited >= HEARD_WITHIN:
            raise ToolError(NOT_GIVEN.format(values="; ".join(missing)))
        await asyncio.sleep(LOOK_EVERY)
        waited += LOOK_EVERY
    _check_all(context, places, [label for label, _ in rows], places_at)
    used = [f for at in found for f in at if f]
    return _Given(
        _label([f.place.name for f in used]),
        [[f.written if f else "" for f in at] for at in found],
        any(f.place.cut for f in used),
    )


def _unsaid(places: list[_Place], label: str, part: str, at: int) -> str:
    """A value not given for its label, with what was given for that label, if any:
    from its row, the part where the other values were found."""
    point = f"{label}: {part.strip()}"
    for place in places[1:]:
        for chunk in _near(place.text, label):
            row = _row_of(chunk)
            given = row[at] if 0 <= at < len(row) else " ".join(chunk.split())[:200]
            return WHAT_WAS_GIVEN.format(point=point, label=label, given=given)
    return point


# What asks for all of something.
_ALL = re.compile(r"\b(?:all|every|each|whole|entire|complete|full)\b", re.IGNORECASE)
_HEADING = re.compile(r"^(#+) ")


def _check_all(
    context: RunContext[OrbitState],
    places: list[_Place],
    labels: list[str],
    values_at: list[int],
) -> None:
    """When the user asked for all of something, and the labels are rows of a list
    on a page read as text, refuses to chart fewer than every row of it, saying
    which, with their values. The same rows left out again are taken as meant."""
    if not _ALL.search(" ".join(latest_request(said_so_far(context)))):
        return
    asked = {_key(label) for label in labels}
    for place in places[1:]:
        listed = _list_of(place.text, asked)
        if not listed:
            continue
        name, every, label_at = listed
        left_out = [row for row in every if not _among(row[label_at], asked)]
        missing = frozenset(row[label_at] for row in left_out)
        told = context.userdata.lists_left_short
        if left_out and told.get(name) != missing:
            told[name] = missing
            written = ", ".join(
                row[label_at]
                + "".join(f": {row[at]}" for at in values_at if 0 <= at < len(row))
                for row in left_out
            )
            raise ToolError(
                NOT_ALL.format(count=len(every), given=len(labels), missing=written)
            )
        return


def _among(label: str, asked: set[str]) -> bool:
    """Whether a row's label was given, in full, shortened, as PostgreSQL for
    "PostgreSQL + pgvector", or written another way. `asked` are labels as keys."""
    label = _key(label)
    return label in asked or any(len(a) >= 4 and a in label for a in asked)


def _list_of(text: str, labels: set[str]) -> tuple[str, list[list[str]], int] | None:
    """In a page read as text, the list rows with these labels belong to: its name,
    its rows, and where the label is among a row's parts. A list runs on under the
    headings beside its own, as a page's parts of one list do, in rows shaped like
    its own."""
    rows: list[tuple[tuple[int, int], tuple[int, int], list[str]]] = []
    headings: list[tuple[int, int]] = []
    lines = text.splitlines()
    for n, line in enumerate(lines):
        if heading := _HEADING.match(line):
            level = len(heading[1])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, n))
        elif line.startswith("- ") and " | " in line:
            own = headings[-1] if headings else (0, -1)
            parent = headings[-2] if len(headings) > 1 else (0, -1)
            rows.append((own, parent, [part.strip() for part in line[2:].split(" | ")]))
    hit = next(
        (
            (row, i)
            for row in rows
            for i, part in enumerate(row[2])
            if _key(part) in labels
        ),
        None,
    )
    if hit is None:
        return None
    (own, parent, parts), at = hit
    shape = [number(part) is None for part in parts]
    every = [
        row_parts
        for row_own, row_parent, row_parts in rows
        if row_parent == parent
        and row_own[0] == own[0]
        and [number(part) is None for part in row_parts] == shape
    ]
    name = lines[parent[1]] if parent[1] >= 0 else lines[own[1]] if own[1] >= 0 else ""
    return f"{name} ({len(every)})", every, at


def _columns(request: Request) -> Request:
    """Numbers from the conversation are charted under the columns the model named:
    the labels along the bottom, and a column for each number of a point."""
    return replace(
        request,
        x=" ".join(request.x.split()) or "Label",
        y=tuple(" ".join(name.split()) for name in request.y if name.strip())
        or ("Value",),
    )


def _source(
    asked: str, points: list[str] | None, datasets: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """The source the model named. Numbers it passed itself are from the
    conversation, whatever it called them."""
    try:
        return sources.parse(asked, datasets)
    except ChartError:
        if not points:
            raise
        return sources.parse(sources.CONVERSATION, datasets)


# The service each source is read from, as the user knows it.
_SERVICES = {
    "sheet": "Google Sheets",
    "calendar": "Google Calendar",
    "mail": "Gmail",
    "drive": "Google Drive",
}


async def _load(
    session: aiohttp.ClientSession,
    source: dict[str, Any],
    points: list[str] | None = None,
) -> Any:
    with reading(_SERVICES.get(source["from"], "Google")):
        return await sources.load(session, source, points)


def _http() -> aiohttp.ClientSession:
    return utils.http_context.http_session()


def _why(error: Exception) -> str:
    if isinstance(error, NotConnectedError):
        return "Orbit is not connected to Google."
    if isinstance(error, GoogleApiError) and error.api_not_enabled:
        return "the Google Sheets API is switched off for Orbit's Google project."
    if isinstance(error, GoogleApiError) and error.status == 403:
        return (
            "Google refused: the Sheets API may be switched off for Orbit's Google "
            "project, or Orbit may lack access to that spreadsheet."
        )
    return "Google could not be reached."


def _told(view: dict[str, Any], chart_id: str, this_time: str) -> dict[str, Any]:
    told: dict[str, Any] = {
        "on_screen": view["title"],
        "chart_id": chart_id,
        "this_time": this_time,
        "gist": gist(view),
        "say": SAY,
    }
    if view.get("notes"):
        told["notes"] = view["notes"]
    if view["y"].get("levels"):
        told["order"] = REORDER
    return told


async def _show(chart_id: str, view: dict[str, Any]) -> None:
    store.put_view(chart_id, view)
    await stage.show_chart(chart_id, view["title"], view["source"].get("label", ""))


async def _make(
    context: RunContext[OrbitState],
    session: aiohttp.ClientSession,
    measured: str,
    source: dict[str, Any],
    request: Request,
    view: dict[str, Any],
    this_time: str = "made",
    cut: bool = False,
) -> dict[str, Any]:
    """Shows a new chart, then saves it: into Orbit charts, or, the first time, as a
    draft of making Orbit charts that waits for the user's yes."""
    from .actions import draft_first_save

    chart_id = store.new_id()
    view = {**view, "chart_id": chart_id, "saved": None}
    await _show(chart_id, view)
    told = _told(view, chart_id, this_time)
    if cut:
        told["page_cut_short"] = CUT_SHORT
    made = {"source": source, **request.as_dict()}
    book = store.book()
    if book is None:
        try:
            told["saving"] = draft_first_save(context, chart_id, measured, made, view)
        except ToolError as e:
            told["not_saved"] = NOT_SAVED.format(why=str(e))
        return told
    try:
        place = await saving.add(session, book.spreadsheet_id, view)
    except saving.GoneError:
        # The user deleted Orbit charts: it is made again, with their yes.
        store.lose_book()
        told["saving"] = draft_first_save(context, chart_id, measured, made, view)
        return told
    except (GoogleApiError, NotConnectedError, aiohttp.ClientError, TimeoutError) as e:
        told["not_saved"] = NOT_SAVED.format(why=_why(e))
        return told
    view = {**view, "saved": saving.saved_as(place)}
    store.save(
        chart_id, measured, request.kind, made, view, place.spreadsheet_id,
        place.spreadsheet_title, place.tab_id, place.tab_title, place.chart_id,
        place.link,
    )  # fmt: skip
    await _show(chart_id, view)
    told["saved_in"] = f"{place.spreadsheet_title}, in the tab {place.tab_title}"
    return told


async def _again(
    context: RunContext[OrbitState],
    session: aiohttp.ClientSession,
    saved: store.Saved,
    kind: str,
    fresh: bool,
    points: list[str] | None = None,
    levels: list[str] | None = None,
) -> dict[str, Any]:
    """Shows a saved chart: pulled from its tab, drawn as another kind or with its
    levels in another order, or with fresh numbers from its source, or for numbers
    from a conversation, the new ones given. One whose tab is gone is made again."""
    kind = kind.strip() or saved.kind
    if kind not in KINDS:
        raise ToolError(NO_KIND.format(kinds=", ".join(KINDS)))
    if family(kind) != family(saved.kind):
        raise ToolError(OTHER_FAMILY.format(was=saved.kind, kind=kind))
    request = replace(Request.of(saved.request), kind=kind)
    reordered = bool(levels) and tuple(levels or ()) != request.levels
    if reordered:
        request = replace(request, levels=tuple(levels or ()))
    source = saved.request["source"]
    measured, remeasured, cut = saved.key, reordered, False
    if fresh and source["from"] in sources.GIVEN:
        if not points:
            raise ToolError(FIXED)
        given = await _given(context, points)
        source = {
            **source,
            "from": sources.CONVERSATION,
            "points": [" ".join(str(item).split()) for item in points],
            "label": given.label,
            "written": given.written,
        }
        measured, remeasured, cut = key(source, request), True, given.cut
    try:
        if fresh or reordered:
            view = build(await _load(session, source), request)
            view = {**view, "chart_id": saved.id, "saved": saved.view.get("saved")}
            await saving.rewrite(session, saved, view)
            this_time = "updated" if fresh else "changed"
        else:
            view = await saving.pull(session, saved)
            this_time = "pulled"
            if kind != saved.kind:
                view = with_kind(view, kind)
                await saving.redraw(session, saved, view)
                this_time = "changed"
    except saving.GoneError:
        store.forget(saved.id)
        view = build(await _load(session, source), request)
        return await _make(
            context, session, measured, source, request, view, "made again", cut
        )
    except (GoogleApiError, NotConnectedError, aiohttp.ClientError, TimeoutError) as e:
        # Shown as it was last saved, with the reason it could not be pulled.
        view = with_kind(saved.view, kind) if family(kind) == "category" else saved.view
        await _show(saved.id, view)
        told = _told(view, saved.id, "shown as last saved")
        told["not_pulled"] = f"{_why(e)} {OLDER.format(when=view['numbers_from'][:10])}"
        return told
    if remeasured:
        made = {"source": source, **request.as_dict()}
        store.changed(saved.id, kind, view, made, measured)
    else:
        store.changed(saved.id, kind, view)
    await _show(saved.id, view)
    told = _told(view, saved.id, this_time)
    told["numbers_from"] = view["numbers_from"][:10]
    if cut:
        told["page_cut_short"] = CUT_SHORT
    return told


def _named(asked: str) -> store.Saved:
    found = store.named(asked)
    if len(found) == 1:
        return found[0]
    if found:
        names = ", ".join(chart.title for chart in found)
        raise ToolError(SEVERAL_CHARTS.format(asked=asked, names=names))
    saved = store.listed()
    if not saved:
        raise ToolError(NO_CHARTS)
    names = ", ".join(chart.title for chart in saved)
    raise ToolError(NO_SUCH_CHART.format(asked=asked, names=names))


@function_tool()
async def show_chart(
    context: RunContext[OrbitState],
    chart: str = "",
    kind: str = "",
    source: str = "",
    x: str = "",
    y: list[str] | None = None,
    how: str = "",
    series: str = "",
    per: str = "",
    title: str = "",
    points: list[str] | None = None,
    fresh: bool = False,
    levels: list[str] | None = None,
) -> dict[str, Any]:
    """Put a chart on the user's screen, only when they ask for a chart or a graph.
    It can chart numbers from anywhere: a source read in full, or numbers in this
    conversation. Name a source and how to chart it, and every number is worked out
    from it. Pass values yourself only from this conversation: ones the user said,
    or a tool gave, such as on a web page the browser read. Levels in words, such as
    5y+ or Expert, are charted in order. A chart is saved into Google Sheets when it
    is made; asked for again, it is pulled from there.

    Args:
        chart: A saved chart's name, to show it again. With kind, it is drawn as that
            kind; with fresh, its numbers are read again from its source.
        kind: bar, horizontal_bar, stacked_bar, stacked_horizontal_bar, percent_bar,
            line, area, stacked_area, pie, donut, scatter or histogram.
        source: Where the numbers are: a dataset as sheets_read named it,
            "conversation" for numbers said or given in this conversation,
            "calendar: YYYY-MM-DD to YYYY-MM-DD", "mail: <a Gmail search>",
            "reminders", "drive:<file id>" or "added:<file name>".
        x: The column along the bottom, or of the labels.
        y: The columns of numbers. Leave empty to count rows.
        how: sum, average, count, min or max, for rows that share a label.
        series: A column whose values make separate bars or lines.
        per: day, week, month, quarter or year, for dates along the bottom.
        title: Leave empty for one made from the columns.
        points: For "conversation": each label with its value, exactly as it was
            said or given for that label, as "label: value", such as "March: 8"
            or "Python: 5y+", or "label: value | value" for several columns in y.
            A value is a number or a level in words, never turned from one into
            the other. Name the columns with x and y. Asked for all of something,
            give every one.
        fresh: True to read a saved chart's numbers again from its source. For one
            of numbers from a conversation, pass the new numbers as points.
        levels: For levels in words: their order, lowest first, when it is not
            plain from them.
    """
    session = _http()
    try:
        if chart.strip():
            # A name for a new chart, when nothing saved is called it and the chart
            # to make is given.
            making = kind.strip() and (source.strip() or points)
            if store.named(chart) or not making:
                saved = _named(chart)
                return await _again(
                    context, session, saved, kind, fresh, points, levels
                )
            title = title.strip() or chart.strip()
        if kind.strip() not in KINDS:
            raise ToolError(NO_KIND.format(kinds=", ".join(KINDS)))
        request = Request(
            kind.strip(),
            x,
            tuple(y or ()),
            how,
            series,
            per,
            title,
            tuple(levels or ()),
        )
        found = _source(source, points, context.userdata.datasets)
        given = found["from"] == sources.CONVERSATION
        if given:
            request = _columns(request)
            found["columns"] = [request.x, *request.y]
            found["points"] = [" ".join(str(item).split()) for item in points or []]
        measured = key(found, request)
        if saved := store.with_key(measured):
            return await _again(context, session, saved, kind, fresh, points, levels)
        cut = False
        if given:
            values = await _given(context, points or [])
            found["label"], found["written"], cut = (
                values.label,
                values.written,
                values.cut,
            )
        view = build(await _load(session, found, points), request)
        return await _make(context, session, measured, found, request, view, cut=cut)
    except ChartError as e:
        raise ToolError(str(e)) from e


def charts_note() -> str:
    """What the model is told of the saved charts, or nothing if there are none."""
    saved = store.listed()
    if not saved:
        return ""
    named = [
        f'"{chart.title}" ({chart.view["source"].get("label", "")}, '
        f"{chart.kind.replace('_', ' ')})"
        for chart in saved[-LISTED:]
    ]
    listing = "; ".join(named)
    if len(saved) > LISTED:
        listing += f"; and {len(saved) - LISTED} older ones, found by name"
    return CHARTS.format(listing=listing)


CHART_TOOLS = [show_chart]
