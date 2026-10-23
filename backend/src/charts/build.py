"""Charts worked out from datasets: every number a chart shows is computed here.

A request says what to chart: its kind, the column along the bottom, the number
columns, how rows that share a label combine, a column that splits them into series,
and how dates are bucketed. From it and a dataset this works out the chart as the
frontend draws it (its view). From a view come the table of its numbers, as it is
saved into Google Sheets, and the gist the model may speak. A view pulled back from
Sheets gives both again in the same way.

What a chart measures is kept apart from how it is drawn. Its key leaves out the
kind (within bars, lines, areas and pies) and the title, so that a chart asked for
again is found, and "make it a pie" changes the chart instead of making another.
"""

import hashlib
import json
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .data import Column, Dataset, blank, columns, day, number

# kind: (what is drawn, which way, how series stack)
KINDS: dict[str, tuple[str, str, str]] = {
    "bar": ("bar", "vertical", "none"),
    "horizontal_bar": ("bar", "horizontal", "none"),
    "stacked_bar": ("bar", "vertical", "stacked"),
    "stacked_horizontal_bar": ("bar", "horizontal", "stacked"),
    "percent_bar": ("bar", "vertical", "percent"),
    "line": ("line", "vertical", "none"),
    "area": ("area", "vertical", "none"),
    "stacked_area": ("area", "vertical", "stacked"),
    "pie": ("pie", "vertical", "none"),
    "donut": ("donut", "vertical", "none"),
    "scatter": ("scatter", "vertical", "none"),
    "histogram": ("histogram", "vertical", "none"),
}
# Kinds that draw the same table of labels and values, and so can stand in for each
# other on one chart.
CATEGORY_KINDS = frozenset(
    k
    for k, (drawn, *_) in KINDS.items()
    if drawn in {"bar", "line", "area", "pie", "donut"}
)
HOWS = ("sum", "average", "count", "min", "max")
_HOW_WORDS = {
    "sum": "sum", "total": "sum", "add": "sum", "average": "average", "mean": "average",
    "avg": "average", "count": "count", "number": "count", "how many": "count",
    "min": "min", "minimum": "min", "lowest": "min", "max": "max", "maximum": "max",
    "highest": "max",
}  # fmt: skip
PERS = ("day", "week", "month", "quarter", "year")

MAX_SLICES = 8
MAX_CATEGORIES = 30
# Bars stand on the bottom for up to this many labels. More are drawn from the left,
# where each label has a line of its own, and up to MAX_ROWS of them.
MAX_COLUMNS = 12
MAX_ROWS = 100
# Words rather than numbers, such as 5y+ or Expert, are charted as levels when a
# column has no more than this many different ones.
MAX_LEVELS = 12
MAX_SERIES = 8
MAX_POINTS = 2000
MAX_BUCKETS = 120
MIN_BINS, MAX_BINS = 5, 40
OTHER = "Other"
BLANK = "(blank)"

NO_KIND = "Say which kind of chart: {kinds}."
NO_X = "Say which column goes along the bottom, as x. The columns are: {names}."
NO_COLUMN = "There is no column called {asked!r}. The columns are: {names}."
SEVERAL = "{asked!r} fits several columns: {names}. Say which."
NOT_NUMBERS = (
    "{column} has no numbers in it. The columns with numbers are: {numbers}. If its "
    "words are levels, give their order as levels, lowest first."
)
LEVELS_DRAWN = (
    "Levels in words, such as {example}, are drawn as bar, horizontal_bar or line."
)
LEVELS_HOW = "Levels cannot be added or averaged: how is max or min for them."
LEVELS_MIXED = "Give y as columns of numbers, or of levels, not both."
EACH_ONCE = (
    "Each {x} is there once, so counting gives 1 for each, which says nothing. Chart "
    "a value for each instead: a column of numbers or levels as y, or for numbers "
    'from the conversation, each label with its value, such as "Python: 5y+".'
)
NO_NUMBER_COLUMNS = (
    "The data has no column of numbers to chart. Count rows instead, with no y."
)
ONE_Y_WITH_SERIES = "With series, give one column of numbers as y."
BAD_HOW = "how is one of: sum, average, count, min, max."
HOW_NEEDS_Y = "To {how} something, say which column of numbers as y."
BAD_PER = "per is one of: day, week, month, quarter, year."
PER_NEEDS_DATES = "per buckets dates, and {column} has no dates in it."
PIE_ONE_SERIES = (
    "A pie shows one set of numbers. Use a bar chart for several, or give one y."
)
PIE_NEGATIVE = (
    "A pie needs values above zero, and {column} has some below. Use a bar chart."
)
NOTHING_LEFT = "None of the rows have both a {x} and a number to chart."
SCATTER_NEEDS = "A scatter plot needs two columns of numbers: x and one y."
HISTOGRAM_NEEDS = "A histogram needs one column of numbers, as x. The columns with numbers are: {numbers}."
TOO_FEW = "There are fewer than two numbers to chart."

# Words that name a level, lowest first, as such scales usually run.
_LEVEL_WORDS = (
    "none never",
    "beginner novice basic elementary familiar learning entry junior low poor rarely",
    "intermediate competent working conversational medium moderate average fair "
    "sometimes mid",
    "advanced proficient experienced fluent strong high good often senior",
    "expert master mastery native excellent always principal lead",
)
_TIERS = {
    word: tier for tier, words in enumerate(_LEVEL_WORDS) for word in words.split()
}
_A_NUMBER = re.compile(r"\d+(?:\.\d+)?")


class ChartError(Exception):
    """A chart that cannot be made as asked. Its message says what to do instead."""


@dataclass(frozen=True)
class Request:
    kind: str
    x: str = ""
    y: tuple[str, ...] = ()
    how: str = ""
    series: str = ""
    per: str = ""
    title: str = ""
    # For levels in words: their order, lowest first, when asked for.
    levels: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "x": self.x,
            "y": list(self.y),
            "how": self.how,
            "series": self.series,
            "per": self.per,
            "title": self.title,
            "levels": list(self.levels),
        }

    @classmethod
    def of(cls, data: dict[str, Any]) -> "Request":
        return cls(
            kind=data.get("kind", ""),
            x=data.get("x", ""),
            y=tuple(data.get("y", ())),
            how=data.get("how", ""),
            series=data.get("series", ""),
            per=data.get("per", ""),
            title=data.get("title", ""),
            levels=tuple(data.get("levels", ())),
        )


def family(kind: str) -> str:
    """What a kind of chart measures: a table of labels and values, points or bins."""
    return "category" if kind in CATEGORY_KINDS else kind


def how_of(asked: str, has_y: bool) -> str:
    said = " ".join(asked.casefold().split())
    if not said:
        return "sum" if has_y else "count"
    if said not in _HOW_WORDS:
        raise ChartError(BAD_HOW)
    return _HOW_WORDS[said]


def per_of(asked: str) -> str:
    said = asked.strip().casefold().removesuffix("ly").removesuffix("s")
    said = {
        "dai": "day",
        "week": "week",
        "month": "month",
        "quarter": "quarter",
        "year": "year",
    }.get(said, said)
    if said and said not in PERS:
        raise ChartError(BAD_PER)
    return said


def key(source: dict[str, Any], request: Request) -> str:
    """What a chart measures, as a short key: the same for the same chart asked for
    again, whatever kind it is drawn as within its family, and whatever its title."""
    identity = {k: v for k, v in source.items() if k not in _SHOWN_ONLY}
    measure = {
        "source": identity,
        "family": family(request.kind),
        "x": " ".join(request.x.casefold().split()),
        "y": sorted(" ".join(name.casefold().split()) for name in request.y),
        "how": how_of(request.how, bool(request.y)),
        "series": " ".join(request.series.casefold().split()),
        "per": per_of(request.per),
    }
    text = json.dumps(measure, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(text.encode()).hexdigest()[:16]


# Parts of a source that only say how to show it, not what it is.
_SHOWN_ONLY = frozenset({"label", "title", "link", "noun", "written"})


def build(dataset: Dataset, request: Request) -> dict[str, Any]:
    """The view of a chart: everything the frontend draws, worked out."""
    if request.kind not in KINDS:
        raise ChartError(NO_KIND.format(kinds=", ".join(KINDS)))
    found = columns(dataset)
    if request.kind == "scatter":
        view = _scatter(dataset, found, request)
    elif request.kind == "histogram":
        view = _histogram(dataset, found, request)
    else:
        view = _categories(dataset, found, request)
    view["notes"] = [*dataset.notes, *view["notes"]]
    view["source"] = {
        "from": dataset.source.get("from", ""),
        "label": dataset.source.get("label") or dataset.name,
        "link": dataset.source.get("link"),
    }
    view["numbers_from"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return view


def _find(found: list[Column], asked: str) -> Column:
    said = " ".join(asked.casefold().split())
    names = ", ".join(column.name for column in found)
    for matching in (
        [c for c in found if c.name.casefold() == said],
        [c for c in found if said and c.name.casefold().startswith(said)],
        [c for c in found if said and said in c.name.casefold()],
    ):
        if len(matching) == 1:
            return matching[0]
        if matching:
            several = ", ".join(column.name for column in matching)
            raise ChartError(SEVERAL.format(asked=asked, names=several))
    raise ChartError(NO_COLUMN.format(asked=asked, names=names))


def _values(dataset: Dataset, found: list[Column], asked: str, ordered: bool) -> Column:
    """A column to chart the values of: one of numbers, or of a few levels in words.
    Words are levels when their order was given, or when most of them read as
    levels do: with a number in them, such as 5y+, or a word such as Expert."""
    column = _find(found, asked)
    if column.kind == "number":
        return column
    texts = {_text(cell) for cell in dataset.cells(column.index) if not blank(cell)}
    leveled = sum(1 for text in texts if _A_NUMBER.search(text) or _tier(text) >= 0)
    many = 0 < len(texts) <= MAX_LEVELS
    if column.kind == "text" and many and (ordered or leveled * 2 >= len(texts)):
        return column
    numbers = ", ".join(c.name for c in found if c.kind == "number") or "none"
    raise ChartError(NOT_NUMBERS.format(column=column.name, numbers=numbers))


def _text(cell: Any) -> str:
    return " ".join(str(cell).split())


def level_order(found: list[str], asked: tuple[str, ...] = ()) -> list[str]:
    """Levels in words in order, lowest first. As asked, or else: those with a number
    in them, such as 2y+, by that number, after those with none. Those are ordered as
    such words usually run, Beginner before Expert, and others as first met."""
    met = list(dict.fromkeys(found))

    def place(level: str) -> tuple[int, float, int]:
        if found_number := _A_NUMBER.search(level):
            return (1, float(found_number[0]), 0)
        return (0, 0.0, _tier(level))

    usual = sorted(met, key=place)
    if not asked:
        return usual
    # As asked, in the data's own spelling. A level left out of the order goes
    # after the one it usually follows.
    spelled = {level.casefold(): level for level in met}
    order = [
        spelled.get(level.casefold(), level)
        for level in dict.fromkeys(_text(level) for level in asked if level.strip())
    ]
    for n, level in enumerate(usual):
        if level.casefold() not in {named.casefold() for named in order}:
            before = [order.index(earlier) for earlier in usual[:n] if earlier in order]
            order.insert(max(before) + 1 if before else 0, level)
    return order


def _tier(level: str) -> int:
    """Where a level in words usually stands, from 0 for the lowest, or -1."""
    tiers = [_TIERS[w] for w in re.findall(r"[a-z]+", level.casefold()) if w in _TIERS]
    return tiers[0] if tiers else -1


def _place_of(cell: Any, levels: list[str]) -> float | None:
    """A level's place on its scale, from 1 for the lowest."""
    folded = [level.casefold() for level in levels]
    text = _text(cell).casefold() if not blank(cell) else ""
    return float(folded.index(text) + 1) if text in folded else None


def _level_named(value: float | None, levels: list[str]) -> str | None:
    return levels[int(value) - 1] if value and 0 < value <= len(levels) else None


def _sideways(drawn: str, orientation: str, x_type: str, labels: int) -> bool:
    """Bars are drawn from the left when asked, or when there are too many labels to
    stand side by side."""
    return drawn == "bar" and (
        orientation == "horizontal" or (x_type == "category" and labels > MAX_COLUMNS)
    )


def _numbers(found: list[Column], asked: str) -> Column:
    column = _find(found, asked)
    if column.kind != "number":
        numbers = ", ".join(c.name for c in found if c.kind == "number") or "none"
        raise ChartError(NOT_NUMBERS.format(column=column.name, numbers=numbers))
    return column


def _view(request: Request, **parts: Any) -> dict[str, Any]:
    drawn, orientation, stacking = KINDS[request.kind]
    return {
        "version": 1,
        "kind": drawn,
        "orientation": orientation,
        "stacking": stacking,
        **parts,
    }


def bucket(moment: date, per: str) -> date:
    """The first day of the day, week (from Monday), month, quarter or year a date is in."""
    if per == "day":
        return moment
    if per == "week":
        return moment - timedelta(days=moment.weekday())
    if per == "month":
        return moment.replace(day=1)
    if per == "quarter":
        return date(moment.year, 3 * ((moment.month - 1) // 3) + 1, 1)
    return date(moment.year, 1, 1)


def _after(start: date, per: str) -> date:
    """The first day of the bucket after the one starting on `start`."""
    if per == "day":
        return start + timedelta(days=1)
    if per == "week":
        return start + timedelta(days=7)
    months = {"month": 1, "quarter": 3, "year": 12}[per]
    index = start.year * 12 + start.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def _auto_per(first: date, last: date) -> str:
    span = (last - first).days
    if span <= 31:
        return "day"
    if span <= 26 * 7:
        return "week"
    if span <= 3 * 366:
        return "month"
    return "year"


def _combine(values: list[float], how: str) -> float | None:
    if how == "count":
        return float(len(values))
    if not values:
        return None
    if how == "sum":
        return math.fsum(values)
    if how == "average":
        return math.fsum(values) / len(values)
    return min(values) if how == "min" else max(values)


def _title(
    dataset: Dataset, how: str, ys: list[Column], x: Column, per: str, levels: bool
) -> str:
    noun = dataset.source.get("noun", "Rows")
    if how == "count":
        what = noun
    else:
        names = " and ".join(column.name for column in ys)
        plain = how == "sum" or levels
        what = names if plain else f"{how.capitalize()} {names.casefold()}"
    return f"{what} per {per}" if per else f"{what} by {x.name}"


def _categories(
    dataset: Dataset, found: list[Column], request: Request
) -> dict[str, Any]:
    drawn = KINDS[request.kind][0]
    if not request.x.strip():
        raise ChartError(NO_X.format(names=", ".join(c.name for c in found)))
    x = _find(found, request.x)
    ordered = bool(request.levels)
    ys = [_values(dataset, found, name, ordered) for name in request.y if name.strip()]
    # Levels in words, such as 5y+ or Expert, each drawn at its place on their scale.
    levels: list[str] = []
    if any(column.kind != "number" for column in ys):
        if any(column.kind == "number" for column in ys):
            raise ChartError(LEVELS_MIXED)
        texts = [
            _text(cell)
            for column in ys
            for cell in dataset.cells(column.index)
            if not blank(cell)
        ]
        if drawn not in ("bar", "line") or KINDS[request.kind][2] != "none":
            raise ChartError(LEVELS_DRAWN.format(example=texts[0]))
        levels = level_order(texts, request.levels)
    split = _find(found, request.series) if request.series.strip() else None
    if split and len(ys) > 1:
        raise ChartError(ONE_Y_WITH_SERIES)
    how = "max" if levels and not request.how.strip() else how_of(request.how, bool(ys))
    if how != "count" and not ys:
        raise ChartError(HOW_NEEDS_Y.format(how=how))
    per = per_of(request.per)
    if per and x.kind != "date":
        raise ChartError(PER_NEEDS_DATES.format(column=x.name))
    notes: list[str] = []

    # Each row's place along the axis.
    places: list[Any] = []
    skipped = 0
    for value in dataset.cells(x.index):
        if x.kind == "date":
            place = day(value, dataset.day_first)
        elif x.kind == "number":
            place = number(value)
        else:
            place = BLANK if blank(value) else " ".join(str(value).split())
        if place is None:
            skipped += 1
        places.append(place)
    dated = [place for place in places if isinstance(place, date)]
    if x.kind == "date" and dated:
        asked_per = per
        per = per or _auto_per(min(dated), max(dated))
        while _count_buckets(min(dated), max(dated), per) > MAX_BUCKETS:
            per = PERS[PERS.index(per) + 1]
        if asked_per and per != asked_per:
            notes.append(f"By {per}, since by {asked_per} would be too many to draw.")
        places = [
            bucket(place, per) if isinstance(place, date) else None for place in places
        ]
    if levels and how not in ("max", "min"):
        # How levels combine matters only where labels come more than once.
        placed = [place for place in places if place is not None]
        if len(placed) != len(set(placed)):
            raise ChartError(LEVELS_HOW)
        how = "max"
    if skipped:
        what = (
            "a date" if x.kind == "date" else "a number" if x.kind == "number" else ""
        )
        notes.append(
            f"{_rows(skipped)} without {what or 'a value'} in {x.name} left out."
        )

    # The values of each place, by series.
    groups: dict[tuple[Any, str], list[float]] = defaultdict(list)
    order: dict[str, int] = {}
    missing = 0
    for index, place in enumerate(places):
        if place is None:
            continue
        row = dataset.rows[index]
        part = None
        if split is not None:
            cell = row[split.index] if split.index < len(row) else None
            part = BLANK if blank(cell) else " ".join(str(cell).split())
        if not ys:
            name = part or "Count"
            order.setdefault(name, len(order))
            groups[(place, name)].append(1.0)
            continue
        for column in ys:
            cell = row[column.index] if column.index < len(row) else None
            value = _place_of(cell, levels) if levels else number(cell)
            name = part or column.name
            order.setdefault(name, len(order))
            if value is None:
                missing += 1
                continue
            groups[(place, name)].append(value)
    if missing:
        what = "a level" if levels else "a number"
        notes.append(f"{_rows(missing, 'cell')} without {what} left out.")
    if not groups:
        raise ChartError(NOTHING_LEFT.format(x=x.name))

    # The places along the axis, in order.
    present = {place for place, _ in groups}
    if x.kind == "date":
        first, last = min(present), max(present)
        axis: list[Any] = []
        moment = first
        while moment <= last:
            axis.append(moment)
            moment = _after(moment, per)
    elif x.kind == "number":
        axis = sorted(present)
    else:
        seen: dict[Any, None] = {}
        for place in places:
            if place in present:
                seen.setdefault(place, None)
        axis = list(seen)

    names = sorted(order, key=order.get)
    fills = how in ("sum", "count")
    table = {
        name: [
            _combine(groups.get((place, name), []), how)
            if (place, name) in groups
            else (0.0 if fills else None)
            for place in axis
        ]
        for name in names
    }
    # Counting labels that each come once gives a chart of ones.
    every_value = [value for name in names for value in table[name]]
    if (
        how == "count"
        and x.kind == "text"
        and len(axis) > 2
        and set(every_value) == {1}
    ):
        raise ChartError(EACH_ONCE.format(x=x.name))

    # Text labels in the order that reads best: largest first, but lines keep
    # the data's own order.
    if x.kind == "text" and drawn in {"bar", "pie", "donut"}:
        totals = [sum(abs(table[n][i] or 0) for n in names) for i in range(len(axis))]
        ranked = sorted(range(len(axis)), key=lambda i: -totals[i])
        axis = [axis[i] for i in ranked]
        table = {n: [table[n][i] for i in ranked] for n in names}

    # Too many series: the largest stay, the rest go under Other where they add up.
    if len(names) > MAX_SERIES:
        size = {n: sum(abs(v or 0) for v in table[n]) for n in names}
        kept = sorted(names, key=lambda n: -size[n])[
            : MAX_SERIES - 1 if fills else MAX_SERIES
        ]
        rest = [n for n in names if n not in kept]
        if fills:
            table[OTHER] = [
                math.fsum(table[n][i] or 0 for n in rest) for i in range(len(axis))
            ]
            notes.append(
                f"The {len(kept)} largest of {len(names)} series; {len(rest)} are under Other."
            )
            kept.append(OTHER)
        else:
            notes.append(f"The {len(kept)} largest of {len(names)} series are shown.")
        names = [n for n in names if n in kept] + ([OTHER] if fills else [])
        names = list(dict.fromkeys(names))

    if drawn in {"pie", "donut"}:
        if len(names) > 1:
            raise ChartError(PIE_ONE_SERIES)
        (name,) = names
        if any((value or 0) < 0 for value in table[name]):
            raise ChartError(PIE_NEGATIVE.format(column=name))
        kept_places = [i for i, value in enumerate(table[name]) if value]
        axis = [axis[i] for i in kept_places]
        table = {name: [table[name][i] for i in kept_places]}
        limit = MAX_SLICES
    else:
        limit = MAX_CATEGORIES if x.kind == "text" else MAX_BUCKETS
    x_type = (
        "time" if x.kind == "date" else "number" if x.kind == "number" else "category"
    )
    sideways = _sideways(drawn, KINDS[request.kind][1], x_type, len(axis))
    if sideways and x.kind == "text":
        limit = MAX_ROWS

    # Too many labels: the largest stay, the rest go under Other where they add up.
    if len(axis) > limit and x.kind == "text":
        keep = limit - 1 if fills else limit
        rest = range(keep, len(axis))
        if fills:
            for n in names:
                table[n] = [*table[n][:keep], math.fsum(table[n][i] or 0 for i in rest)]
            notes.append(
                f"The {keep} largest of {len(axis)} {x.name.casefold()} values; {len(axis) - keep} are under Other."
            )
            axis = [*axis[:keep], OTHER]
        else:
            for n in names:
                table[n] = table[n][:keep]
            notes.append(
                f"The {keep} largest of {len(axis)} {x.name.casefold()} values are shown."
            )
            axis = axis[:keep]
    elif len(axis) > limit:
        for n in names:
            table[n] = table[n][:limit]
        notes.append(
            f"The first {limit} of {len(axis)} {x.name.casefold()} values are shown."
        )
        axis = axis[:limit]

    unit = ys[0].unit if ys and all(c.unit == ys[0].unit for c in ys) else ""
    decimals = max((c.decimals for c in ys), default=0)
    if how == "average":
        decimals = max(decimals, 1)
    if how == "count" or levels:
        unit, decimals = "", 0
    y_label = "Count" if how == "count" else " and ".join(c.name for c in ys)
    y_axis: dict[str, Any] = {"label": y_label, "unit": unit, "decimals": decimals}
    if levels:
        # A value is a level's place on this scale, from 1 for the lowest.
        y_axis["levels"] = levels
    view = _view(
        request,
        title=request.title.strip()
        or _title(dataset, how, ys, x, per if x.kind == "date" else "", bool(levels)),
        how=how,
        x={
            "label": x.name,
            "type": x_type,
            "per": per if x.kind == "date" else None,
            "decimals": x.decimals,
        },
        y=y_axis,
        categories=[_label(place) for place in axis],
        series=[{"name": n, "values": table[n]} for n in names],
        notes=notes,
    )
    if sideways:
        view["orientation"] = "horizontal"
    return view


def _level_gist(view: dict[str, Any], levels: list[str]) -> dict[str, Any]:
    """The gist of a chart of levels: which labels are at the highest and the lowest,
    and how many are at each."""
    at: dict[str, list[str]] = {}
    for series in view["series"]:
        for label, value in zip(view["categories"], series["values"], strict=True):
            if (level := _level_named(value, levels)) is not None:
                at.setdefault(level, []).append(label)
    present = [level for level in levels if level in at]
    told: dict[str, Any] = {
        "labels": len(view["categories"]),
        "levels": "lowest first: " + ", ".join(levels),
    }
    if present:
        top, bottom = present[-1], present[0]
        told["highest"] = {"level": top, "labels": at[top][:8], "count": len(at[top])}
        told["lowest"] = {
            "level": bottom,
            "labels": at[bottom][:8],
            "count": len(at[bottom]),
        }
        told["at_each"] = {level: len(at[level]) for level in reversed(present)}
    return told


def _rows(count: int, what: str = "row") -> str:
    """A count of what was left out, as a note says it: "1 row is", "3 rows are"."""
    return f"1 {what} is" if count == 1 else f"{count} {what}s are"


def _count_buckets(first: date, last: date, per: str) -> int:
    count, moment = 0, bucket(first, per)
    while moment <= last:
        count += 1
        if count > MAX_BUCKETS:
            break
        moment = _after(moment, per)
    return count


def _label(place: Any) -> str:
    if isinstance(place, date):
        return place.isoformat()
    if isinstance(place, float):
        return str(int(place)) if place == int(place) else f"{place:g}"
    return str(place)


def _scatter(dataset: Dataset, found: list[Column], request: Request) -> dict[str, Any]:
    if not request.x.strip() or len([y for y in request.y if y.strip()]) != 1:
        raise ChartError(SCATTER_NEEDS)
    x = _numbers(found, request.x)
    y = _numbers(found, next(y for y in request.y if y.strip()))
    split = _find(found, request.series) if request.series.strip() else None
    points = []
    for row in dataset.rows:
        across = number(row[x.index] if x.index < len(row) else None)
        up = number(row[y.index] if y.index < len(row) else None)
        if across is None or up is None:
            continue
        point: dict[str, Any] = {"x": across, "y": up}
        if split is not None:
            cell = row[split.index] if split.index < len(row) else None
            point["series"] = BLANK if blank(cell) else " ".join(str(cell).split())
        points.append(point)
    if len(points) < 2:
        raise ChartError(TOO_FEW)
    notes = []
    left_out = len(dataset.rows) - len(points)
    if left_out:
        notes.append(f"{_rows(left_out)} without both numbers left out.")
    if len(points) > MAX_POINTS:
        step = len(points) / MAX_POINTS
        notes.append(f"{MAX_POINTS} of {len(points)} points, taken evenly, are shown.")
        points = [points[int(i * step)] for i in range(MAX_POINTS)]
    names = list(dict.fromkeys(p["series"] for p in points if "series" in p))
    return _view(
        request,
        title=request.title.strip() or f"{y.name} against {x.name}",
        how="none",
        x={"label": x.name, "type": "number", "unit": x.unit, "decimals": x.decimals},
        y={"label": y.name, "unit": y.unit, "decimals": y.decimals},
        points=points,
        series_names=names,
        notes=notes,
    )


def _nice(step: float) -> float:
    """A step of 1, 2, 2.5 or 5 times a power of ten, no smaller than `step`."""
    if step <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(step))
    for factor in (1, 2, 2.5, 5, 10):
        if factor * power >= step - 1e-12:
            return factor * power
    return 10 * power


def _histogram(
    dataset: Dataset, found: list[Column], request: Request
) -> dict[str, Any]:
    asked = request.x.strip() or next((y for y in request.y if y.strip()), "")
    if not asked:
        numbers = ", ".join(c.name for c in found if c.kind == "number") or "none"
        raise ChartError(HISTOGRAM_NEEDS.format(numbers=numbers))
    column = _numbers(found, asked)
    values = sorted(
        v for v in (number(c) for c in dataset.cells(column.index)) if v is not None
    )
    if len(values) < 2:
        raise ChartError(TOO_FEW)
    low, high = values[0], values[-1]
    if high == low:
        width = _nice(abs(low) or 1.0)
        start = math.floor(low / width) * width
        edges = [start, start + width]
    else:
        quartiles = statistics.quantiles(values, n=4)
        spread = quartiles[2] - quartiles[0]
        width = 2 * spread / len(values) ** (1 / 3) if spread > 0 else 0.0
        count = (
            math.ceil((high - low) / width)
            if width
            else math.ceil(math.log2(len(values)) + 1)
        )
        count = min(max(count, MIN_BINS), MAX_BINS)
        width = _nice((high - low) / count)
        start = math.floor(low / width) * width
        edges = [start]
        while edges[-1] <= high:
            edges.append(round(edges[-1] + width, 10))
    counts = [0] * (len(edges) - 1)
    for value in values:
        index = min(int((value - edges[0]) // width), len(counts) - 1)
        counts[index] += 1
    left_out = len(dataset.rows) - len(values)
    notes = (
        [f"{_rows(left_out)} without a number in {column.name} left out."]
        if left_out
        else []
    )
    return _view(
        request,
        title=request.title.strip() or f"How {column.name.casefold()} is spread",
        how="count",
        x={
            "label": column.name,
            "type": "number",
            "unit": column.unit,
            "decimals": column.decimals,
        },
        y={"label": "Count", "unit": "", "decimals": 0},
        bins=[
            {"from": edges[i], "to": edges[i + 1], "count": counts[i]}
            for i in range(len(counts))
        ],
        notes=notes,
    )


def bin_label(start: float, end: float) -> str:
    return f"{start:g}\u2013{end:g}"


def table_of(view: dict[str, Any]) -> list[list[Any]]:
    """The numbers of a chart as a table, headers first: what is saved into Sheets,
    and what is pulled back from there."""
    if view["kind"] == "histogram":
        return [
            ["Range", "Count"],
            *([bin_label(b["from"], b["to"]), b["count"]] for b in view["bins"]),
        ]
    if view["kind"] == "scatter":
        names = view.get("series_names") or []
        if not names:
            return [
                [view["x"]["label"], view["y"]["label"]],
                *([p["x"], p["y"]] for p in view["points"]),
            ]
        rows = []
        for point in view["points"]:
            row: list[Any] = [point["x"], *([None] * len(names))]
            row[1 + names.index(point["series"])] = point["y"]
            rows.append(row)
        return [[view["x"]["label"], *names], *rows]
    header = [view["x"]["label"], *(s["name"] for s in view["series"])]
    if levels := view["y"].get("levels"):
        # Each level in words, then its place on the scale, which Sheets draws.
        scale = f"(1-{len(levels)})"
        return [
            [*header, *(f"{s['name']} {scale}" for s in view["series"])],
            *(
                [
                    label,
                    *(_level_named(s["values"][i], levels) for s in view["series"]),
                    *(s["values"][i] for s in view["series"]),
                ]
                for i, label in enumerate(view["categories"])
            ),
        ]
    return [
        header,
        *(
            [label, *(s["values"][i] for s in view["series"])]
            for i, label in enumerate(view["categories"])
        ),
    ]


def from_table(view: dict[str, Any], table: list[list[Any]]) -> dict[str, Any]:
    """A saved view with its numbers taken from a table pulled back from Sheets, where
    the user may have corrected them."""
    pulled = dict(view)
    header, rows = (table[0], table[1:]) if table else ([], [])
    if view["kind"] == "histogram":
        bins = []
        for i, row in enumerate(rows):
            count = number(row[1]) if len(row) > 1 else None
            if i < len(view["bins"]):
                edges = view["bins"][i]
                bins.append(
                    {"from": edges["from"], "to": edges["to"], "count": int(count or 0)}
                )
        pulled["bins"] = bins
        return pulled
    if view["kind"] == "scatter":
        # Saved with series, each series has a column of its own after x.
        names = [str(name) for name in header[1:]] if view.get("series_names") else []
        points = []
        for row in rows:
            across = number(row[0]) if row else None
            if across is None:
                continue
            if names:
                for i, name in enumerate(names):
                    up = number(row[1 + i]) if len(row) > 1 + i else None
                    if up is not None:
                        points.append({"x": across, "y": up, "series": name})
            elif len(row) > 1 and (up := number(row[1])) is not None:
                points.append({"x": across, "y": up})
        pulled["points"] = points
        pulled["series_names"] = names
        return pulled
    names = [str(name) for name in header[1:]]
    pulled["categories"] = [str(row[0]) if row else "" for row in rows]
    if levels := view["y"].get("levels"):
        # A level as the user may have changed it in words, or else its place.
        count = len(view["series"])

        def place(row: list[Any], i: int) -> float | None:
            said = _place_of(row[1 + i], levels) if len(row) > 1 + i else None
            kept = number(row[1 + count + i]) if len(row) > 1 + count + i else None
            return said if said is not None else kept

        pulled["series"] = [
            {"name": name, "values": [place(row, i) for row in rows]}
            for i, name in enumerate(names[:count])
        ]
        return pulled
    pulled["series"] = [
        {
            "name": name,
            "values": [
                number(row[1 + i]) if len(row) > 1 + i else None for row in rows
            ],
        }
        for i, name in enumerate(names)
    ]
    return pulled


def with_kind(view: dict[str, Any], kind: str) -> dict[str, Any]:
    """A view drawn as another kind of the same family."""
    drawn, orientation, stacking = KINDS[kind]
    levels = view["y"].get("levels")
    if levels and (drawn not in ("bar", "line") or stacking != "none"):
        raise ChartError(LEVELS_DRAWN.format(example=levels[0]))
    if drawn in {"pie", "donut"}:
        if len(view["series"]) != 1:
            raise ChartError(PIE_ONE_SERIES)
        if any((v or 0) < 0 for v in view["series"][0]["values"]):
            raise ChartError(PIE_NEGATIVE.format(column=view["series"][0]["name"]))
    labels = len(view.get("categories") or [])
    if _sideways(drawn, orientation, view["x"].get("type", ""), labels):
        orientation = "horizontal"
    return {**view, "kind": drawn, "orientation": orientation, "stacking": stacking}


def kind_of_view(view: dict[str, Any]) -> str:
    """The kind of chart a view is drawn as, by its name in KINDS."""
    for kind, shape in KINDS.items():
        if shape == (view["kind"], view["orientation"], view["stacking"]):
            return kind
    return view["kind"]


def _said(value: float | None, decimals: int) -> float | None:
    if value is None:
        return None
    rounded = round(value, decimals)
    return int(rounded) if rounded == int(rounded) else rounded


def _said_place(label: str, per: str | None) -> str:
    """A label as it is said: a week by its first day, a month by its name."""
    try:
        moment = date.fromisoformat(label)
    except ValueError:
        return label
    if per == "week":
        return f"the week of {moment.day} {moment:%B %Y}"
    if per == "month":
        return f"{moment:%B %Y}"
    if per == "quarter":
        return f"Q{(moment.month - 1) // 3 + 1} {moment.year}"
    if per == "year":
        return str(moment.year)
    return f"{moment.day} {moment:%B %Y}"


def gist(view: dict[str, Any]) -> dict[str, Any]:
    """What the model may say about a chart: worked out here, so that every number it
    speaks is one the chart shows."""
    if levels := view["y"].get("levels"):
        return _level_gist(view, levels)
    unit = view["y"].get("unit") or ""
    told: dict[str, Any] = {
        "unit": unit or "none: say the numbers as plain numbers, in no currency",
    }
    if view["kind"] == "histogram":
        bins = view["bins"]
        counts = [b["count"] for b in bins]
        busiest = bins[counts.index(max(counts))]
        told.update(
            values=sum(counts),
            most_in=bin_label(busiest["from"], busiest["to"]),
            most=max(counts),
        )
        return told
    if view["kind"] == "scatter":
        points = view["points"]
        xs, ys = [p["x"] for p in points], [p["y"] for p in points]
        told["points"] = len(points)
        if len(points) > 2 and statistics.pstdev(xs) and statistics.pstdev(ys):
            told["correlation"] = round(statistics.correlation(xs, ys), 2)
        return told
    decimals = view["y"].get("decimals", 0)
    per = view["x"].get("per")
    places = view["categories"]
    told["labels"] = len(places)
    adds_up = view.get("how") in ("sum", "count")
    if len(view["series"]) == 1:
        values = view["series"][0]["values"]
        known = [(places[i], v) for i, v in enumerate(values) if v is not None]
        if not known:
            return told
        total = math.fsum(v for _, v in known)
        top = max(known, key=lambda item: item[1])
        bottom = min(known, key=lambda item: item[1])

        def share(value: float) -> str | None:
            if not adds_up or total <= 0 or any(v < 0 for _, v in known):
                return None
            return f"{round(100 * value / total)}%"

        if adds_up:
            told["total"] = _said(total, decimals)
        told["largest"] = {
            "label": _said_place(top[0], per),
            "value": _said(top[1], decimals),
            "share": share(top[1]),
        }
        told["smallest"] = {
            "label": _said_place(bottom[0], per),
            "value": _said(bottom[1], decimals),
            "share": share(bottom[1]),
        }
        if view["x"]["type"] == "time" and len(known) > 1:
            first, last = known[0][1], known[-1][1]
            told["first"] = {
                "label": _said_place(known[0][0], per),
                "value": _said(first, decimals),
            }
            told["last"] = {
                "label": _said_place(known[-1][0], per),
                "value": _said(last, decimals),
            }
            if first:
                told["change"] = f"{round(100 * (last - first) / abs(first))}%"
        return told
    totals = {
        s["name"]: _said(math.fsum(v for v in s["values"] if v is not None), decimals)
        for s in view["series"]
    }
    if adds_up:
        told["totals"] = totals
        told["largest_series"] = max(totals, key=lambda name: totals[name] or 0)
    else:
        told["series"] = list(totals)
    return told
