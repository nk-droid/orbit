"""The Chrome that the agent's browser tools drive.

Orbit starts Chrome itself, apart from the conversation that asked for it, so the
browser does not close when the conversation does: what it is playing goes on
playing in standby, and the next conversation finds the same browser again.
Playwright's MCP server attaches to it through Chrome's remote debugging port, and
a server that leaves does not take the browser with it.

Several browsers can be open at once, each with a name to tell it by: "clean", or
the profile it is a copy of, with a number when there are two of a kind.

Each conversation runs in a process of its own, so which browsers are running is
written to browser.json in Orbit's data folder.
See https://chromedevtools.github.io/devtools-protocol/#endpoints

The browser is a clean one unless the user asks for one of their own Chrome
profiles. Chrome does not let its real profile folder be driven from outside, so
the browser is then given a copy of that profile: signed in to what the profile is
signed in to, and deleted with the browser like a clean one.
See https://developer.chrome.com/blog/remote-debugging-port
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from mcp.client.stdio import get_default_environment

from storage import data_dir

logger = logging.getLogger("orbit.browser")

DATA_PREFIX = "orbit-chrome-"
# What a browser signed in to nothing is called.
CLEAN = "clean"
# What Chrome would use to open the user's own tabs again in the copy.
_TABS = ("Sessions", "Current Session", "Current Tabs", "Last Session", "Last Tabs")
# Most of a profile's size, and nothing a site needs to know the user.
_CACHES = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GrShaderCache",
    "ShaderCache",
    "CacheStorage",
    "ScriptCache",
)
_START_SECONDS = 20
_STOP_SECONDS = 5
_ANSWER_SECONDS = 2

# No first-run screens, and a page that keeps working while Orbit's own window
# covers the browser's. Playwright passes the same.
_SWITCHES = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-search-engine-choice-screen",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)
# A clean browser has nothing to unlock, so it never touches the user's keychain.
_CLEAN = ("--password-store=basic", "--use-mock-keychain")
# A copy of a profile needs the keychain to read the profile's cookies. It does not
# sync, so what Orbit does in it stays out of the user's Google account, and it
# does not offer to bring back the tabs of a Chrome that was running when copied.
_COPY = ("--disable-sync", "--hide-crash-restore-bubble")
# A hidden browser makes no sound, as when Playwright started it.
_HIDDEN = (
    "--headless=new",
    "--mute-audio",
    "--hide-scrollbars",
    "--window-size=1280,720",
)

# The debugging port is asked for on this machine only, never through a proxy.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# The browsers this process started, which it has to collect when they end.
_children: dict[int, subprocess.Popen[bytes]] = {}
# The record is read, changed and written back in one go.
_recording = threading.Lock()


class ProfileError(Exception):
    """A copy of the user's profile could not be made."""


class ProfilesBlockedError(Exception):
    """The system does not let Orbit read Chrome's own data.

    macOS keeps other apps out of it until the user lets the app that runs Orbit
    in, under Privacy & Security in System Settings.
    """


@dataclass(frozen=True)
class Profile:
    """One of the user's own Chrome profiles."""

    # Its folder in Chrome's data, such as "Default" or "Profile 1".
    folder: str
    # What the user calls it in Chrome.
    name: str


@dataclass(frozen=True)
class Running:
    """A browser Orbit started and has not closed."""

    pid: int
    port: int
    # Chrome's own name for this run of the browser, which tells it apart from a
    # stranger that was later given the same port.
    browser_id: str
    shown: bool
    # The folder holding this browser's own data, deleted with the browser.
    profile: str
    # The profile of the user's that this browser was given a copy of. None for a
    # clean browser, signed in to nothing.
    copy_of: Profile | None = None
    # The page last opened in it.
    url: str | None = None
    # What it is called while it is open: "clean" or the profile it is a copy of,
    # with a number when there is more than one of a kind.
    name: str = ""

    @property
    def endpoint(self) -> str:
        """Where Playwright's server attaches."""
        return f"http://127.0.0.1:{self.port}"


def kind_of(copy_of: Profile | None) -> str:
    """What a browser is called after: the profile it is a copy of, or "clean"."""
    return copy_of.name if copy_of else CLEAN


def _free_name(copy_of: Profile | None, taken: set[str]) -> str:
    """A name no open browser has: its kind, numbered from the second on."""
    kind = kind_of(copy_of)
    name, number = kind, 1
    while name in taken:
        number += 1
        name = f"{kind} {number}"
    return name


class Chrome:
    """Starts, finds and closes Orbit's browsers."""

    async def running(self) -> list[Running]:
        """The browsers that are running, in the order they were started.

        A browser that has gone, closed by hand or lost in a restart, is cleared
        away here. So is one whose last window the user has closed: on a Mac that
        leaves Chrome running with nothing in it.
        """
        return await asyncio.to_thread(self._running)

    async def profiles(self) -> list[Profile]:
        """The user's own Chrome profiles, by the names they have in Chrome.

        Raises ProfilesBlockedError if the system refuses to let them be read, which
        is not the same as there being none.
        """
        return await asyncio.to_thread(_profiles)

    async def start(
        self, show: bool, copy_of: Profile | None = None, name: str | None = None
    ) -> Running:
        """Starts one more browser, on the user's screen or hidden, and waits until
        it answers. It is a clean one, or holds a copy of the user's profile.

        It gets a name of its own unless one is given, which is how a browser is
        started again under the name it had: an open browser of that name is
        closed first.
        """
        return await asyncio.to_thread(self._start, show, copy_of, name)

    async def stop(self, name: str) -> None:
        """Closes one browser and deletes its data, the copy of a profile included."""
        await asyncio.to_thread(self._stop, name)

    async def pages(self, name: str) -> list[str]:
        """The addresses of the tabs open in a browser, the one last used last."""
        return await asyncio.to_thread(self._pages, name)

    def note_page(self, name: str, url: str | None) -> None:
        """Records the page last opened in a running browser."""
        with _recording:
            _write_all(
                [replace(r, url=url) if r.name == name else r for r in _read_all()]
            )

    def _running(self) -> list[Running]:
        alive = []
        for running in _read_all():
            if _answers(running) and _has_a_page(running):
                alive.append(running)
            else:
                _close(running)
        return alive

    def _pages(self, name: str) -> list[str]:
        running = next((r for r in _read_all() if r.name == name), None)
        targets = _ask(running, "/json/list") if running else None
        if not isinstance(targets, list):
            return []
        # Chrome lists the tab used last first.
        return [
            target["url"]
            for target in reversed(targets)
            if isinstance(target, dict)
            and target.get("type") == "page"
            and isinstance(target.get("url"), str)
        ]

    def _stop(self, name: str) -> None:
        for running in _read_all():
            if running.name == name:
                _close(running)

    def _start(self, show: bool, copy_of: Profile | None, name: str | None) -> Running:
        executable = _executable()
        if executable is None:
            raise FileNotFoundError("Google Chrome is not installed")
        if name is None:
            name = _free_name(copy_of, {running.name for running in self._running()})
        else:
            self._stop(name)
        # Fresh data, deleted with the browser: signed in to nothing, unless it is
        # given a copy of a profile.
        data = tempfile.mkdtemp(prefix=DATA_PREFIX)
        kind: tuple[str, ...] = _CLEAN
        if copy_of is not None:
            try:
                _copy(copy_of, Path(data))
            except Exception as e:
                shutil.rmtree(data, ignore_errors=True)
                raise ProfileError(copy_of.name) from e
            kind = (*_COPY, f"--profile-directory={copy_of.folder}")
        process = subprocess.Popen(
            [
                executable,
                f"--user-data-dir={data}",
                # Chrome picks a free port and writes it into its data folder.
                "--remote-debugging-port=0",
                *_SWITCHES,
                *kind,
                *(() if show else _HIDDEN),
                "about:blank",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Orbit's keys are in this process's environment. The browser gets what
            # Playwright's server gets, which leaves them out.
            env=get_default_environment(),
            # A session of its own, so the browser outlives this process.
            start_new_session=True,
        )
        deadline = time.monotonic() + _START_SECONDS
        while True:
            running = _announced(process.pid, data, show, copy_of)
            if running is not None and _answers(running) and _has_a_page(running):
                break
            if process.poll() is not None or time.monotonic() > deadline:
                _kill(process.pid)
                process.wait()
                shutil.rmtree(data, ignore_errors=True)
                raise RuntimeError("Chrome did not start")
            time.sleep(0.05)
        running = replace(running, name=name)
        _children[process.pid] = process
        with _recording:
            _write_all([r for r in _read_all() if r.name != name] + [running])
        logger.info(
            "started a %s browser, %s",
            "shown" if show else "hidden",
            "a copy of a profile" if copy_of else "clean",
        )
        return running


def _close(running: Running) -> None:
    """Closes a browser on record, if it is still there, and forgets it."""
    # Only a browser that still answers as the one on record is signalled: the
    # process id of one that has gone may belong to something else by now.
    if _answers(running):
        with contextlib.suppress(OSError):
            os.kill(running.pid, signal.SIGTERM)
        if not _wait_until_gone(running.pid, _STOP_SECONDS):
            _kill(running.pid)
            _wait_until_gone(running.pid, _STOP_SECONDS)
        logger.info("closed a browser")
    else:
        # Collects one that ended by itself, if this process started it.
        _gone(running.pid)
    _forget(running)


def _executable() -> str | None:
    """Where Google Chrome is installed, in the places Playwright looks for it."""
    chrome = "Google Chrome.app/Contents/MacOS/Google Chrome"
    if sys.platform == "darwin":
        candidates = [
            f"/Applications/{chrome}",
            str(Path.home() / "Applications" / chrome),
        ]
    elif os.name == "nt":
        candidates = [
            str(Path(os.environ[folder]) / "Google/Chrome/Application/chrome.exe")
            for folder in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
            if os.environ.get(folder)
        ]
    else:
        candidates = [
            shutil.which("google-chrome") or "",
            shutil.which("google-chrome-stable") or "",
            "/opt/google/chrome/chrome",
        ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def _announced(
    pid: int, data: str, show: bool, copy_of: Profile | None
) -> Running | None:
    """The browser as Chrome announces it in its data folder, once it is
    listening: the port on one line and its name for this run on the next."""
    try:
        port, browser_id = (Path(data) / "DevToolsActivePort").read_text().split()
        return Running(pid, int(port), browser_id, show, data, copy_of)
    except (OSError, ValueError):
        return None


def _user_data() -> Path:
    """Where the user's own Chrome keeps its profiles."""
    if os.getenv("ORBIT_CHROME_USER_DATA"):
        return Path(os.environ["ORBIT_CHROME_USER_DATA"])
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data"
    return Path.home() / ".config/google-chrome"


def _local_state() -> dict[str, Any]:
    """What Chrome keeps for all profiles together, their names among it."""
    state = json.loads((_user_data() / "Local State").read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("profile"), dict):
        raise ValueError("not Chrome's Local State")
    return state


def _profiles() -> list[Profile]:
    try:
        known = _local_state()["profile"].get("info_cache")
    except PermissionError as e:
        raise ProfilesBlockedError(str(_user_data())) from e
    except (OSError, ValueError):
        return []
    if not isinstance(known, dict):
        return []
    profiles = [
        Profile(folder, " ".join(str(about.get("name") or folder).split()))
        for folder, about in known.items()
        if isinstance(about, dict) and (_user_data() / folder).is_dir()
    ]
    return sorted(profiles, key=lambda profile: profile.name.casefold())


def _copy(profile: Profile, into: Path) -> None:
    """Fills a browser's data folder with a copy of one of the user's profiles."""
    state = _local_state()
    about = state["profile"].get("info_cache", {}).get(profile.folder, {})
    # The copy knows this one profile only, so Chrome opens it without asking which.
    state["profile"] = {
        **state["profile"],
        "info_cache": {profile.folder: about},
        "last_used": profile.folder,
        "last_active_profiles": [profile.folder],
    }
    (into / "Local State").write_text(json.dumps(state), encoding="utf-8")
    _copy_folder(_user_data() / profile.folder, into / profile.folder)


def _copy_folder(
    source: Path, into: Path, clone: bool = sys.platform == "darwin"
) -> None:
    """Copies a profile's folder, without the user's open tabs.

    The user's Chrome is usually running, so a file may change or go while it is
    copied. That is not an error: Chrome repairs a half-written file on opening it.
    """
    if clone:
        # On a Mac the files are cloned: done at once, and using no space until
        # one side changes. So the caches can come along.
        subprocess.run(
            ["cp", "-cR", str(source), str(into)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
    if into.is_dir():
        for name in _TABS:
            shutil.rmtree(into / name, ignore_errors=True)
            (into / name).unlink(missing_ok=True)
        return
    try:
        shutil.copytree(
            source,
            into,
            symlinks=True,
            ignore=shutil.ignore_patterns(*_TABS, *_CACHES),
        )
    except shutil.Error as e:
        if not into.is_dir():
            raise
        logger.warning("%d files of the profile were not copied", len(e.args[0]))


def _ask(running: Running, path: str) -> Any:
    """What the browser's debugging port answers, or None if it does not."""
    try:
        with _LOCAL.open(running.endpoint + path, timeout=_ANSWER_SECONDS) as answer:
            return json.load(answer)
    except (OSError, ValueError):
        return None


def _answers(running: Running) -> bool:
    """True if the browser on record is the one answering on its port."""
    version = _ask(running, "/json/version")
    address = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else ""
    return isinstance(address, str) and address.endswith(running.browser_id)


def _has_a_page(running: Running) -> bool:
    """True while the browser has a tab open."""
    targets = _ask(running, "/json/list")
    return isinstance(targets, list) and any(
        isinstance(target, dict) and target.get("type") == "page" for target in targets
    )


def _gone(pid: int) -> bool:
    """True once the browser's process has ended."""
    child = _children.get(pid)
    if child is not None:
        if child.poll() is None:
            return False
        del _children[pid]
        return True
    if os.name == "nt":
        # There is no harmless signal to ask with on Windows.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        pass
    return False


def _wait_until_gone(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not _gone(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def _kill(pid: int) -> None:
    """Ends the browser and its helper processes at once."""
    with contextlib.suppress(OSError):
        if hasattr(os, "killpg"):
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGTERM)


def _record() -> Path:
    return data_dir() / "browser.json"


def _read_all() -> list[Running]:
    """Every browser on record, the ones that have since gone included."""
    try:
        recorded = json.loads(_record().read_text())
    except (OSError, ValueError):
        return []
    # An Orbit that had one browser at a time wrote that one down on its own, and
    # without a name. It may still be open, so it is read as the first of a list.
    if isinstance(recorded, dict):
        recorded = [recorded]
    found: list[Running] = []
    for entry in recorded if isinstance(recorded, list) else []:
        try:
            entry = dict(entry)
            copy_of = entry.pop("copy_of", None)
            copy_of = Profile(**copy_of) if copy_of else None
            running = Running(**entry, copy_of=copy_of)
        except (TypeError, ValueError):
            continue
        if not running.name:
            name = _free_name(copy_of, {known.name for known in found})
            running = replace(running, name=name)
        found.append(running)
    return found


def _write_all(running: list[Running]) -> None:
    if not running:
        _record().unlink(missing_ok=True)
        return
    data_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    _record().write_text(json.dumps([asdict(browser) for browser in running]))


def _forget(running: Running) -> None:
    """Removes the record of a browser, and its data."""
    with _recording:
        _write_all([r for r in _read_all() if r.browser_id != running.browser_id])
    data = Path(running.profile)
    # Only ever a folder this module made.
    if data.name.startswith(DATA_PREFIX) and data.parent == Path(tempfile.gettempdir()):
        shutil.rmtree(data, ignore_errors=True)
