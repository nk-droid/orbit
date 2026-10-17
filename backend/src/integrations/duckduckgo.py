"""Web search through DuckDuckGo.

DuckDuckGo has no search API. This goes through the `ddgs` library, which reads the
results page a browser would get. DuckDuckGo answers some such requests with a bot
check instead of results, and the library cannot tell that apart from a search that
found nothing, so both are reported as SearchUnavailableError.
"""

import asyncio
import os
from typing import Any
from urllib.parse import urlparse

from ddgs import DDGS
from ddgs.exceptions import DDGSException

MAX_RESULTS = 8
# Enough of each result for the agent to answer from, without flooding its context.
SNIPPET_CHARS = 300
TIMEOUT_SECONDS = 6
# A second try uses a fresh connection, which DuckDuckGo often lets through.
ATTEMPTS = 2
RETRY_PAUSE_SECONDS = 0.5


class SearchUnavailableError(Exception):
    """DuckDuckGo returned nothing: it refused the search, or there was nothing."""


def _backend() -> str:
    """Which engine the library asks. Setting ORBIT_SEARCH_BACKEND=auto lets it fall
    back to other engines when DuckDuckGo refuses."""
    return os.getenv("ORBIT_SEARCH_BACKEND", "duckduckgo")


def _site(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def _result(item: dict[str, Any]) -> dict[str, Any]:
    # Web results carry "href"; news results carry "url", a source and a date.
    url = item.get("href") or item.get("url") or ""
    result = {
        "title": (item.get("title") or "").strip(),
        "site": item.get("source") or _site(url),
        "url": url,
        "snippet": " ".join((item.get("body") or "").split())[:SNIPPET_CHARS],
    }
    if item.get("date"):
        result["date"] = item["date"][:10]
    return result


def _search(query: str, news: bool, limit: int) -> list[dict[str, Any]]:
    engine = DDGS(timeout=TIMEOUT_SECONDS)
    look = engine.news if news else engine.text
    return look(query, max_results=limit, backend=_backend())


async def search(
    query: str, *, news: bool = False, limit: int = 5
) -> list[dict[str, Any]]:
    """Results for a search phrase, best first. With `news`, recent dated articles."""
    limit = min(max(limit, 1), MAX_RESULTS)
    for attempt in range(ATTEMPTS):
        if attempt:
            await asyncio.sleep(RETRY_PAUSE_SECONDS)
        try:
            # The library is blocking, so it runs off the event loop.
            found = await asyncio.to_thread(_search, query, news, limit)
        except DDGSException:
            continue
        return [_result(item) for item in found[:limit]]
    raise SearchUnavailableError
