"""Charts in Google Sheets: a tab of a chart's numbers, and a native chart drawn from it.

The tab holds the numbers exactly as Orbit drew them: labels as text and values as
numbers, in the chart's own format. So the chart saved is the one the user saw, and
a chart pulled back from its tab reads the same. A tab with the chart's own name is
added; the user's other tabs are never touched.
See https://developers.google.com/sheets/api/guides/charts
"""

import re
from typing import Any

from .build import table_of

# How big the native chart is drawn, in pixels, beside the numbers.
_WIDTH, _HEIGHT = 640, 400
_NOT_IN_A_TAB = re.compile(r"[\[\]*?/\\:]")
_MAX_TAB_CHARS = 90


def tab_title(title: str, taken: list[str]) -> str:
    """A name for a chart's tab: the chart's title, made unique among the tabs."""
    base = _NOT_IN_A_TAB.sub("-", " ".join(title.split()))[:_MAX_TAB_CHARS] or "Chart"
    used = {name.casefold() for name in taken}
    name, number = base, 1
    while name.casefold() in used:
        number += 1
        name = f"{base} ({number})"
    return name


def pattern(axis: dict[str, Any]) -> str:
    """The Sheets number format for an axis's numbers, unit and decimals included."""
    decimals = axis.get("decimals") or 0
    digits = "#,##0" + ("." + "0" * decimals if decimals else "")
    unit = axis.get("unit") or ""
    if unit == "%":
        return f'{digits}"%"'
    if unit:
        return f'"{unit}"{digits}'
    return digits


def _cell(value: Any, number_format: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, int | float) and not isinstance(value, bool):
        return {
            "userEnteredValue": {"numberValue": value},
            "userEnteredFormat": {
                "numberFormat": {"type": "NUMBER", "pattern": number_format}
            },
        }
    return {"userEnteredValue": {"stringValue": str(value)}}


def cells(view: dict[str, Any]) -> list[dict[str, Any]]:
    """The tab's rows as Sheets cells: the headers in bold, then the numbers."""
    table = table_of(view)
    header, rows = table[0], table[1:]
    across = pattern(view["x"]) if view["kind"] == "scatter" else "@"
    values = pattern(view["y"])
    bold = {"userEnteredFormat": {"textFormat": {"bold": True}}}
    return [
        {
            "values": [
                {"userEnteredValue": {"stringValue": str(h)}, **bold} for h in header
            ]
        },
        *(
            {"values": [_cell(row[0], across), *(_cell(v, values) for v in row[1:])]}
            for row in rows
        ),
    ]


def _range(tab_id: int, rows: int, column: int) -> dict[str, Any]:
    return {
        "sourceRange": {
            "sources": [
                {
                    "sheetId": tab_id,
                    "startRowIndex": 0,
                    "endRowIndex": rows,
                    "startColumnIndex": column,
                    "endColumnIndex": column + 1,
                }
            ]
        }
    }


def spec(view: dict[str, Any], tab_id: int) -> dict[str, Any]:
    """The native Sheets chart for a view, drawn from its tab."""
    table = table_of(view)
    rows, columns = len(table), len(table[0])
    title = view["title"]
    if view["kind"] in ("pie", "donut"):
        return {
            "title": title,
            "pieChart": {
                "legendPosition": "RIGHT_LEGEND",
                "domain": _range(tab_id, rows, 0),
                "series": _range(tab_id, rows, 1),
                "pieHole": 0.5 if view["kind"] == "donut" else 0,
            },
        }
    sideways = view["kind"] == "bar" and view.get("orientation") == "horizontal"
    chart_type = {
        "bar": "BAR" if sideways else "COLUMN",
        "line": "LINE",
        "area": "AREA",
        "scatter": "SCATTER",
        # Orbit's own bins, drawn as columns, so they are the bins the user saw.
        "histogram": "COLUMN",
    }[view["kind"]]
    values_axis = "BOTTOM_AXIS" if sideways else "LEFT_AXIS"
    labels_axis = "LEFT_AXIS" if sideways else "BOTTOM_AXIS"
    # Levels in words are drawn from the columns of their places on the scale.
    first = 1 + len(view["series"]) if view["y"].get("levels") else 1
    drawn = range(first, columns)
    basic: dict[str, Any] = {
        "chartType": chart_type,
        "legendPosition": "BOTTOM_LEGEND" if len(drawn) > 1 else "NO_LEGEND",
        "axis": [
            {"position": labels_axis, "title": view["x"]["label"]},
            {"position": values_axis, "title": view["y"]["label"]},
        ],
        "domains": [{"domain": _range(tab_id, rows, 0)}],
        "series": [
            {"series": _range(tab_id, rows, column), "targetAxis": values_axis}
            for column in drawn
        ],
        "headerCount": 1,
    }
    if chart_type in ("COLUMN", "BAR", "AREA"):
        basic["stackedType"] = {
            "none": "NOT_STACKED",
            "stacked": "STACKED",
            "percent": "PERCENT_STACKED",
        }[view.get("stacking", "none")]
    return {"title": title, "basicChart": basic}


def _grid(tab_id: int, view: dict[str, Any]) -> dict[str, Any]:
    table = table_of(view)
    return {
        "sheetId": tab_id,
        "gridProperties": {
            "rowCount": max(len(table) + 5, 50),
            "columnCount": max(len(table[0]) + 12, 26),
            "frozenRowCount": 1,
        },
    }


def adding(view: dict[str, Any], tab_id: int, title: str) -> list[dict[str, Any]]:
    """The requests that add a chart's tab, its numbers, and the chart beside them."""
    columns = len(table_of(view)[0])
    return [
        {"addSheet": {"properties": {**_grid(tab_id, view), "title": title}}},
        {
            "updateCells": {
                "rows": cells(view),
                "fields": "userEnteredValue,userEnteredFormat",
                "start": {"sheetId": tab_id, "rowIndex": 0, "columnIndex": 0},
            }
        },
        {
            "addChart": {
                "chart": {
                    "spec": spec(view, tab_id),
                    "position": {
                        "overlayPosition": {
                            "anchorCell": {
                                "sheetId": tab_id,
                                "rowIndex": 0,
                                "columnIndex": columns + 1,
                            },
                            "widthPixels": _WIDTH,
                            "heightPixels": _HEIGHT,
                        }
                    },
                }
            }
        },
    ]


def rewriting(view: dict[str, Any], tab_id: int, chart_id: int) -> list[dict[str, Any]]:
    """The requests that put fresh numbers in a chart's tab, and draw it again."""
    return [
        {
            "updateSheetProperties": {
                "properties": _grid(tab_id, view),
                "fields": "gridProperties(rowCount,columnCount,frozenRowCount)",
            }
        },
        # With no rows given, the values in the range are cleared.
        {"updateCells": {"range": {"sheetId": tab_id}, "fields": "userEnteredValue"}},
        {
            "updateCells": {
                "rows": cells(view),
                "fields": "userEnteredValue,userEnteredFormat",
                "start": {"sheetId": tab_id, "rowIndex": 0, "columnIndex": 0},
            }
        },
        {"updateChartSpec": {"chartId": chart_id, "spec": spec(view, tab_id)}},
    ]


def redrawing(view: dict[str, Any], tab_id: int, chart_id: int) -> list[dict[str, Any]]:
    """The request that draws a saved chart as another kind."""
    return [{"updateChartSpec": {"chartId": chart_id, "spec": spec(view, tab_id)}}]
