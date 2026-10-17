"""A check shared by every route that changes something."""

from fastapi import Header, HTTPException


def require_app(x_orbit_app: str | None = Header(default=None)) -> None:
    """A custom header forces a CORS preflight, so no other site open in the
    browser can trigger a change."""
    if x_orbit_app != "1":
        raise HTTPException(403, "Requests must come from the Orbit app.")
