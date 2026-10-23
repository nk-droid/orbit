"""The charts Orbit draws, worked out by the agent, for the chart cards to fetch."""

import re
from typing import Any

from fastapi import APIRouter, HTTPException

from charts import store

router = APIRouter()

_ID = re.compile(r"[0-9a-f]{8}")


@router.get("/charts/{chart_id}")
async def chart(chart_id: str) -> dict[str, Any]:
    """A chart as its card draws it: every number already worked out."""
    view = store.view(chart_id) if _ID.fullmatch(chart_id) else None
    if view is None:
        raise HTTPException(status_code=404, detail="There is no such chart.")
    return view
