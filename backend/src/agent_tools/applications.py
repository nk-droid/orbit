"""Applying for the jobs the user chose, one at a time, with two yeses.

For each job, Orbit opens its application in the browser and reads the form, drafts
an answer to every question (jobs/forms.py, and the drafting model for the rest),
checks the details and formatting (jobs/checks.py), and puts the draft on the user's
screen. Their first yes fills the form in, in the browser; the page is then read
back and checked again. Their second yes submits it, once the page still holds what
they approved.

Both yeses go through the approval gate, as an email does: the draft is told to the
user, and they must answer. Filling in and submitting are carried out here, by the
handlers confirm_action calls for those kinds of draft.

A job that can be applied to only on the job board it was found on, LinkedIn's Easy
Apply or Naukri's own Apply, is applied to there when the user allows such jobs, as
they do unless they said not to: when the company's own page has the job, it is
applied to there instead. Naukri's Apply sends the user's Naukri profile in one press,
with nothing to fill in, so its one yes is the one to send it.
"""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from typing import Any

from livekit.agents import RunContext, utils
from livekit.agents.llm import ToolError

from actions.gate import Action, NotDoneError
from drafting.triage import ModelError
from jobs import (
    boards,
    checks,
    forms,
    letter,
    profile,
    resumes,
    search,
    settings,
    sheet,
    store,
    writing,
)

from .applying import (
    CHALLENGE,
    SIGN_IN,
    WHAT,
    Applier,
    Opened,
    hand_over,
    same_page,
    sent,
    shows_applied,
    wait_for_the_user,
)
from .applying import HIDDEN as CHECK_HIDDEN
from .browser import BrowserToolset
from .state import OrbitState

logger = logging.getLogger("orbit.jobs")

FILL = "jobs.fill"
SUBMIT = "jobs.submit"
# An application's draft waits for the user this long: they read it, and look at
# the form in the browser.
EXPIRY_SECONDS = 30 * 60

NO_BROWSER = (
    "The browser has not been started. Ask the user which of their Chrome profiles "
    "to apply in, if they have not said, and call browser_start with it."
)
BUSY = "The browser is busy {what}. Wait until that is done."
NO_BOOK = (
    "Orbit jobs has not been made yet, and jobs are applied to from it. Show the jobs "
    "with jobs_list, which drafts making it."
)
NOT_READ = (
    "Orbit jobs could not be read, and applying waits for it: {why} Tell the user."
)
NO_SUCH_JOB = "There is no job {number}. Look at the list with jobs_list."
NOTHING_TICKED = (
    "No job is ticked to apply to that has not been applied to yet. Tell the user, "
    "and that they tick jobs in Orbit jobs or by voice."
)
APPLIED = "Job {number} was applied to already, on {day}. It is not applied to twice."
BOARD_ONLY = (
    "Job {number} can be applied to only on {board} itself, not on {company}'s own "
    "site. Ask the user whether to apply there; if they say so, call application_draft "
    "again with on_the_board."
)
LIMIT = (
    "That is {count} applications this sitting, the limit the user set. Tell them; "
    "they can change it with job_settings."
)
NO_RESUME = (
    "Orbit has no résumé to send. Ask the user for theirs, then keep it with "
    "job_profile."
)
# How the written answers are drafted again when the user changes the résumé sent.
FROM_THE_RESUME = (
    "The résumé sent is now {name}: write from what it says, for this job, and keep "
    "to what the user asked before."
)
DRAFTING = (
    "Tell the user in a sentence that you are opening the application for {job} and "
    "drafting it, and that you will tell them when it is on their screen. The "
    "browser is busy until then."
)
NEEDS_YOU = (
    "The application for {job} needs the user before Orbit can go on: {why} Tell "
    "them. When they say it is done, or that the application is open in the browser, "
    "call application_draft for job {number} with already_open true: Orbit then reads "
    "the page as it is, without opening the job again."
)
BOARD_ONLY_FOUND = (
    "The job {job} turned out to be one that can be applied to only on {board} "
    "itself. Tell the user, and ask whether to apply there."
)
PRESS_DRAFTED = (
    "The application for {job} is ready on {board}, action_id {action_id}: {board}'s "
    "Apply sends the user's {board} profile, and the résumé on it, in one press, with "
    "nothing to fill in. Tell the user in a sentence, and ask whether to apply. Call "
    "confirm_action with this action_id only after they clearly agree: it presses "
    "Apply, which sends the application."
)
NO_PRESS = (
    "Nothing was sent: {board}'s Apply is no longer on the page, which may mean the "
    "job was applied to already. Tell the user; they can look at the page in the "
    "browser."
)
PRESSED_NOT_SENT = (
    "{board}'s Apply was pressed, but the page did not say the application was sent: "
    "{board} may be asking the recruiter's questions first. Tell the user to answer "
    "them in the browser, and to say when it is done."
)
MODEL_DOWN = "The application for {job} could not be drafted: {why} Tell the user."
DRAFT_FAILED = (
    "Something went wrong drafting the application for {job}, and it was left. "
    "Tell the user."
)
DRAFTED = (
    "The draft of the application for {job} is on the user's screen, action_id "
    "{action_id}: {gist}. {details}Tell the user in a sentence or two. Ask about what "
    "needs them and what to confirm, one or two at a time, and pass their answers "
    "with application_change. Once nothing needs them, ask whether to fill the form "
    "in. Call confirm_action with this action_id only after they clearly agree; it "
    "fills the form in and submits nothing."
)
STILL_NEEDED = (
    "The form cannot be filled in yet: {what}. Ask the user, and pass their answers "
    "with application_change."
)
CHANGED = (
    "The draft is changed and on the user's screen, action_id {action_id}: {gist}. "
    "{details}Tell the user what changed in a sentence, then ask whether to fill the "
    "form in. Call confirm_action with this action_id only after they clearly agree."
)
REWRITING = (
    "Tell the user in a sentence that you are making that change, and that you will "
    "tell them when the draft is updated."
)
NOTHING_DRAFTED = "There is no application being drafted in this conversation."
NOT_A_QUESTION = (
    "{named!r} is not a question on the form. The questions are numbered on the "
    "draft: name one by its number or its words."
)
NO_ANSWER = 'Write each answer as "question: answer", such as "3: 30 days".'
KEPT = "Kept for next time: {what}."
OFFER_KEEPING = (
    "Offer to keep the user's answers to {what} for the next application; if they "
    "agree, call application_change with keep."
)
SKIPPED = "Job {number} is skipped and will not be applied to."
NOT_WRITTEN = "Orbit jobs could not be changed to say so: {why}"
FORM_CHANGED = (
    "The form on the page has changed since it was drafted: it no longer asks "
    "{questions}. Nothing was filled in. Draft the application again with "
    "application_draft."
)
STILL_BLOCKED = "Nothing was filled in, since the draft still has problems: {what}"
FILLED = "The form is filled in, in the browser. Nothing was submitted."
WENT_ON = "Filled in, and on to the next step. Nothing was submitted."
ON_TO_NEXT = (
    "This step is filled in and the form has gone on to its next step, which is being "
    "drafted: you will be told when it is on the user's screen. Nothing was "
    "submitted. Tell the user in a sentence."
)
NOT_ON = (
    "The page did not go on to the form's next step: {errors} Tell the user; they "
    "can put it right in the browser or tell you the answers, then call "
    "application_check."
)
READ_BACK_SAME = "Every answer is on the page as drafted."
READ_BACK_DIFFERENT = "The page holds some answers differently from the draft: {what}"
SUBMIT_NEXT = (
    "Tell the user what the read-back found, and that the form is ready to submit: "
    "they can look at it in the browser first, and change it there or by voice. Ask "
    "whether to submit it. Call confirm_action with action_id {action_id} only after "
    "they clearly agree."
)
HIDDEN = "The browser is hidden, so the user cannot look at the form: offer to show it."
FIX_FIRST = (
    "The page has problems to put right before it can be submitted: {what} The user "
    "can put them right in the browser, or tell you the answers for "
    "application_change. When they say it is done, call application_check."
)
MOVED = (
    "The browser is no longer on the application, so nothing was submitted. Call "
    "application_check once it is back on the form."
)
HAND_EDITS = (
    "Nothing was submitted: the form on the page is not as the user approved it. "
    "{what} A new draft holds the page as it is now, action_id {action_id}. Tell the "
    "user what differs and ask whether to submit it as it is."
)
NO_SUBMIT = "Nothing was submitted: the page has no button that submits the form."
REFUSED = "The page did not take the form, so it was not sent: {errors} Tell the user."
DRY_RUN = (
    "This is a dry run: everything was done but the click on Submit, so nothing was "
    "sent. Tell the user."
)
SENT = "The application to {company} was submitted. The page said: {confirmation}"
SENT_NEXT = (
    "Tell the user in a sentence. {count} of {limit} applications this sitting. Ask "
    "whether to go on to the next job; if so, call application_draft."
)
FOLLOW_UP = (
    "Offer once, in a few words, a reminder to follow up on the applications sent "
    "today a week from now, and add it with reminder_add only if the user says yes."
)
CHECK_AFTER = (
    "A check that the user is a person came up when Submit was pressed. Only they "
    "may do it: never click, type into or try to get past it yourself. Tell them it "
    "is waiting for them in the browser. Orbit watches the page and tells you when "
    "the application has gone through."
)
CHECK_ON_THE_FORM = (
    "The form has a check that the user is a person, which only they may do. Tell "
    "them to do it in the browser before they say to submit."
)
CHECK_FIRST = (
    "Nothing was submitted: the check that the user is a person on the form is not "
    "done yet. Only they may do it. Ask them to do it in the browser; once they say "
    "it is done and to submit, call confirm_action with action_id {action_id}."
)
NOT_DONE_IN_TIME = "{what} was not done within ten minutes."
NOT_DONE_AFTER = (
    "The check that came up when Submit was pressed was not done within ten minutes, "
    "so Orbit stopped watching. Tell the user; when they have done it, call "
    "application_check."
)
DONE_NOT_SENT = (
    "The check is done, but the form was not sent. A new draft submits it, action_id "
    "{action_id}: ask the user whether to submit it now, and call confirm_action only "
    "after they clearly agree."
)
# How many times opening an application is handed to the user before Orbit gives up.
HANDED_OVER = 2
NO_CONFIRMATION = (
    "Submit was pressed, but the page shows no confirmation. Ask the user to look at "
    "the browser; call application_check to read it again."
)
SUBMIT_DRAFTED = (
    "The form as the page holds it is drafted for submitting, action_id {action_id}. "
    "Tell the user and ask whether to submit it. Call confirm_action only after they "
    "clearly agree."
)

# Brings Orbit jobs up to date, with changes to the user's own columns, Apply and
# Notes, written as voice changes: "untick 3", "note 3: ...". Returns any trouble.
Syncing = Callable[..., Awaitable[str | None]]


def _http() -> Any:
    return utils.http_context.http_session()


async def _tell(context: Any, told: str) -> None:
    """Has the model tell the user something now, while Orbit works on."""
    try:
        await context.session.generate_reply(instructions=told)
    except Exception:
        logger.warning("the user could not be told", exc_info=True)


def _board(name: str) -> search.Site | None:
    """The job site of that name, as a job's applies_on names it."""
    return next((site for site in search.SITES.values() if site.name == name), None)


def _board_allowed(name: str) -> bool:
    """Whether a job that can be applied to only on that job board is applied to
    there: as the user allows LinkedIn's Easy Apply and Naukri's own Apply, but not a
    site whose every job applies there, as Cutshort's does, which they choose."""
    site = _board(name)
    return (
        site is not None
        and not site.applied_on_site
        and bool(settings.current()["easy_apply"])
    )


def _job_named(job: store.Job) -> str:
    return f"{job.title} at {job.company}"


def _answers_of(application: store.Application) -> list[forms.Answer]:
    return forms.from_saved(application.answers)


def _known() -> dict[str, Any]:
    return {key: known.value for key, known in store.profile().items()}


def _resume_choice(job: store.Job) -> resumes.Choice | None:
    """The résumé of the user's that suits a job, and why."""
    known = _known()
    roles = known.get("roles") or []
    return resumes.choose(
        store.resumes(),
        job.title,
        job.posting,
        owner=str(known.get("name") or ""),
        roles=roles if isinstance(roles, list) else [str(roles)],
    )


def _resume_of(application: store.Application, job: store.Job) -> store.Resume | None:
    """The résumé an application sends: the one it was drafted with, while the user
    still has it, or else the one that suits its job."""
    if application.resume and (kept := store.resume(application.resume)) is not None:
        return kept
    chosen = _resume_choice(job)
    return chosen.resume if chosen else None


def _resume_told(resume: store.Resume | None, job: store.Job) -> str:
    """The résumé as the draft names it, and when the user has more than one, why
    it is the one sent."""
    if resume is None:
        return ""
    chosen = _resume_choice(job)
    if chosen is None or not chosen.why:
        return resume.name
    if chosen.resume.name == resume.name:
        return f"{resume.name}: {chosen.why}"
    return f"{resume.name}, as you chose"


def _sources(known: dict[str, Any], resume: str) -> str:
    """What facts about the user are checked against: the profile and the résumé."""
    lines = [
        f"{key}: {', '.join(v) if isinstance(v, list) else v}"
        for key, v in known.items()
    ]
    return "\n".join([*lines, resume])


def _shown(answer: forms.Answer) -> str:
    if answer.needs:
        return f"Needs you: {answer.needs}"
    if answer.blank:
        return "Left blank"
    return answer.shown()


def _from(answer: forms.Answer, confirming: bool) -> str:
    if answer.needs or answer.blank:
        return ""
    if answer.confirm and confirming:
        return f"{answer.source}, to confirm"
    return answer.source


def _asked(answer: forms.Answer) -> str:
    """A question with its answer, in a line."""
    joined = " " if answer.question.endswith("?") else ": "
    return f"{answer.question}{joined}{answer.shown()}"


def _gist(answers: list[forms.Answer], letter_text: str) -> str:
    """How much of a form the draft answers, as the user's card says it."""
    answered = sum(1 for a in answers if not a.blank and not a.needs)
    parts = [f"{answered} of {len(answers)} questions answered"]
    if letter_text:
        parts.append("a cover letter")
    needs = sum(1 for a in answers if a.needs)
    if needs:
        parts.append(f"{needs} {'needs' if needs == 1 else 'need'} you")
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"


def _details(answers: list[forms.Answer], findings: list[checks.Finding]) -> str:
    """What the model is told to raise with the user: what needs them, what to
    confirm, what the checks found, and what was left blank for them."""
    told = []
    needs = [f"{a.question!r} ({a.needs})" for a in answers if a.needs]
    if needs:
        told.append("These need the user: " + "; ".join(needs) + ".")
    confirm = [f"{_asked(a)!r}" for a in answers if a.confirm]
    if confirm:
        told.append("Ask the user to confirm: " + "; ".join(confirm) + ".")
    found = [
        f.said() for f in findings if not f.problem.startswith("needs your answer")
    ]
    if found:
        told.append("The checks found: " + " ".join(found))
    left = [a.question for a in answers if a.blank and not a.needs]
    if left:
        told.append(
            "Left blank, for the user to answer if they want to: "
            + "; ".join(left)
            + "."
        )
    return " ".join(told) + (" " if told else "")


def _preview(
    what: str,
    job: store.Job,
    url: str,
    answers: list[forms.Answer],
    letter_text: str,
    findings: list[checks.Finding],
    resume_name: str,
    confirming: bool = True,
) -> dict[str, Any]:
    """An application's draft as the user's screen shows it and the model is told
    it: the job and its page, what needs the user and what to confirm, what the
    checks found, the résumé and the cover letter, then every question with its
    answer and where the answer came from. Once the form is filled in, the user has
    confirmed what they were asked to, so `confirming` is then False."""
    preview: dict[str, Any] = {
        "what": what,
        "job": f"{_job_named(job)} (job {job.number})",
        "page": url,
        "answers": _gist(answers, letter_text),
    }
    needs = [a.question for a in answers if a.needs]
    if needs:
        preview["needs you"] = "\n".join(needs)
    confirm = [_asked(a) for a in answers if a.confirm]
    if confirm and confirming:
        preview["to confirm"] = "\n".join(confirm)
    found = [
        f.said() for f in findings if not f.problem.startswith("needs your answer")
    ]
    if found:
        preview["checks"] = "\n".join(found)
    if resume_name:
        preview["résumé"] = resume_name
    if letter_text:
        preview["cover letter"] = letter_text
    preview["questions"] = [
        {
            "number": a.number,
            "question": a.question,
            "answer": _shown(a),
            "from": _from(a, confirming),
            "needs": bool(a.needs),
            "blank": a.blank and not a.needs,
        }
        for a in answers
    ]
    return preview


def _letter_field(form: forms.Form) -> forms.Field | None:
    return forms.letter_field(form)


class Applications:
    """Applying in this conversation: the application being drafted, the background
    work of drafting it, and how many were sent this sitting."""

    def __init__(
        self,
        browser: BrowserToolset | None,
        busy: Callable[[], str | None],
        sync: Syncing,
    ) -> None:
        self._browser = browser
        self._applier = Applier(browser) if browser is not None else None
        self._busy = busy
        self._sync = sync
        self._working: asyncio.Task | None = None
        self._doing = ""
        # The order the Jobs tab lists jobs in, as last read.
        self._order: list[int] = []
        # What watches a submitted form while the user does a check that came up.
        self._watching: asyncio.Task | None = None
        self.current: int | None = None
        self.sent_here = 0

    @property
    def working(self) -> str | None:
        """What the background work of applying is doing, while it runs."""
        if self._working is not None and not self._working.done():
            return self._doing
        return None

    def wire(self, context: RunContext[OrbitState]) -> None:
        """Lets confirm_action fill in and submit applications in this conversation."""
        context.userdata.handlers[FILL] = self.fill
        context.userdata.handlers[SUBMIT] = self.submit

    def _free(self) -> None:
        if what := self._busy() or self.working:
            raise ToolError(BUSY.format(what=what))

    def _ready(self) -> tuple[BrowserToolset, Applier]:
        if self._browser is None or self._applier is None or not self._browser.in_use:
            raise ToolError(NO_BROWSER)
        self._free()
        return self._browser, self._applier

    def _others(self, job: store.Job) -> list[str]:
        """The other companies the user is applying to, which this application must
        not name in their place."""
        own = str(_known().get("company") or "").casefold()
        names = {
            other.company
            for other in store.jobs()
            if other.number != job.number
            and other.company.strip()
            and (
                other.apply
                or other.status
                in (store.APPROVED, store.DRAFTING, store.FILLED, store.SUBMITTED)
            )
        }
        return sorted(
            name
            for name in names
            if name.casefold() not in (job.company.casefold(), own)
        )

    def _findings(
        self,
        application: store.Application,
        answers: list[forms.Answer],
        form: forms.Form,
        job: store.Job,
        letter_text: str,
    ) -> list[checks.Finding]:
        resume = _resume_of(application, job)
        in_a_file = (item := _letter_field(form)) is not None and item.kind == "file"
        return checks.check(
            answers,
            form.fields,
            company=job.company,
            known=_known(),
            resume_name=resume.name if resume else "",
            others=self._others(job),
            letter=letter_text if in_a_file else "",
        )

    def _propose(
        self,
        context: RunContext[OrbitState],
        kind: str,
        application: store.Application,
        job: store.Job,
        answers: list[forms.Answer],
        letter_text: str,
        findings: list[checks.Finding],
    ) -> Action:
        what = "Fill in application" if kind == FILL else "Submit application"
        preview = _preview(
            what,
            job,
            application.url,
            answers,
            letter_text,
            findings,
            _resume_told(_resume_of(application, job), job),
            confirming=kind == FILL,
        )
        payload = {
            "application": application.number,
            "answers": forms.as_saved(answers),
            "letter": letter_text,
        }
        return context.userdata.gate.propose(
            kind, payload, preview, expiry=EXPIRY_SECONDS
        )

    # Drafting.

    async def draft(
        self,
        context: RunContext[OrbitState],
        number: int,
        on_the_board: bool,
        resume: str = "",
        already_open: bool = False,
    ) -> dict[str, Any]:
        """Starts drafting the application for a job, or the next one ticked, in the
        background, with the résumé the user asked for or the one that suits the
        job. With `already_open` its application is read from the browser as the
        user has it open. Returns what the model tells the user meanwhile."""
        self._ready()
        if not store.resumes():
            raise ToolError(NO_RESUME)
        asked = ""
        if resume.strip():
            try:
                asked = profile.named_resume(resume).name
            except ValueError as e:
                raise ToolError(str(e)) from e
        limit = settings.current()["applications_per_sitting"]
        if self.sent_here >= limit:
            raise ToolError(LIMIT.format(count=self.sent_here))
        job = await self._chosen(number)
        if job.status == store.SUBMITTED:
            day = time.strftime("%Y-%m-%d", time.localtime(job.applied_at or 0))
            raise ToolError(APPLIED.format(number=job.number, day=day))
        if job.applies_on != "company" and not on_the_board:
            if not _board_allowed(job.applies_on):
                raise ToolError(
                    BOARD_ONLY.format(
                        number=job.number, board=job.applies_on, company=job.company
                    )
                )
            on_the_board = True
        self.wire(context)
        store.change_job(job.number, status=store.DRAFTING)
        self._doing = f"drafting the application for {_job_named(job)}"
        self._working = asyncio.create_task(
            self._drafting(
                context,
                job,
                self._draft_one(context, job, on_the_board, asked, already_open),
            )
        )
        return {
            "drafting": _job_named(job),
            "job": job.number,
            "say": DRAFTING.format(job=_job_named(job)),
        }

    async def _chosen(self, number: int) -> store.Job:
        """The job asked for, or the next ticked in Orbit jobs not yet applied to, as
        the Sheet orders them: read back first, since it is the list the user
        approved. The ticked jobs are marked approved."""
        if store.book() is None:
            raise ToolError(NO_BOOK)
        try:
            synced = await sheet.sync(_http())
        except sheet.GoneError as e:
            store.lose_book()
            raise ToolError(NO_BOOK) from e
        except sheet.TROUBLE as e:
            raise ToolError(NOT_READ.format(why=sheet.why(e))) from e
        if synced is not None:
            self._order = synced.order
        if number:
            job = store.job(number)
            if job is None:
                raise ToolError(NO_SUCH_JOB.format(number=number))
            return job
        jobs = {job.number: job for job in store.jobs()}
        order = self._order or sorted(jobs)
        waiting = [
            jobs[n]
            for n in order
            if n in jobs
            and jobs[n].apply
            and (jobs[n].applies_on == "company" or _board_allowed(jobs[n].applies_on))
            and jobs[n].status not in (store.SUBMITTED, store.SKIPPED)
        ]
        if not waiting:
            raise ToolError(NOTHING_TICKED)
        for job in waiting:
            if job.status == store.FOUND:
                store.change_job(job.number, status=store.APPROVED)
        return waiting[0]

    def remember_order(self, order: list[int]) -> None:
        """The order the Jobs tab lists jobs in, from its last read."""
        self._order = list(order)

    async def _drafting(
        self,
        context: RunContext[OrbitState],
        job: store.Job,
        drafting: Awaitable[str],
    ) -> None:
        """Drafts an application, or a step of one, in the background, then tells the
        model what to tell the user."""
        try:
            told = await drafting
        except ModelError as e:
            store.change_job(job.number, status=store.APPROVED)
            told = MODEL_DOWN.format(job=_job_named(job), why=e.reason)
        except ToolError as e:
            told = str(e)
        except Exception:
            logger.exception("an application could not be drafted")
            store.change_job(job.number, status=store.APPROVED)
            told = DRAFT_FAILED.format(job=_job_named(job))
        await self._sync()
        try:
            await context.session.generate_reply(instructions=told)
        except Exception:
            logger.warning("the drafted application could not be told", exc_info=True)

    async def _draft_one(
        self,
        context: RunContext[OrbitState],
        job: store.Job,
        on_board: bool,
        resume: str = "",
        already_open: bool = False,
    ) -> str:
        """Opens a job's application and drafts its form: from the job's page, or
        with `already_open`, as the user has it open in the browser. A check that
        the user is a person, or a sign-in, on the way is handed to them, and opening
        the application goes on once they have done it."""
        assert self._browser is not None and self._applier is not None
        self._browser.hold(self._doing)
        try:
            opened = await self._opened(context, job, on_board, already_open)
            if opened.board_only and _board_allowed(opened.board_only):
                # Not on the company's own page after all: applied to on the board.
                store.change_job(job.number, applies_on=opened.board_only)
                opened = await self._opened(context, job, True, False)
        finally:
            self._browser.release()
        if opened.press is not None:
            return self._draft_press(context, store.job(job.number) or job)
        if opened.board_only:
            store.change_job(
                job.number,
                status=store.FOUND,
                applies_on=opened.board_only,
                apply=False,
            )
            await self._sync([f"untick {job.number}"])
            return BOARD_ONLY_FOUND.format(job=_job_named(job), board=opened.board_only)
        if opened.form is None:
            store.change_job(job.number, status=store.NEEDS_YOU)
            return NEEDS_YOU.format(
                job=_job_named(job), why=opened.needs_you, number=job.number
            )
        url = str(opened.form.get("url") or "")
        store.change_job(job.number, apply_url=url)
        # A company's application on its own board shows where its jobs are: its
        # board is searched from now on.
        if (on := boards.board_of(url)) is not None and job.company.strip():
            known = store.board(job.company)
            if known is None or not known.system:
                store.keep_board(job.company, on[0].key, on[1], "found")
        return await self._draft_page(context, job, opened.form, resume=resume)

    async def _opened(
        self,
        context: RunContext[OrbitState],
        job: store.Job,
        on_board: bool,
        already_open: bool,
    ) -> Opened:
        """A job's application opened: from its page, on its board's own way to apply
        `on_board`, or as the user has it open. A check that the user is a person, or
        a sign-in, on the way is handed to them, and opening goes on once done."""
        assert self._browser is not None and self._applier is not None
        for _ in range(HANDED_OVER):
            opened = (
                await self._applier.open_here(context)
                if already_open
                else await self._applier.open_application(
                    context,
                    job.apply_url or job.url,
                    search.SITES.get(job.site.casefold()),
                    on_board,
                )
            )
            if not opened.check:
                break
            where = f"The application for {_job_named(job)}"
            await _tell(context, hand_over(where, opened.check, self._browser.shown))
            waited = await wait_for_the_user(self._browser, context, opened.check)
            if waited is None:
                opened.needs_you = NOT_DONE_IN_TIME.format(what=WHAT[opened.check])
                break
        return opened

    def _draft_press(self, context: RunContext[OrbitState], job: store.Job) -> str:
        """Puts a job board's one-press Apply before the user for their yes, as an
        application with nothing to fill in: the yes is the one to send it."""
        board = _board(job.applies_on) or search.SITES[job.site.casefold()]
        url = job.url
        found = {"url": url, "fields": [], "press": {"board": board.key}}
        number = store.start_application(job.number, url, found, 1, 0, "")
        self.current = number
        application = store.application(number)
        assert application is not None
        store.change_application(number, status=store.FILLED_IN)
        preview = {
            "what": f"Apply on {board.name}",
            "job": f"{_job_named(job)} (job {job.number})",
            "page": url,
            "sends": f"Your {board.name} profile, and the résumé on it, in one press "
            f"of {board.name}'s Apply. There is nothing to fill in.",
        }
        payload = {"application": number, "answers": [], "letter": ""}
        action = context.userdata.gate.propose(
            SUBMIT, payload, preview, expiry=EXPIRY_SECONDS
        )
        hidden = " " + HIDDEN if self._browser and self._browser.shown is False else ""
        return (
            PRESS_DRAFTED.format(
                job=_job_named(job), board=board.name, action_id=action.id
            )
            + hidden
        )

    async def _draft_page(
        self,
        context: RunContext[OrbitState],
        job: store.Job,
        found: dict[str, Any],
        step: int = 1,
        first: int = 0,
        resume: str = "",
    ) -> str:
        """Drafts the form on the page, a whole application or a step of one, and
        puts it before the user for their yes to fill it in. Its résumé is the one
        its first step sent, the one the user asked for (`resume`), or else the one
        that suits the job."""
        assert self._browser is not None
        form = forms.read(found)
        earlier = store.application(first) if first else None
        sent = (
            _resume_of(earlier, job)
            if earlier is not None
            else store.resume(resume)
            if resume
            else None
        )
        if sent is None and (chosen := _resume_choice(job)) is not None:
            sent = chosen.resume
        number = store.start_application(
            job.number, form.url, found, step, first, sent.name if sent else ""
        )
        self.current = number
        started = store.application(number)
        assert started is not None
        known = _known()
        resume_text = sent.text if sent else ""
        drafted = forms.draft(
            form,
            known,
            store.answers(),
            sent.name if sent else "",
            job.site,
            date.today(),
        )
        sources = _sources(known, resume_text)
        letter_item = drafted.letter
        wanted = None
        if letter_item is not None:
            wanted = {"wanted": True}
            characters, _ = checks.limits(letter_item)
            if characters:
                wanted["max_characters"] = characters
        about = {"company": job.company, "role": job.title, "location": job.location}
        written, letter_text = await writing.answer_form(
            known,
            resume_text,
            about,
            job.posting,
            forms.questions_for_model(drafted, form, sources),
            wanted,
        )
        for given in written.values():
            given["answer"] = checks.tidy(given["answer"], job.company, job.title)
        letter_text = checks.tidy(letter_text, job.company, job.title)
        answers = forms.with_written(
            drafted,
            form,
            written,
            sources,
            letter_text,
            letter.file_name(str(known.get("name") or "")),
        )
        findings = self._findings(started, answers, form, job, letter_text)
        findings += await self._proofread(
            known, resume_text, about, answers, letter_text
        )
        store.change_application(
            number,
            answers=forms.as_saved(answers),
            letter=letter_text,
            findings=[asdict(f) for f in findings],
            status=store.DRAFTED,
        )
        application = store.application(number)
        assert application is not None
        action = self._propose(
            context, FILL, application, job, answers, letter_text, findings
        )
        hidden = " " + HIDDEN if self._browser.shown is False else ""
        named = _job_named(job)
        return (
            DRAFTED.format(
                job=named if step == 1 else f"{named}, its step {step}",
                action_id=action.id,
                gist=_gist(answers, letter_text),
                details=_details(answers, findings),
            )
            + hidden
        )

    async def _proofread(
        self,
        known: dict[str, Any],
        resume_text: str,
        about: dict[str, str],
        answers: list[forms.Answer],
        letter_text: str,
    ) -> list[checks.Finding]:
        """What the drafting model's proofread finds in what it wrote. Never a reason
        to stop: it is told to the user."""
        written = [
            {"id": a.number, "question": a.question, "answer": a.value}
            for a in answers
            if a.source == forms.WRITTEN
            and isinstance(a.value, str)
            and a.value
            and a.asks != "cover_letter"
        ]
        if letter_text:
            written.append({"id": 0, "question": "Cover letter", "answer": letter_text})
        by_id = {item["id"]: item["question"] for item in written}
        try:
            problems = await writing.proofread(known, resume_text, about, written)
        except ModelError:
            logger.warning("an application could not be proofread", exc_info=True)
            return [
                checks.Finding("", "The proofread could not be done.", blocking=False)
            ]
        return [
            checks.Finding(
                by_id[problem["id"]],
                f"{problem['problem']} ({problem['words']!r})"
                if problem["words"]
                else problem["problem"],
                blocking=False,
            )
            for problem in problems
        ]

    # The user's changes.

    def _application(self) -> tuple[store.Application, store.Job]:
        number = self.current
        application = store.application(number) if number else None
        if application is None or application.status in (store.SENT, store.STOPPED):
            raise ToolError(NOTHING_DRAFTED)
        job = store.job(application.job)
        assert job is not None
        return application, job

    async def change(
        self,
        context: RunContext[OrbitState],
        answers: list[str],
        rewrite: str,
        keep: list[str],
        resume: str = "",
    ) -> dict[str, Any]:
        """Changes the application being drafted as the user said: their answers,
        kept for next time where they said so, the résumé it sends, and a rewrite of
        the writing, which runs in the background. Another résumé has what is
        written drafted again from it."""
        application, job = self._application()
        form = forms.read(application.form)
        current = _answers_of(application)
        by_number = {answer.number: answer for answer in current}
        changed = []
        for item in answers:
            named, colon, said = item.partition(":")
            if not colon or not said.strip():
                raise ToolError(NO_ANSWER)
            number = forms.number_of(form, named)
            if number is None or number not in by_number:
                raise ToolError(NOT_A_QUESTION.format(named=named.strip()))
            try:
                new = forms.changed(form, number, said)
            except ValueError as e:
                raise ToolError(str(e)) from e
            by_number[number] = new
            changed.append(new)
        told: dict[str, Any] = {}
        kept = self._keep(keep, list(by_number.values()), form)
        if kept:
            told["kept"] = KEPT.format(what=", ".join(kept))
        previous = current
        current = [by_number[a.number] for a in current]
        if resume.strip():
            try:
                sent = profile.named_resume(resume)
            except ValueError as e:
                raise ToolError(str(e)) from e
            for step in store.steps_of(application.first):
                store.change_application(step.number, resume=sent.name)
            application = store.application(application.number) or application
            current = [
                replace(a, value=sent.name) if a.asks == "resume" and a.value else a
                for a in current
            ]
            told["resume"] = sent.name
            if application.letter or any(a.source == forms.WRITTEN for a in current):
                rewrite = f"{FROM_THE_RESUME.format(name=sent.name)} {rewrite}".strip()
        if rewrite.strip():
            self._free()
            self._doing = "changing the application's draft"
            self._working = asyncio.create_task(
                self._rewriting(context, application, job, current, rewrite.strip())
            )
            return {**told, "say": REWRITING}
        store.change_application(application.number, answers=forms.as_saved(current))
        told.update(
            self._redrafted(context, application.number, job, current, previous)
        )
        yours = [
            a.question for a in changed if forms.asks(_field(form, a.number)) == "yours"
        ]
        if yours and not keep:
            told["then"] = OFFER_KEEPING.format(what=", ".join(yours))
        return told

    def _keep(
        self, keep: list[str], answers: list[forms.Answer], form: forms.Form
    ) -> list[str]:
        """Keeps the answers the user said to keep for the next application: a
        personal question's or salary's by its topic, a fact about them in their
        profile, and any other by its question."""
        kept = []
        by_number = {answer.number: answer for answer in answers}
        for named in keep:
            number = forms.number_of(form, named)
            if number is None or number not in by_number:
                raise ToolError(NOT_A_QUESTION.format(named=named.strip()))
            answer = by_number[number]
            if answer.blank:
                continue
            topic = forms.topic_of(answer.question)
            profile_key = _PROFILE_KEYS.get(answer.asks)
            if topic:
                store.keep_answer(topic, answer.shown())
            elif profile_key:
                profile.change(f"{profile_key}: {answer.shown()}")
            else:
                store.keep_answer(forms.plain(answer.question), answer.shown())
            kept.append(answer.question)
        return kept

    async def _rewriting(
        self,
        context: RunContext[OrbitState],
        application: store.Application,
        job: store.Job,
        answers: list[forms.Answer],
        asked: str,
    ) -> None:
        try:
            told = await self._rewrite(context, application, job, answers, asked)
        except ModelError as e:
            told = MODEL_DOWN.format(job=_job_named(job), why=e.reason)
        except Exception:
            logger.exception("an application's draft could not be changed")
            told = DRAFT_FAILED.format(job=_job_named(job))
        try:
            await context.session.generate_reply(instructions=told)
        except Exception:
            logger.warning("the changed draft could not be told", exc_info=True)

    async def _rewrite(
        self,
        context: RunContext[OrbitState],
        application: store.Application,
        job: store.Job,
        answers: list[forms.Answer],
        asked: str,
    ) -> str:
        form = forms.read(application.form)
        known = _known()
        resume = _resume_of(application, job)
        resume_text = resume.text if resume else ""
        about = {"company": job.company, "role": job.title, "location": job.location}
        written = [
            {
                "id": a.number,
                "question": a.question,
                "answer": a.value,
                **(
                    {"max_characters": c}
                    if (c := checks.limits(_field(form, a.number))[0])
                    else {}
                ),
            }
            for a in answers
            if isinstance(a.value, str)
            and a.value
            and a.source == forms.WRITTEN
            and a.asks != "cover_letter"
        ]
        if application.letter:
            written.append(
                {"id": 0, "question": "Cover letter", "answer": application.letter}
            )
        new = await writing.rewrite(
            known, resume_text, about, job.posting, asked, written
        )
        letter_text = checks.tidy(
            new.pop(0, application.letter), job.company, job.title
        )
        letter_item = _letter_field(form)
        previous = answers
        answers = [
            replace(a, value=checks.tidy(new[a.number], job.company, job.title))
            if a.number in new
            else a
            for a in answers
        ]
        if letter_item is not None and letter_item.kind != "file":
            answers = [
                replace(a, value=letter_text) if a.number == letter_item.number else a
                for a in answers
            ]
        rewritten = [a for a in answers if a.number in new]
        extra = await self._proofread(
            known,
            resume_text,
            about,
            rewritten,
            letter_text if letter_text != application.letter else "",
        )
        store.change_application(
            application.number, answers=forms.as_saved(answers), letter=letter_text
        )
        told = self._redrafted(
            context,
            application.number,
            job,
            answers,
            previous,
            extra,
            letter_changed=letter_text != application.letter,
        )
        return told["say"]

    def _redrafted(
        self,
        context: RunContext[OrbitState],
        number: int,
        job: store.Job,
        answers: list[forms.Answer],
        previous: list[forms.Answer],
        extra: list[checks.Finding] | None = None,
        letter_changed: bool = False,
    ) -> dict[str, Any]:
        """A changed draft checked again and put before the user for their first yes.
        What the proofread found in an answer that did not change still stands."""
        application = store.application(number)
        assert application is not None
        form = forms.read(application.form)
        findings = self._findings(application, answers, form, job, application.letter)
        same = {a.question for a, b in zip(previous, answers, strict=True) if a == b}
        if not letter_changed:
            same.add("Cover letter")
        carried = [
            checks.Finding(**f)
            for f in application.findings
            if not f["blocking"] and f["question"] in same
        ]
        findings += [f for f in [*carried, *(extra or [])] if f not in findings]
        store.change_application(
            number,
            findings=[asdict(f) for f in findings],
            status=store.DRAFTED,
        )
        action = self._propose(
            context, FILL, application, job, answers, application.letter, findings
        )
        return {
            "action_id": action.id,
            "say": CHANGED.format(
                action_id=action.id,
                gist=_gist(answers, application.letter),
                details=_details(answers, findings),
            ),
        }

    async def skip(self, number: int, why: str) -> dict[str, Any]:
        job = store.job(number)
        if job is None:
            raise ToolError(NO_SUCH_JOB.format(number=number))
        notes = "; ".join(part for part in (job.notes, why.strip()) if part)
        store.change_job(number, status=store.SKIPPED, apply=False, notes=notes)
        application = store.application(self.current) if self.current else None
        if application is not None and application.job == number:
            store.change_application(application.number, status=store.STOPPED)
            self.current = None
        said = [f"skip {number}", *([f"note {number}: {notes}"] if notes else [])]
        told = {"skipped": SKIPPED.format(number=number)}
        if trouble := await self._sync(said):
            told["orbit_jobs"] = NOT_WRITTEN.format(why=trouble)
        return told

    # The two yeses.

    async def fill(self, context: Any, action: Action) -> dict[str, Any]:
        """Fills in the approved draft of an application, in the browser, and reads it
        back: the first yes. Nothing is submitted. Drafts the second yes."""
        number = action.payload["application"]
        application = store.application(number)
        if application is None:
            raise NotDoneError(NOTHING_DRAFTED)
        job = store.job(application.job)
        assert job is not None
        answers = forms.from_saved(action.payload["answers"])
        letter_text = str(action.payload.get("letter") or "")
        form = forms.read(application.form)
        blocking = [
            f
            for f in self._findings(application, answers, form, job, letter_text)
            if f.blocking
        ]
        if blocking:
            raise NotDoneError(
                STILL_BLOCKED.format(what=" ".join(f.said() for f in blocking))
            )
        if self._browser is None or self._applier is None or not self._browser.in_use:
            raise NotDoneError(NO_BROWSER)
        if what := self._busy():
            raise NotDoneError(BUSY.format(what=what))
        files = self._files(application, job, form, letter_text)
        self._browser.hold("filling in an application")
        try:
            page = forms.read(await self._applier.read_form(context))
            if not same_page(page.url, application.url):
                await self._browser.open_page(context, application.url)
                page = forms.read(await self._applier.read_form(context))
            if page.password:
                raise NotDoneError(SIGN_IN)
            if gone := forms.missing_fields(answers, page):
                raise NotDoneError(FORM_CHANGED.format(questions="; ".join(gone)))
            picked = await self._applier.fill(
                context, forms.steps(answers, page, files)
            )
            answers = [
                replace(a, value=picked[a.question]) if a.question in picked else a
                for a in answers
            ]
            after = forms.read(await self._applier.read_form(context))
        finally:
            self._browser.release()
        differences = forms.compare(answers, after)
        held = forms.page_answers(answers, after)
        findings = self._findings(application, held, form, job, letter_text)
        store.change_application(
            number,
            answers=forms.as_saved(answers),
            findings=[asdict(f) for f in findings],
            status=store.FILLED_IN,
        )
        store.change_job(job.number, status=store.FILLED)
        await self._sync()
        told: dict[str, Any] = {
            "filled": FILLED,
            "read_back": READ_BACK_DIFFERENT.format(what=_differences(differences))
            if differences
            else READ_BACK_SAME,
        }
        if after.challenge:
            told["check"] = CHECK_ON_THE_FORM
        blocking = [f for f in findings if f.blocking]
        if blocking:
            told["next"] = FIX_FIRST.format(what=" ".join(f.said() for f in blocking))
            return {"outcome": FILLED, "told": told}
        if after.next and not after.submit:
            return await self._went_on(context, number, job, after, told)
        application = store.application(number)
        assert application is not None
        submit = self._propose(
            context, SUBMIT, application, job, held, letter_text, findings
        )
        told["submit_action_id"] = submit.id
        told["next"] = SUBMIT_NEXT.format(action_id=submit.id)
        if self._browser.shown is False:
            told["browser"] = HIDDEN
        return {"outcome": FILLED, "told": told}

    async def _went_on(
        self,
        context: Any,
        number: int,
        job: store.Job,
        page: forms.Form,
        told: dict[str, Any],
    ) -> dict[str, Any]:
        """Goes on to the next step of a form over several, which sends nothing, and
        drafts that step in the background for the user's yes."""
        assert self._browser is not None and self._applier is not None
        assert page.next is not None
        application = store.application(number)
        assert application is not None
        self._browser.hold("going on to the form's next step")
        try:
            found = await self._applier.next_step(
                context, page.next["selector"], page.next.get("said", "")
            )
        finally:
            self._browser.release()
        moved = forms.read(found)
        if not moved.fields or [f.question for f in moved.fields] == [
            f.question for f in page.fields
        ]:
            why = "; ".join(moved.errors) or "it did not say why."
            told["next"] = NOT_ON.format(errors=why)
            return {"outcome": FILLED, "told": told}
        self._doing = f"drafting the next step of the application for {_job_named(job)}"
        drafting = self._draft_page(
            context, job, found, application.step + 1, application.first
        )
        self._working = asyncio.create_task(self._drafting(context, job, drafting))
        told["next"] = ON_TO_NEXT
        return {"outcome": WENT_ON, "told": told}

    def _files(
        self,
        application: store.Application,
        job: store.Job,
        form: forms.Form,
        letter_text: str,
    ) -> dict[str, Path]:
        """The files that may go into this form, by name: the résumé it sends, and
        the cover letter as a PDF when the form takes it as a file."""
        files = {}
        if (resume := _resume_of(application, job)) is not None:
            files[resume.name] = Path(resume.path)
        item = _letter_field(form)
        if letter_text and item is not None and item.kind == "file":
            name = str(_known().get("name") or "")
            path = letter.kept(letter_text, name, application.number)
            files[path.name] = path
        return files

    async def submit(self, context: Any, action: Action) -> dict[str, Any]:
        """Submits a filled-in application once the page still holds what the user
        approved, and reads what the page says: the second yes."""
        number = action.payload["application"]
        application = store.application(number)
        if application is None:
            raise NotDoneError(NOTHING_DRAFTED)
        job = store.job(application.job)
        assert job is not None
        if (press := application.form.get("press")) is not None:
            return await self._press(context, application, job, str(press["board"]))
        expected = forms.from_saved(action.payload["answers"])
        letter_text = str(action.payload.get("letter") or "")
        form = forms.read(application.form)
        if self._browser is None or self._applier is None or not self._browser.in_use:
            raise NotDoneError(NO_BROWSER)
        if what := self._busy():
            raise NotDoneError(BUSY.format(what=what))
        self._browser.hold("submitting an application")
        try:
            found = await self._applier.read_form(context)
            page = forms.read(found)
            if not same_page(page.url, application.url):
                raise NotDoneError(MOVED)
            if page.challenge:
                # The user has not done the check yet: once they have, they need
                # only say so, and this new draft is submitted.
                again = self._propose(
                    context, SUBMIT, application, job, expected, letter_text, []
                )
                raise NotDoneError(CHECK_FIRST.format(action_id=again.id))
            if differences := forms.compare(expected, page):
                held = forms.page_answers(expected, page)
                findings = self._findings(application, held, form, job, letter_text)
                again = self._propose(
                    context, SUBMIT, application, job, held, letter_text, findings
                )
                raise NotDoneError(
                    HAND_EDITS.format(
                        what=_differences(differences), action_id=again.id
                    )
                )
            blocking = [
                f
                for f in self._findings(application, expected, form, job, letter_text)
                if f.blocking
            ]
            if blocking:
                raise NotDoneError(
                    FIX_FIRST.format(what=" ".join(f.said() for f in blocking))
                )
            if os.getenv("ORBIT_DRY_RUN"):
                return {
                    "outcome": DRY_RUN,
                    "dry_run": True,
                    "told": {"dry_run": DRY_RUN},
                }
            if page.submit is None:
                raise NotDoneError(NO_SUBMIT)
            result = await self._applier.submit(
                context, page.submit["selector"], page.submit.get("said", ""), found
            )
        finally:
            self._browser.release()
        if result.sent:
            return await self._sent(application, job, expected, result.confirmation)
        if result.challenge:
            # Orbit watches the page while the user does the check, and tells them
            # when the application has gone through.
            self._watching = asyncio.create_task(
                self._watch(context, application.number, job, expected)
            )
            hidden = CHECK_HIDDEN if self._browser.shown is False else ""
            return {
                "outcome": "Waiting for the user",
                "told": {"next": CHECK_AFTER + hidden},
            }
        if result.errors:
            raise NotDoneError(REFUSED.format(errors="; ".join(result.errors)))
        store.change_job(job.number, status=store.NEEDS_YOU)
        await self._sync()
        return {"outcome": "No confirmation seen", "told": {"next": NO_CONFIRMATION}}

    async def _press(
        self,
        context: Any,
        application: store.Application,
        job: store.Job,
        board_key: str,
    ) -> dict[str, Any]:
        """Sends an application on a job board whose own Apply does so in one press,
        on the posting open in the browser: the user's yes to send it."""
        board = search.SITES[board_key]
        if self._browser is None or self._applier is None or not self._browser.in_use:
            raise NotDoneError(NO_BROWSER)
        if what := self._busy():
            raise NotDoneError(BUSY.format(what=what))
        self._browser.hold("submitting an application")
        try:
            found = await self._applier.read_form(context)
            if not same_page(str(found.get("url") or ""), application.url):
                raise NotDoneError(MOVED)
            if os.getenv("ORBIT_DRY_RUN"):
                return {
                    "outcome": DRY_RUN,
                    "dry_run": True,
                    "told": {"dry_run": DRY_RUN},
                }
            result = await self._applier.press_apply(context, board)
        finally:
            self._browser.release()
        if result is None:
            raise NotDoneError(NO_PRESS.format(board=board.name))
        if result.sent:
            return await self._sent(application, job, [], result.confirmation)
        if result.errors:
            raise NotDoneError(REFUSED.format(errors="; ".join(result.errors)))
        store.change_job(job.number, status=store.NEEDS_YOU)
        await self._sync()
        return {
            "outcome": "No confirmation seen",
            "told": {"next": PRESSED_NOT_SENT.format(board=board.name)},
        }

    async def _sent(
        self,
        application: store.Application,
        job: store.Job,
        answers: list[forms.Answer],
        confirmation: str,
    ) -> dict[str, Any]:
        now = time.time()
        # A form over several steps was sent as one: its record holds every step's
        # answers and its cover letter, whichever step took it.
        steps = store.steps_of(application.first)
        earlier = [step for step in steps if step.number != application.number]
        sent_answers = [a for step in earlier for a in _answers_of(step)] + answers
        for step in earlier:
            store.change_application(step.number, status=store.SENT, in_sheet=True)
        store.change_application(
            application.number,
            answers=forms.as_saved(sent_answers),
            letter=next((step.letter for step in steps if step.letter), ""),
            status=store.SENT,
            confirmation=confirmation,
            submitted_at=now,
        )
        store.change_job(
            job.number,
            status=store.SUBMITTED,
            applied_at=now,
            confirmation=confirmation,
        )
        self.sent_here += 1
        self.current = None
        await self._sync()
        limit = settings.current()["applications_per_sitting"]
        told = {
            "submitted": SENT.format(company=job.company, confirmation=confirmation),
            "next": SENT_NEXT.format(count=self.sent_here, limit=limit),
        }
        if self.sent_here == 1:
            told["follow_up"] = FOLLOW_UP
        return {"outcome": "Submitted", "told": told}

    async def _watch(
        self,
        context: Any,
        number: int,
        job: store.Job,
        answers: list[forms.Answer],
    ) -> None:
        """Watches a submitted form while the user does the check that came up when
        Submit was pressed, then has the model tell them what came of it: sent, or
        not sent and to be submitted again with their yes."""
        assert self._browser is not None and self._applier is not None
        try:
            waited = await wait_for_the_user(self._browser, context, search.A_PERSON)
            application = store.application(number)
            if application is None or application.status == store.SENT:
                return
            if waited is None:
                told = NOT_DONE_AFTER
            else:
                found = await self._applier.read_form(context)
                result = sent(waited, str(found.get("url") or ""), {}, found)
                if result.sent:
                    done = await self._sent(
                        application, job, answers, result.confirmation
                    )
                    told = " ".join(str(v) for v in done["told"].values())
                elif result.errors:
                    told = REFUSED.format(errors="; ".join(result.errors))
                elif (page := forms.read(found)).submit is not None:
                    held = forms.page_answers(answers, page)
                    again = self._propose(
                        context, SUBMIT, application, job, held, application.letter, []
                    )
                    told = DONE_NOT_SENT.format(action_id=again.id)
                else:
                    told = NO_CONFIRMATION
        except Exception:
            logger.exception("an application could not be watched")
            return
        await _tell(context, told)

    async def check(self, context: RunContext[OrbitState]) -> dict[str, Any]:
        """Reads the application on the page again, after the user did something
        there: sent, it is recorded; filled in, its page is drafted for submitting."""
        if self._watching is not None and not self._watching.done():
            self._watching.cancel()
        application, job = self._application()
        browser, applier = self._ready()
        browser.hold("reading the application")
        try:
            text = await browser.read_page(context)
            found = await applier.read_form(context)
        finally:
            browser.release()
        result = sent(text, str(found.get("url") or ""), {}, found)
        answers = _answers_of(application)
        if result.sent:
            return (await self._sent(application, job, answers, result.confirmation))[
                "told"
            ]
        if application.form.get("press") is not None and shows_applied(text):
            # A board's one-press Apply the user finished, its questions answered.
            return (await self._sent(application, job, answers, "Applied"))["told"]
        page = forms.read(found)
        if page.challenge:
            return {"next": CHALLENGE}
        if not same_page(page.url, application.url):
            raise ToolError(MOVED)
        held = forms.page_answers(answers, page)
        form = forms.read(application.form)
        findings = self._findings(application, held, form, job, application.letter)
        blocking = [f for f in findings if f.blocking]
        if blocking:
            return {"next": FIX_FIRST.format(what=" ".join(f.said() for f in blocking))}
        self.wire(context)
        if page.next and not page.submit:
            # A step the user filled in themselves: going on sends nothing.
            store.change_application(
                application.number,
                answers=forms.as_saved(held),
                status=store.FILLED_IN,
            )
            gone = await self._went_on(context, application.number, job, page, {})
            return gone["told"]
        action = self._propose(
            context, SUBMIT, application, job, held, application.letter, findings
        )
        told = {"say": SUBMIT_DRAFTED.format(action_id=action.id)}
        if differences := forms.compare(answers, page):
            told["differences"] = _differences(differences)
        return told


# What a fact about the user answers in their profile, by what a question asks.
_PROFILE_KEYS = {
    "name": "name",
    "email": "email",
    "phone": "phone",
    "city": "city",
    "linkedin": "linkedin",
    "github": "github",
    "portfolio": "portfolio",
    "company": "company",
    "title": "title",
    "years": "years",
    "notice": "notice_period",
}


def _field(form: forms.Form, number: int) -> forms.Field:
    return next(item for item in form.fields if item.number == number)


def _differences(differences: list[forms.Difference]) -> str:
    return " ".join(
        f"{d.question!r} is new on the page."
        if d.new
        else f"{d.question!r} was {d.approved!r} and the page has {d.on_page!r}."
        for d in differences
    )
