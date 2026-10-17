# Starts the real Google Chrome, always hidden, so nothing appears on screen and
# nothing is heard. Skipped on a machine without Chrome.

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
import websockets

from agent_tools import chrome
from agent_tools.chrome import (
    Chrome,
    Profile,
    ProfileError,
    ProfilesBlockedError,
    Running,
)

pytestmark = pytest.mark.skipif(
    chrome._executable() is None, reason="needs Google Chrome"
)

SRC = str(Path(__file__).parents[1] / "src")
# What a conversation's process does: starts a browser and ends without closing it.
START_AND_LEAVE = """
import asyncio
from agent_tools.chrome import Chrome
print(asyncio.run(Chrome().start(False)).pid)
"""


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path))
    # Never the profiles of whoever runs the tests.
    monkeypatch.setenv("ORBIT_CHROME_USER_DATA", str(tmp_path / "no-chrome-here"))


async def _ask_browser(running: Running, method: str, params: dict | None = None):
    """One command over the browser's debugging port."""
    address = chrome._ask(running, "/json/version")["webSocketDebuggerUrl"]
    async with websockets.connect(address, max_size=None) as socket:
        await socket.send(
            json.dumps({"id": 1, "method": method, "params": params or {}})
        )
        while True:
            answer = json.loads(await socket.recv())
            if answer.get("id") == 1:
                return answer.get("result", {})


async def _cookies(running: Running) -> dict[str, str]:
    cookies = (await _ask_browser(running, "Storage.getCookies"))["cookies"]
    return {cookie["name"]: cookie["value"] for cookie in cookies}


def _own_chrome(user_data: Path, folder: str) -> tuple[subprocess.Popen, Running]:
    """A Chrome like the user's own: started by them, on a profile of theirs."""
    process = subprocess.Popen(
        [
            chrome._executable(),
            f"--user-data-dir={user_data}",
            f"--profile-directory={folder}",
            "--remote-debugging-port=0",
            "--headless=new",
            "--no-first-run",
            "about:blank",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(400):
        running = chrome._announced(process.pid, str(user_data), False, None)
        if running is not None and chrome._answers(running):
            return process, running
        time.sleep(0.05)
    process.kill()
    raise AssertionError("the stand-in for the user's Chrome did not start")


SIGNED_IN = {
    "name": "session",
    "value": "signed-in-as-the-user",
    "domain": "example.com",
    "path": "/",
    "secure": True,
}


@pytest.fixture(scope="module")
def users_chrome(tmp_path_factory) -> Path:
    """Chrome data like the user's own, made by Chrome itself: a profile they
    call Work that is signed in somewhere, and one they call Personal."""
    user_data = tmp_path_factory.mktemp("users-chrome") / "User Data"
    process, running = _own_chrome(user_data, "Profile 1")

    async def sign_in_and_quit() -> None:
        cookie = {**SIGNED_IN, "expires": time.time() + 86400}
        await _ask_browser(running, "Storage.setCookies", {"cookies": [cookie]})
        # Closing it properly is what writes the cookie to the profile.
        await _ask_browser(running, "Browser.close")

    asyncio.run(sign_in_and_quit())
    process.wait(20)

    state = json.loads((user_data / "Local State").read_text())
    known = state.setdefault("profile", {}).setdefault("info_cache", {})
    known.setdefault("Profile 1", {})["name"] = "Work"
    known["Default"] = {"name": "Personal"}
    (user_data / "Default").mkdir()
    (user_data / "Local State").write_text(json.dumps(state))
    return user_data


@pytest.fixture
def profiles(users_chrome, monkeypatch) -> Path:
    monkeypatch.setenv("ORBIT_CHROME_USER_DATA", str(users_chrome))
    return users_chrome


@pytest.fixture
async def browser():
    """Whatever a test leaves running is closed afterwards."""
    yield Chrome()
    for left in await Chrome().running():
        await Chrome().stop(left.name)


def _recorded(tmp_path) -> list[dict]:
    return json.loads((tmp_path / "browser.json").read_text())


async def test_nothing_is_running_until_a_browser_is_started(browser):
    assert await browser.running() == []


async def test_a_started_browser_answers_and_is_on_record(browser, tmp_path):
    started = await browser.start(False)

    assert await browser.running() == [started]
    assert started.shown is False
    assert started.name == "clean"
    assert [(r["name"], r["port"]) for r in _recorded(tmp_path)] == [
        ("clean", started.port)
    ]
    assert Path(started.profile).is_dir()


async def test_the_browser_outlives_the_process_that_started_it(browser, tmp_path):
    left = subprocess.run(
        [sys.executable, "-c", START_AND_LEAVE],
        env={**os.environ, "PYTHONPATH": SRC, "ORBIT_DATA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=True,
    )

    (found,) = await browser.running()

    assert found.pid == int(left.stdout)


async def test_stopping_closes_the_browser_and_deletes_its_profile(browser, tmp_path):
    started = await browser.start(False)

    # As a later conversation does it: from an object that did not start the browser.
    await Chrome().stop("clean")

    assert await browser.running() == []
    assert not (tmp_path / "browser.json").exists()
    assert not Path(started.profile).exists()


async def test_a_browser_closed_by_hand_is_forgotten(browser, tmp_path):
    started = await browser.start(False)

    os.killpg(started.pid, signal.SIGKILL)

    assert await browser.running() == []
    assert not (tmp_path / "browser.json").exists()
    assert not Path(started.profile).exists()


async def test_a_browser_whose_last_window_was_closed_is_closed_and_forgotten(
    browser, tmp_path
):
    started = await browser.start(False)

    # What closing the window by hand does: the tabs go, and on a Mac Chrome stays.
    for target in chrome._ask(started, "/json/list"):
        if target["type"] == "page":
            chrome._LOCAL.open(f"{started.endpoint}/json/close/{target['id']}").close()
    time.sleep(0.5)

    assert await browser.running() == []
    assert chrome._wait_until_gone(started.pid, 5)
    assert not (tmp_path / "browser.json").exists()
    assert not Path(started.profile).exists()


async def test_the_page_last_opened_is_kept_with_the_browser(browser):
    await browser.start(False)
    await browser.start(False)

    browser.note_page("clean 2", "https://example.com/")

    first, second = await Chrome().running()
    assert (first.url, second.url) == (None, "https://example.com/")


async def test_another_browser_is_started_beside_the_first(browser, tmp_path):
    first = await browser.start(False)

    second = await browser.start(False)

    assert await browser.running() == [first, second]
    assert (first.name, second.name) == ("clean", "clean 2")
    # Two browsers of their own: each its process, its data and its port.
    assert first.pid != second.pid
    assert first.port != second.port
    assert Path(first.profile).is_dir() and Path(second.profile).is_dir()
    assert first.profile != second.profile
    assert [r["name"] for r in _recorded(tmp_path)] == ["clean", "clean 2"]


async def test_closing_one_browser_leaves_the_other_running(browser, tmp_path):
    first = await browser.start(False)
    second = await browser.start(False)

    await Chrome().stop("clean")

    assert await browser.running() == [second]
    assert chrome._wait_until_gone(first.pid, 5)
    assert not Path(first.profile).exists()
    assert chrome._answers(second)
    # The name the first one had is free again.
    third = await browser.start(False)
    assert third.name == "clean"
    assert [r.name for r in await browser.running()] == ["clean 2", "clean"]


async def test_a_browser_started_again_under_its_name_takes_the_place_of_the_old(
    browser,
):
    first = await browser.start(False)
    other = await browser.start(False)

    again = await browser.start(False, None, "clean")

    assert [r.name for r in await browser.running()] == ["clean 2", "clean"]
    assert again.pid != first.pid
    assert chrome._wait_until_gone(first.pid, 5)
    assert not Path(first.profile).exists()
    assert chrome._answers(other)


async def test_the_tabs_of_a_browser_are_known_by_their_addresses(browser):
    started = await browser.start(False)
    await _ask_browser(started, "Target.createTarget", {"url": "data:text/html,second"})
    await _ask_browser(started, "Target.createTarget", {"url": "data:text/html,third"})
    time.sleep(0.3)

    pages = await browser.pages("clean")

    assert sorted(pages) == [
        "about:blank",
        "data:text/html,second",
        "data:text/html,third",
    ]
    assert await browser.pages("no such browser") == []


async def test_a_browser_an_older_orbit_wrote_down_is_still_found(browser, tmp_path):
    started = await browser.start(False)
    # As Orbit recorded its one browser before it could have several: on its own,
    # and without a name.
    old = {k: v for k, v in _recorded(tmp_path)[0].items() if k != "name"}
    (tmp_path / "browser.json").write_text(json.dumps(old))

    (found,) = await Chrome().running()

    assert found == started
    assert found.name == "clean"
    # It is kept in the new way from the first change on, and closes like any other.
    Chrome().note_page("clean", "https://example.com/")
    assert [r["name"] for r in _recorded(tmp_path)] == ["clean"]
    await Chrome().stop("clean")
    assert chrome._wait_until_gone(started.pid, 5)
    assert not (tmp_path / "browser.json").exists()


async def test_a_stranger_on_the_same_port_is_neither_used_nor_closed(
    browser, tmp_path
):
    started = await browser.start(False)
    # The record of a browser that has gone, whose port a different one now has.
    stale = replace(started, browser_id="/devtools/browser/gone", profile="")
    chrome._write_all([stale])

    assert await browser.running() == []
    await browser.stop("clean")

    assert chrome._answers(started)
    chrome._write_all([started])


async def test_the_browser_is_not_given_orbits_keys(browser, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "not-for-the-browser")

    started = await browser.start(False)

    environment = subprocess.run(
        ["ps", "eww", "-p", str(started.pid)], capture_output=True, text=True
    ).stdout
    assert "--remote-debugging-port" in environment
    assert "not-for-the-browser" not in environment


async def test_the_users_profiles_are_listed_by_the_names_they_gave_them(
    browser, profiles
):
    assert await browser.profiles() == [
        Profile("Default", "Personal"),
        Profile("Profile 1", "Work"),
    ]


async def test_without_chrome_data_there_are_no_profiles(browser):
    assert await browser.profiles() == []


async def test_profiles_the_system_keeps_from_orbit_are_not_taken_for_none(
    browser, profiles, monkeypatch
):
    # What macOS does to another app that opens Chrome's data without leave.
    def refused():
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(chrome, "_local_state", refused)

    with pytest.raises(ProfilesBlockedError):
        await browser.profiles()


async def test_a_clean_browser_knows_nothing_of_the_users_profiles(browser, profiles):
    started = await browser.start(False)

    assert started.copy_of is None
    assert await _cookies(started) == {}


async def test_a_copy_of_a_profile_is_signed_in_where_the_profile_is(browser, profiles):
    work = Profile("Profile 1", "Work")

    started = await browser.start(False, work)

    assert started.copy_of == work
    assert started.name == "Work"
    assert (await _cookies(started))["session"] == "signed-in-as-the-user"
    # A later conversation learns from the record which profile it is a copy of.
    assert [r.copy_of for r in await Chrome().running()] == [work]
    # The copy is Orbit's own, apart from the user's Chrome.
    assert Path(started.profile) != profiles
    assert Path(started.profile, "Profile 1").is_dir()


async def test_a_profile_is_copied_while_the_users_chrome_is_running(browser, profiles):
    process, theirs = _own_chrome(profiles, "Profile 1")
    try:
        started = await browser.start(False, Profile("Profile 1", "Work"))

        assert (await _cookies(started))["session"] == "signed-in-as-the-user"
        # Theirs was not disturbed.
        assert chrome._answers(theirs)
    finally:
        await _ask_browser(theirs, "Browser.close")
        process.wait(20)


async def test_closing_deletes_the_copy_and_leaves_the_users_profile_alone(
    browser, profiles
):
    started = await browser.start(False, Profile("Profile 1", "Work"))
    await _ask_browser(
        started,
        "Storage.setCookies",
        {"cookies": [{"name": "orbit", "value": "was-here", "domain": "example.com"}]},
    )

    await browser.stop("Work")

    assert not Path(started.profile).exists()
    again = await browser.start(False, Profile("Profile 1", "Work"))
    cookies = await _cookies(again)
    assert cookies["session"] == "signed-in-as-the-user"
    assert "orbit" not in cookies


async def test_two_browsers_on_one_profile_each_have_a_copy_of_it(browser, profiles):
    work = Profile("Profile 1", "Work")

    first = await browser.start(False, work)
    second = await browser.start(False, work)
    clean = await browser.start(False)

    assert [r.name for r in await browser.running()] == ["Work", "Work 2", "clean"]
    assert first.profile != second.profile
    # Signed in alike, and apart from each other from then on.
    await _ask_browser(
        first,
        "Storage.setCookies",
        {
            "cookies": [
                {"name": "only", "value": "in-the-first", "domain": "example.com"}
            ]
        },
    )
    assert (await _cookies(first))["session"] == "signed-in-as-the-user"
    assert (await _cookies(second)) == {"session": "signed-in-as-the-user"}
    assert await _cookies(clean) == {}


async def test_an_older_orbits_profile_browser_keeps_its_profile_as_its_name(
    browser, profiles, tmp_path
):
    work = Profile("Profile 1", "Work")
    await browser.start(False, work)
    old = {k: v for k, v in _recorded(tmp_path)[0].items() if k != "name"}
    (tmp_path / "browser.json").write_text(json.dumps(old))

    (found,) = await Chrome().running()

    assert (found.name, found.copy_of) == ("Work", work)


async def test_a_profile_that_cannot_be_copied_starts_nothing(browser, profiles):
    with pytest.raises(ProfileError):
        await browser.start(False, Profile("Profile 9", "Gone"))

    assert await browser.running() == []
    assert chrome._read_all() == []


@pytest.mark.parametrize("clone", [True, False])
def test_the_users_open_tabs_are_not_copied(tmp_path, clone):
    if clone and sys.platform != "darwin":
        pytest.skip("files are cloned on a Mac only")
    source = tmp_path / "Profile 1"
    (source / "Sessions").mkdir(parents=True)
    (source / "Sessions" / "Tabs_1").write_text("the user's open tabs")
    (source / "Cache").mkdir()
    (source / "Cache" / "data_0").write_text("a cached page")
    (source / "Cookies").write_text("what keeps the user signed in")
    (source / "Local Storage").mkdir()
    (source / "Local Storage" / "leveldb").write_text("site data")

    chrome._copy_folder(source, tmp_path / "copy", clone=clone)

    copied = {path.name for path in (tmp_path / "copy").rglob("*")}
    assert {"Cookies", "Local Storage", "leveldb"} <= copied
    assert "Sessions" not in copied
    assert "Tabs_1" not in copied
    # Cloned files take no space, so the cache may come along. A real copy leaves it.
    assert ("Cache" in copied) is clone
    assert (source / "Sessions" / "Tabs_1").exists()
