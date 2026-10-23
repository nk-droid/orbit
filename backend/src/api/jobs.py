"""The user's job profile, for its card to fetch."""

from typing import Any

from fastapi import APIRouter

from jobs import profile

router = APIRouter()


@router.get("/jobs/profile")
async def job_profile() -> dict[str, Any]:
    """The profile as its card shows it: each field, where it came from, what other
    sources said, the résumé, kept answers and the search settings."""
    return profile.card()
