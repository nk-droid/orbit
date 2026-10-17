"""Google Sheets for the app: one tab of a spreadsheet, as the sheet card shows it.

The card is opened by the agent with only which sheet, tab and part of it to show
(agent_tools/stage.py), and fetches the cells from here, so it shows the sheet as
it is now: after a change too.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from integrations.google import gsheets

from .files import _google

router = APIRouter()

# A card is for looking through. Longer tabs say how many rows are not shown.
MAX_SHOWN_ROWS = 2000


class SheetRow(BaseModel):
    row: int
    cells: list[str]


class SheetView(BaseModel):
    id: str
    title: str
    # Where the spreadsheet opens in Google Sheets.
    link: str | None = None
    tabs: list[str]
    tab: str
    # The column the cells start at, counted from 0: 0 is A.
    first_column: int
    header_row: int | None = None
    headers: list[str]
    rows: list[SheetRow]
    # Rows found but not shown here.
    more: int = 0
    # False when the tab has more rows than are read at once.
    complete: bool = True


@router.get("/sheets/{spreadsheet_id}", response_model=SheetView)
async def sheet(
    spreadsheet_id: str, tab: str = "", cells: str = "", matching: str = ""
) -> SheetView:
    async with _google() as session:
        try:
            found = await gsheets.read(session, spreadsheet_id, tab, cells, matching)
        except gsheets.SheetError as e:
            raise HTTPException(404, "Orbit could not find that sheet.") from e
    rows = found["rows"]
    return SheetView(
        id=found["id"],
        title=found["title"],
        link=found["link"],
        tabs=found["tabs"],
        tab=found["tab"],
        first_column=found["first_column"],
        header_row=found["header_row"],
        headers=found["headers"],
        rows=[SheetRow(**row) for row in rows[:MAX_SHOWN_ROWS]],
        more=max(len(rows) - MAX_SHOWN_ROWS, 0),
        complete=found["complete"],
    )
