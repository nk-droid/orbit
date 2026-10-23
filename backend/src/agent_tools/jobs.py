"""The job tools: the user's profile, how searches run, and the jobs found.

The profile is what every application is filled from. It is built from the user's
résumé, their own pages and what Orbit remembers about them (jobs/profile.py), read
here in the browser in use: LinkedIn shows a profile only to someone signed in, so
the user's own Chrome profile is the browser for it.

Jobs are found in the browser, one page at a time, in the background: a search reads
a site's results, then each posting, and the drafting model judges each against the
profile while the browser moves on. What it keeps goes into Orbit jobs, a Google Sheet
(jobs/sheet.py), which is read back each time the list is looked at, so what the user
changed there counts.
"""

import asyncio
import logging
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from livekit.agents import RunContext, function_tool, utils
from livekit.agents.llm import ToolError, Toolset
from mcp.shared.exceptions import McpError

import attachments
from drafting.triage import ModelError
from integrations.google.api import GoogleApiError, NotConnectedError
from jobs import (
    boards,
    google,
    profile,
    relevance,
    resumes,
    scripts,
    search,
    settings,
    sheet,
    store,
    writing,
)

from . import applying, stage
from .actions import draft_jobs_book
from .applications import Applications
from .browser import BrowserToolset
from .heard import given_by_tools, said_by_the_user
from .memory import NO_MEMORY, NOTHING_YET
from .state import OrbitState

PROFILE = (
    "The user's profile for job applications: what every form is filled from. With no "
    "arguments it is put on the user's screen, and you are told what is missing and "
    "where sources disagree. Build it when the user first asks for help with jobs."
)
SETTINGS = (
    "How job searches run: which sites, how many results pages, how recent, and how "
    "many applications at a sitting. With no arguments you are told them; change "
    "them when the user asks."
)
LIST = (
    "The jobs found, in Orbit jobs, the Google Sheet the user reviews them in: put on "
    "their screen, and read back, so what they changed there counts. Change it when "
    "they ask by voice."
)
NOTHING_FOUND = "No jobs have been found yet. Search for some with jobs_search."
NOT_READ = "Orbit jobs could not be read: {why} Tell the user."
SHEET_GONE = (
    "Orbit jobs is no longer in the user's Drive. It is made again with their yes: "
    "tell them, and ask."
)
LISTED = (
    "Orbit jobs is on the user's screen. Say in a sentence or two how many jobs there "
    "are and how many are ticked to apply to."
)
# How many jobs the model is told of by name, the newest first. The Sheet has all.
JOBS_TOLD = 60
SEARCH = (
    "Search for jobs that fit the user, in the browser, as their profile and settings "
    "say: companies' own job boards first, then job sites such as LinkedIn. Only "
    "jobs in their roles and places that fit well are kept. It runs in the "
    "background: you are told when it is done, and what it finds goes into Orbit "
    "jobs. Call it again to hear how it is going."
)
# The parts of a search that are companies' own job boards, and a Google search of
# them.
COMPANIES = "companies"
FOUND_COMPANIES = "found companies"
GOOGLE = "google"
COMPANY_BOARDS = "Company job boards"
COMPANY_SITE = "Company site"
# The two kinds of Google search, as the user is told of them.
_KINDS = {"boards": "company job boards", "careers": "companies' careers pages"}
NO_BOARD = "No job board of their own was found for {names}."
# How many names a company's board is tried under on each system, and how long a
# company with none found is left before it is looked for again.
SLUGS_TRIED = 2
# The most pages of one company's board read, on a system that shows a board a page
# at a time: far more than any company has openings for.
BOARD_PAGES = 40
BOARD_LOOKED_FOR_SECONDS = 30 * 86400
# Why a listing is not read again: a job found before, or one the user applied to.
ALREADY_FOUND = "already found"
APPLIED_TO = "already applied to"
MORE_EXPERIENCE = "asks for more experience"
NOT_READ_POSTING = "could not be read"
TOO_LITTLE = "said too little to judge"
PAST_MOST = "left once {most} jobs were found"
ENOUGH = (
    "It stopped once it had found {most} jobs, the most one search keeps: searching "
    "again goes on past them."
)
NO_ROLES = "The profile names no roles to search for. Ask the user which roles."
NO_PLACES = "The profile names no places to search in. Ask the user where, or remote."
STARTED = (
    "Tell the user in a sentence that the search has started and runs in the "
    "background, reading every posting that may fit until it has found up to {most} "
    "jobs, which can take a while, and that you will tell them when the list is "
    "ready. The browser is busy until then."
)
RUNNING = "A search is running: {doing}. {read} postings read so far, {kept} kept."
CANNOT_MAKE = (
    "Orbit jobs cannot be made yet, so what is found waits on the user's computer "
    "until it can be:"
)
STOPPED = "The search was stopped. What it had found is kept."
FINISHED = (
    "The job search has finished. {read} postings were read and {kept} kept{left}. "
    "{where} Tell the user in a sentence or two, then show the list with jobs_list."
)
IN_THE_SHEET = "They are in Orbit jobs."
NOT_IN_THE_SHEET = "Orbit jobs has not been made yet: they go into it once it is."
SHEET_TROUBLE = "They could not be written into Orbit jobs: {why}"
REFUSED = "{site} refused the browser"
NOT_OPENED = "{site}'s page did not open in time."
# How many pages in a row may not open in time before a search stops: the browser
# has stopped answering.
STUCK_AFTER = 3
STUCK = "The browser stopped answering, so the search stopped there."
NOT_DONE = (
    "{site} asked the user for {what}, which was not done within ten minutes, so "
    "Orbit stopped searching it."
)
# How many times a page is handed to the user before Orbit gives up on it.
HANDED_OVER = 2
BUSY = "The browser is busy {what}. Wait until that is done."
DRAFT = (
    "Apply for a job the user chose: opens its application in the browser, reads the "
    "form and drafts every answer, in the background. You are told when the draft is "
    "on the user's screen. Without a job, the next one ticked in Orbit jobs."
)
CHANGE = (
    "Change the job application being drafted as the user says: answers they give, "
    "the résumé it sends, a change to the writing or the cover letter, and answers "
    "to keep for next time."
)
CHECK = (
    "Read the job application in the browser again after the user did something "
    "there themselves: changed it by hand, signed in, or did a check that they are a "
    "person. Drafts submitting it, or records it as sent."
)
SKIP = "Skip a job the user does not want to apply to."
# How many postings the drafting model judges at once, while the browser moves on.
JUDGED_AT_ONCE = 3
# How many listings are weighed before the search lets the agent's other work run.
LISTINGS_AT_ONCE = 100

logger = logging.getLogger("orbit.jobs")


@dataclass
class _Tally:
    """What one search has read and kept so far."""

    read: int = 0
    kept: int = 0


@dataclass
class _Progress:
    """How a search running in the background is going."""

    doing: str = ""
    read: int = 0
    kept: int = 0
    notes: list[str] = field(default_factory=list)
    # The companies of the jobs kept from job sites, whose own boards are searched
    # after them.
    companies: list[str] = field(default_factory=list)
    # The listings already weighed in this search: another part of it may list
    # them too.
    seen: set[str] = field(default_factory=set)
    # The companies whose boards this search has read, in lower case.
    boards_read: set[str] = field(default_factory=set)
    # How many pages in a row did not open in time.
    timed_out: int = 0


class _StuckError(Exception):
    """The browser has stopped answering: page after page did not open in time."""


@dataclass(frozen=True)
class _Run:
    """One part of a search: a job site for one role in one place, a Google search
    of one kind for one, or companies' own boards."""

    site: str
    role: str = ""
    place: str = ""
    kind: str = ""


@dataclass(frozen=True)
class _Candidate:
    """A listing that may fit, to be read: its key, the site or company board it is
    on, and the job site's own rules when it is on one."""

    listing: search.Listing
    key: str
    site: str
    board: search.Site | None
    # The hiring system a company's own posting is on.
    system: boards.System | None = None
    # Whether its company's whole board is searched after, once the job is kept.
    remember: bool = False
    # A careers page of a company's own, on no system Orbit knows: what it is, its
    # title and its company are as the drafting model reads them from the page.
    careers: bool = False
    # What its posting's page says of the job in the shape search engines read.
    facts: search.Facts = field(default_factory=search.Facts)


NO_BROWSER = (
    "The browser has not been started. Ask the user which of their Chrome profiles "
    "to use for jobs, if they have not said, and call browser_start with it. Then "
    "build the profile again."
)
NOT_GIVEN = (
    "{page} was not said by the user or found on a page: ask them for the address of "
    "their {what}."
)
NOTHING_TO_BUILD = (
    "There is nothing to build the profile from yet. Ask the user for their résumé, "
    "with ask_for_file, or for the address of their portfolio."
)
NO_PORTFOLIO = (
    "There is no portfolio to take the résumé from. Ask the user for its address, or "
    "for their résumé with ask_for_file."
)
NO_RESUME_LINK = (
    "{page} has no link to a résumé. Ask the user for it with ask_for_file."
)
NO_SUCH_FILE = (
    "The user has handed over no file called {asked!r}. Their files are: {names}. "
    "Ask for it with ask_for_file."
)
NO_FILES = "The user has handed over no files. Ask for the résumé with ask_for_file."
RESUME_ROLE = (
    'Write what a résumé is for as "résumé: role", such as "resume-backend.pdf: '
    'Backend Developer".'
)
NOT_DOWNLOADED = "The résumé at {url} could not be fetched: {why}."
SIGN_IN = (
    "LinkedIn asked for a sign-in, so it was left out. It can be read in a copy of a "
    "Chrome profile of the user's that is signed in to LinkedIn."
)
BUILT = (
    "The profile is on the user's screen. Say in a sentence where it came from. Then "
    "ask about what is missing and what sources disagree on, one or two at a time, "
    "and pass their answers as change."
)
SHOWN = (
    "The profile is on the user's screen. Say in a sentence or two what is missing, "
    "if anything."
)
# A résumé bigger than this is not fetched.
MAX_RESUME_BYTES = 10_000_000
_DOWNLOAD = aiohttp.ClientTimeout(total=30)
# The links on a page, as text and address, so a profile's links can be checked
# against the page they came from.
_LINKS = """() => {
  const seen = new Set();
  const links = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href;
    if (!/^https?:/.test(href) || seen.has(href)) continue;
    seen.add(href);
    const text = (a.innerText || a.getAttribute('aria-label') || a.title || '').trim();
    links.push({text: text.replace(/\\s+/g, ' ').slice(0, 80), href});
    if (links.length >= 80) break;
  }
  return links;
}"""
# Where a page is, as the profile names its source.
_SITES = {"linkedin.com": "LinkedIn", "github.com": "GitHub"}
# A person's own profile on those sites, as their other pages link to it.
_OWN = re.compile(
    r"^https?://(?:www\.)?(?:linkedin\.com/in/[\w%-]+|github\.com/[\w-]+)/?$",
    re.IGNORECASE,
)
_RESUME = re.compile(r"\b(?:r[ée]sum[ée]|cv)\b|\.pdf(?:$|[?#])", re.IGNORECASE)


def _source(url: str) -> str:
    host = urlsplit(url).netloc.casefold().removeprefix("www.")
    return next(
        (name for site, name in _SITES.items() if host.endswith(site)), "portfolio"
    )


def _bare(url: str) -> str:
    return re.sub(r"^https?://(?:www\.)?", "", url.strip().casefold()).rstrip("/")


def _remembered(note: str) -> str:
    """What Orbit remembers about the user, from the note the model is given."""
    return "" if note.strip() in ("", NO_MEMORY, NOTHING_YET) else note


class JobsToolset(Toolset):
    """The job tools, in the browser in use. `remembered` is the note on what Orbit
    remembers about the user, as the conversation began."""

    def __init__(
        self, browser: BrowserToolset | None = None, remembered: str = ""
    ) -> None:
        super().__init__(id="jobs")
        self._browser = browser
        self._remembered = _remembered(remembered)
        self._searching: asyncio.Task | None = None
        self._progress = _Progress()
        self._applications = Applications(
            browser, busy=self._searching_now, sync=self._sync_quietly
        )
        self._tools = [
            function_tool(self._job_profile, name="job_profile", description=PROFILE),
            function_tool(
                self._job_settings, name="job_settings", description=SETTINGS
            ),
            function_tool(self._jobs_search, name="jobs_search", description=SEARCH),
            function_tool(self._jobs_list, name="jobs_list", description=LIST),
            function_tool(
                self._application_draft, name="application_draft", description=DRAFT
            ),
            function_tool(
                self._application_change,
                name="application_change",
                description=CHANGE,
            ),
            function_tool(
                self._application_check, name="application_check", description=CHECK
            ),
            function_tool(
                self._application_skip, name="application_skip", description=SKIP
            ),
        ]

    def _searching_now(self) -> str | None:
        if self._searching is not None and not self._searching.done():
            return "searching for jobs"
        return None

    async def _application_draft(
        self,
        context: RunContext[OrbitState],
        job: int = 0,
        on_the_board: bool = False,
        resume: str = "",
        already_open: bool = False,
    ) -> dict[str, Any]:
        """
        Args:
            job: The job's number, when the user named one. Left out, the next job
                ticked in Orbit jobs that has not been applied to.
            on_the_board: True only when the user said to apply on the job board
                itself, such as with LinkedIn's Easy Apply, for a job that can be
                applied to only there. Jobs that apply only on LinkedIn or Naukri
                are applied to there anyway, unless the user said to leave them out.
            resume: The résumé the user said to send, by its name or the work it is
                for. Left out, the one of theirs that suits the job.
            already_open: True when the user says the application is already open
                in the browser, or that they did what it needed there: Orbit reads
                the page as it is, in whichever tab holds the form, instead of
                opening the job again.
        """
        return await self._applications.draft(
            context, job, on_the_board, resume, already_open
        )

    async def _application_change(
        self,
        context: RunContext[OrbitState],
        answers: list[str] | None = None,
        rewrite: str = "",
        keep: list[str] | None = None,
        resume: str = "",
    ) -> dict[str, Any]:
        """
        Args:
            answers: Answers the user gave, each as "question: answer", the question
                by its number on the draft or its words, such as "3: 30 days" or
                "Gender: Decline to self-identify". For a tick box, "yes" or "no".
            rewrite: What the user asked to change in the written answers or the
                cover letter, in their words, such as "make the cover letter shorter".
            keep: The questions whose answers the user said to keep for next time,
                by number or words.
            resume: Another of the user's résumés to send, when they ask, by its
                name or the work it is for: what is written is drafted again from it.
        """
        return await self._applications.change(
            context, answers or [], rewrite, keep or [], resume
        )

    async def _application_check(
        self, context: RunContext[OrbitState]
    ) -> dict[str, Any]:
        return await self._applications.check(context)

    async def _application_skip(
        self, context: RunContext[OrbitState], job: int, why: str = ""
    ) -> dict[str, Any]:
        """
        Args:
            job: The job's number.
            why: Why, in the user's words, if they said.
        """
        return await self._applications.skip(job, why)

    async def _job_profile(
        self,
        context: RunContext[OrbitState],
        build: bool = False,
        pages: list[str] | None = None,
        resumes: list[str] | None = None,
        resume_roles: list[str] | None = None,
        forget_resumes: list[str] | None = None,
        change: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            build: True to build the profile, or build it again, from the résumés,
                the user's own pages and what you remember about them.
            pages: For build: the addresses of the user's own pages, such as their
                portfolio, GitHub and LinkedIn, as they gave them. Their GitHub and
                LinkedIn are also found from their portfolio.
            resumes: Résumés to keep, each the name of a file the user handed over,
                or "portfolio" for the one their portfolio links to. Each joins
                those kept before: the user may have one for each kind of role, and
                each application sends the one that suits its job.
            resume_roles: What a résumé is for, when the user says, each as
                "résumé: role", such as "resume-backend.pdf: Backend Developer".
            forget_resumes: Résumés the user no longer wants sent, by name.
            change: Changes the user asked for, each as "field: value", such as
                "notice_period: 30 days", or for a list "skills: add Kubernetes".
        """
        told: dict[str, Any] = {}
        notes: list[str] = []
        for asked in resumes or []:
            if asked.strip():
                kept = await self._keep_resume(context, asked.strip(), pages or [])
                told.setdefault("resumes_kept", []).append(
                    f"{kept.name}, {kept.source}"
                )
        for item in resume_roles or []:
            named, colon, role = item.partition(":")
            if not colon or not role.strip():
                raise ToolError(RESUME_ROLE)
            try:
                marked = profile.resume_for(named.strip(), role)
            except ValueError as e:
                raise ToolError(str(e)) from e
            told.setdefault("resumes_for", []).append(f"{marked.name}: {marked.role}")
        for asked in forget_resumes or []:
            try:
                gone = profile.forget_resume(asked)
            except ValueError as e:
                raise ToolError(str(e)) from e
            told.setdefault("resumes_forgotten", []).append(gone.name)
        if change:
            try:
                changed = [profile.change(item) for item in change]
            except ValueError as e:
                raise ToolError(str(e)) from e
            told["changed"] = [f"{known.key}: {known.value}" for known in changed]
        if build:
            built, notes = await self._build(context, pages or [])
            told["built_from"] = built.sources
        await stage.show_profile()
        told.update(_summary())
        if notes:
            told["notes"] = notes
        told["say"] = BUILT if build else SHOWN
        return told

    async def _job_settings(
        self, context: RunContext[OrbitState], change: list[str] | None = None
    ) -> dict[str, Any]:
        """
        Args:
            change: Changes the user asked for, each as "setting: value", such as
                "pages_per_search: 5", "posted_within_days: 7" or "sites: add naukri".
        """
        changed = {}
        for item in change or []:
            try:
                key, value = settings.change(item)
            except ValueError as e:
                raise ToolError(str(e)) from e
            changed[key] = value
        told: dict[str, Any] = {"settings": settings.current()}
        if changed:
            told["changed"] = changed
            told["applies"] = "from the next search"
            await stage.show_profile()
        return told

    async def _jobs_search(
        self,
        context: RunContext[OrbitState],
        role: str = "",
        place: str = "",
        stop: bool = False,
    ) -> dict[str, Any]:
        """
        Args:
            role: A role the user named for this search. Left empty, the roles in
                their profile.
            place: A place the user named, or "remote". Left empty, the places in
                their profile.
            stop: True to stop the search that is running.
        """
        running = self._searching is not None and not self._searching.done()
        if stop:
            if running and self._searching is not None:
                self._searching.cancel()
                return {"stopped": STOPPED}
            return {"stopped": "No search was running."}
        if running:
            progress = self._progress
            return {
                "running": RUNNING.format(
                    doing=progress.doing, read=progress.read, kept=progress.kept
                )
            }
        if what := self._applications.working:
            raise ToolError(BUSY.format(what=what))
        roles = [role.strip()] if role.strip() else _listed(profile.value("roles"))
        places = (
            [place.strip()] if place.strip() else _listed(profile.value("locations"))
        )
        if not roles:
            raise ToolError(NO_ROLES)
        if not places:
            raise ToolError(NO_PLACES)
        if self._browser is None or not self._browser.in_use:
            raise ToolError(NO_BROWSER)
        now = settings.current()
        runs = _runs(now["sites"], roles, places)
        watched = self._watched(now)
        told: dict[str, Any] = {
            "started": [_described(run, watched, now["sites"]) for run in runs],
            "limits": now,
            "say": STARTED.format(most=settings.MOST_KEPT),
        }
        if store.book() is None:
            # Asked for now, so the list is there to review when the search is done.
            # Until it is made, what is found waits on the user's computer.
            try:
                told["saving"] = draft_jobs_book(context)
            except ToolError as e:
                told["orbit_jobs"] = f"{CANNOT_MAKE} {e}"
        self._progress = _Progress()
        wanted = {**_known(), "roles": roles, "locations": places}
        self._searching = asyncio.create_task(self._search(context, runs, now, wanted))
        return told

    def _watched(self, now: dict[str, Any]) -> list[str]:
        """The companies whose own job boards are searched: those the user named,
        those in their profile to seek, then those Orbit found a board for before."""
        named = [*now["companies"], *_listed(profile.value("seek"))]
        found = [board.name for board in store.boards() if board.system]
        return list(
            {name.casefold(): name for name in reversed([*named, *found])}.values()
        )[::-1]

    async def _search(
        self,
        context: RunContext[OrbitState],
        runs: list[_Run],
        now: dict[str, Any],
        wanted: dict[str, Any],
    ) -> None:
        """Runs a search in the background, then tells the model, so it tells the
        user. The browser is Orbit's own until then. Every part of it reads each
        posting that may fit, however many there are, and every company a job was
        kept at has its own board searched: the more good jobs found, the more the
        user can apply to."""
        browser = self._browser
        assert browser is not None
        browser.hold("searching for jobs")
        progress = self._progress
        left: Counter[str] = Counter()
        try:
            for run in runs:
                if progress.kept >= settings.MOST_KEPT:
                    progress.notes.append(ENOUGH.format(most=settings.MOST_KEPT))
                    break
                if run.site == COMPANIES:
                    names = self._watched(now)
                elif run.site == FOUND_COMPANIES:
                    names = [
                        name
                        for name in progress.companies
                        if name.casefold() not in progress.boards_read
                    ]
                elif run.site == GOOGLE:
                    await self._search_google(context, run, now, left, wanted)
                    continue
                else:
                    await self._search_one(
                        context, run.site, run.role, run.place, now, left, wanted
                    )
                    continue
                await self._search_boards(context, names, now, left, wanted)
        except _StuckError:
            progress.notes.append(STUCK)
        except Exception:
            logger.exception("the job search failed")
            progress.notes.append("The search stopped early on an error.")
        finally:
            browser.release()
        trouble = await self._sync_quietly()
        where = (
            SHEET_TROUBLE.format(why=trouble)
            if trouble
            else IN_THE_SHEET
            if store.book() is not None
            else NOT_IN_THE_SHEET
        )
        named = ", ".join(f"{count} {why}" for why, count in left.items() if count)
        told = FINISHED.format(
            read=progress.read,
            kept=progress.kept,
            left=f", and these left out: {named}" if named else "",
            where=" ".join([where, *progress.notes]),
        )
        try:
            await context.session.generate_reply(instructions=told)
        except RuntimeError:
            # Its conversation ended first: what it found is in Orbit jobs.
            logger.info("the job search ended after its conversation")
        except Exception:
            logger.warning("the end of the job search could not be told", exc_info=True)

    async def _search_one(
        self,
        context: RunContext[OrbitState],
        site_key: str,
        role: str,
        place: str,
        now: dict[str, Any],
        left: Counter[str],
        wanted: dict[str, Any],
    ) -> None:
        """Searches one job site for one role in one place: its results pages, then
        each posting that may fit."""
        assert self._browser is not None
        browser, site, progress = self._browser, search.SITES[site_key], self._progress
        days = now["posted_within_days"]
        progress.doing = f"{site.name}: {role} in {place}"
        number = store.start_search(site.name, f"{role} in {place}")
        here: Counter[str] = Counter()
        tally = _Tally()
        years = _years()
        try:
            found: list[search.Listing] = []
            seen: set[str] = set()
            pages = min(now["pages_per_search"], site.pages or now["pages_per_search"])
            for page in range(1, pages + 1):
                url = site.search(role, place, days, page)
                text, why = await self._past_checks(
                    context, site.name, url, await self._open(context, url)
                )
                if text is None:
                    progress.notes.append(why)
                    break
                items = await browser.run_script(
                    context, scripts.listings(site.posting)
                )
                new = [
                    site.listing(item) for item in items if str(item["id"]) not in seen
                ]
                seen |= {item.id for item in new}
                found += new
                if not new:
                    break
            candidates = []
            before = _Before()
            for listing in found:
                key = f"{site_key}:{listing.id}"
                if key in progress.seen:
                    continue
                progress.seen.add(key)
                age = search.days_old(listing.posted)
                if listing.applied:
                    here[APPLIED_TO] += 1
                elif why := before.why(key, listing):
                    here[why] += 1
                elif age is not None and age > days:
                    here["too old"] += 1
                elif listing.easy_apply and not now["easy_apply"]:
                    here[f"applied to only on {site.name}"] += 1
                elif _more_experience(listing.experience, years):
                    here[MORE_EXPERIENCE] += 1
                elif why := relevance.left_out(
                    listing.title, listing.company, listing.location, wanted
                ):
                    here[why] += 1
                else:
                    candidates.append(
                        _Candidate(listing, key, site.name, site, remember=True)
                    )
            await self._read_postings(
                context, number, candidates, here, tally, now, wanted
            )
        finally:
            store.finish_search(number, tally.read, tally.kept, dict(here))
            left.update(here)
        await self._sync_quietly()

    async def _search_boards(
        self,
        context: RunContext[OrbitState],
        names: list[str],
        now: dict[str, Any],
        left: Counter[str],
        wanted: dict[str, Any],
    ) -> None:
        """Searches companies' own job boards: each company's board, found the first
        time, then each of its openings that may fit."""
        assert self._browser is not None
        progress = self._progress
        if not names:
            return
        asked = f"{', '.join(wanted['roles'])} in {', '.join(wanted['locations'])}"
        number = store.start_search(COMPANY_BOARDS, asked)
        here: Counter[str] = Counter()
        tally = _Tally()
        yours = {name.casefold() for name in now["companies"]}
        try:
            candidates: list[_Candidate] = []
            before = _Before()
            missing = []
            for name in names:
                progress.doing = f"{name}'s job board"
                source = "you" if name.casefold() in yours else "found"
                board = await self._board_of(context, name, source)
                if board is None:
                    missing.append(name)
                    continue
                system = boards.BY_KEY[board.system]
                progress.boards_read.add(name.casefold())
                for at, item in enumerate(
                    await self._board_postings(context, board, system)
                ):
                    if at % LISTINGS_AT_ONCE == LISTINGS_AT_ONCE - 1:
                        # A board may list thousands: the agent is not kept from
                        # its conversation while they are weighed.
                        await asyncio.sleep(0)
                    listing = boards.listing(item, board.name)
                    key = f"{system.key}:{listing.id}"
                    if key in progress.seen:
                        continue
                    progress.seen.add(key)
                    why = before.why(key, listing) or relevance.left_out(
                        listing.title, listing.company, listing.location, wanted
                    )
                    if why:
                        here[why] += 1
                    else:
                        candidates.append(
                            _Candidate(listing, key, system.name, None, system)
                        )
            if missing:
                progress.notes.append(NO_BOARD.format(names=", ".join(missing)))
            await self._read_postings(
                context, number, candidates, here, tally, now, wanted
            )
        finally:
            store.finish_search(number, tally.read, tally.kept, dict(here))
            left.update(here)
        await self._sync_quietly()

    async def _board_postings(
        self, context: RunContext[OrbitState], board: store.Board, system: boards.System
    ) -> list[dict[str, Any]]:
        """Every posting a company's board lists, as the listings script finds them:
        page by page, on a system that shows a board's openings a page at a time,
        until a page lists none it has not."""
        assert self._browser is not None
        browser, found, ids = self._browser, [], set()
        for page in range(1, BOARD_PAGES + 1):
            url = (
                system.board.format(slug=board.slug)
                if page == 1
                else system.paged.format(slug=board.slug, page=page)
            )
            text, why = await self._past_checks(
                context, board.name, url, await self._open(context, url)
            )
            if text is None:
                self._progress.notes.append(why)
                break
            items = await browser.run_script(context, scripts.listings(system.posting))
            new = [item for item in items if str(item["id"]) not in ids]
            ids |= {str(item["id"]) for item in new}
            found += new
            if not new or not system.paged:
                break
        return found

    async def _search_google(
        self,
        context: RunContext[OrbitState],
        run: _Run,
        now: dict[str, Any],
        left: Counter[str],
        wanted: dict[str, Any],
    ) -> None:
        """Searches Google for companies' own postings of one role in one place, on
        their job boards or their careers pages as `run.kind` says: its results
        pages, then each posting that may fit. The board of each company whose
        posting may fit is remembered."""
        assert self._browser is not None
        browser, progress = self._browser, self._progress
        role, place, kind = run.role, run.place, run.kind
        days = now["posted_within_days"]
        progress.doing = f"Google: {role} in {place}, {_KINDS[kind]}"
        number = store.start_search("Google", f"{role} in {place}, {_KINDS[kind]}")
        pattern = boards.ANY_POSTING if kind == google.BOARDS else google.CAREERS_RESULT
        read = google.found if kind == google.BOARDS else google.page
        here: Counter[str] = Counter()
        tally = _Tally()
        names = {(b.system, b.slug.casefold()): b.name for b in store.boards()}
        try:
            results: list[google.Found] = []
            seen: set[str] = set()
            for page in range(1, now["pages_per_search"] + 1):
                url = google.address(
                    kind, role, place, wanted, days, page, date.today()
                )
                text, why = await self._past_checks(
                    context, "Google", url, await self._open(context, url)
                )
                if text is None:
                    progress.notes.append(why)
                    break
                items = await browser.run_script(context, scripts.listings(pattern))
                new = [
                    result
                    for item in items
                    if (result := read(item, names)) is not None
                    and result.listing.url not in seen
                ]
                seen |= {result.listing.url for result in new}
                results += new
                # A page short of results is Google's last.
                if len(new) < google.PER_PAGE // 2:
                    break
            candidates = []
            before = _Before()
            for result in results:
                listing = result.listing
                system = result.system
                key = f"{system.key if system else 'web'}:{listing.id}"
                if key in progress.seen:
                    continue
                progress.seen.add(key)
                age = search.days_old(listing.posted)
                if why := before.why(key, listing):
                    here[why] += 1
                elif age is not None and age > days:
                    here["too old"] += 1
                elif why := relevance.left_out(
                    listing.title, listing.company, listing.location, wanted
                ):
                    here[why] += 1
                elif system is None:
                    candidates.append(
                        _Candidate(listing, key, COMPANY_SITE, None, careers=True)
                    )
                else:
                    known = store.board(listing.company)
                    if known is None or not known.system:
                        store.keep_board(
                            listing.company, system.key, result.slug, "found"
                        )
                    candidates.append(
                        _Candidate(
                            listing, key, system.name, None, system, remember=True
                        )
                    )
            await self._read_postings(
                context, number, candidates, here, tally, now, wanted
            )
        finally:
            store.finish_search(number, tally.read, tally.kept, dict(here))
            left.update(here)
        await self._sync_quietly()

    async def _board_of(
        self, context: RunContext[OrbitState], name: str, source: str
    ) -> store.Board | None:
        """A company's own job board: as Orbit knows it, or found now by trying the
        addresses its name gives on each hiring system. That none was found is kept
        too, and looked for again after a while."""
        assert self._browser is not None
        board = store.board(name)
        if board is not None and (board.system or not _unknown_board(name)):
            return board if board.system else None
        found = None
        for system in boards.SYSTEMS:
            for slug in boards.slugs(name)[:SLUGS_TRIED]:
                text = await self._open(context, system.board.format(slug=slug))
                if text is None or search.blocked(text):
                    continue
                count = await self._browser.run_script(
                    context, scripts.board_postings(system.posting)
                )
                if count and _names(text, name):
                    found = (system.key, slug)
                    break
            if found:
                break
        store.keep_board(name, *(found or ("", "")), source)
        return store.board(name) if found else None

    async def _read_postings(
        self,
        context: RunContext[OrbitState],
        number: int,
        candidates: list[_Candidate],
        here: Counter[str],
        tally: _Tally,
        now: dict[str, Any],
        wanted: dict[str, Any],
    ) -> None:
        """Reads each posting that may fit and keeps those that do, judged while the
        browser moves on, counting in `tally` as it goes. Once the search has found
        as many jobs as one search keeps, the rest are left."""
        assert self._browser is not None
        browser, progress = self._browser, self._progress
        judging = asyncio.Semaphore(JUDGED_AT_ONCE)
        about = _about_the_user()
        before = _Before()
        waiting: deque[asyncio.Task] = deque()
        years = _years()
        try:
            for at, candidate in enumerate(candidates):
                # What has been judged is kept as it comes, in order, so the search
                # knows when it has found as many jobs as it may.
                while waiting and waiting[0].done():
                    self._keep(
                        waiting.popleft().result(), number, here, tally, wanted, years
                    )
                if progress.kept >= settings.MOST_KEPT:
                    here[PAST_MOST.format(most=settings.MOST_KEPT)] += (
                        len(candidates) - at
                    )
                    break
                listing = candidate.listing
                text, _ = await self._past_checks(
                    context,
                    candidate.site,
                    listing.url,
                    await self._open(context, listing.url, scripts.SETTLED),
                    scripts.SETTLED,
                )
                tally.read += 1
                progress.read += 1
                if text is None or search.unread(text):
                    here[NOT_READ_POSTING] += 1
                    continue
                candidate = replace(candidate, facts=await self._facts(context))
                if search.applied(text):
                    here[APPLIED_TO] += 1
                    continue
                # A posting's own page names its role and company best, whole: a list
                # or a search may cut them short, and a card may be misread.
                if candidate.system is not None:
                    named = boards.titled(_page_title(text), candidate.system)
                elif candidate.board is not None:
                    named = search.titled(_page_title(text), candidate.board)
                else:
                    named = None
                if named is not None and named != (listing.title, listing.company):
                    listing = replace(listing, title=named[0], company=named[1])
                    candidate = replace(candidate, listing=listing)
                    # What the card's words passed is asked again of the page's.
                    why = before.why(candidate.key, listing) or relevance.left_out(
                        listing.title, listing.company, listing.location, wanted
                    )
                    if why:
                        here[why] += 1
                        continue
                applies_on = "company"
                board = candidate.board
                if board is not None and board.applied_on_site:
                    applies_on = board.name
                elif board is not None:
                    buttons = await browser.run_script(context, scripts.APPLYING)
                    if any(
                        re.search(board.on_site_only, b["said"], re.I) for b in buttons
                    ):
                        applies_on = board.name
                    if applies_on != "company" and not now["easy_apply"]:
                        here[f"applied to only on {applies_on}"] += 1
                        continue
                waiting.append(
                    asyncio.create_task(
                        _judged(judging, about, candidate, text, applies_on)
                    )
                )
            while waiting:
                self._keep(await waiting.popleft(), number, here, tally, wanted, years)
        finally:
            for task in waiting:
                task.cancel()

    def _keep(
        self,
        judged: tuple[_Candidate, str, str, dict[str, Any] | None],
        number: int,
        here: Counter[str],
        tally: _Tally,
        wanted: dict[str, Any],
        years: float | None,
    ) -> None:
        """Keeps a posting that was read and judged, if it fits and the search may
        keep more, counting in `here` why it was not when it was not."""
        progress = self._progress
        candidate, text, applies_on, fit = judged
        if candidate.careers and fit is not None:
            # Only the page itself says what a careers page holds.
            if not fit["posting"]:
                here["not one job's posting"] += 1
                return
            candidate = replace(
                candidate,
                listing=replace(
                    candidate.listing,
                    title=fit["title"] or candidate.listing.title,
                    company=fit["company"] or candidate.listing.company,
                ),
            )
        listing = candidate.listing
        why = _left_out(candidate, text, fit, wanted, years)
        if why:
            here[why] += 1
            return
        if progress.kept >= settings.MOST_KEPT:
            here[PAST_MOST.format(most=settings.MOST_KEPT)] += 1
            return
        assert fit is not None
        facts = candidate.facts
        job = store.add_job(
            key=candidate.key,
            site=candidate.site,
            url=listing.url,
            # A company's own posting is where its application is.
            apply_url=listing.url if candidate.board is None else "",
            applies_on=applies_on,
            company=listing.company,
            title=listing.title,
            location=_where(candidate, fit, kept=True),
            remote=fit["work_mode"]
            or ("remote" if facts.location.startswith("Remote") else ""),
            posted=listing.posted or facts.posted,
            experience=_experience(candidate, fit),
            salary=fit["salary"] or facts.salary or listing.salary,
            fit=fit["fit"],
            why="; ".join(fit["why"]),
            posting=text,
            search=number,
            # Ticked to apply to, as the user allows a job that applies only on the
            # board it was found on, but on Cutshort, where they choose its jobs.
            apply=applies_on == "company"
            or (candidate.board is not None and not candidate.board.applied_on_site),
        )
        if job is None:
            here[ALREADY_FOUND] += 1
            return
        tally.kept += 1
        progress.kept += 1
        if candidate.remember and job.company not in progress.companies:
            progress.companies.append(job.company)

    async def _facts(self, context: RunContext[OrbitState]) -> search.Facts:
        """What the posting open in the browser says of the job in the shape search
        engines read, or nothing, when the page cannot be looked through."""
        assert self._browser is not None
        try:
            return search.facts(
                await self._browser.run_script(context, scripts.JOB_POSTING)
            )
        except (ToolError, McpError, TimeoutError):
            return search.Facts()

    async def _open(
        self, context: RunContext[OrbitState], url: str, settle: str = ""
    ) -> str | None:
        """A page, opened and read as the browser does, settled by `settle`; None when
        the browser did not answer in time, as a page that never finishes loading
        makes it. Once too many in a row have not, the search stops: the browser has
        stopped answering."""
        assert self._browser is not None
        try:
            text = await self._browser.open_page(context, url, settle)
        except (McpError, TimeoutError):
            logger.warning("a page did not open in time: %s", url)
            self._progress.timed_out += 1
            if self._progress.timed_out >= STUCK_AFTER:
                raise _StuckError from None
            return None
        self._progress.timed_out = 0
        return text

    async def _past_checks(
        self,
        context: RunContext[OrbitState],
        site: str,
        url: str,
        text: str | None,
        settle: str = "",
    ) -> tuple[str | None, str]:
        """A page once nothing stops it being read. A check that the user is a person,
        or a sign-in, is handed to the user, and the page is opened again once they
        have done it, settled by `settle` as it was first. Its text is None, with
        why, when it did not open in time, when the site refuses the browser, or when
        the user did not do the check in time."""
        assert self._browser is not None
        progress = self._progress
        if text is None:
            return None, NOT_OPENED.format(site=site)
        for _ in range(HANDED_OVER):
            kind = search.block(text)
            if not kind:
                return text, ""
            if kind == search.REFUSED:
                return None, REFUSED.format(site=site) + "."
            where = f"{site}, during the job search,"
            await _tell(context, applying.hand_over(where, kind, self._browser.shown))
            doing = progress.doing
            progress.doing = f"waiting for the user to do {applying.WHAT[kind]}"
            try:
                waited = await applying.wait_for_the_user(self._browser, context, kind)
            finally:
                progress.doing = doing
            if waited is None:
                return None, NOT_DONE.format(site=site, what=applying.WHAT[kind])
            text = await self._open(context, url, settle)
            if text is None:
                return None, NOT_OPENED.format(site=site)
        kind = search.block(text)
        if not kind:
            return text, ""
        if kind == search.REFUSED:
            return None, REFUSED.format(site=site) + "."
        return None, NOT_DONE.format(site=site, what=applying.WHAT[kind])

    async def _sync_quietly(self, said: list[str] | None = None) -> str | None:
        """Writes what was found into Orbit jobs, if it is made, with changes to the
        user's own columns written as if they had said them (`said`). Returns why it
        could not be, if it could not."""
        if store.book() is None:
            return None
        try:
            await sheet.sync(_http(), said or [])
        except sheet.GoneError:
            store.lose_book()
            return "Orbit jobs is no longer in the user's Drive."
        except (
            GoogleApiError,
            NotConnectedError,
            aiohttp.ClientError,
            TimeoutError,
        ) as e:
            return _why(e)
        return None

    async def _jobs_list(
        self, context: RunContext[OrbitState], change: list[str] | None = None
    ) -> dict[str, Any]:
        """
        Args:
            change: Changes the user asked for, by the job's number: "untick 3",
                "tick 5", "skip 7", or "note 4: ask about the visa".
        """
        if not store.jobs():
            raise ToolError(NOTHING_FOUND)
        book = store.book()
        if book is None:
            return {"saving": draft_jobs_book(context)}
        try:
            synced = await sheet.sync(_http(), change or [])
        except ValueError as e:
            raise ToolError(str(e)) from e
        except sheet.GoneError:
            store.lose_book()
            return {"gone": SHEET_GONE, "saving": draft_jobs_book(context)}
        except (
            GoogleApiError,
            NotConnectedError,
            aiohttp.ClientError,
            TimeoutError,
        ) as e:
            raise ToolError(NOT_READ.format(why=_why(e))) from e
        if synced is not None:
            self._applications.remember_order(synced.order)
        await stage.show_sheet(book.spreadsheet_id, book.title, sheet.JOBS)
        told = {**_jobs_summary(), "say": LISTED}
        if synced and synced.deleted:
            told["deleted_by_the_user"] = synced.deleted
        if change:
            told["changed"] = change
        return told

    def _check_given(self, context: RunContext[OrbitState], url: str) -> None:
        """An address is opened for the profile only if the user said it, Orbit
        remembers it, or a page gave it: never one the model made up."""
        bare = _bare(url)
        heard = [*said_by_the_user(context), self._remembered]
        heard += [gave for _, _, gave in given_by_tools(context)]
        if not bare or not any(bare in _bare_all(text) for text in heard):
            what = {"LinkedIn": "LinkedIn", "GitHub": "GitHub"}.get(
                _source(url), "page"
            )
            raise ToolError(NOT_GIVEN.format(page=url, what=what))

    async def _read(
        self, context: RunContext[OrbitState], url: str
    ) -> tuple[str, list]:
        """A page, opened and read whole, with its links written out after it."""
        assert self._browser is not None
        text = await self._browser.open_page(context, url)
        links = await self._browser.run_script(context, _LINKS)
        listed = [f"- {link['text'] or link['href']}: {link['href']}" for link in links]
        return text + ("\nLinks:\n" + "\n".join(listed) if listed else ""), links

    async def _build(
        self, context: RunContext[OrbitState], pages: list[str]
    ) -> tuple[profile.Built, list[str]]:
        """Builds the profile from the résumé, the pages and what Orbit remembers.
        The user's GitHub and LinkedIn are followed from their other pages."""
        sources: dict[str, str] = {}
        notes: list[str] = []
        if kept := store.resumes():
            sources["résumé"] = "\n\n".join(resume.text for resume in kept)
        if pages and (self._browser is None or not self._browser.in_use):
            raise ToolError(NO_BROWSER)
        queue = [page.strip() for page in pages if page.strip()]
        for page in queue:
            self._check_given(context, page)
        read: set[str] = set()
        while queue:
            page = queue.pop(0)
            name = _source(page)
            if _bare(page) in read or name in sources:
                continue
            read.add(_bare(page))
            text, links = await self._read(context, page)
            if name == "LinkedIn" and re.search(
                r"authwall|/login|sign ?in", text[:600], re.I
            ):
                notes.append(SIGN_IN)
                continue
            sources[name] = text
            for link in links:
                own = _OWN.match(link["href"])
                if own and _source(link["href"]) not in sources:
                    queue.append(link["href"])
        if self._remembered:
            sources["memory"] = self._remembered
        if not sources:
            raise ToolError(NOTHING_TO_BUILD)
        try:
            return await profile.build(sources), notes
        except ModelError as e:
            raise ToolError(f"The profile could not be built: {e.reason}") from e

    async def _keep_resume(
        self, context: RunContext[OrbitState], asked: str, pages: list[str]
    ) -> store.Resume:
        """Keeps the résumé: one the user handed over, or the one their portfolio
        links to."""
        if asked.casefold() != "portfolio":
            held = attachments.held_by(attachments.TRAY)
            if not held:
                raise ToolError(NO_FILES)
            wanted = asked.casefold()
            found = [f for f in held if f.name.casefold() == wanted] or [
                f for f in held if wanted in f.name.casefold()
            ]
            if not found:
                names = ", ".join(f.name for f in held)
                raise ToolError(NO_SUCH_FILE.format(asked=asked, names=names))
            file = found[-1]
            return _kept(file.name, attachments.read(file), attachments.FROM_THE_USER)
        portfolio = next((p for p in pages if _source(p) == "portfolio"), "")
        if not portfolio and isinstance(link := profile.value("portfolio"), str):
            portfolio = link if "://" in link else f"https://{link}"
        if not portfolio:
            raise ToolError(NO_PORTFOLIO)
        if self._browser is None or not self._browser.in_use:
            raise ToolError(NO_BROWSER)
        self._check_given(context, portfolio)
        _, links = await self._read(context, portfolio)
        link = next(
            (
                urljoin(portfolio, item["href"])
                for item in links
                if _RESUME.search(item["text"]) or _RESUME.search(item["href"])
            ),
            None,
        )
        if link is None:
            raise ToolError(NO_RESUME_LINK.format(page=portfolio))
        name = urlsplit(link).path.rsplit("/", 1)[-1] or "resume.pdf"
        return _kept(name, await _download(link), link)


async def _tell(context: RunContext[OrbitState], told: str) -> None:
    """Has the model tell the user something now, while Orbit works on."""
    try:
        await context.session.generate_reply(instructions=told)
    except Exception:
        logger.warning("the user could not be told", exc_info=True)


def _bare_all(text: str) -> str:
    return re.sub(r"https?://(?:www\.)?", "", text.casefold())


def _kept(name: str, data: bytes, source: str) -> store.Resume:
    try:
        return profile.keep_resume(name, data, source)
    except ValueError as e:
        raise ToolError(str(e)) from e


async def _download(url: str) -> bytes:
    """A résumé from the user's own page."""
    try:
        async with (
            aiohttp.ClientSession(timeout=_DOWNLOAD) as session,
            session.get(url) as response,
        ):
            if response.status >= 400:
                raise ToolError(
                    NOT_DOWNLOADED.format(url=url, why=f"{response.status}")
                )
            data = await response.content.read(MAX_RESUME_BYTES + 1)
    except (aiohttp.ClientError, TimeoutError) as e:
        raise ToolError(NOT_DOWNLOADED.format(url=url, why="it did not answer")) from e
    if len(data) > MAX_RESUME_BYTES:
        raise ToolError(NOT_DOWNLOADED.format(url=url, why="it is over 10 MB"))
    return data


def _summary() -> dict[str, Any]:
    """The profile as the model is told of it."""
    known = store.profile()
    owner = str(known["name"].value) if "name" in known else ""
    kept = [
        f"{resume.name}{f' (for {role} roles)' if (role := resumes.role_of(resume, owner)) else ''}, "
        f"{resume.source}"
        for resume in store.resumes()
    ]
    return {
        "on_screen": "The user's job profile",
        "known": {
            key: field.value
            for key, field in known.items()
            if field.value not in ("", [])
        },
        "missing": profile.missing(),
        "disagree": profile.disagreements(),
        "resumes": kept or "none yet",
        "settings": settings.current(),
    }


def _http() -> aiohttp.ClientSession:
    return utils.http_context.http_session()


def _why(error: Exception) -> str:
    return sheet.why(error)


def _jobs_summary() -> dict[str, Any]:
    """The jobs as the model is told of them: how many stand where, and each by its
    number, the newest first."""
    jobs = store.jobs()
    by_status: dict[str, int] = {}
    for job in jobs:
        by_status[job.status] = by_status.get(job.status, 0) + 1
    ticked = [
        job
        for job in jobs
        if job.apply and job.status not in (store.SUBMITTED, store.SKIPPED)
    ]
    told: dict[str, Any] = {
        "jobs": len(jobs),
        "by_status": by_status,
        "ticked_to_apply": len(ticked),
        "listed": [
            {
                "no": job.number,
                "company": job.company,
                "role": job.title,
                "fit": job.fit,
                "apply": job.apply,
                "status": job.status,
                "applies_on": job.applies_on,
            }
            for job in jobs[:JOBS_TOLD]
        ],
    }
    if len(jobs) > JOBS_TOLD:
        told["more"] = (
            f"{len(jobs) - JOBS_TOLD} older jobs are in the Sheet and not listed here."
        )
    return told


JOBS_NOTE = (
    "Applying for jobs, as it stands: {applied} of the {ticked} jobs ticked to apply "
    "to in Orbit jobs have been applied to.{waiting}{next} When the user says to "
    "carry on applying, call application_draft."
)


def jobs_note() -> str:
    """What the model is told at the start of a conversation of where applying for
    jobs stands, or nothing if no job was ever ticked."""
    jobs = store.jobs()
    ticked = [job for job in jobs if job.apply or job.status == store.SUBMITTED]
    if not ticked:
        return ""
    applied = [job for job in ticked if job.status == store.SUBMITTED]
    waiting = [
        f"job {job.number}, {job.title} at {job.company}, {job.status.lower()}"
        for job in ticked
        if job.status in (store.NEEDS_YOU, store.DRAFTING, store.FILLED)
    ]
    upcoming = [
        job
        for job in ticked
        if job.status in (store.FOUND, store.APPROVED) and job.applies_on == "company"
    ]
    return JOBS_NOTE.format(
        applied=len(applied),
        ticked=len(ticked),
        waiting=" Left part way: " + "; ".join(waiting) + "." if waiting else "",
        next=f" The next is job {upcoming[0].number}, {upcoming[0].title} at "
        f"{upcoming[0].company}."
        if upcoming
        else " None are left to apply to.",
    )


def _listed(value: str | list[str] | None) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if item.strip()]
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _about_the_user() -> dict[str, Any]:
    """What the drafting model is told of the user to judge a posting by."""
    known = store.profile()
    wanted = (
        "title", "years", "skills", "highlights", "roles", "seniority", "locations",
        "work_mode", "job_types", "seek", "avoid",
    )  # fmt: skip
    return {key: known[key].value for key in wanted if key in known}


async def _judged(
    judging: asyncio.Semaphore,
    about: dict[str, Any],
    candidate: _Candidate,
    text: str,
    applies_on: str,
) -> tuple[_Candidate, str, str, dict[str, Any] | None]:
    async with judging:
        try:
            fit = await writing.judge_fit(about, text)
        except ModelError:
            logger.warning("a posting could not be judged", exc_info=True)
            fit = None
    return candidate, text, applies_on, fit


def _left_out(
    candidate: _Candidate,
    text: str,
    fit: dict[str, Any] | None,
    wanted: dict[str, Any],
    years: float | None,
) -> str:
    """Why a posting is left out of the list once read, or "" when it is kept: only
    a good fit, in one of the user's places, is."""
    if fit is None:
        return "could not be judged"
    listing = candidate.listing
    page = text.casefold()
    if not listing.title or not _on_page(page, listing.title):
        return "did not match its page"
    # A job site's card names the company; a company's own board is the company.
    if (
        candidate.board is not None
        and listing.company
        and not _on_page(page, listing.company)
    ):
        return "did not match its page"
    if fit["fit"] == "none":
        return "did not fit"
    if fit["fit"] == "low":
        return "did not fit well enough"
    if not _shown(candidate, fit):
        return TOO_LITTLE
    where = _where(candidate, fit)
    if relevance.in_place(where, _listed(wanted.get("locations"))) is False:
        return relevance.ELSEWHERE
    if _more_experience(_experience(candidate, fit), years):
        return MORE_EXPERIENCE
    return ""


# Words a posting's heading says besides its title, company and place: how the job
# is worked, when it went up, and how many have applied.
_HEADING = frozenset(
    {
        "remote", "hybrid", "site", "onsite", "office", "full", "part", "time",
        "contract", "permanent", "temporary", "ago", "day", "days", "week", "weeks",
        "month", "months", "hour", "hours", "posted", "reposted", "over", "people",
        "clicked", "apply", "applicants", "applied",
    }
)  # fmt: skip


def _words(text: str) -> set[str]:
    return set(re.findall(r"[^\W\d_]{3,}", text.casefold()))


def _shown(candidate: _Candidate, fit: dict[str, Any]) -> bool:
    """Whether the posting's own words show how it fits: a quote the drafting model
    gave says more than the job's heading, its title, company, place and when it went
    up. A page read before its description showed has nothing else to quote."""
    listing = candidate.listing
    heading = _HEADING | _words(
        " ".join(
            (
                listing.title,
                listing.company,
                listing.location,
                fit["location"],
                candidate.facts.location,
            )
        )
    )
    return any(_words(quote) - heading for quote in fit["why"])


def _where(candidate: _Candidate, fit: dict[str, Any], kept: bool = False) -> str:
    """Where a job is: as its posting says it, in its words or in the shape search
    engines read, or else as its card does. What is `kept` is only a place, never a
    search result's snippet that a card may hold where its place is."""
    said = fit["location"] or candidate.facts.location
    if said or not kept:
        return said or candidate.listing.location
    return search.place(candidate.listing.location)


def _experience(candidate: _Candidate, fit: dict[str, Any]) -> str:
    """The years of experience a job asks for, written one way: as its posting says
    them, in its words or in the shape search engines read, or as its card or its
    address does. "" when none says."""
    return (
        search.experience_of(fit["experience"])
        or candidate.facts.experience
        or candidate.listing.experience
        or search.experience_in(candidate.listing.url)
    )


def _years() -> float | None:
    """The years of work the user has had, as their profile says."""
    return relevance.years_of({"years": profile.value("years")})


def _more_experience(asked: str, years: float | None) -> bool:
    """Whether a job asks for far more experience than the user has: more than two
    years more than they have."""
    least = search.least_years(asked)
    return least is not None and years is not None and least > years + 2


class _Before:
    """The jobs found before, to tell a listing that is one of them: the same
    posting, or the same role at the same company on another site."""

    def __init__(self) -> None:
        self._by_key: dict[str, store.Job] = {}
        self._by_role: dict[tuple[str, frozenset[str]], store.Job] = {}
        for job in store.jobs():
            self._by_key[job.key] = job
            same = relevance.job_key(job.title, job.company)
            if same is not None and (same not in self._by_role or _applied(job)):
                self._by_role[same] = job

    def why(self, key: str, listing: search.Listing) -> str:
        """Why a listing is not read again: the user applied to it already, or it
        was found before. "" for a job that is new. A company's own posting found
        after a job site's gives that job its application, there rather than on the
        job site."""
        same = relevance.job_key(listing.title, listing.company)
        alike = self._by_role.get(same) if same is not None else None
        if alike is not None and not alike.apply_url and boards.board_of(listing.url):
            store.change_job(alike.number, apply_url=listing.url, applies_on="company")
        found = [job for job in (self._by_key.get(key), alike) if job is not None]
        if any(_applied(job) for job in found):
            return APPLIED_TO
        return ALREADY_FOUND if found else ""


def _applied(job: store.Job) -> bool:
    """Whether the user applied to a job, through Orbit."""
    return job.status == store.SUBMITTED or job.applied_at is not None


def _unknown_board(name: str) -> bool:
    """Whether a company's board is yet to be looked for: never, or long enough ago
    that it may have one now."""
    board = store.board(name)
    return board is None or (
        not board.system and time.time() - board.checked_at > BOARD_LOOKED_FOR_SECONDS
    )


def _names(text: str, company: str) -> bool:
    """Whether a board's page is the company's: its title names it."""
    title = next(
        (line for line in text.splitlines() if line.startswith("- Page Title:")), ""
    )
    said = re.sub(r"[^a-z0-9]", "", title.casefold())
    return any(slug.replace("-", "") in said for slug in boards.slugs(company)[:2])


def _page_title(text: str) -> str:
    """A page's title, as the browser reads it out."""
    line = next(
        (line for line in text.splitlines() if line.startswith("- Page Title:")), ""
    )
    return line.removeprefix("- Page Title:").strip()


def _known() -> dict[str, Any]:
    return {key: known.value for key, known in store.profile().items()}


def _runs(sites: list[str], roles: list[str], places: list[str]) -> list[_Run]:
    """A search's parts: companies' own boards first, then Google's search of them,
    then each job site, each for every role and place, then the whole boards of the
    companies those turned up. Two roles or places a site searches as one, as
    Cutshort does AI Engineer and LLM Engineer, are one part."""
    runs = []
    if COMPANIES in sites:
        runs.append(_Run(COMPANIES))
    if GOOGLE in sites:
        runs += [
            _Run(GOOGLE, role, place, kind)
            for kind in (google.BOARDS, google.CAREERS)
            for role in roles
            for place in places
        ]
    searched: set[str] = set()
    for site in sites:
        if site in (COMPANIES, GOOGLE):
            continue
        for role in roles:
            for place in places:
                # A search's address, the same for every part however recent.
                address = search.SITES[site].search(role, place, 1, 1)
                if address not in searched:
                    searched.add(address)
                    runs.append(_Run(site, role, place))
    if COMPANIES in sites and any(site != COMPANIES for site in sites):
        runs.append(_Run(FOUND_COMPANIES))
    return runs


def _described(run: _Run, watched: list[str], sites: list[str]) -> str:
    if run.site == COMPANIES:
        if not watched:
            return f"{COMPANY_BOARDS}: none known yet"
        more = f" and {len(watched) - 5} more" if len(watched) > 5 else ""
        return f"{COMPANY_BOARDS}: {', '.join(watched[:5])}{more}"
    if run.site == FOUND_COMPANIES:
        found_on = [
            "Google" if site == GOOGLE else search.SITES[site].name
            for site in sites
            if site == GOOGLE or site in search.SITES
        ]
        named = ", ".join(found_on[:-1])
        named = f"{named} and {found_on[-1]}" if named else found_on[-1]
        return f"{COMPANY_BOARDS} of the companies found on {named}"
    if run.site == GOOGLE:
        return f"Google, {_KINDS[run.kind]}: {run.role} in {run.place}"
    return f"{search.SITES[run.site].name}: {run.role} in {run.place}"


def _on_page(page: str, said: str) -> bool:
    """Whether most of what a listing says is on its posting's page: a card may
    shorten or add to it."""
    words = [w for w in re.findall(r"[a-z0-9+#]+", said.casefold()) if len(w) >= 3]
    return not words or sum(word in page for word in words) * 2 >= len(words)
