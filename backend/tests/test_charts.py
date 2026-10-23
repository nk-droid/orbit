import itertools
import json
import re
from datetime import date
from types import SimpleNamespace

import pytest
from livekit.agents.llm import ToolError

import agent_tools.actions as actions
import agent_tools.charts as chart_tool
from actions import effects
from actions.gate import ActionGate
from agent_tools import stage
from agent_tools.browser import CUT_SHORT_MARK
from agent_tools.state import OrbitState
from charts import saving, sheet, sources, store
from charts.build import (
    ChartError,
    Request,
    bucket,
    build,
    from_table,
    gist,
    key,
    level_order,
    table_of,
    with_kind,
)
from charts.data import Dataset, column, day, kind_of, number, unit_of
from integrations.google.api import GoogleApiError

SOURCE = {
    "from": "sheet",
    "spreadsheet_id": "abc",
    "tab": "October",
    "label": "Budget / October",
}


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Charts are kept in a throwaway folder, never the real one."""
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def shown(monkeypatch) -> list[dict]:
    """The cards the tools asked the app to put on screen."""
    cards: list[dict] = []

    async def show(card, *, push=None):
        cards.append(card)

    monkeypatch.setattr(stage, "show", show)
    return cards


def budget(**changed) -> Dataset:
    """A month of spending, as a sheet keeps it and as it shows it."""
    rows = [
        ["2026-10-01", "Rent", 900, "Me"],
        ["2026-10-02", "Food", 120.5, "Me"],
        ["2026-10-05", "Food", 80, "Asha"],
        ["2026-10-09", "Travel", 410, "Asha"],
        ["2026-10-12", "Food", 439.5, "Me"],
        ["2026-10-20", "Fun", 60, "Me"],
    ]
    shown_rows = [[r[0], r[1], f"₹{r[2]:,.2f}", r[3]] for r in rows]
    return Dataset(
        name="Budget / October",
        source=SOURCE,
        headers=["Date", "Category", "Amount", "Paid by"],
        rows=rows,
        shown=shown_rows,
        **changed,
    )


def labels(view) -> dict:
    return dict(zip(view["categories"], view["series"][0]["values"], strict=True))


# Reading values.


@pytest.mark.parametrize(
    ("cell", "read"),
    [
        ("₹1,200.50", 1200.5),
        ("(45)", -45.0),
        ("12%", 12.0),
        ("1 200", 1200.0),
        ("Rs. 450", 450.0),
        ("1,20,000", 120000.0),
        ("1.2e3", 1200.0),
        (7, 7.0),
        ("-", None),
        ("", None),
        ("Food", None),
        (True, None),
    ],
)
def test_numbers_are_read_as_people_write_them(cell, read):
    assert number(cell) == read


@pytest.mark.parametrize(
    ("cell", "read"),
    [
        ("2026-10-03", date(2026, 10, 3)),
        ("3 Oct 2026", date(2026, 10, 3)),
        ("Oct 3, 2026", date(2026, 10, 3)),
        ("03/10/2026", date(2026, 10, 3)),
        ("2026-10-03T14:00:00+05:30", date(2026, 10, 3)),
        ("Oct 2026", date(2026, 10, 1)),
        ("2026", None),
        ("12", None),
    ],
)
def test_dates_are_read_but_bare_numbers_are_not(cell, read):
    assert day(cell) == read


def test_month_first_dates_are_read_where_the_sheet_writes_them_so():
    assert day("10/3/2026", day_first=False) == date(2026, 10, 3)


def test_a_column_is_what_most_of_its_cells_are():
    assert kind_of(["₹1,200", "₹300", "", "₹45"]) == "number"
    assert kind_of(["2026-10-01", "2026-10-02"]) == "date"
    assert kind_of(["Food", "Rent", "3"]) == "text"


def test_a_unit_comes_only_from_the_data():
    assert unit_of("Amount", ["₹1,200", "₹300"]) == "₹"
    assert unit_of("Amount ($)", []) == "$"
    assert unit_of("Share", ["12%", "30%"]) == "%"
    # Bare amounts are not dollars.
    assert unit_of("Amount", ["4.50", "3.00"]) == ""
    assert column(budget(), 2).unit == "₹"
    assert column(budget(), 2).decimals == 2


# Building charts.


def test_a_pie_adds_up_each_label():
    view = build(budget(), Request("pie", "Category", ("Amount",)))

    assert view["kind"] == "pie"
    assert labels(view) == {"Rent": 900, "Food": 640, "Travel": 410, "Fun": 60}
    assert view["y"] == {"label": "Amount", "unit": "₹", "decimals": 2}
    told = gist(view)
    assert told["total"] == 2010
    assert told["largest"] == {"label": "Rent", "value": 900, "share": "45%"}


def test_a_pie_keeps_seven_slices_and_puts_the_rest_under_other():
    rows = [[f"Item {n}", n] for n in range(1, 13)]
    data = Dataset("Things", {"from": "said"}, ["Item", "Value"], rows)

    view = build(data, Request("donut", "Item", ("Value",)))

    assert len(view["categories"]) == 8
    assert view["categories"][-1] == "Other"
    assert labels(view)["Other"] == sum(range(1, 6))
    assert "under Other" in view["notes"][0]


def test_many_labels_are_drawn_from_the_left_and_every_one_kept():
    rows = [[f"Item {n}", n] for n in range(1, 41)]
    data = Dataset("Things", {"from": "said"}, ["Item", "Value"], rows)

    view = build(data, Request("bar", "Item", ("Value",)))

    assert view["orientation"] == "horizontal"
    assert len(view["categories"]) == 40
    assert view["categories"][0] == "Item 40"
    assert "Other" not in view["categories"]
    assert sheet.spec(view, 7)["basicChart"]["chartType"] == "BAR"


def test_past_a_hundred_labels_the_rest_go_under_other():
    rows = [[f"Item {n}", n] for n in range(1, 106)]
    data = Dataset("Things", {"from": "said"}, ["Item", "Value"], rows)

    view = build(data, Request("bar", "Item", ("Value",)))

    assert len(view["categories"]) == 100
    assert labels(view)["Other"] == sum(range(1, 7))


# Skills with their levels, as a portfolio gives them.
SKILLS = [
    ["Python", "5y+"],
    ["SQL", "3y+"],
    ["JavaScript", "Academic"],
    ["Java", "2y+"],
    ["Ray", "Project-based"],
    ["FAISS", "2+"],
    ["Git", "5y+"],
    ["LlamaIndex", "1y+"],
]


def skills() -> Dataset:
    rows = [list(row) for row in SKILLS]
    return Dataset("Skills", {"from": "conversation"}, ["Skill", "Level"], rows)


def test_levels_go_by_their_numbers_after_those_with_none():
    assert level_order([level for _, level in SKILLS]) == [
        "Academic",
        "Project-based",
        "1y+",
        "2y+",
        "2+",
        "3y+",
        "5y+",
    ]


@pytest.mark.parametrize(
    ("found", "ordered"),
    [
        (
            ["Expert", "Beginner", "Advanced", "Intermediate"],
            ["Beginner", "Intermediate", "Advanced", "Expert"],
        ),
        (["High", "Low", "Medium"], ["Low", "Medium", "High"]),
        (["Level 3", "Level 1", "Level 10"], ["Level 1", "Level 3", "Level 10"]),
    ],
)
def test_familiar_levels_go_in_their_usual_order(found, ordered):
    assert level_order(found) == ordered


def test_levels_go_in_the_order_asked_for():
    asked = ("academic", "1y+", "Project-based", "2+", "2y+", "3y+", "4y+", "5y+")

    assert level_order([level for _, level in SKILLS], asked) == [
        "Academic",
        "1y+",
        "Project-based",
        "2+",
        "2y+",
        "3y+",
        "4y+",
        "5y+",
    ]
    # A level left out of the order goes after the one it usually follows.
    assert level_order(["Academic", "1y+"], ("1y+",)) == ["Academic", "1y+"]
    assert level_order(["2y+", "2+", "5y+"], ("2y+", "5y+")) == ["2y+", "2+", "5y+"]


def test_words_that_do_not_read_as_levels_are_levels_only_in_an_order_given():
    rows = [["A", "Gold"], ["B", "Silver"], ["C", "Bronze"]]
    data = Dataset("Medals", {"from": "conversation"}, ["Team", "Medal"], rows)

    with pytest.raises(ChartError, match="give their order as levels"):
        build(data, Request("bar", "Team", ("Medal",)))
    ordered = Request("bar", "Team", ("Medal",), levels=("Bronze", "Silver", "Gold"))
    assert labels(build(data, ordered)) == {"A": 3, "B": 2, "C": 1}


def test_a_chart_of_levels_draws_each_at_its_place():
    view = build(skills(), Request("bar", "Skill", ("Level",)))

    assert view["y"]["levels"] == level_order([level for _, level in SKILLS])
    assert view["how"] == "max"
    assert view["title"] == "Level by Skill"
    assert labels(view) == {
        "Python": 7,
        "Git": 7,
        "SQL": 6,
        "FAISS": 5,
        "Java": 4,
        "LlamaIndex": 3,
        "Ray": 2,
        "JavaScript": 1,
    }
    told = gist(view)
    assert told["highest"] == {"level": "5y+", "labels": ["Python", "Git"], "count": 2}
    assert told["lowest"] == {"level": "Academic", "labels": ["JavaScript"], "count": 1}
    assert told["at_each"]["2y+"] == 1


def test_levels_are_drawn_as_bars_or_a_line_and_never_added_up():
    with pytest.raises(ChartError, match="bar, horizontal_bar or line"):
        build(skills(), Request("pie", "Skill", ("Level",)))
    # With each label once there is nothing to add up, so how makes no difference.
    summed = build(skills(), Request("bar", "Skill", ("Level",), how="sum"))
    assert summed["how"] == "max"
    twice = skills()
    twice.rows.append(["Python", "3y+"])
    with pytest.raises(ChartError, match="cannot be added or averaged"):
        build(twice, Request("bar", "Skill", ("Level",), how="sum"))
    view = build(skills(), Request("bar", "Skill", ("Level",)))
    assert with_kind(view, "line")["kind"] == "line"
    with pytest.raises(ChartError, match="bar, horizontal_bar or line"):
        with_kind(view, "donut")


def test_a_chart_of_levels_is_saved_in_words_and_drawn_from_their_places():
    view = build(skills(), Request("horizontal_bar", "Skill", ("Level",)))
    table = table_of(view)

    assert table[0] == ["Skill", "Level", "Level (1-7)"]
    assert table[1] == ["Python", "5y+", 7.0]
    drawn = sheet.spec(view, 7)["basicChart"]["series"]
    assert [
        s["series"]["sourceRange"]["sources"][0]["startColumnIndex"] for s in drawn
    ] == [2]
    # A level changed in words in Sheets is read back as that level.
    table[1][1] = "3y+"
    assert labels(from_table(view, table))["Python"] == 6


def test_counting_labels_that_each_come_once_is_refused():
    with pytest.raises(ChartError, match="Each Skill is there once"):
        build(skills(), Request("bar", "Skill"))


def test_a_series_column_splits_the_bars():
    view = build(
        budget(), Request("stacked_bar", "Category", ("Amount",), series="Paid by")
    )

    assert (view["kind"], view["stacking"]) == ("bar", "stacked")
    assert [s["name"] for s in view["series"]] == ["Me", "Asha"]
    assert view["series"][1]["values"][view["categories"].index("Travel")] == 410
    assert gist(view)["totals"] == {"Me": 1520, "Asha": 490}


def test_dates_are_bucketed_and_every_bucket_is_there():
    view = build(budget(), Request("line", "Date", ("Amount",), per="week"))

    assert view["x"]["type"] == "time" and view["x"]["per"] == "week"
    assert view["categories"] == [
        "2026-09-28",
        "2026-10-05",
        "2026-10-12",
        "2026-10-19",
    ]
    assert view["title"] == "Amount per week"
    told = gist(view)
    assert told["first"]["label"] == "the week of 28 September 2026"
    assert told["change"] == "-94%"


def test_the_bucket_is_chosen_by_the_range_when_not_asked_for():
    view = build(budget(), Request("bar", "Date", ()))

    # Twenty days: a bar a day, counting rows, the days with none included.
    assert view["x"]["per"] == "day"
    assert len(view["categories"]) == 20
    assert view["title"] == "Rows per day"
    assert sum(view["series"][0]["values"]) == 6


@pytest.mark.parametrize(
    ("per", "first"),
    [
        ("week", date(2026, 9, 28)),
        ("month", date(2026, 10, 1)),
        ("quarter", date(2026, 10, 1)),
        ("year", date(2026, 1, 1)),
    ],
)
def test_a_date_falls_in_its_bucket(per, first):
    assert bucket(date(2026, 10, 3), per) == first


def test_too_many_buckets_are_drawn_coarser_and_said_so():
    rows = [[f"2026-01-{d:02d}", 1] for d in range(1, 32)] + [["2026-12-31", 1]]
    data = Dataset("Days", {"from": "said"}, ["Date", "Value"], rows * 1)

    view = build(data, Request("line", "Date", ("Value",), per="day"))

    assert view["x"]["per"] == "week"
    assert "By week, since by day" in view["notes"][0]


def test_rows_without_a_value_are_left_out_and_said():
    data = budget()
    data.rows.append(["", "Misc", "", "Me"])
    data.shown.append(["", "Misc", "", "Me"])

    view = build(data, Request("bar", "Category", ("Amount",)))

    assert "1 cell is without a number left out." in view["notes"]


@pytest.mark.parametrize(
    ("request_", "refused"),
    [
        (Request("bar", "Kind", ("Amount",)), "no column called 'Kind'"),
        (Request("bar", "Category", ("Paid by",)), "Paid by has no numbers"),
        (
            Request("pie", "Category", ("Amount",), series="Paid by"),
            "A pie shows one set",
        ),
        (Request("bar", "", ("Amount",)), "Say which column goes along the bottom"),
        (Request("scatter", "Amount", ()), "two columns of numbers"),
        (Request("histogram", ""), "A histogram needs one column"),
        (Request("bar", "Category", ("Amount",), per="week"), "per buckets dates"),
        (Request("bar", "Category", ("Amount",), how="median"), "how is one of"),
        (Request("chart", "Category"), "Say which kind"),
    ],
)
def test_a_chart_that_cannot_be_made_says_what_to_do(request_, refused):
    with pytest.raises(ChartError, match=refused):
        build(budget(), request_)


def test_a_pie_refuses_values_below_zero():
    data = Dataset(
        "Balance", {"from": "said"}, ["Who", "Balance"], [["A", 5], ["B", -2]]
    )

    with pytest.raises(ChartError, match="values above zero"):
        build(data, Request("pie", "Who", ("Balance",)))


def test_a_scatter_plot_pairs_two_numbers():
    data = Dataset(
        "Shop",
        {"from": "said"},
        ["Visits", "Sales"],
        [[n, 2 * n + 1] for n in range(10)],
    )

    view = build(data, Request("scatter", "Visits", ("Sales",)))

    assert len(view["points"]) == 10
    assert gist(view)["correlation"] == 1.0


def test_a_histogram_bins_one_column():
    data = Dataset(
        "Ages",
        {"from": "said"},
        ["Age"],
        [[a] for a in [21, 22, 25, 31, 33, 38, 45, 52, 58, 64]],
    )

    view = build(data, Request("histogram", "Age"))

    assert sum(b["count"] for b in view["bins"]) == 10
    assert view["bins"][0]["from"] <= 21 and view["bins"][-1]["to"] > 64
    assert 5 <= len(view["bins"]) <= 40


def test_what_a_chart_measures_leaves_out_its_kind_and_title():
    asked = key(SOURCE, Request("bar", "Category", ("Amount",)))

    assert asked == key(SOURCE, Request("pie", "category", ("amount",), title="Spend"))
    assert asked != key(SOURCE, Request("bar", "Category", ("Amount",), how="average"))
    assert asked != key(SOURCE, Request("histogram", "Category", ("Amount",)))
    assert asked != key(
        {**SOURCE, "tab": "November"}, Request("bar", "Category", ("Amount",))
    )
    # How the source is shown is no part of it.
    assert asked == key(
        {**SOURCE, "label": "Renamed"}, Request("bar", "Category", ("Amount",))
    )


@pytest.mark.parametrize(
    "request_",
    [
        Request("bar", "Category", ("Amount",), series="Paid by"),
        Request("histogram", "Amount"),
        Request("scatter", "Amount", ("Amount",)),
    ],
)
def test_a_chart_reads_back_the_same_from_its_table(request_):
    view = build(budget(), request_)

    back = from_table(view, table_of(view))

    for part in ("categories", "series", "bins", "points"):
        assert back.get(part) == view.get(part)


def test_a_number_corrected_in_the_tab_is_pulled():
    view = build(budget(), Request("bar", "Category", ("Amount",)))
    table = table_of(view)
    table[1][1] = 950

    assert labels(from_table(view, table))["Rent"] == 950


def test_a_chart_can_be_drawn_as_another_of_its_family():
    view = build(budget(), Request("bar", "Category", ("Amount",)))

    assert with_kind(view, "donut")["kind"] == "donut"
    assert with_kind(view, "horizontal_bar")["orientation"] == "horizontal"
    split = build(budget(), Request("bar", "Category", ("Amount",), series="Paid by"))
    with pytest.raises(ChartError, match="A pie shows one set"):
        with_kind(split, "pie")


# Sources.


def test_a_source_is_named_in_one_of_a_few_forms():
    datasets = {"Budget / October": SOURCE}

    assert sources.parse("budget", datasets) == SOURCE
    assert (
        sources.parse("calendar: 2026-09-01 to 2026-10-31", {})["start"] == "2026-09-01"
    )
    assert (
        sources.parse("mail: from:asha newer_than:90d", {})["query"]
        == "from:asha newer_than:90d"
    )
    assert sources.parse("reminders", {})["from"] == "reminders"
    assert sources.parse("drive:1AbCdEfGhIjK", {})["file_id"] == "1AbCdEfGhIjK"
    assert sources.parse("added: sales.csv", {})["name"] == "sales.csv"
    with pytest.raises(
        ChartError, match="datasets read in this conversation are: Budget"
    ):
        sources.parse("expenses", datasets)
    with pytest.raises(ChartError, match="Name the source as one of"):
        sources.parse("calendar: last week", {})


def test_numbers_the_user_said_are_a_dataset():
    data = sources.from_points(
        {"from": "said"}, ["January: 3", "February: 5", "March: 8"]
    )

    view = build(data, Request("bar", "Label", ("Value",)))

    assert labels(view) == {"March": 8, "February": 5, "January": 3}
    with pytest.raises(ChartError, match="label: value"):
        sources.from_points({"from": "said"}, ["three"])


def test_reminders_are_a_dataset(monkeypatch):
    from reminders import store as reminders

    reminders.add("Pay rent", 1_790_000_000)
    reminders.add("Call Asha", 1_790_100_000)

    data = sources.from_reminders(sources.parse("reminders", {}))

    assert data.headers == ["Title", "Due", "Status", "Created"]
    assert [row[0] for row in data.rows] == ["Pay rent", "Call Asha"]


async def test_every_event_in_the_range_is_read(monkeypatch):
    events = [
        {
            "summary": "Standup",
            "start": {"dateTime": "2026-10-05T10:00:00+05:30"},
            "end": {"dateTime": "2026-10-05T10:30:00+05:30"},
            "attendees": [{"self": True}, {"email": "a@x.com"}],
        },
        {
            "summary": "Holiday",
            "start": {"date": "2026-10-06"},
            "end": {"date": "2026-10-07"},
        },
    ]
    asked = []

    async def every_event(session, start, end, limit):
        asked.append((start, end, limit))
        return events, False

    monkeypatch.setattr(sources.gcal, "every_event", every_event)

    data = await sources.load(
        None, sources.parse("calendar: 2026-10-01 to 2026-10-31", {})
    )

    assert data.rows[0][3] == 0.5 and data.rows[0][5] == 1
    assert data.rows[1][6] == "yes" and data.rows[1][3] is None
    view = build(data, Request("bar", "Date", ("Hours",), per="week"))
    assert view["title"] == "Hours per week"


async def test_mail_is_read_page_after_page_up_to_a_limit(monkeypatch):
    async def every_id(session, query, limit):
        return ["m1", "m2"], True

    async def headers_of(session, ids):
        return [
            {
                "internalDate": "1790000000000",
                "labelIds": ["UNREAD", "CATEGORY_UPDATES"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Asha <asha@example.com>"},
                        {"name": "Subject", "value": "Hi"},
                    ]
                },
            },
            {
                "internalDate": "1790100000000",
                "labelIds": [],
                "payload": {"headers": [{"name": "From", "value": "bob@shop.com"}]},
            },
        ]

    monkeypatch.setattr(sources.gmail, "every_id", every_id)
    monkeypatch.setattr(sources.gmail, "headers_of", headers_of)

    data = await sources.load(None, sources.parse("mail: newer_than:30d", {}))

    assert [row[1:3] for row in data.rows] == [
        ["Asha", "example.com"],
        ["bob@shop.com", "shop.com"],
    ]
    assert data.rows[0][4:] == ["yes", "Updates"]
    assert "Only the latest 500 emails were read." in data.notes
    view = build(data, Request("bar", "Domain", ()))
    assert view["title"] == "Emails by Domain"


async def test_a_sheet_is_read_with_its_numbers_as_numbers(monkeypatch):
    async def read(session, spreadsheet_id, tab, cells, matching):
        return {
            "id": "abc", "title": "Budget", "tab": "October", "link": "https://x", "locale": "en_US",
            "first_column": 0, "headers": ["Date", "Amount"], "complete": True,
            "rows": [{"row": 2, "cells": ["10/3/2026", "$1,200.00"]}, {"row": 3, "cells": ["10/4/2026", "$300.00"]}],
        }  # fmt: skip

    async def raw_rows(session, spreadsheet_id, tab, cells):
        return {2: ["10/3/2026", 1200], 3: ["10/4/2026", 300]}

    monkeypatch.setattr(sources.gsheets, "read", read)
    monkeypatch.setattr(sources.gsheets, "raw_rows", raw_rows)

    data = await sources.load(None, SOURCE)

    assert data.rows == [["10/3/2026", 1200], ["10/4/2026", 300]]
    view = build(data, Request("line", "Date", ("Amount",)))
    # The sheet writes the month first, and shows dollars.
    assert view["categories"] == ["2026-10-03", "2026-10-04"]
    assert view["y"]["unit"] == "$"


def test_a_csv_file_handed_over_is_a_dataset():
    import attachments

    attachments.keep(
        attachments.TRAY,
        "sales.csv",
        "text/csv",
        b"Month,Sales\nJan,10\nFeb,12\n",
        "from your computer",
    )

    data = sources.from_added({"from": "added", "name": "sales"})

    assert data.headers == ["Month", "Sales"]
    assert data.rows == [["Jan", "10"], ["Feb", "12"]]


# Saving into Sheets.


def test_each_kind_is_drawn_as_its_native_sheets_chart():
    view = build(budget(), Request("bar", "Category", ("Amount",), series="Paid by"))

    def drawn(kind):
        return sheet.spec(with_kind(view, kind) if kind != "split" else view, 7)

    assert drawn("bar")["basicChart"]["chartType"] == "COLUMN"
    assert drawn("horizontal_bar")["basicChart"]["chartType"] == "BAR"
    assert (
        drawn("horizontal_bar")["basicChart"]["series"][0]["targetAxis"]
        == "BOTTOM_AXIS"
    )
    assert drawn("stacked_bar")["basicChart"]["stackedType"] == "STACKED"
    assert drawn("percent_bar")["basicChart"]["stackedType"] == "PERCENT_STACKED"
    assert drawn("line")["basicChart"]["chartType"] == "LINE"
    assert drawn("stacked_area")["basicChart"]["stackedType"] == "STACKED"
    assert len(drawn("bar")["basicChart"]["series"]) == 2
    single = build(budget(), Request("donut", "Category", ("Amount",)))
    assert sheet.spec(single, 7)["pieChart"]["pieHole"] == 0.5
    histogram = build(budget(), Request("histogram", "Amount"))
    assert sheet.spec(histogram, 7)["basicChart"]["chartType"] == "COLUMN"


def test_a_tab_holds_labels_as_text_and_numbers_in_the_charts_format():
    view = build(budget(), Request("bar", "Category", ("Amount",)))

    rows = sheet.cells(view)

    assert rows[0]["values"][0]["userEnteredValue"] == {"stringValue": "Category"}
    rent = rows[1]["values"]
    assert rent[0]["userEnteredValue"] == {"stringValue": "Rent"}
    assert rent[1]["userEnteredValue"] == {"numberValue": 900}
    assert rent[1]["userEnteredFormat"]["numberFormat"]["pattern"] == '"₹"#,##0.00'
    assert sheet.pattern({"unit": "%", "decimals": 1}) == '#,##0.0"%"'


def test_a_tab_is_named_after_its_chart_and_never_twice():
    assert sheet.tab_title("Spending: October", []) == "Spending- October"
    assert sheet.tab_title("Spending", ["Spending", "spending (2)"]) == "Spending (3)"


async def test_a_chart_is_added_as_a_tab_with_the_chart_beside_it(monkeypatch):
    sent = []

    async def spreadsheet(session, spreadsheet_id):
        return {
            "id": "book",
            "title": "Orbit charts",
            "link": "https://docs.google.com/spreadsheets/d/book/edit",
            "tabs": [{"id": 0, "title": "About"}],
            "locale": "",
        }

    async def update(session, spreadsheet_id, requests):
        sent.append(requests)
        return {"replies": [{}, {}, {"addChart": {"chart": {"chartId": 42}}}]}

    monkeypatch.setattr(saving.gsheets, "spreadsheet", spreadsheet)
    monkeypatch.setattr(saving.gsheets, "update", update)
    view = build(budget(), Request("pie", "Category", ("Amount",)))

    place = await saving.add(None, "book", view)

    assert (place.tab_title, place.chart_id) == ("Amount by Category", 42)
    assert (
        place.link
        == f"https://docs.google.com/spreadsheets/d/book/edit#gid={place.tab_id}"
    )
    (requests,) = sent
    assert [next(iter(r)) for r in requests] == ["addSheet", "updateCells", "addChart"]


async def test_a_spreadsheet_that_is_gone_is_said_so(monkeypatch):
    async def spreadsheet(session, spreadsheet_id):
        raise GoogleApiError(404, "Requested entity was not found.")

    monkeypatch.setattr(saving.gsheets, "spreadsheet", spreadsheet)

    with pytest.raises(saving.GoneError):
        await saving.add(
            None, "book", build(budget(), Request("pie", "Category", ("Amount",)))
        )


# The store.


def test_a_saved_chart_is_found_by_what_it_measures_and_by_name():
    view = build(budget(), Request("bar", "Category", ("Amount",)))
    store.save(
        "c1",
        "k1",
        "bar",
        {"source": SOURCE},
        view,
        "book",
        "Orbit charts",
        7,
        "Spend",
        42,
        "https://x",
    )

    assert store.with_key("k1").id == "c1"
    assert [c.id for c in store.named("amount by")] == ["c1"]
    store.changed("c1", "pie", with_kind(view, "pie"))
    assert store.get("c1").kind == "pie"
    store.forget("c1")
    assert store.listed() == []


def test_the_charts_cards_fetch_are_kept_for_thirty_days(monkeypatch):
    store.put_view("c1", {"title": "Old"})
    monkeypatch.setattr(store.time, "time", lambda: 10**10)

    store.put_view("c2", {"title": "New"})

    assert store.view("c1") is None and store.view("c2") == {"title": "New"}


# The tool.


def _talk(*said: str, used: list[SimpleNamespace] = ()) -> SimpleNamespace:
    """A conversation in which the user said this, after the budget was read, and the
    tools `used` gave what they gave."""
    messages = [
        SimpleNamespace(type="message", role="user", text_content=t) for t in said
    ]
    state = OrbitState(gate=ActionGate())
    state.datasets["Budget / October"] = SOURCE
    return SimpleNamespace(
        session=SimpleNamespace(history=SimpleNamespace(items=[*messages, *used])),
        userdata=state,
        disallow_interruptions=lambda: None,
    )


_CALLS = itertools.count()


def _used(
    name: str, asked: str, gave: str, *, failed: bool = False
) -> list[SimpleNamespace]:
    """A tool the model called in the conversation: what it asked, what it gave."""
    call = f"call_{next(_CALLS)}"
    return [
        SimpleNamespace(type="function_call", call_id=call, name=name, arguments=asked),
        SimpleNamespace(
            type="function_call_output",
            id=f"item_{call}",
            call_id=call,
            name=name,
            output=gave,
            is_error=failed,
        ),
    ]


# A page the browser read, as its server gives it.
PAGE = """### Page state
- Page URL: https://www.phones.example/compare
- Page Title: Compare phones
- Page Snapshot:
- row "Phone A \u20b979,900 4.5 stars" [ref=e12]
- row "Phone B \u20b969,900 4.2 stars" [ref=e13]
- row "Phone C \u20b91.2 lakh 4.8 stars" [ref=e14]
"""


class FakeSheets:
    """Stands in for Google Sheets, for what charts do there."""

    def __init__(self) -> None:
        self.added: list[str] = []
        self.redrawn: list[str] = []
        self.rewritten: list[str] = []
        self.tables: dict[str, list] = {}
        self.down = False
        self.gone: set[str] = set()

    async def add(self, session, spreadsheet_id, view):
        if self.down:
            raise GoogleApiError(503, "Unavailable")
        self.added.append(view["title"])
        self.tables[view["title"]] = table_of(view)
        n = len(self.added)
        return saving.Place(
            spreadsheet_id,
            "Orbit charts",
            100 + n,
            view["title"],
            n,
            f"https://sheet#gid={100 + n}",
        )

    async def pull(self, session, saved):
        if saved.tab_title in self.gone:
            raise saving.GoneError
        return from_table(saved.view, self.tables[saved.tab_title])

    async def redraw(self, session, saved, view):
        self.redrawn.append(view["kind"])

    async def rewrite(self, session, saved, view):
        self.rewritten.append(view["title"])
        self.tables[saved.tab_title] = table_of(view)


@pytest.fixture
def sheets(monkeypatch) -> FakeSheets:
    fake = FakeSheets()
    for name in ("add", "pull", "redraw", "rewrite"):
        monkeypatch.setattr(saving, name, getattr(fake, name))
    monkeypatch.setattr(chart_tool, "_http", lambda: None)
    loads = []

    async def load(session, source, points=None):
        loads.append(source)
        if source["from"] in sources.GIVEN:
            return sources.from_points(source, points or source.get("points") or [])
        return budget()

    monkeypatch.setattr(sources, "load", load)
    fake.loads = loads
    store.keep_book("book", "Orbit charts", "https://sheet")
    return fake


async def test_a_chart_is_made_saved_and_shown(sheets, shown):
    told = await chart_tool.show_chart(
        _talk("Chart my spending by category"),
        kind="pie",
        source="Budget / October",
        x="Category",
        y=["Amount"],
    )

    assert told["this_time"] == "made"
    assert told["gist"]["largest"]["label"] == "Rent"
    assert "Orbit charts" in told["saved_in"]
    assert sheets.added == ["Amount by Category"]
    (saved,) = store.listed()
    assert saved.kind == "pie" and saved.view["saved"]["link"].startswith(
        "https://sheet"
    )
    # Shown at once, and again once it was saved.
    assert [card["kind"] for card in shown] == ["chart", "chart"]
    assert store.view(told["chart_id"])["saved"]["title"] == "Orbit charts"


async def test_a_chart_asked_for_again_is_pulled_not_made(sheets, shown):
    talk = _talk("Chart my spending by category")
    first = await chart_tool.show_chart(
        talk, kind="pie", source="Budget / October", x="Category", y=["Amount"]
    )
    sheets.tables["Amount by Category"][1][1] = 950

    again = await chart_tool.show_chart(
        talk, kind="pie", source="budget", x="category", y=["amount"]
    )

    assert again["this_time"] == "pulled"
    assert again["chart_id"] == first["chart_id"]
    assert sheets.added == ["Amount by Category"]
    assert again["gist"]["largest"]["value"] == 950
    # Only the first was read from the sheet: the second was pulled.
    assert len(sheets.loads) == 0 or all(s["from"] != "sheet" for s in sheets.loads[1:])


async def test_a_saved_chart_is_shown_by_its_name_and_drawn_as_another_kind(
    sheets, shown
):
    talk = _talk("Chart my spending by category")
    await chart_tool.show_chart(
        talk, kind="bar", source="Budget / October", x="Category", y=["Amount"]
    )

    changed = await chart_tool.show_chart(
        talk, chart="amount by category", kind="donut"
    )

    assert changed["this_time"] == "changed"
    assert sheets.redrawn == ["donut"]
    assert store.listed()[0].kind == "donut"
    with pytest.raises(ToolError, match="would measure something else"):
        await chart_tool.show_chart(talk, chart="amount by category", kind="scatter")


async def test_fresh_numbers_rewrite_the_saved_chart(sheets, shown):
    talk = _talk("Chart my spending by category")
    await chart_tool.show_chart(
        talk, kind="bar", source="Budget / October", x="Category", y=["Amount"]
    )

    updated = await chart_tool.show_chart(talk, chart="amount by category", fresh=True)

    assert updated["this_time"] == "updated"
    assert sheets.rewritten == ["Amount by Category"]


async def test_a_chart_whose_tab_was_deleted_is_made_again(sheets, shown):
    talk = _talk("Chart my spending by category")
    await chart_tool.show_chart(
        talk, kind="bar", source="Budget / October", x="Category", y=["Amount"]
    )
    sheets.gone.add("Amount by Category")

    again = await chart_tool.show_chart(talk, chart="amount by category")

    assert again["this_time"] == "made again"
    assert len(sheets.added) == 2
    assert len(store.listed()) == 1


async def test_when_google_is_down_the_chart_is_shown_unsaved(sheets, shown):
    sheets.down = True

    told = await chart_tool.show_chart(
        _talk("Chart it"),
        kind="bar",
        source="Budget / October",
        x="Category",
        y=["Amount"],
    )

    assert "could not be saved" in told["not_saved"]
    assert store.listed() == []
    assert shown[-1]["kind"] == "chart"


async def test_the_first_chart_ever_waits_for_a_yes_to_make_orbit_charts(
    sheets, shown, monkeypatch
):
    store.lose_book()
    monkeypatch.setattr(actions, "_require_write", lambda service: None)
    talk = _talk("Chart my spending by category")

    told = await chart_tool.show_chart(
        talk, kind="pie", source="Budget / October", x="Category", y=["Amount"]
    )

    assert told["saving"]["status"] == "awaiting_approval"
    assert "Orbit charts" in told["saving"]["next"]
    assert sheets.added == [] and store.listed() == []
    action = talk.userdata.gate.awaiting()
    assert action.kind == "sheets.add_chart"
    assert action.preview["table"]["rows"][0] == ["Rent", "900"]

    made = []

    async def make_book(session):
        made.append(True)
        store.keep_book("new-book", "Orbit charts", "https://new")
        return store.book()

    monkeypatch.setattr(saving, "make_book", make_book)
    result = await effects._add_chart(None, action.payload)

    assert made and result["spreadsheet"] == "Orbit charts"
    (saved,) = store.listed()
    assert saved.id == told["chart_id"] and saved.kind == "pie"


async def test_numbers_the_user_said_are_charted_and_others_refused(
    sheets, shown, monkeypatch
):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    talk = _talk("Plot 3, five and 8 for January, February and March")

    told = await chart_tool.show_chart(
        talk,
        kind="bar",
        source="conversation",
        points=["January: 3", "February: 5", "March: 8"],
    )

    assert told["gist"]["total"] == 16
    assert store.listed()[0].view["source"]["label"] == "Numbers you said"
    # "said" is what this source was called before.
    with pytest.raises(ToolError, match=r"for its label: January: 9\. "):
        await chart_tool.show_chart(
            talk, kind="line", source="said", points=["January: 9"]
        )


async def test_numbers_a_tool_gave_are_charted_with_where_they_came_from(
    sheets, shown, monkeypatch
):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    talk = _talk(
        "Show these prices and ratings in a graph",
        used=_used("browser_navigate", '{"url": "https://phones.example"}', PAGE),
    )

    told = await chart_tool.show_chart(
        talk,
        kind="bar",
        source="conversation",
        x="Phone",
        y=["Price", "Rating"],
        # The page wrote 1.2 lakh.
        points=[
            "Phone A: 79900 | 4.5",
            "Phone B: 69,900 | 4.2",
            "Phone C: 120000 | 4.8",
        ],
    )

    view = store.view(told["chart_id"])
    assert [series["name"] for series in view["series"]] == ["Price", "Rating"]
    assert labels(view) == {"Phone C": 120000, "Phone A": 79900, "Phone B": 69900}
    assert view["source"]["label"] == "From phones.example"
    assert shown[-1]["tag"] == "From phones.example"
    # Each number is kept as the page wrote it, for its unit and decimals.
    assert store.listed()[0].request["source"]["written"] == [
        ["\u20b979,900", "4.5"],
        ["\u20b969,900", "4.2"],
        ["\u20b9120,000", "4.8"],
    ]


async def test_numbers_passed_under_any_source_name_are_from_the_conversation(
    sheets, shown, monkeypatch
):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    talk = _talk("Graph that", used=_used("browser_snapshot", "{}", PAGE))

    told = await chart_tool.show_chart(
        talk, kind="pie", source="the web page", points=["A: 79900", "B: 69900"]
    )

    assert told["gist"]["total"] == 149800
    # In rupees, with no decimals, as the page wrote them.
    view = store.view(told["chart_id"])
    assert (view["y"]["unit"], view["y"]["decimals"]) == ("\u20b9", 0)


async def test_numbers_the_model_sent_a_tool_are_not_given_by_it(
    sheets, shown, monkeypatch
):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    talk = _talk(
        "Chart India's GDP",
        used=[
            # Written by the model, and shown back.
            *_used("write_on_screen", '{"text": "2023: 3,550"}', "GDP 2023: 3,550"),
            # A tool that failed gave nothing, whatever it said.
            *_used("show_chart", "{}", "3,700: not said or given", failed=True),
        ],
    )

    for points in (["2023: 3550"], ["2023: 3700"]):
        with pytest.raises(ToolError, match="Not said by the user or given by a tool"):
            await chart_tool.show_chart(
                talk, kind="bar", source="conversation", points=points
            )
    assert store.listed() == []


async def test_a_chart_of_numbers_from_a_page_is_updated_in_place(
    sheets, shown, monkeypatch
):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    talk = _talk("Chart the prices", used=_used("browser_snapshot", "{}", PAGE))
    asked = {"kind": "bar", "source": "conversation", "x": "Phone", "y": ["Price"]}
    told = await chart_tool.show_chart(
        talk, **asked, points=["Phone A: 79900", "Phone B: 69900"]
    )
    with pytest.raises(ToolError, match="no source to read again"):
        await chart_tool.show_chart(talk, chart=told["on_screen"], fresh=True)

    cheaper = PAGE.replace("79,900", "74,900")
    later = _talk("Update it", used=_used("browser_snapshot", "{}", cheaper))
    new = ["Phone A: 74900", "Phone B: 69900"]
    updated = await chart_tool.show_chart(
        later, chart=told["on_screen"], fresh=True, points=new
    )

    assert updated["this_time"] == "updated"
    assert updated["chart_id"] == told["chart_id"]
    assert sheets.rewritten == [told["on_screen"]]
    # Asked for by its new numbers, it is the same chart, pulled.
    pulled = await chart_tool.show_chart(later, **asked, points=new)
    assert (pulled["this_time"], pulled["chart_id"]) == ("pulled", told["chart_id"])
    assert len(store.listed()) == 1


# A page the browser read whole, skills and levels on its rows.
SKILLS_READ = """- Page URL: https://www.me.example/
- Page Title: Me
### Text
## Skills
- Python | AI/ML, backend | 5y+
- SQL | Queries | 3y+
- JavaScript | Frontend | Academic
"""


async def test_levels_a_page_gave_are_charted_in_order(sheets, shown, monkeypatch):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    talk = _talk(
        "Show all the skills with their level as a graph",
        used=_used("browser_read", "{}", SKILLS_READ),
    )
    asked = {"kind": "bar", "source": "conversation", "x": "Skill", "y": ["Level"]}

    told = await chart_tool.show_chart(
        talk, **asked, points=["Python: 5y+", "SQL: 3y+", "JavaScript: Academic"]
    )

    view = store.view(told["chart_id"])
    assert view["y"]["levels"] == ["Academic", "3y+", "5y+"]
    assert view["source"]["label"] == "From me.example"
    assert told["gist"]["highest"]["labels"] == ["Python"]
    assert told["order"].startswith("To order the levels otherwise")
    # A level the page does not give for that label is not taken, and the model is
    # told what the page does give for it.
    given = '"- Python | AI/ML, backend | 5y+"'
    with pytest.raises(
        ToolError,
        match=rf"Python: Expert \(what was given for Python: {re.escape(given)}\)",
    ):
        await chart_tool.show_chart(talk, **asked, points=["Python: Expert"])
    with pytest.raises(
        ToolError, match="JavaScript: 5y\\+ \\(what was given for JavaScript"
    ):
        await chart_tool.show_chart(talk, **asked, points=["JavaScript: 5y+"])


# A page whose one list runs on under several headings, as a portfolio's skills do.
SKILLS_IN_PARTS = """- Page URL: https://www.me.example/
### Text
## Skills
### Languages
- Python | AI/ML | 5y+
- SQL | Queries | 3y+
### Tools
- Docker | Containers | 2y+
## Projects
- Orbit | A voice assistant | 2026
"""


async def test_a_label_with_a_row_of_its_own_has_its_value_there(
    sheets, shown, monkeypatch
):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    # JavaScript is also named in a sentence, next to a number that is not its level.
    page = SKILLS_READ + "I first wrote JavaScript 1 summer at school.\n"
    talk = _talk("Graph my skills", used=_used("browser_read", "{}", page))
    asked = {"kind": "bar", "source": "conversation", "x": "Skill", "y": ["Level"]}

    with pytest.raises(ToolError, match="JavaScript: 1 "):
        await chart_tool.show_chart(talk, **asked, points=["JavaScript: 1"])
    told = await chart_tool.show_chart(
        talk, **asked, points=["JavaScript: Academic", "Python: 5y+"]
    )

    assert told["gist"]["lowest"] == {
        "level": "Academic",
        "labels": ["JavaScript"],
        "count": 1,
    }


async def test_asked_for_all_every_row_of_the_list_must_be_charted(
    sheets, shown, monkeypatch
):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    talk = _talk(
        "Show me all the skills with their level as a graph",
        used=_used("browser_read", "{}", SKILLS_IN_PARTS),
    )
    asked = {"kind": "bar", "source": "conversation", "x": "Skill", "y": ["Level"]}

    with pytest.raises(
        ToolError,
        match=r"lists 3 like these, of which 2 were given\. Not given: Docker",
    ):
        await chart_tool.show_chart(talk, **asked, points=["Python: 5y+", "SQL: 3y+"])
    every = await chart_tool.show_chart(
        talk, **asked, points=["Python: 5y+", "SQL: 3y+", "Docker: 2y+"]
    )

    assert every["gist"]["labels"] == 3


async def test_a_list_left_short_is_said_once_and_then_believed(
    sheets, shown, monkeypatch
):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    talk = _talk(
        "Chart all my languages", used=_used("browser_read", "{}", SKILLS_IN_PARTS)
    )
    asked = {"kind": "bar", "source": "conversation", "x": "Skill", "y": ["Level"]}

    with pytest.raises(ToolError, match="Not given: Docker"):
        await chart_tool.show_chart(talk, **asked, points=["Python: 5y+", "SQL: 3y+"])
    # Languages are two of the three: the model knows the list better.
    told = await chart_tool.show_chart(
        talk, **asked, points=["Python: 5y+", "SQL: 3y+"]
    )

    assert told["gist"]["labels"] == 2


async def test_rows_left_out_anew_are_named_again_with_their_values(
    sheets, shown, monkeypatch
):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    talk = _talk(
        "Chart all my skills", used=_used("browser_read", "{}", SKILLS_IN_PARTS)
    )
    asked = {"kind": "bar", "source": "conversation", "x": "Skill", "y": ["Level"]}

    with pytest.raises(ToolError, match="Not given: SQL: 3y\\+, Docker: 2y\\+"):
        await chart_tool.show_chart(talk, **asked, points=["Python: 5y+"])
    # A level put as a number is not what the page gives, and it says what it gives.
    with pytest.raises(ToolError, match='what was given for SQL: "3y\\+"'):
        await chart_tool.show_chart(
            talk, **asked, points=["Python: 5y+", "SQL: 0", "Docker: 2y+"]
        )
    with pytest.raises(ToolError, match="Not given: SQL: 3y\\+\\."):
        await chart_tool.show_chart(
            talk, **asked, points=["Python: 5y+", "Docker: 2y+"]
        )


@pytest.mark.parametrize(
    "point", ["Python: 5y+", "Python; 5y+", "Python = 5y+", "Python - 5y+"]
)
def test_a_point_may_be_written_with_other_marks_than_a_colon(point):
    assert sources.split_point(point) == ("Python", ["5y+"])


async def test_a_label_written_another_way_finds_its_row(sheets, shown, monkeypatch):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    page = SKILLS_READ + "- PostgreSQL + pgvector | Vector data | 2y+\n"
    talk = _talk("Graph my skills", used=_used("browser_read", "{}", page))

    told = await chart_tool.show_chart(
        talk,
        kind="bar",
        source="conversation",
        x="Skill",
        y=["Level"],
        points=["PostgreSQL and pgvector: 2y+", "Python: 5y+"],
    )

    assert told["gist"]["labels"] == 2


async def test_a_name_for_a_chart_not_saved_yet_is_its_title(
    sheets, shown, monkeypatch
):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    talk = _talk("Graph my skills", used=_used("browser_read", "{}", SKILLS_READ))

    told = await chart_tool.show_chart(
        talk,
        chart="My skills",
        kind="bar",
        source="conversation",
        x="Skill",
        y=["Level"],
        points=["Python: 5y+", "SQL: 3y+"],
    )

    assert told["on_screen"] == "My skills"
    with pytest.raises(ToolError, match="No saved chart is called 'Other'"):
        await chart_tool.show_chart(talk, chart="Other")


def test_a_row_given_by_a_shortened_label_counts_as_given():
    assert chart_tool._among("PostgreSQL + pgvector", {"postgresql"})
    assert chart_tool._among("PostgreSQL + pgvector", {"postgresql pgvector"})
    assert not chart_tool._among("PostgreSQL + pgvector", {"sql"})
    assert chart_tool._among("C", {"c"})


async def test_without_all_some_rows_of_a_list_are_charted_as_asked(
    sheets, shown, monkeypatch
):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    talk = _talk(
        "Graph Python and SQL by level",
        used=_used("browser_read", "{}", SKILLS_IN_PARTS),
    )

    told = await chart_tool.show_chart(
        talk,
        kind="bar",
        source="conversation",
        x="Skill",
        y=["Level"],
        points=["Python: 5y+", "SQL: 3y+"],
    )

    assert told["gist"]["labels"] == 2


async def test_a_saved_chart_of_levels_is_put_in_another_order(
    sheets, shown, monkeypatch
):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    talk = _talk("Graph my skills", used=_used("browser_read", "{}", SKILLS_READ))
    told = await chart_tool.show_chart(
        talk,
        kind="horizontal_bar",
        source="conversation",
        x="Skill",
        y=["Level"],
        points=["Python: 5y+", "SQL: 3y+", "JavaScript: Academic"],
    )
    order = ["3y+", "Academic", "5y+"]

    again = await chart_tool.show_chart(talk, chart=told["on_screen"], levels=order)

    assert again["this_time"] == "changed"
    assert sheets.rewritten == [told["on_screen"]]
    assert store.view(told["chart_id"])["y"]["levels"] == order
    assert store.get(told["chart_id"]).request["levels"] == order


async def test_a_chart_from_a_page_cut_short_says_it_may_hold_more(
    sheets, shown, monkeypatch
):
    monkeypatch.setattr(chart_tool, "HEARD_WITHIN", 0.0)
    cut = f"{PAGE}\n\n{CUT_SHORT_MARK} 35555 characters, of which the first 5000 are shown.]"
    talk = _talk("Chart the prices", used=_used("browser_snapshot", "{}", cut))

    told = await chart_tool.show_chart(
        talk, kind="bar", source="conversation", points=["Phone A: 79900"]
    )

    assert "browser_read" in told["page_cut_short"]


def test_numbers_are_found_in_digits_with_scales_and_in_words():
    assert set(chart_tool.numbers_in("plot 3, five and 1,200 and twenty five")) >= {
        3,
        5,
        1200,
        25,
    }
    assert 3500 in chart_tool.numbers_in("three thousand five hundred")
    assert 200_000 in chart_tool.numbers_in("two lakhs")
    scaled = {round(n) for n in chart_tool.numbers_in("1.2 lakh, $3.4M, 5bn, 10k")}
    assert scaled >= {120_000, 3_400_000, 5_000_000_000, 10_000}
    assert dict(chart_tool._in_digits("Rs. 1,200 and 45% of $3.4M")) == {
        1200: "Rs. 1,200",
        45: "45%",
        3.4: "$3.4",
        3_400_000: "$3,400,000",
    }
    # A word that only starts like a scale is not one.
    assert 3_000_000_000 not in chart_tool.numbers_in("3 bedrooms")


def test_the_saved_charts_are_listed_for_the_model(sheets):
    assert chart_tool.charts_note() == ""
    view = build(budget(), Request("bar", "Category", ("Amount",)))
    store.save(
        "c1",
        "k1",
        "bar",
        {"source": SOURCE},
        view,
        "book",
        "Orbit charts",
        7,
        "Spend",
        42,
        "https://x",
    )

    assert '"Amount by Category" (Budget / October, bar)' in chart_tool.charts_note()


async def test_a_chart_can_be_saved_into_the_users_own_spreadsheet(sheets, monkeypatch):
    monkeypatch.setattr(actions, "_require_write", lambda service: None)
    monkeypatch.setattr(actions, "_http", lambda: None)

    async def spreadsheets_named(session, name):
        return [{"id": "budget-id", "name": "Budget"}]

    monkeypatch.setattr(actions.gdrive, "spreadsheets_named", spreadsheets_named)
    talk = _talk("Chart my spending by category")
    told = await chart_tool.show_chart(
        talk, kind="bar", source="Budget / October", x="Category", y=["Amount"]
    )

    drafted = await actions.sheets_draft_chart(talk, told["chart_id"], "budget")

    assert drafted["draft"]["where"] == "Budget, in a new tab"
    action = talk.userdata.gate.awaiting()
    result = await effects._add_chart(None, action.payload)
    assert result["tab"] == "Amount by Category"
    # A copy: the chart is still kept, and pulled, from Orbit charts.
    assert len(store.listed()) == 1


async def test_the_api_gives_a_chart_its_card_fetches(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LIVEKIT_URL", "wss://example")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret")
    from api.app import app

    store.put_view("0a1b2c3d", {"title": "Spend"})
    client = TestClient(app)

    assert client.get("/api/charts/0a1b2c3d").json() == {"title": "Spend"}
    assert client.get("/api/charts/ffffffff").status_code == 404
    assert client.get("/api/charts/not-an-id").status_code == 404
    assert json.loads(json.dumps(store.view("0a1b2c3d"))) == {"title": "Spend"}
