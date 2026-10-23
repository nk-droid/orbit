"""Tables to chart, and what their columns hold.

A dataset is a table read from one of the user's sources: a tab of a Google Sheet,
the events in a calendar range, the emails a search finds, reminders, a CSV file, or
numbers the user said. Its cells are kept as they came, numbers as numbers where the
source gives them so. What each column holds, and in what unit, is read here.

Numbers are read as people write them in sheets: "₹1,200.50", "(45)" for a minus,
"12%", "1 200". A unit is taken only from the data itself, from a symbol in the
cells or the header: the model, asked to say bare amounts, called them dollars.
"""

import csv
import io
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

Kind = Literal["number", "date", "text"]

# A column is of a kind when this share of its filled cells read as that kind.
_MOSTLY = 0.8
# Symbols that come before an amount, and the codes written for them.
_SYMBOLS = "$€£₹¥₩₽¢"
_CODES = {
    "rs": "₹", "inr": "₹", "usd": "$", "eur": "€", "gbp": "£", "jpy": "¥",
    "krw": "₩", "rub": "₽",
}  # fmt: skip
_CODE = re.compile(r"\b(rs|inr|usd|eur|gbp|jpy|krw|rub)\b\.?", re.IGNORECASE)
_SPACES = re.compile(r"[\s\u00a0\u202f']")
_NUMBER = re.compile(r"[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?")
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%a %d %b %Y",
    "%d-%b-%Y",
    "%d-%b-%y",
)
_DAY_FIRST = ("%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y", "%d-%m-%Y")
_MONTH_FIRST = ("%m/%d/%Y", "%m/%d/%y")
_MONTHS = ("%b %Y", "%B %Y", "%Y-%m")
_TIME = re.compile(
    r"[ T]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?\s*([AaPp][Mm])?([+-]\d{2}:?\d{2}|Z)?$"
)


@dataclass
class Dataset:
    # What the model calls it, such as "Budget / October".
    name: str
    # Where it was read from: enough to read it again.
    source: dict[str, Any]
    headers: list[str]
    rows: list[list[Any]]
    # The cells as the source shows them, where it does: a sheet's formatted cells,
    # in which a currency can be seen. Same shape as rows.
    shown: list[list[str]] | None = None
    # What the source left out, such as messages past a limit.
    notes: list[str] = field(default_factory=list)
    # Whether a date such as 03/10/2026 is the 3rd of October.
    day_first: bool = True

    def cells(self, column: int) -> list[Any]:
        return [row[column] if column < len(row) else None for row in self.rows]

    def shown_cells(self, column: int) -> list[str]:
        source = self.shown or self.rows
        return [str(row[column]) if column < len(row) else "" for row in source]


@dataclass(frozen=True)
class Column:
    index: int
    name: str
    kind: Kind
    # A currency symbol, or "%", when the cells or the header show one.
    unit: str = ""
    # How many decimals its numbers are written with, at most 2.
    decimals: int = 0


def blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def number(value: Any) -> float | None:
    """A cell as a number, or None if it is not one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return None if math.isnan(value) or math.isinf(value) else float(value)
    text = str(value).strip()
    if not text or text in {"-", "\u2014", "\u2013"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = _CODE.sub("", text)
    text = text.strip().strip(_SYMBOLS).strip()
    if text.endswith("%"):
        text = text[:-1]
    text = text.strip(_SYMBOLS).strip()
    text = _SPACES.sub("", text).replace(",", "")
    if not _NUMBER.fullmatch(text):
        return None
    found = float(text)
    return -abs(found) if negative else found


def day(value: Any, day_first: bool = True) -> date | None:
    """A cell as a date, or None if it is not one. A bare number is not a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = _TIME.sub("", value.strip())
    if not text or _NUMBER.fullmatch(text.replace(",", "")):
        return None
    numeric = _DAY_FIRST + _MONTH_FIRST if day_first else _MONTH_FIRST + _DAY_FIRST
    for pattern in (*_DATE_FORMATS, *numeric, *_MONTHS):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def kind_of(values: list[Any], day_first: bool = True) -> Kind:
    """What a column holds: dates, numbers or text, by what most of its cells are."""
    filled = [value for value in values if not blank(value)]
    if not filled:
        return "text"
    dates = sum(1 for value in filled if day(value, day_first) is not None)
    if dates >= _MOSTLY * len(filled):
        return "date"
    numbers = sum(1 for value in filled if number(value) is not None)
    if numbers >= _MOSTLY * len(filled):
        return "number"
    return "text"


def unit_of(header: str, shown: list[str]) -> str:
    """The unit of a column of numbers, as the data shows it, or "" if it shows none."""
    for symbol in _SYMBOLS:
        if symbol in header:
            return symbol
    if found := _CODE.search(header):
        return _CODES[found[1].casefold()]
    if "%" in header:
        return "%"
    filled = [text.strip() for text in shown if text.strip()]
    if not filled:
        return ""
    for symbol in _SYMBOLS:
        if sum(symbol in text for text in filled) * 2 >= len(filled):
            return symbol
    codes = [found[1].casefold() for text in filled if (found := _CODE.search(text))]
    if codes and len(codes) * 2 >= len(filled):
        return _CODES[max(set(codes), key=codes.count)]
    if sum(text.endswith("%") for text in filled) * 2 >= len(filled):
        return "%"
    return ""


def _decimals(shown: list[str], values: list[float]) -> int:
    """How many decimals the numbers are written with, at most 2."""
    most = 0
    for text in shown:
        match = re.search(r"\.(\d+)", text)
        if match and number(text) is not None:
            most = max(most, len(match[1]))
    if not shown or not any(text.strip() for text in shown):
        for value in values:
            if value != int(value):
                most = max(most, len(repr(value).split(".")[-1]))
    return min(most, 2)


def column(dataset: Dataset, index: int) -> Column:
    values = dataset.cells(index)
    kind = kind_of(values, dataset.day_first)
    name = dataset.headers[index] if index < len(dataset.headers) else ""
    name = name.strip() or f"Column {index + 1}"
    if kind != "number":
        return Column(index, name, kind)
    shown = dataset.shown_cells(index)
    numbers = [found for value in values if (found := number(value)) is not None]
    return Column(index, name, kind, unit_of(name, shown), _decimals(shown, numbers))


def columns(dataset: Dataset) -> list[Column]:
    width = max([len(dataset.headers), *(len(row) for row in dataset.rows)], default=0)
    return [column(dataset, index) for index in range(width)]


def from_csv(text: str) -> tuple[list[str], list[list[str]]]:
    """A CSV file's headers and rows. The first row is taken as the headers."""
    sample = text[:4096]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [
        row
        for row in csv.reader(io.StringIO(text), dialect)
        if any(cell.strip() for cell in row)
    ]
    if not rows:
        return [], []
    return [cell.strip() for cell in rows[0]], rows[1:]
