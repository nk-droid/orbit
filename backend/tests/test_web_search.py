import pytest
from ddgs.exceptions import DDGSException
from livekit.agents.llm import ToolError

from agent_tools.web import web_search
from integrations import duckduckgo

WEB = [
    {
        "title": " Canberra - Wikipedia ",
        "href": "https://en.wikipedia.org/wiki/Canberra",
        "body": "Canberra is the capital city of Australia.\n\n" + "More text. " * 60,
    },
    {
        "title": "Australia | Britannica",
        "href": "https://www.britannica.com/place/Australia",
        "body": "Australia, the smallest continent.",
    },
]
NEWS = [
    {
        "title": "Russell wins in Azerbaijan",
        "url": "https://news.example.com/f1/russell-wins",
        "source": "Example News",
        "date": "2026-09-28T14:05:00+00:00",
        "body": "George Russell wins Sunday's race.",
        "image": "https://news.example.com/img.jpg",
    }
]


@pytest.fixture
def duck(monkeypatch) -> dict:
    """DuckDuckGo is replaced: a test says what each attempt returns or raises."""
    world: dict = {"answers": [], "asked": []}

    class FakeEngine:
        def __init__(self, timeout=None):
            pass

        def _answer(self, kind, query, max_results, backend):
            world["asked"].append((kind, query, max_results, backend))
            answer = world["answers"].pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        def text(self, query, max_results, backend):
            return self._answer("web", query, max_results, backend)

        def news(self, query, max_results, backend):
            return self._answer("news", query, max_results, backend)

    monkeypatch.setattr(duckduckgo, "DDGS", FakeEngine)
    monkeypatch.setattr(duckduckgo, "RETRY_PAUSE_SECONDS", 0)
    monkeypatch.delenv("ORBIT_SEARCH_BACKEND", raising=False)
    return world


async def test_web_results_become_short_excerpts_with_their_site(duck):
    duck["answers"] = [WEB]

    first, second = await duckduckgo.search("capital of Australia")

    assert first["title"] == "Canberra - Wikipedia"
    assert first["site"] == "en.wikipedia.org"
    assert first["url"] == "https://en.wikipedia.org/wiki/Canberra"
    assert first["snippet"].startswith(
        "Canberra is the capital city of Australia. More"
    )
    assert len(first["snippet"]) == duckduckgo.SNIPPET_CHARS
    assert second["site"] == "britannica.com"
    assert duck["asked"] == [("web", "capital of Australia", 5, "duckduckgo")]


async def test_news_results_carry_their_source_and_date(duck):
    duck["answers"] = [NEWS]

    (article,) = await duckduckgo.search("Formula 1 result", news=True)

    assert article["site"] == "Example News"
    assert article["date"] == "2026-09-28"
    assert "image" not in article
    assert duck["asked"][0][0] == "news"


async def test_the_number_of_results_is_kept_within_bounds(duck):
    duck["answers"] = [WEB, WEB]

    await duckduckgo.search("anything", limit=50)
    await duckduckgo.search("anything", limit=0)

    assert [asked[2] for asked in duck["asked"]] == [duckduckgo.MAX_RESULTS, 1]


async def test_a_refused_search_is_tried_once_more(duck):
    duck["answers"] = [DDGSException("No results found."), WEB]

    results = await duckduckgo.search("capital of Australia")

    assert len(results) == 2
    assert len(duck["asked"]) == 2


async def test_a_search_refused_twice_is_reported_as_unavailable(duck):
    duck["answers"] = [DDGSException("No results found.")] * 2

    with pytest.raises(duckduckgo.SearchUnavailableError):
        await duckduckgo.search("capital of Australia")

    assert len(duck["asked"]) == duckduckgo.ATTEMPTS


async def test_another_engine_is_used_only_when_asked_for(duck, monkeypatch):
    monkeypatch.setenv("ORBIT_SEARCH_BACKEND", "auto")
    duck["answers"] = [WEB]

    await duckduckgo.search("capital of Australia")

    assert duck["asked"][0][3] == "auto"


async def test_the_agent_is_told_when_the_search_did_not_work(duck):
    duck["answers"] = [DDGSException("No results found.")] * 2

    with pytest.raises(ToolError, match="could not look it up"):
        await web_search(None, "capital of Australia")


async def test_the_agent_gets_the_results_and_how_many(duck):
    duck["answers"] = [WEB]

    found = await web_search(None, "  capital of Australia  ", max_results=2)

    assert found["query"] == "capital of Australia"
    assert found["count"] == 2
    assert found["results"][0]["site"] == "en.wikipedia.org"


async def test_an_empty_search_is_not_sent(duck):
    with pytest.raises(ToolError):
        await web_search(None, "   ")

    assert duck["asked"] == []
