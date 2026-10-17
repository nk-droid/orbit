"""The agent's tool for looking things up on the web.

It returns short excerpts only and never opens a page. What comes back was written
by strangers, so the agent is told to weigh it as information and never to act on
instructions found in it.
"""

from typing import Any

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from integrations import duckduckgo

from . import stage


@function_tool()
async def web_search(
    context: RunContext, query: str, news: bool = False, max_results: int = 5
) -> dict[str, Any]:
    """Search the web with DuckDuckGo for public or current information.

    Use this when the answer depends on something you cannot be sure of from
    memory: news, sports results, recent events, or facts about places, people,
    products and organisations. Do not use it for the user's own email, calendar,
    files or reminders, and never put details from those into the query.

    Args:
        query: A short search phrase, as you would type into a search engine.
        news: True to search recent news articles, which come with their dates.
            Use it for anything that happened lately.
        max_results: How many results to return, from 1 to 8.
    """
    query = query.strip()
    if not query:
        raise ToolError("There is nothing to search for. Ask the user what to look up.")
    try:
        results = await duckduckgo.search(query, news=news, limit=max_results)
    except duckduckgo.SearchUnavailableError as e:
        raise ToolError(
            "The web search returned nothing: DuckDuckGo sometimes refuses automated "
            "searches. Tell the user you could not look it up just now. Do not "
            "answer as though you had."
        ) from e
    await stage.show_list(
        "found:web",
        "Web",
        "globe",
        [
            stage.row(
                result["title"],
                " · ".join(filter(None, [result["site"], result.get("date")])),
                result["snippet"],
            )
            for result in results
        ],
        tag=query[:40],
    )
    return {"query": query, "results": results, "count": len(results)}


WEB_TOOLS = [web_search]
