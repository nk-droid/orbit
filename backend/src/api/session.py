"""Keeping this tab's session of Orbit remembered while the app is open.

The app says it is open every few minutes, and that it is closing when its tab goes.
What was said in the session (session_memory.py) is then kept, or let go soon after.
"""

import contextlib

from fastapi import APIRouter, Depends, HTTPException, Response
from redis.exceptions import RedisError

import session_memory

from .guard import require_app

router = APIRouter()

_store = None


def _redis():
    """One Redis client for the API, made the first time it is needed."""
    global _store
    if _store is None:
        _store = session_memory.client()
    return _store


async def _keep(session: str, seconds: int) -> Response:
    found = session_memory.session_id(session)
    if found is None:
        raise HTTPException(404, "No such session.")
    # Without Redis nothing was remembered, so there is nothing to keep.
    with contextlib.suppress(RedisError, OSError):
        await session_memory.keep(_redis(), found, seconds)
    return Response(status_code=204)


@router.post("/session/{session}/alive", dependencies=[Depends(require_app)])
async def alive(session: str) -> Response:
    """The app is open: keep what was said in its session."""
    return await _keep(session, session_memory.ALIVE_SECONDS)


@router.post("/session/{session}/closing", dependencies=[Depends(require_app)])
async def closing(session: str) -> Response:
    """The app's tab is going: let what was said go, unless it is only reloading."""
    return await _keep(session, session_memory.CLOSING_SECONDS)
