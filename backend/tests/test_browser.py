import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from livekit.agents import mcp
from livekit.agents.llm import ToolError

from actions.gate import ActionGate
from agent import Assistant
from agent_tools import browser as browser_tools
from agent_tools import chrome
from agent_tools.browser import (
    MAX_READ_CHARS,
    MAX_RESULT_CHARS,
    TOOLS,
    BrowserToolset,
    asked_for_in,
    in_their_words,
    is_local,
    named,
    readable,
    readable_whole,
    whole_address,
)
from agent_tools.chrome import (
    Profile,
    ProfileError,
    ProfilesBlockedError,
    Running,
)

STUB = str(Path(__file__).parent / "browser_stub.py")
EXAMPLE = "### Page\n- Page URL: https://example.com/\n- Page Title: Example Domain"
PERSONAL = Profile("Default", "Personal")
WORK = Profile("Profile 1", "Work")


class FakeChrome:
    """Stands in for Orbit's Chrome: browsers that outlive the toolset using them,
    on a machine where the user has two Chrome profiles of their own."""

    def __init__(self) -> None:
        self.started: list[bool] = []
        # For each browser started, the name of the profile it was a copy of.
        self.copied: list[str | None] = []
        # The browsers that are open, in the order they were started.
        self.open: list[Running] = []
        # The addresses of the tabs open in each, by its name.
        self.tabs: dict[str, list[str]] = {}
        self.users_profiles = [PERSONAL, WORK]
        # The system keeps Orbit out of Chrome's data, as macOS does by default.
        self.blocked = False

    @property
    def current(self) -> Running | None:
        """The browser started last that is still open."""
        return self.open[-1] if self.open else None

    def named(self, name: str) -> Running | None:
        return next((browser for browser in self.open if browser.name == name), None)

    async def profiles(self) -> list[Profile]:
        if self.blocked:
            raise ProfilesBlockedError("Chrome")
        return self.users_profiles

    async def running(self) -> list[Running]:
        return list(self.open)

    async def start(
        self, show: bool, copy_of: Profile | None = None, name: str | None = None
    ) -> Running:
        if copy_of is not None and copy_of not in self.users_profiles:
            raise ProfileError(copy_of.name)
        if name is None:
            name = chrome._free_name(copy_of, {browser.name for browser in self.open})
        else:
            await self.stop(name)
        self.started.append(show)
        self.copied.append(copy_of.name if copy_of else None)
        port = 9000 + len(self.started)
        browser = Running(
            1, port, f"/devtools/browser/fake-{port}", show, "", copy_of, name=name
        )
        self.open.append(browser)
        self.tabs[name] = []
        return browser

    async def stop(self, name: str) -> None:
        self.open = [browser for browser in self.open if browser.name != name]
        self.tabs.pop(name, None)

    async def pages(self, name: str) -> list[str]:
        return list(self.tabs.get(name, []))

    def note_page(self, name: str, url: str | None) -> None:
        self.open = [replace(b, url=url) if b.name == name else b for b in self.open]
        # One tab, sent from page to page, unless a test opened more by hand.
        if name in self.tabs and len(self.tabs[name]) <= 1:
            self.tabs[name] = [url] if url else []

    def closed_by_hand(self, name: str | None = None) -> None:
        """The user closes a browser's window themselves: the last one, or by name."""
        gone = name or (self.open[-1].name if self.open else "")
        self.open = [browser for browser in self.open if browser.name != gone]
        self.tabs.pop(gone, None)


class StubBrowser:
    """Stand-in browsers and stand-in Playwright servers: what they answer, and
    which one was used."""

    def __init__(self, directory: Path) -> None:
        self._script = directory / "browser-script.json"
        self._calls = directory / "browser-calls.jsonl"
        self._script.write_text("{}")
        self._calls.write_text("")
        self.chrome = FakeChrome()
        self.toolset = BrowserToolset(self._server, self.chrome)

    @property
    def started(self) -> list[bool]:
        """Every browser that was started, as shown or not."""
        return self.chrome.started

    async def next_conversation(self) -> BrowserToolset:
        """Ends the conversation and begins another, as the agent does: with a
        toolset of its own that takes over whatever browsers are still open."""
        await self.toolset.aclose()
        self.toolset = BrowserToolset(self._server, self.chrome)
        await self.toolset.resume()
        await self.toolset.setup()
        return self.toolset

    def _server(self, endpoint: str, output_dir: str) -> mcp.MCPServer:
        (attached,) = [b for b in self.chrome.open if b.endpoint == endpoint]
        mode = "shown" if attached.shown else "hidden"
        return mcp.MCPServerStdio(
            command=sys.executable,
            args=[STUB, mode, str(self._script), str(self._calls), attached.name],
            client_session_timeout_seconds=30,
        )

    def answers(self, tool: str, text: str) -> None:
        script = json.loads(self._script.read_text())
        script[tool] = text
        self._script.write_text(json.dumps(script))

    def asked(self) -> list[dict]:
        """Every call the stand-in servers received, in full."""
        return [json.loads(line) for line in self._calls.read_text().splitlines()]

    def calls(self) -> list[tuple[str, str]]:
        """Every call the stand-in servers received, as (mode, tool)."""
        return [(call["mode"], call["tool"]) for call in self.asked()]

    def calls_in(self) -> list[tuple[str, str]]:
        """Every call the stand-in servers received, as (browser, tool)."""
        return [(call["browser"], call["tool"]) for call in self.asked()]


@pytest.fixture(autouse=True)
def apps_answer(monkeypatch):
    """Whatever address on this computer a test opens, something answers there."""

    async def answering(ports):
        return list(ports)

    monkeypatch.setattr(browser_tools, "answering", answering)


@pytest.fixture
async def browser(tmp_path):
    stub = StubBrowser(tmp_path)
    await stub.toolset.setup()
    yield stub
    await stub.toolset.aclose()


def tool(browser: StubBrowser, name: str):
    return next(t for t in browser.toolset.tools if t.id == name)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/api/mail/waiting",
        "http://127.0.0.1:5173/mail",
        "http://[::1]:8000/",
        "http://192.168.1.1/",
        "http://10.0.0.5/admin",
        "http://printer.local/",
        "file:///etc/hosts",
        "javascript:alert(1)",
        "",
    ],
)
def test_addresses_on_the_users_own_machine_or_network_are_local(url):
    assert is_local(url)


@pytest.mark.parametrize(
    "url", ["https://example.com/", "http://93.184.216.34/", "about:blank"]
)
def test_public_addresses_are_not_local(url):
    assert not is_local(url)


def test_an_answer_is_stripped_to_what_the_model_needs():
    answer = (
        "### Ran Playwright code\n```js\nawait page.goto('https://example.com');\n```\n"
        f"{EXAMPLE}\n### Snapshot\n- [Snapshot](../tmp/page-1.yml)\n"
    )

    assert readable(answer) == EXAMPLE


# A page as Playwright's server gives it: each skill named twice, as a label and as
# text, and a table whose rows are named by all of their cells.
SKILLS_PAGE = """### Page
- Page URL: https://www.me.example/
- Page Title: Me
### Snapshot
```yaml
- main [ref=e1]:
  - heading "Skills" [level=2] [ref=e2]
  - paragraph [ref=e3]: What I work with.
  - list [ref=e4]:
    - listitem [ref=e5]:
      - generic "Python" [ref=e6]
      - img [aria-hidden] [ref=e7]
      - generic [ref=e8]:
        - generic [ref=e9]: Python
        - generic [ref=e10]: AI/ML, backend
      - generic [ref=e11]: 5y+
    - listitem [ref=e12]:
      - generic "SQL" [ref=e13]
      - generic [ref=e14]:
        - generic [ref=e15]: SQL
        - generic [ref=e16]: Queries
      - generic [ref=e17]: Academic
  - link "Resume" [ref=e18] [cursor=pointer]:
    - /url: /resume.pdf
  - table [ref=e19]:
    - row "Year Hours" [ref=e20]:
      - columnheader "Year" [ref=e21]
      - columnheader "Hours" [ref=e22]
    - row "2025 1,200" [ref=e23]:
      - cell "2025" [ref=e24]
      - cell "1,200" [ref=e25]
```"""


def test_a_page_read_whole_is_written_as_text():
    assert readable_whole(SKILLS_PAGE).splitlines() == [
        "- Page URL: https://www.me.example/",
        "- Page Title: Me",
        "### Text",
        "## Skills",
        "What I work with.",
        "- Python | AI/ML, backend | 5y+",
        "- SQL | Queries | Academic",
        "Resume",
        "- Year | Hours",
        "- 2025 | 1,200",
    ]


def test_a_page_read_whole_is_cut_at_its_bound_and_says_so():
    words = "- paragraph [ref=e9]: a sentence of the page\n" * 3000
    answer = f"{EXAMPLE}\n### Snapshot\n```yaml\n{words}```"

    text = readable_whole(answer)

    assert text.startswith(EXAMPLE.split("\n", 1)[1])
    assert len(text) < MAX_READ_CHARS + 400
    assert "[The page is long:" in text
    assert "target" in text


def test_a_page_read_whole_on_the_users_machine_is_not_passed_on():
    answer = SKILLS_PAGE.replace("https://www.me.example/", "http://localhost:5173/")

    with pytest.raises(ToolError, match="own computer"):
        readable_whole(answer)


async def test_browser_read_reads_the_page_in_use_whole(browser):
    browser.answers("browser_snapshot", SKILLS_PAGE)
    await tool(browser, "browser_start")(None, show_browser=True)

    whole = await tool(browser, "browser_read")(None)
    part = await tool(browser, "browser_read")(None, target="e4")

    assert "- Python | AI/ML, backend | 5y+" in whole
    assert "[ref=" not in whole
    assert [name for _, name in browser.calls()[-2:]] == [
        "browser_snapshot",
        "browser_snapshot",
    ]
    assert [call["arguments"] for call in browser.asked()[-2:]] == [
        {},
        {"target": "e4"},
    ]
    assert "- SQL | Queries | Academic" in part


def test_a_long_page_is_cut_and_says_how_to_read_the_rest():
    answer = f"{EXAMPLE}\n### Snapshot\n" + "- paragraph: text\n" * 2000

    cut = readable(answer)

    assert cut.startswith(EXAMPLE)
    assert len(cut) < MAX_RESULT_CHARS + 400
    assert "browser_find" in cut
    assert "browser_read" in cut


def test_a_page_that_ended_up_on_the_users_machine_is_not_passed_on():
    answer = "### Page\n- Page URL: http://localhost:5173/mail\n### Snapshot\nsecret"

    with pytest.raises(ToolError, match="own computer"):
        readable(answer)


async def test_the_browser_tools_are_all_there_before_the_browser_is(browser):
    assert [t.id for t in browser.toolset.tools] == [
        "browser_start",
        "browser_read",
        "browser_list",
        *TOOLS,
    ]
    assert browser.started == []
    assert browser.toolset.shown is None


async def test_a_browser_tool_refuses_until_the_user_has_been_asked(browser):
    with pytest.raises(ToolError, match="Ask the user whether they want to see"):
        await tool(browser, "browser_navigate")(None, {"url": "https://example.com"})

    assert browser.started == []
    assert browser.calls() == []


async def test_the_browser_starts_hidden_when_the_user_does_not_want_to_see_it(
    browser,
):
    browser.answers("browser_navigate", EXAMPLE)

    started = await tool(browser, "browser_start")(None, show_browser=False)
    page = await tool(browser, "browser_navigate")(None, {"url": "https://example.com"})

    assert started == {"status": "ready", "browser": "clean", "shown": False}
    assert browser.started == [False]
    assert browser.calls() == [("hidden", "browser_navigate")]
    assert page == EXAMPLE


async def test_the_browser_starts_shown_when_the_user_wants_to_see_it(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_snapshot")(None, {})

    assert browser.toolset.shown is True
    assert browser.calls() == [("shown", "browser_snapshot")]


async def test_starting_again_the_same_way_keeps_the_running_browser(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    again = await tool(browser, "browser_start")(None, show_browser=True)

    assert again["status"] == "already running"
    assert browser.started == [True]


async def test_changing_the_answer_restarts_the_browser_the_other_way(browser):
    await tool(browser, "browser_start")(None, show_browser=False)
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_snapshot")(None, {})

    assert browser.started == [False, True]
    assert browser.calls() == [("shown", "browser_snapshot")]


async def test_switching_to_shown_opens_the_page_that_was_open_again(browser):
    browser.answers("browser_navigate", EXAMPLE)
    await tool(browser, "browser_start")(None, show_browser=False)
    await tool(browser, "browser_navigate")(None, {"url": "https://example.com"})

    switched = await tool(browser, "browser_start")(None, show_browser=True)

    assert switched == {
        "status": "ready",
        "browser": "clean",
        "shown": True,
        "pages_reopened": 1,
    }
    assert browser.calls() == [
        ("hidden", "browser_navigate"),
        ("shown", "browser_navigate"),
    ]


async def test_switching_after_the_page_was_closed_opens_nothing(browser):
    browser.answers("browser_navigate", EXAMPLE)
    await tool(browser, "browser_start")(None, show_browser=False)
    await tool(browser, "browser_navigate")(None, {"url": "https://example.com"})
    await tool(browser, "browser_close")(None, {})

    switched = await tool(browser, "browser_start")(None, show_browser=True)

    assert switched == {"status": "ready", "browser": "clean", "shown": True}
    assert ("shown", "browser_navigate") not in browser.calls()


async def test_a_local_address_never_reaches_the_browser(browser):
    await tool(browser, "browser_start")(None, show_browser=False)

    with pytest.raises(ToolError, match="own computer"):
        await tool(browser, "browser_navigate")(
            None, {"url": "http://localhost:8000/api/mail/waiting"}
        )

    assert browser.calls() == []


async def test_a_hidden_browser_closes_with_the_conversation(tmp_path):
    stub = StubBrowser(tmp_path)
    await stub.toolset.setup()
    await tool(stub, "browser_start")(None, show_browser=False)

    await stub.toolset.aclose()

    assert stub.chrome.current is None
    assert stub.toolset.shown is None
    with pytest.raises(ToolError, match="has not been started"):
        await tool(stub, "browser_snapshot")(None, {})


async def test_a_browser_the_user_can_see_stays_open_when_the_conversation_ends(
    tmp_path,
):
    stub = StubBrowser(tmp_path)
    await stub.toolset.setup()
    await tool(stub, "browser_start")(None, show_browser=True)
    await tool(stub, "browser_navigate")(None, {"url": "https://example.com"})

    await stub.toolset.aclose()

    assert stub.chrome.current is not None
    assert stub.chrome.current.url == "https://example.com"
    # The conversation that is over has let go of it all the same.
    with pytest.raises(ToolError, match="has not been started"):
        await tool(stub, "browser_snapshot")(None, {})


async def test_the_next_conversation_uses_the_browser_that_was_left_open(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_navigate")(None, {"url": "https://example.com"})

    await browser.next_conversation()
    await tool(browser, "browser_click")(None, {"element": "Pause", "target": "e5"})

    assert browser.toolset.shown is True
    # The same browser, not a new one: what it was playing was never interrupted.
    assert browser.started == [True]
    assert browser.calls() == [
        ("shown", "browser_navigate"),
        ("shown", "browser_click"),
    ]


async def test_asking_for_a_shown_browser_again_does_not_restart_the_one_left_open(
    browser,
):
    await tool(browser, "browser_start")(None, show_browser=True)

    await browser.next_conversation()
    again = await tool(browser, "browser_start")(None, show_browser=True)

    assert again["status"] == "already running"
    assert browser.started == [True]


async def test_hiding_a_browser_left_open_opens_its_page_again(browser):
    browser.answers("browser_navigate", EXAMPLE)
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_navigate")(None, {"url": "https://example.com"})

    await browser.next_conversation()
    switched = await tool(browser, "browser_start")(None, show_browser=False)

    assert switched == {
        "status": "ready",
        "browser": "clean",
        "shown": False,
        "pages_reopened": 1,
    }
    assert browser.started == [True, False]
    assert browser.calls()[-1] == ("hidden", "browser_navigate")


async def test_a_hidden_browser_left_behind_is_closed_and_not_taken_over(tmp_path):
    stub = StubBrowser(tmp_path)
    await stub.chrome.start(False)

    await stub.toolset.resume()

    assert stub.chrome.current is None
    assert stub.toolset.shown is None


async def test_closing_the_browser_closes_it_for_good(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_navigate")(None, {"url": "https://example.com"})

    closed = await tool(browser, "browser_close")(None, {})

    assert "closed" in closed
    assert browser.chrome.current is None
    # Nothing is left to open again in a later conversation.
    await browser.next_conversation()
    assert browser.toolset.shown is None


async def test_after_closing_the_browser_starts_again_without_asking(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_close")(None, {})

    await tool(browser, "browser_navigate")(None, {"url": "https://example.com"})

    assert browser.started == [True, True]
    assert browser.calls() == [("shown", "browser_navigate")]


async def test_a_browser_the_user_closed_by_hand_is_started_again(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_snapshot")(None, {})

    browser.chrome.closed_by_hand()
    await tool(browser, "browser_snapshot")(None, {})

    assert browser.started == [True, True]
    assert browser.chrome.current is not None


async def test_a_page_the_user_closed_by_hand_is_not_opened_again(browser):
    browser.answers("browser_navigate", EXAMPLE)
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_navigate")(None, {"url": "https://example.com"})

    browser.chrome.closed_by_hand()
    again = await tool(browser, "browser_start")(None, show_browser=True)

    assert again == {"status": "ready", "browser": "clean", "shown": True}
    assert browser.calls() == [("shown", "browser_navigate")]


async def test_a_local_address_does_not_start_a_closed_browser_again(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_close")(None, {})

    with pytest.raises(ToolError, match="own computer"):
        await tool(browser, "browser_navigate")(None, {"url": "http://localhost:8000"})

    assert browser.started == [True]


@pytest.mark.parametrize(
    ("asked", "meant"),
    [
        ("Work", WORK),
        ("work", WORK),
        ("work profile", WORK),
        ("  Personal  ", PERSONAL),
        ("pers", PERSONAL),
        # Chrome's own name for the first profile.
        ("default", PERSONAL),
        ("Profile 1", WORK),
    ],
)
def test_a_profile_is_found_by_the_name_the_user_gave_it(asked, meant):
    assert named(asked, [PERSONAL, WORK]) == meant


def test_an_unknown_profile_is_answered_with_the_ones_there_are():
    with pytest.raises(ToolError, match="no Chrome profile called 'School'") as error:
        named("School", [PERSONAL, WORK])

    assert "Personal, Work" in str(error.value)


def test_a_name_that_fits_two_profiles_is_asked_about():
    side = Profile("Profile 2", "Work side project")

    with pytest.raises(ToolError, match="fits several"):
        named("wor", [PERSONAL, WORK, side])
    # The whole name of one of them is not in doubt.
    assert named("work", [PERSONAL, WORK, side]) == WORK


def test_without_profiles_only_the_clean_browser_is_on_offer():
    with pytest.raises(ToolError, match="only the usual clean browser"):
        named("Work", [])


async def test_the_browser_is_clean_unless_a_profile_is_asked_for(browser):
    started = await tool(browser, "browser_start")(None, show_browser=True)

    assert started == {"status": "ready", "browser": "clean", "shown": True}
    assert browser.chrome.copied == [None]
    assert browser.toolset.profile is None


async def test_the_browser_starts_on_a_copy_of_the_profile_the_user_names(browser):
    started = await tool(browser, "browser_start")(
        None, show_browser=True, profile="work"
    )

    assert started == {
        "status": "ready",
        "browser": "Work",
        "shown": True,
        "profile": "Work",
    }
    assert browser.chrome.copied == ["Work"]
    assert browser.toolset.profile == "Work"


async def test_a_profile_the_user_does_not_have_starts_nothing(browser):
    with pytest.raises(ToolError, match="Personal, Work"):
        await tool(browser, "browser_start")(None, show_browser=True, profile="School")

    assert browser.started == []
    assert browser.toolset.shown is None


async def test_a_profile_that_cannot_be_copied_is_reported(browser):
    gone = Profile("Profile 7", "Gone")
    browser.toolset._profiles = [gone]

    async def profiles() -> list[Profile]:
        return [gone]

    browser.chrome.profiles = profiles

    with pytest.raises(ToolError, match="Gone Chrome profile could not be copied"):
        await tool(browser, "browser_start")(None, show_browser=True, profile="Gone")

    assert browser.started == []


async def test_starting_again_without_a_profile_keeps_the_profile_in_use(browser):
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")

    again = await tool(browser, "browser_start")(None, show_browser=True)

    assert again == {
        "status": "already running",
        "browser": "Work",
        "shown": True,
        "profile": "Work",
    }
    assert browser.chrome.copied == ["Work"]


async def test_hiding_a_profile_browser_keeps_it_on_the_profile(browser):
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")

    await tool(browser, "browser_start")(None, show_browser=False)

    assert browser.started == [True, False]
    assert browser.chrome.copied == ["Work", "Work"]


async def test_asking_for_the_clean_browser_opens_one_beside_the_profile(browser):
    browser.answers("browser_navigate", EXAMPLE)
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")
    await tool(browser, "browser_navigate")(None, {"url": "https://example.com"})

    clean = await tool(browser, "browser_start")(
        None, show_browser=True, profile="clean"
    )

    assert clean == {
        "status": "ready",
        "browser": "clean",
        "shown": True,
        "browsers_open": ["Work", "clean"],
    }
    assert browser.chrome.copied == ["Work", None]
    # The profile browser is untouched, with its page: nothing was carried over.
    assert browser.chrome.named("Work").url == "https://example.com"
    assert browser.calls_in() == [("Work", "browser_navigate")]
    # The one asked for last is the one in use.
    assert browser.toolset.profile is None


async def test_asking_for_another_profile_opens_it_beside_the_browser_in_use(browser):
    browser.answers("browser_navigate", EXAMPLE)
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_navigate")(None, {"url": "https://example.com"})

    personal = await tool(browser, "browser_start")(
        None, show_browser=True, profile="Personal"
    )

    assert personal == {
        "status": "ready",
        "browser": "Personal",
        "shown": True,
        "profile": "Personal",
        "browsers_open": ["clean", "Personal"],
    }
    assert browser.chrome.copied == [None, "Personal"]
    assert [b.name for b in browser.chrome.open] == ["clean", "Personal"]
    assert len(browser.calls()) == 1


async def test_the_next_conversation_keeps_the_profile_of_the_browser_left_open(
    browser,
):
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")

    await browser.next_conversation()
    again = await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_snapshot")(None, {})

    assert browser.toolset.profile == "Work"
    assert again["status"] == "already running"
    # Not copied a second time, and not swapped for a clean browser.
    assert browser.chrome.copied == ["Work"]


async def test_a_profile_browser_closed_by_hand_starts_again_on_the_profile(browser):
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")

    browser.chrome.closed_by_hand()
    await tool(browser, "browser_snapshot")(None, {})

    assert browser.chrome.copied == ["Work", "Work"]


async def test_the_model_is_told_which_profiles_the_user_has(browser):
    instructions = Assistant(browser=await browser.next_conversation()).instructions

    assert "The user's own Chrome profiles are: Personal, Work." in instructions
    assert "still open" not in instructions


async def test_the_model_is_told_the_browser_left_open_is_on_a_profile(browser):
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")

    instructions = Assistant(browser=await browser.next_conversation()).instructions

    assert "earlier conversation, in a copy of their Work profile," in instructions


async def test_a_user_without_profiles_is_offered_none(browser):
    browser.chrome.users_profiles = []

    instructions = Assistant(browser=await browser.next_conversation()).instructions

    assert "Chrome profiles are" not in instructions


async def test_profiles_the_system_blocks_are_reported_as_blocked(browser):
    browser.chrome.blocked = True

    with pytest.raises(ToolError, match="does not let Orbit read") as error:
        await tool(browser, "browser_start")(None, show_browser=True, profile="Work")

    assert "No Chrome profiles" not in str(error.value)
    assert browser.started == []


async def test_the_clean_browser_still_opens_when_profiles_are_blocked(browser):
    browser.chrome.blocked = True
    await browser.next_conversation()

    started = await tool(browser, "browser_start")(None, show_browser=True)

    assert started == {"status": "ready", "browser": "clean", "shown": True}
    assert browser.chrome.copied == [None]


async def test_the_model_is_told_when_the_users_profiles_are_blocked(browser):
    browser.chrome.blocked = True

    instructions = Assistant(browser=await browser.next_conversation()).instructions

    assert "does not let you read them" in instructions
    assert "Chrome profiles are:" not in instructions


def _conversation(*said: str, gate: ActionGate | None = None) -> SimpleNamespace:
    """What a tool is handed in a conversation in which the user has said this."""
    messages = [SimpleNamespace(role="user", text_content=text) for text in said]
    return SimpleNamespace(
        session=SimpleNamespace(history=SimpleNamespace(items=messages)),
        userdata=SimpleNamespace(gate=gate),
    )


ASKED_TO_OPEN = "Open example dot com in the browser and tell me its heading."


@pytest.mark.parametrize(
    ("reported", "said"),
    [
        ("show it", "Yes, show it."),
        ("yes", "Yes."),
        ("keep it hidden", "No, keep it hidden."),
        ("in view mode", "Open it in view mode please."),
        ("a browser I can watch", "Open example dot com in a browser I can watch."),
        ("Without showing me the browser", "Without showing me the browser, open it."),
    ],
)
def test_what_the_user_said_about_seeing_the_browser_is_found(reported, said):
    assert in_their_words(reported, [ASKED_TO_OPEN, said])


@pytest.mark.parametrize(
    "reported",
    [
        # Never said.
        "show it",
        "yes",
        "",
        # Said, but it says nothing about seeing the browser.
        "in the browser",
        "Open example dot com in the browser",
        # A word of theirs, and the rest made up.
        "tell me you want it shown on the screen",
    ],
)
def test_what_the_user_did_not_say_is_not_taken_for_their_wish(reported):
    assert not in_their_words(reported, [ASKED_TO_OPEN])


@pytest.fixture
def no_waiting(monkeypatch):
    """The user's words are either on record or not: nothing is on its way."""
    monkeypatch.setattr(browser_tools, "HEARD_WITHIN", 0.0)


async def test_the_first_browser_waits_until_the_user_has_said_how(browser, no_waiting):
    conversation = _conversation(ASKED_TO_OPEN)

    for made_up in ("", "show it", "in the browser"):
        with pytest.raises(ToolError, match="has not said whether they want to see"):
            await tool(browser, "browser_start")(
                conversation, show_browser=True, said=made_up
            )

    assert browser.started == []
    assert browser.toolset.shown is None


async def test_with_no_answer_given_the_tool_says_to_ask_for_the_first_browser(
    browser, no_waiting
):
    moment = [100.0]
    gate = ActionGate(clock=lambda: moment[0])
    conversation = _conversation(ASKED_TO_OPEN, gate=gate)

    with pytest.raises(ToolError, match="needs the user's answer") as needed:
        await tool(browser, "browser_start")(conversation)
    # Either may be so: they said and it was left out, or they have yet to be asked.
    assert "If they have already said so" in str(needed.value)
    assert "ask them now" in str(needed.value)
    assert browser.started == []
    # They answer, and what the model then reports is theirs.
    moment[0] = 103.0
    gate.note_user_input()
    await tool(browser, "browser_start")(conversation, show_browser=True, said="yes")

    assert browser.started == [True]


async def test_with_no_answer_given_a_further_browser_is_shown_like_the_one_in_use(
    browser,
):
    conversation = _conversation("Open it in a browser I can watch.")
    await tool(browser, "browser_start")(
        conversation, show_browser=True, said="a browser I can watch"
    )

    second = await tool(browser, "browser_start")(conversation, another=True)
    same = await tool(browser, "browser_start")(conversation, browser="clean")
    third = await tool(browser, "browser_start")(conversation, profile="Work")

    assert (second["browser"], second["shown"]) == ("clean 2", True)
    assert same["status"] == "already running"
    assert (third["browser"], third["shown"]) == ("Work", True)
    assert browser.started == [True, True, True]


async def test_a_hidden_browser_is_followed_by_hidden_ones(browser):
    await tool(browser, "browser_start")(None, show_browser=False)

    second = await tool(browser, "browser_start")(None, another=True)

    assert second["shown"] is False
    assert browser.started == [False, False]


async def test_after_the_last_browser_is_closed_the_next_is_shown_as_it_was(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_close")(None, {})

    again = await tool(browser, "browser_start")(None)

    assert (again["status"], again["shown"]) == ("ready", True)


async def test_the_users_own_words_start_the_first_browser(browser):
    conversation = _conversation("Open example dot com in a browser I can watch.")

    started = await tool(browser, "browser_start")(
        conversation, show_browser=True, said="a browser I can watch"
    )

    assert started["status"] == "ready"
    assert browser.started == [True]


async def test_words_just_spoken_are_given_a_moment_to_reach_the_record(browser):
    conversation = _conversation(ASKED_TO_OPEN)

    async def heard_a_moment_later() -> None:
        await asyncio.sleep(0.3)
        conversation.session.history.items.append(
            SimpleNamespace(role="user", text_content="Yes, show it on my screen.")
        )

    hearing = asyncio.create_task(heard_a_moment_later())
    await tool(browser, "browser_start")(
        conversation, show_browser=True, said="show it"
    )
    await hearing

    assert browser.started == [True]


async def test_sent_to_ask_the_answer_is_taken_once_the_user_has_spoken(
    browser, no_waiting
):
    moment = [100.0]
    gate = ActionGate(clock=lambda: moment[0])
    conversation = _conversation(ASKED_TO_OPEN, gate=gate)
    with pytest.raises(ToolError, match="Ask them now"):
        await tool(browser, "browser_start")(conversation, show_browser=True)

    # Asked again before the user has answered: still not theirs to give.
    with pytest.raises(ToolError, match="Ask them now"):
        await tool(browser, "browser_start")(
            conversation, show_browser=False, said="keep it hidden"
        )
    # They answer. What was heard is not written down yet, as happens with speech.
    moment[0] = 105.0
    gate.note_user_input()
    await tool(browser, "browser_start")(
        conversation, show_browser=False, said="keep it hidden"
    )

    assert browser.started == [False]


async def test_once_the_user_has_said_how_they_are_not_asked_for_each_browser(browser):
    conversation = _conversation("Open it in a browser I can watch.")
    await tool(browser, "browser_start")(
        conversation, show_browser=True, said="a browser I can watch"
    )

    await tool(browser, "browser_start")(conversation, show_browser=True, another=True)
    await tool(browser, "browser_start")(conversation, show_browser=False)

    assert browser.started == [True, True, False]


async def test_a_conversation_that_takes_over_a_browser_knows_how_it_is_wanted(browser):
    await tool(browser, "browser_start")(None, show_browser=True)

    await browser.next_conversation()
    await tool(browser, "browser_start")(
        _conversation("Open another browser."), show_browser=True, another=True
    )

    assert browser.started == [True, True]


TABS = "### Result\n- 0: [Example Domain](https://example.com/)\n- 1: (current) [](about:blank)"


async def test_a_new_tab_is_opened_in_the_browser_that_is_open(browser):
    browser.answers("browser_tabs", TABS)
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_navigate")(None, {"url": "https://example.com"})

    opened = await tool(browser, "browser_tabs")(
        None, {"action": "new", "url": "https://example.org/docs"}
    )

    assert opened == TABS
    assert browser.asked()[-1] == {
        "mode": "shown",
        "browser": "clean",
        "tool": "browser_tabs",
        "arguments": {"action": "new", "url": "https://example.org/docs"},
    }
    # The same browser: no other was started for the tab.
    assert browser.started == [True]
    assert browser.chrome.current.url == "https://example.org/docs"


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "list"},
        {"action": "select", "index": 1},
        {"action": "close", "index": 0},
    ],
)
async def test_tabs_are_listed_switched_to_and_closed(browser, arguments):
    await tool(browser, "browser_start")(None, show_browser=False)

    await tool(browser, "browser_tabs")(None, arguments)

    assert browser.asked()[-1]["arguments"] == arguments


async def test_a_tab_is_never_opened_on_the_users_own_machine(browser):
    await tool(browser, "browser_start")(None, show_browser=False)

    with pytest.raises(ToolError, match="own computer"):
        await tool(browser, "browser_tabs")(
            None, {"action": "new", "url": "http://localhost:8000/api/mail/waiting"}
        )

    assert browser.calls() == []


LOCAL_APP = "### Page\n- Page URL: http://localhost:3000/\n- Page Title: My app"


def _talk(*said: tuple[str, str]) -> SimpleNamespace:
    """What a tool is handed in a conversation in which this was said, by "user"
    or "assistant"."""
    messages = [SimpleNamespace(role=who, text_content=text) for who, text in said]
    return SimpleNamespace(
        session=SimpleNamespace(history=SimpleNamespace(items=messages)),
        userdata=SimpleNamespace(gate=None),
    )


@pytest.mark.parametrize(
    "said",
    [
        "Open http://localhost:3000 in the browser.",
        "Open localhost 3000.",
        "Open my app on local host 3000.",
        "Go to localhost:3000/login please.",
        "Open 127.0.0.1:3000.",
    ],
)
async def test_an_address_on_the_users_computer_opens_when_they_ask_for_it(
    browser, said
):
    browser.answers("browser_navigate", LOCAL_APP)
    await tool(browser, "browser_start")(None, show_browser=True)

    answer = await tool(browser, "browser_navigate")(
        _conversation(said), {"url": "http://localhost:3000"}
    )

    assert "My app" in answer
    assert browser.calls() == [("shown", "browser_navigate")]


@pytest.mark.parametrize(
    ("url", "said", "asks"),
    [
        ("http://localhost:3000/", "Open my app on port 3000.", True),
        ("http://localhost:3000/", "Open localhost three thousand.", True),
        ("http://localhost:3000/", "Open the app I'm running locally.", True),
        ("http://localhost:3000/", "3000", True),
        ("http://localhost/", "Open localhost.", True),
        ("http://192.168.1.5:8080/", "Open my router.", True),
        ("http://192.168.1.5/", "Open 192 dot 168 dot 1 dot 5.", True),
        ("http://nas.local/", "Show me the NAS.", True),
        ("http://printer.local/", "Show me the printer's page.", True),
        ("http://localhost:3000/", "Summarize the top story on Hacker News.", False),
        ("http://localhost:3000/", "Open my app.", False),
    ],
)
def test_the_user_asks_for_something_on_their_computer(url, said, asks):
    assert asked_for_in(url, [said]) is asks


@pytest.mark.parametrize(
    ("written", "whole"),
    [
        ("http:localhost:3000", "http://localhost:3000"),
        ("http:/localhost:3000", "http://localhost:3000"),
        ("localhost:3000", "http://localhost:3000"),
        ("127.0.0.1:8080/x", "http://127.0.0.1:8080/x"),
        ("printer.local/admin", "http://printer.local/admin"),
        ("https://example.com/", "https://example.com/"),
    ],
)
def test_an_address_written_in_part_is_made_whole(written, whole):
    assert whole_address(written) == whole


async def test_an_address_typed_as_the_user_typed_it_is_opened(browser):
    # The user typed it without the slashes, and the model passed it on as it was.
    browser.answers("browser_navigate", LOCAL_APP)
    await tool(browser, "browser_start")(None, show_browser=True)

    await tool(browser, "browser_navigate")(
        _conversation("http:localhost:3000"), {"url": "http:localhost:3000"}
    )

    assert browser.asked()[-1]["arguments"] == {"url": "http://localhost:3000"}


async def test_an_address_without_a_host_is_not_asked_about(browser):
    await tool(browser, "browser_start")(None, show_browser=True)

    with pytest.raises(ToolError, match="not a whole web address"):
        await tool(browser, "browser_navigate")(
            _conversation("Open it."), {"url": "http:///x"}
        )


async def test_an_address_on_the_users_computer_is_asked_about_first(
    browser, no_waiting
):
    browser.answers("browser_navigate", LOCAL_APP)
    await tool(browser, "browser_start")(None, show_browser=True)
    navigate = tool(browser, "browser_navigate")
    said = [("user", "Open my app in the browser.")]

    with pytest.raises(ToolError, match=r"Should I open http://localhost:3000\?"):
        await navigate(_talk(*said), {"url": "http://localhost:3000"})
    # A yes to another question, or a no, does not open it.
    said += [("assistant", "Do you want to see the browser?"), ("user", "Yes.")]
    with pytest.raises(ToolError, match="Ask them in a few words whether to open it"):
        await navigate(_talk(*said), {"url": "http://localhost:3000"})
    said += [("assistant", "Should I open localhost 3000?"), ("user", "No.")]
    with pytest.raises(ToolError, match="Ask them in a few words whether to open it"):
        await navigate(_talk(*said), {"url": "http://localhost:3000"})
    assert browser.calls() == []

    said += [("assistant", "Should I open localhost 3000?"), ("user", "Yes, open it.")]
    await navigate(_talk(*said), {"url": "http://localhost:3000"})
    assert browser.calls() == [("shown", "browser_navigate")]


async def test_orbits_own_app_is_never_opened_even_when_asked_for(browser):
    await tool(browser, "browser_start")(None, show_browser=True)

    with pytest.raises(ToolError, match="Orbit's own app"):
        await tool(browser, "browser_navigate")(
            _conversation("Open localhost 8000."),
            {"url": "http://localhost:8000/api/mail/waiting"},
        )

    assert browser.calls() == []


async def test_a_page_cannot_lead_to_an_address_the_user_did_not_ask_for(
    browser, no_waiting
):
    # A page on the web sent the browser on to an address on the user's computer.
    browser.answers("browser_navigate", "### Page\n- Page URL: http://localhost:4000/")
    await tool(browser, "browser_start")(None, show_browser=True)
    reading = _conversation("Summarize the top story on example dot com.")

    with pytest.raises(ToolError, match="own computer or network"):
        await tool(browser, "browser_navigate")(reading, {"url": "https://example.com"})
    # Nor can what a page says have the model go there.
    with pytest.raises(ToolError, match=r"Should I open http://localhost:4000\?"):
        await tool(browser, "browser_navigate")(
            reading, {"url": "http://localhost:4000"}
        )


async def test_only_the_users_latest_request_can_open_their_computer(
    browser, no_waiting
):
    await tool(browser, "browser_start")(None, show_browser=True)
    said = [
        ("user", "Open localhost 3000."),
        ("assistant", "It's open."),
        ("user", "Now summarize the top story on Hacker News."),
    ]

    with pytest.raises(ToolError, match=r"Should I open http://localhost:9200\?"):
        await tool(browser, "browser_navigate")(
            _talk(*said), {"url": "http://localhost:9200/_cat/indices"}
        )


async def test_the_users_app_may_send_the_browser_on_to_another_of_its_addresses(
    browser,
):
    browser.answers(
        "browser_navigate", "### Page\n- Page URL: http://localhost:4000/login"
    )
    await tool(browser, "browser_start")(None, show_browser=True)

    answer = await tool(browser, "browser_navigate")(
        _conversation("Open my app on localhost 3000."),
        {"url": "http://localhost:3000"},
    )

    assert "localhost:4000/login" in answer


async def test_when_nothing_answers_the_apps_that_do_are_named(browser, monkeypatch):
    async def answering(ports):
        # The user's app is on 4000, and Orbit's own API on 8000.
        return [port for port in ports if port in (4000, 8000)]

    monkeypatch.setattr(browser_tools, "answering", answering)
    await tool(browser, "browser_start")(None, show_browser=True)

    with pytest.raises(ToolError, match=r"answer at: http://localhost:4000\. Open the"):
        await tool(browser, "browser_navigate")(
            _conversation("Open the app I'm running locally."),
            {"url": "http://localhost:3000"},
        )
    assert browser.calls() == []


async def test_when_nothing_runs_the_user_is_asked_about_their_app(
    browser, monkeypatch
):
    async def answering(ports):
        return []

    monkeypatch.setattr(browser_tools, "answering", answering)
    await tool(browser, "browser_start")(None, show_browser=True)

    with pytest.raises(ToolError, match="whether their app is running"):
        await tool(browser, "browser_navigate")(
            _conversation("Open my app on port 3000."), {"url": "http://localhost:3000"}
        )


async def test_an_address_the_user_asked_for_stays_open_to_the_browser(browser):
    browser.answers("browser_navigate", LOCAL_APP)
    await tool(browser, "browser_start")(None, show_browser=False)
    await tool(browser, "browser_navigate")(
        _conversation("Open localhost 3000."), {"url": "http://localhost:3000"}
    )

    # Later steps need not name it again, and it is opened again when shown.
    await tool(browser, "browser_navigate")(
        None, {"url": "http://localhost:3000/settings"}
    )
    shown = await tool(browser, "browser_start")(None, show_browser=True)

    assert shown["pages_reopened"] == 1
    assert browser.asked()[-1]["arguments"] == {"url": "http://localhost:3000/settings"}


async def test_asked_for_another_one_a_second_browser_opens_beside_the_first(browser):
    await tool(browser, "browser_start")(None, show_browser=True)

    second = await tool(browser, "browser_start")(None, show_browser=True, another=True)

    assert second == {
        "status": "ready",
        "browser": "clean 2",
        "shown": True,
        "browsers_open": ["clean", "clean 2"],
    }
    assert browser.started == [True, True]
    assert [b.name for b in browser.chrome.open] == ["clean", "clean 2"]


async def test_two_browsers_can_be_on_the_same_profile(browser):
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")

    second = await tool(browser, "browser_start")(
        None, show_browser=True, profile="Work", another=True
    )

    assert (second["browser"], second["profile"]) == ("Work 2", "Work")
    # Each has a copy of the profile of its own.
    assert browser.chrome.copied == ["Work", "Work"]
    assert browser.toolset.browsers == ["Work", "Work 2"]


async def test_without_another_a_browser_of_that_kind_is_used_not_doubled(browser):
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")
    await tool(browser, "browser_start")(None, show_browser=True, profile="clean")

    again = await tool(browser, "browser_start")(
        None, show_browser=True, profile="Work"
    )

    assert again["status"] == "already running"
    assert again["browser"] == "Work"
    assert browser.chrome.copied == ["Work", None]
    assert browser.toolset.profile == "Work"


async def test_another_browser_is_a_clean_one_unless_a_profile_is_named(browser):
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")

    second = await tool(browser, "browser_start")(None, show_browser=True, another=True)

    assert second["browser"] == "clean"
    assert "profile" not in second


async def test_each_call_goes_to_the_browser_it_names(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")

    await tool(browser, "browser_snapshot")(None, {})
    await tool(browser, "browser_navigate")(
        None, {"url": "https://example.com", "browser": "clean"}
    )
    await tool(browser, "browser_snapshot")(None, {})
    await tool(browser, "browser_snapshot")(None, {"browser": "the Work browser"})

    assert browser.calls_in() == [
        # With no name given, the browser used last: first the one just started.
        ("Work", "browser_snapshot"),
        ("clean", "browser_navigate"),
        ("clean", "browser_snapshot"),
        ("Work", "browser_snapshot"),
    ]
    # The name is Orbit's own, and is not passed on to Playwright's server.
    assert all("browser" not in call["arguments"] for call in browser.asked())
    assert browser.chrome.named("clean").url == "https://example.com"
    assert browser.chrome.named("Work").url is None


async def test_with_several_open_an_answer_says_which_browser_gave_it(browser):
    browser.answers("browser_snapshot", EXAMPLE)
    await tool(browser, "browser_start")(None, show_browser=True)
    alone = await tool(browser, "browser_snapshot")(None, {})
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")

    together = await tool(browser, "browser_snapshot")(None, {})

    assert alone == EXAMPLE
    assert together == f"[In the Work browser]\n{EXAMPLE}"


@pytest.mark.parametrize(
    ("said", "meant"),
    [
        ("Work", "Work"),
        ("work", "Work"),
        ("the Work browser", "Work"),
        ("Work 2", "Work 2"),
        ("clean", "clean"),
        ("Clean browser", "clean"),
        ("cle", "clean"),
    ],
)
async def test_a_browser_is_told_by_its_name(browser, said, meant):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")
    await tool(browser, "browser_start")(
        None, show_browser=True, profile="Work", another=True
    )

    await tool(browser, "browser_snapshot")(None, {"browser": said})

    assert browser.calls_in() == [(meant, "browser_snapshot")]


@pytest.mark.parametrize("said", ["Personal", "the second one", "wor"])
async def test_a_name_no_open_browser_has_is_not_guessed_at(browser, said):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")
    await tool(browser, "browser_start")(
        None, show_browser=True, profile="Work", another=True
    )

    with pytest.raises(ToolError, match="clean, Work, Work 2"):
        await tool(browser, "browser_snapshot")(None, {"browser": said})

    assert browser.calls() == []


async def test_closing_one_browser_leaves_the_others_open(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")

    closed = await tool(browser, "browser_close")(None, {"browser": "clean"})
    await tool(browser, "browser_snapshot")(None, {})

    assert closed == (
        "The clean browser is closed, and whatever it was playing has stopped. "
        "Still open: Work."
    )
    assert [b.name for b in browser.chrome.open] == ["Work"]
    assert browser.calls_in() == [("Work", "browser_snapshot")]
    assert browser.started == [True, True]


async def test_with_no_name_the_browser_used_last_is_the_one_closed(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")
    await tool(browser, "browser_snapshot")(None, {"browser": "clean"})

    await tool(browser, "browser_close")(None, {})

    assert [b.name for b in browser.chrome.open] == ["Work"]


async def test_every_browser_can_be_closed_at_once(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_start")(None, show_browser=False, profile="Work")

    closed = await tool(browser, "browser_close")(None, {"browser": "all"})
    nothing_left = await tool(browser, "browser_close")(None, {"browser": "all"})

    assert "The clean browser is closed" in closed
    assert "The Work browser is closed" in closed
    assert "Still open" not in closed
    assert browser.chrome.open == []
    assert nothing_left == "No browser is open."


async def test_the_open_browsers_are_listed_with_what_each_is(browser):
    await tool(browser, "browser_start")(None, show_browser=False)
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")
    browser.chrome.tabs["Work"] = ["https://example.com/", "https://example.org/"]

    listed = await tool(browser, "browser_list")(None)

    assert listed == {
        "browsers": [
            {"name": "clean", "shown": False, "tabs": 0, "used_last": False},
            {
                "name": "Work",
                "shown": True,
                "profile": "Work",
                "tabs": 2,
                "used_last": True,
            },
        ],
        "count": 2,
    }


async def test_a_browser_the_user_closed_is_no_longer_listed(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")

    browser.chrome.closed_by_hand("Work")
    listed = await tool(browser, "browser_list")(None)

    assert [about["name"] for about in listed["browsers"]] == ["clean"]
    assert listed["browsers"][0]["used_last"] is True
    assert browser.started == [True, True]


async def test_before_any_browser_is_started_none_is_listed(browser):
    assert await tool(browser, "browser_list")(None) == {"browsers": [], "count": 0}


async def test_showing_a_browser_opens_every_one_of_its_tabs_again(browser):
    await tool(browser, "browser_start")(None, show_browser=False)
    await tool(browser, "browser_navigate")(None, {"url": "https://example.com"})
    browser.chrome.tabs["clean"] = [
        "https://example.com/",
        # Not pages to send a browser back to.
        "http://localhost:5173/mail",
        "about:blank",
        "chrome-error://chromewebdata/",
        "https://example.org/docs",
    ]

    shown = await tool(browser, "browser_start")(None, show_browser=True)

    assert shown["pages_reopened"] == 2
    reopened = [call for call in browser.asked() if call["mode"] == "shown"]
    assert [(call["tool"], call["arguments"]) for call in reopened] == [
        ("browser_navigate", {"url": "https://example.com/"}),
        ("browser_tabs", {"action": "new", "url": "https://example.org/docs"}),
    ]


async def test_one_browser_is_shown_while_the_others_stay_as_they_are(browser):
    await tool(browser, "browser_start")(None, show_browser=False)
    await tool(browser, "browser_start")(None, show_browser=False, profile="Work")

    shown = await tool(browser, "browser_start")(
        None, show_browser=True, browser="clean"
    )

    assert (shown["status"], shown["browser"], shown["shown"]) == (
        "ready",
        "clean",
        True,
    )
    assert browser.started == [False, False, True]
    assert browser.chrome.named("clean").shown is True
    assert browser.chrome.named("Work").shown is False
    assert browser.chrome.copied == [None, "Work", None]


async def test_only_the_hidden_browsers_close_with_the_conversation(tmp_path):
    stub = StubBrowser(tmp_path)
    await stub.toolset.setup()
    await tool(stub, "browser_start")(None, show_browser=True)
    await tool(stub, "browser_start")(None, show_browser=False, profile="Work")
    await tool(stub, "browser_start")(None, show_browser=True, another=True)

    await stub.toolset.aclose()

    assert [b.name for b in stub.chrome.open] == ["clean", "clean 2"]


async def test_the_next_conversation_takes_over_every_browser_left_open(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_navigate")(None, {"url": "https://example.com/a"})
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")
    await tool(browser, "browser_navigate")(
        None, {"url": "https://music.example.com/play"}
    )

    toolset = await browser.next_conversation()
    instructions = Assistant(browser=toolset).instructions
    await tool(browser, "browser_snapshot")(None, {"browser": "clean"})
    again = await tool(browser, "browser_start")(None, show_browser=True)

    assert toolset.browsers == ["clean", "Work"]
    assert "These browsers are still open on the user's screen" in instructions
    assert "clean, on example.com;" in instructions
    assert (
        "Work, in a copy of their Work profile, on music.example.com." in instructions
    )
    # Neither was started afresh, whichever the model went on with.
    assert again["status"] == "already running"
    assert browser.started == [True, True]
    assert browser.calls_in()[-1] == ("clean", "browser_snapshot")


async def test_opening_one_more_never_restarts_a_browser_left_open(browser):
    await tool(browser, "browser_start")(None, show_browser=True, profile="Work")

    await browser.next_conversation()
    await tool(browser, "browser_start")(None, show_browser=True, profile="clean")
    await tool(browser, "browser_start")(None, show_browser=True, another=True)

    # The one that may be playing something was started once, and is still there.
    assert browser.chrome.copied == ["Work", None, None]
    assert [b.name for b in browser.chrome.open] == ["Work", "clean", "clean 2"]


async def test_a_browser_that_cannot_be_started_leaves_the_others_in_use(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    gone = Profile("Profile 7", "Gone")

    async def profiles() -> list[Profile]:
        return [PERSONAL, WORK, gone]

    browser.chrome.profiles = profiles

    with pytest.raises(ToolError, match="Gone Chrome profile could not be copied"):
        await tool(browser, "browser_start")(None, show_browser=True, profile="Gone")
    await tool(browser, "browser_snapshot")(None, {})

    assert browser.toolset.browsers == ["clean"]
    assert browser.calls_in() == [("clean", "browser_snapshot")]


def test_the_model_is_told_whether_it_has_a_browser(tmp_path):
    with_browser = Assistant(browser=StubBrowser(tmp_path).toolset).instructions
    without = Assistant().instructions

    assert "Whether the user sees the browser while you work is their choice" in (
        with_browser
    )
    assert "still open" not in with_browser
    assert "You have no web browser in this conversation." in without
    assert with_browser.startswith("You are Orbit")


async def test_the_model_is_told_about_a_browser_that_was_left_open(browser):
    await tool(browser, "browser_start")(None, show_browser=True)
    await tool(browser, "browser_navigate")(
        None, {"url": "https://music.example.com/play"}
    )

    instructions = Assistant(browser=await browser.next_conversation()).instructions

    assert "still open on the user's screen" in instructions
    assert "on music.example.com," in instructions
    assert "without calling browser_start" in instructions
    # The note sits in the browser section, indented like the rest.
    assert "\nThe browser is still open" in instructions


# Orbit's own calls, for the job tools.


async def test_the_model_is_never_offered_orbits_own_tools(browser):
    await tool(browser, "browser_start")(None, show_browser=True)

    offered = {t.id for t in browser.toolset.tools}
    assert not offered & set(browser_tools.CODE_TOOLS)


async def test_a_file_is_put_into_a_form_through_its_chooser(browser, tmp_path):
    browser.answers(
        "browser_click",
        "### Modal state\n- [File chooser]: can be handled by browser_file_upload",
    )
    await tool(browser, "browser_start")(None, show_browser=True)
    resume = tmp_path / "Asha Rao - Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 a resume")

    await browser.toolset.put_file(None, "#attach", resume, "Resume/CV")

    click, upload = browser.asked()[-2:]
    assert (click["tool"], click["arguments"]["target"]) == ("browser_click", "#attach")
    assert upload["tool"] == "browser_file_upload"
    [copy] = map(Path, upload["arguments"]["paths"])
    # The browser takes files only from its own folder: a copy goes there.
    (open_browser,) = browser.toolset._open.values()
    assert copy.is_relative_to(open_browser.output_dir)
    assert copy.name == resume.name and copy.read_bytes() == resume.read_bytes()


async def test_no_file_is_put_in_when_no_chooser_opens(browser, tmp_path):
    browser.answers("browser_click", "### Page\n- Page URL: https://example.com/")
    await tool(browser, "browser_start")(None, show_browser=True)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4")

    with pytest.raises(ToolError, match="opened no file chooser"):
        await browser.toolset.put_file(None, "#attach", resume, "Resume/CV")
    assert "browser_file_upload" not in [name for _, name in browser.calls()]


async def test_the_tabs_open_are_read(browser):
    browser.answers(
        "browser_tabs",
        "### Result\n- 0: [Jobs | LinkedIn](https://www.linkedin.com/jobs/view/1/)\n"
        "- 1: (current) [Apply (GitLab)](https://job-boards.greenhouse.io/gitlab/jobs/8)",
    )
    await tool(browser, "browser_start")(None, show_browser=True)

    tabs = await browser.toolset.tabs(None)

    assert tabs == [
        (0, False, "https://www.linkedin.com/jobs/view/1/"),
        (1, True, "https://job-boards.greenhouse.io/gitlab/jobs/8"),
    ]


# Checks that the user is a person.


async def test_a_page_asking_whether_the_user_is_a_person_is_left_to_them(browser):
    browser.answers(
        "browser_navigate",
        "### Page\n- Page URL: https://example.com/\n### Snapshot\n```yaml\n"
        '- heading "Verify you are human" [level=1] [ref=e2]\n```',
    )
    await tool(browser, "browser_start")(None, show_browser=True)

    told = await tool(browser, "browser_navigate")(
        None, {"url": "https://example.com/"}
    )

    assert "Only the user may do it: never click, type into or try to get past it" in (
        told
    )
    assert "never click, type into or try to get past it" in (
        browser_tools.BROWSER_RULES
    )


async def test_while_orbit_waits_for_the_user_the_browser_may_only_be_shown(browser):
    await tool(browser, "browser_start")(None, show_browser=False)
    browser.toolset.hold("waiting for the user to do a check", showing=True)

    with pytest.raises(ToolError, match="busy waiting for the user"):
        await tool(browser, "browser_navigate")(None, {"url": "https://example.com/"})
    shown = await tool(browser, "browser_start")(
        None, show_browser=True, said="show it"
    )

    assert shown["shown"] is True
    browser.toolset.hold("searching for jobs")
    with pytest.raises(ToolError, match="busy searching for jobs"):
        await tool(browser, "browser_start")(None, show_browser=False, said="hide it")
