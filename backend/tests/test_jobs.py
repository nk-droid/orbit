import asyncio
import io
import json
import re
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from livekit.agents.llm import ToolError
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData
from pypdf import PdfWriter
from pypdf.annotations import Link

import agent_tools.actions as action_tools
import agent_tools.jobs as job_tools
import attachments
import storage
from actions import effects
from actions.gate import ActionGate
from agent_tools import applying, stage
from agent_tools.browser import BrowserToolset
from agent_tools.state import OrbitState
from integrations.google import gsheets
from integrations.google.api import GoogleApiError
from jobs import google as job_google
from jobs import (
    profile,
    relevance,
    scripts,
    search,
    settings,
    sheet,
    store,
    writing,
)

# What the user's own pages say, as the browser reads them.
PORTFOLIO = "https://nidhishk.vercel.app/"
PORTFOLIO_TEXT = """- Page URL: https://nidhishk.vercel.app/
### Text
# Nidhish Kumar
AI Engineer · Full-Stack AI Systems
## Skills
- Python | AI/ML, backend | 5y+
- SQL | Queries | 3y+
nidhish.22k@gmail.com
Patna, Bihar, India"""
PORTFOLIO_LINKS = [
    {"text": "Download Resume", "href": "https://nidhishk.vercel.app/resume.pdf"},
    {"text": "GitHub", "href": "https://github.com/nk-droid"},
    {"text": "LinkedIn", "href": "https://www.linkedin.com/in/nidhishkumar/"},
    {"text": "AutoPR", "href": "https://github.com/nk-droid/AutoPR"},
]
GITHUB_TEXT = "- Page URL: https://github.com/nk-droid\n### Text\nnk-droid · Nidhish Kumar\nPython"
LINKEDIN_TEXT = (
    "- Page URL: https://www.linkedin.com/in/nidhishkumar/\n### Text\nNidhish Kumar\n"
    "SDE (AI/ML) at 2Sigma School\nIndian Institute of Technology, Madras"
)
AUTHWALL = (
    "- Page URL: https://www.linkedin.com/authwall?trk=x\n### Text\nSign in to see more"
)


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Job data is kept in a throwaway folder, never the real one."""
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def shown(monkeypatch) -> list[dict]:
    cards: list[dict] = []

    async def show(card, *, push=None):
        cards.append(card)

    monkeypatch.setattr(stage, "show", show)
    return cards


def _pdf(*lines: str) -> bytes:
    """A small PDF whose one page has these lines of text."""
    text = " ".join(f"({line}) Tj 0 -16 Td" for line in lines)
    stream = f"BT /F1 12 Tf 72 720 Td {text} ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
    ).encode()
    return out


def _linked(pdf: bytes, *addresses: str) -> bytes:
    """A PDF with links over its text, as a résumé's "LinkedIn" hides an address."""
    writer = PdfWriter(clone_from=io.BytesIO(pdf))
    for at, address in enumerate(addresses):
        place = (72, 700 - 16 * at, 200, 716 - 16 * at)
        writer.add_annotation(page_number=0, annotation=Link(rect=place, url=address))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


RESUME = _pdf(
    "Nidhish Kumar",
    "nidhish.22k@gmail.com +91 98765 43210",
    "SDE (AI/ML), 2Sigma School, Oct 2024 - Present",
)


# Settings.


def test_settings_start_at_their_defaults_and_change_by_voice():
    assert settings.current() == {
        "sites": ["companies", "google", "linkedin", "cutshort"],
        "companies": [],
        "easy_apply": True,
        "pages_per_search": 3,
        "posted_within_days": 14,
        "applications_per_sitting": 10,
    }

    assert settings.change("results pages: 5") == ("pages_per_search", 5)
    assert settings.change("how recent: 7") == ("posted_within_days", 7)
    assert settings.change("Applications per sitting: 5")[1] == 5
    assert settings.current()["pages_per_search"] == 5


@pytest.mark.parametrize(
    ("said", "sites"),
    [
        (
            "sites: add naukri",
            ["companies", "google", "linkedin", "cutshort", "naukri"],
        ),
        ("sites: remove google", ["companies", "linkedin", "cutshort"]),
        ("sites: naukri", ["naukri"]),
        ("sites: only naukri", ["naukri"]),
        ("sites: linkedin and naukri", ["linkedin", "naukri"]),
        ("sites: remove company boards", ["google", "linkedin", "cutshort"]),
        ("sites: career pages and linkedin", ["companies", "linkedin"]),
        ("sites: linkedin and cut short", ["linkedin", "cutshort"]),
    ],
)
def test_the_sites_searched_change_by_voice(said, sites):
    assert settings.change(said) == ("sites", sites)


@pytest.mark.parametrize(
    ("said", "refused"),
    [
        ("pages_per_search: 50", "from 1 to 10"),
        ("pages_per_search: many", "whole number"),
        # A search reads every posting that may fit, up to the jobs one search keeps.
        ("postings_per_run: 60", "reads every posting that may fit until it has found"),
        ("jobs per search: 100", "250 jobs, the most one search keeps"),
        ("speed: 3", "no setting called 'speed'"),
        ("sites: indeed", "not one of them"),
        (
            "sites: remove linkedin, google, cutshort and companies",
            "At least one site",
        ),
        ("easy apply: perhaps", "on or off"),
        ("postings 60", "setting: value"),
    ],
)
def test_a_setting_that_cannot_be_says_why(said, refused):
    with pytest.raises(ValueError, match=refused):
        settings.change(said)


# The profile.


SOURCES = {
    "résumé": "Nidhish Kumar\nnidhish.22k@gmail.com\n+91 98765 43210",
    "portfolio": PORTFOLIO_TEXT,
}


@pytest.mark.parametrize(
    ("key", "finding", "kept"),
    [
        (
            "name",
            {"value": "Nidhish Kumar", "source": "résumé", "quote": "Nidhish Kumar"},
            True,
        ),
        # The words quoted must be in the source named.
        (
            "name",
            {"value": "Nidhish Kumar", "source": "portfolio", "quote": "Nidhish K."},
            False,
        ),
        ("city", {"value": "Patna", "source": "LinkedIn", "quote": "Patna"}, False),
        # Contact details are checked as they are written.
        (
            "email",
            {
                "value": "nidhish.22k@gmail.com",
                "source": "résumé",
                "quote": "nidhish.22k@gmail.com",
            },
            True,
        ),
        (
            "email",
            {
                "value": "nidhish22k@gmail.com",
                "source": "résumé",
                "quote": "Nidhish Kumar",
            },
            False,
        ),
        (
            "phone",
            {"value": "+91 9876543210", "source": "résumé", "quote": "+91 98765 43210"},
            True,
        ),
        (
            "phone",
            {"value": "+91 9876500000", "source": "résumé", "quote": "+91 98765 43210"},
            False,
        ),
    ],
)
def test_a_finding_is_kept_only_if_its_source_says_it(key, finding, kept):
    field = next(f for f in profile.FIELDS if f.key == key)

    assert profile.supported(field, finding, SOURCES) is kept


def test_a_link_is_checked_without_its_beginning():
    field = next(f for f in profile.FIELDS if f.key == "github")
    text = "Links:\n- GitHub: https://github.com/nk-droid"
    finding = {"value": "github.com/nk-droid", "source": "portfolio", "quote": "GitHub"}

    assert profile.supported(field, finding, {"portfolio": text})
    assert not profile.supported(
        field, {**finding, "value": "github.com/someone"}, {"portfolio": text}
    )


def test_a_link_is_an_address_not_the_word_a_resume_shows_for_it():
    text = "Nidhish Kumar\nLinkedIn | GitHub | Portfolio\nlinkedin.com/in/nidhishkumar"

    def kept(key: str, value: str) -> bool:
        field = next(f for f in profile.FIELDS if f.key == key)
        finding = {"value": value, "source": "résumé", "quote": value}
        return profile.supported(field, finding, {"résumé": text})

    assert not kept("linkedin", "LinkedIn")
    assert not kept("github", "GitHub")
    assert not kept("portfolio", "Portfolio")
    assert kept("linkedin", "linkedin.com/in/nidhishkumar")


def test_the_most_trusted_source_wins_and_the_others_are_kept():
    sources = {
        "résumé": "SDE (AI/ML)",
        "portfolio": "AI Engineer · Python · SQL",
        "GitHub": "Python, Go",
    }
    found = {
        "title": [
            {"value": "AI Engineer", "source": "portfolio", "quote": "AI Engineer"},
            {"value": "SDE (AI/ML)", "source": "résumé", "quote": "SDE (AI/ML)"},
        ],
        "skills": [
            {"value": "Python", "source": "portfolio", "quote": "Python"},
            {"value": "python", "source": "GitHub", "quote": "Python"},
            {"value": "Go", "source": "GitHub", "quote": "Go"},
        ],
    }

    known = {k.key: k for k in profile.merge(found, sources, {})}

    assert known["title"].value == "SDE (AI/ML)"
    assert known["title"].others == [{"value": "AI Engineer", "source": "portfolio"}]
    assert known["skills"].value == ["Python", "Go"]
    assert known["skills"].source == "portfolio, GitHub"


def test_what_the_user_said_outranks_every_source():
    said = store.Known("title", "Senior AI Engineer", profile.YOU, [])
    found = {"title": [{"value": "SDE (AI/ML)", "source": "résumé", "quote": "SDE"}]}

    (known,) = profile.merge(found, {"résumé": "SDE (AI/ML)"}, {"title": said})

    assert known == said


def test_the_profile_changes_as_the_user_says():
    profile.change("notice_period: 30 days")
    profile.change("skills: Python, SQL")
    profile.change("skills: add Kubernetes, python")
    profile.change("skills: remove sql")
    profile.change("Current role: Senior AI Engineer")

    known = store.profile()
    assert known["notice_period"].value == "30 days"
    assert known["notice_period"].source == profile.YOU
    assert known["skills"].value == ["Python", "Kubernetes"]
    assert known["title"].value == "Senior AI Engineer"


@pytest.mark.parametrize(
    ("said", "refused"),
    [
        ("shoe size: 9", "no field called 'shoe size'"),
        ("notice_period:", "Say what Notice period is"),
        ("notice period 30 days", "field: value"),
    ],
)
def test_a_change_that_cannot_be_says_why(said, refused):
    with pytest.raises(ValueError, match=refused):
        profile.change(said)


async def test_the_profile_is_built_from_what_its_sources_say(monkeypatch):
    async def ask(instructions, data, think="high"):
        given = json.loads(data)
        assert set(given["sources"]) == {"résumé", "portfolio"}
        return json.dumps(
            {
                "fields": {
                    "name": [
                        {
                            "value": "Nidhish Kumar",
                            "source": "résumé",
                            "quote": "Nidhish Kumar",
                        }
                    ],
                    "email": [
                        {
                            "value": "nidhish.22k@gmail.com",
                            "source": "portfolio",
                            "quote": "nidhish.22k@gmail.com",
                        }
                    ],
                    # Not on the page named: left out.
                    "city": [
                        {
                            "value": "Bengaluru",
                            "source": "portfolio",
                            "quote": "Bengaluru",
                        }
                    ],
                    "not_a_field": [{"value": "x", "source": "résumé", "quote": "x"}],
                }
            }
        )

    monkeypatch.setattr(writing, "ask", ask)

    built = await profile.build(
        {"résumé": SOURCES["résumé"], "portfolio": PORTFOLIO_TEXT}
    )

    known = store.profile()
    assert known["name"].value == "Nidhish Kumar"
    assert known["email"].source == "portfolio"
    assert "city" not in known
    assert built.filled == 2
    assert "City" in built.missing and "Name" not in built.missing


def test_a_resume_is_kept_with_its_text_beside_the_others():
    kept = profile.keep_resume("Nidhish_Kumar_Resume.pdf", RESUME, "from your computer")

    assert kept.name == "Nidhish_Kumar_Resume.pdf"
    assert "SDE (AI/ML), 2Sigma School" in kept.text
    assert Path(kept.path).read_bytes() == RESUME
    # Another joins it, and one of the same name takes its place.
    other = profile.keep_resume("CV.pdf", RESUME, "from your computer")
    again = profile.keep_resume("Nidhish_Kumar_Resume.pdf", RESUME, "my portfolio")
    assert [resume.name for resume in store.resumes()] == [
        "CV.pdf",
        "Nidhish_Kumar_Resume.pdf",
    ]
    assert again.source == "my portfolio" and again.path == kept.path
    assert Path(kept.path).exists() and Path(other.path).exists()


async def test_a_resume_for_each_kind_of_role_is_kept_and_told_apart(shown):
    for name in ("resume-backend.pdf", "resume-ai-engineer.pdf"):
        attachments.keep(
            attachments.TRAY, name, "application/pdf", RESUME, "from your computer"
        )
    tools = job_tools.JobsToolset(None)

    told = await tools._job_profile(
        _talk("Keep both: the second is for AI Engineer roles"),
        resumes=["resume-backend.pdf", "resume-ai-engineer.pdf"],
        resume_roles=["resume-ai-engineer.pdf: AI Engineer"],
    )

    assert len(told["resumes_kept"]) == 2
    assert told["resumes_for"] == ["resume-ai-engineer.pdf: AI Engineer"]
    # One is for the work its name says, the other for what the user said.
    assert [(r["name"], r["role"]) for r in profile.card()["resumes"]] == [
        ("resume-backend.pdf", "backend"),
        ("resume-ai-engineer.pdf", "AI Engineer"),
    ]
    told = await tools._job_profile(
        _talk("Drop the backend one"), forget_resumes=["backend"]
    )
    assert told["resumes_forgotten"] == ["resume-backend.pdf"]
    assert [resume.name for resume in store.resumes()] == ["resume-ai-engineer.pdf"]
    with pytest.raises(ToolError, match="no résumé called 'design'"):
        await tools._job_profile(_talk("x"), forget_resumes=["design"])


def test_an_application_drafted_before_sends_the_resume_its_draft_named():
    # As applications were kept before each named the résumé it sends.
    before = store._APPLICATIONS.replace(",\n    resume TEXT NOT NULL DEFAULT ''", "")
    assert "resume" not in before
    with storage.connect(before) as connection:
        connection.execute(
            "INSERT INTO job_applications (job, step, first, url, form, answers, "
            "letter, findings, status, confirmation, started_at, updated_at, "
            "in_sheet) VALUES (1, 1, 1, 'https://x', '{}', ?, '', '[]', 'drafted', "
            "'', 1, 1, 0)",
            (json.dumps([{"asks": "resume", "value": "resume-ai-engineer.pdf"}]),),
        )

    assert store.application(1).resume == "resume-ai-engineer.pdf"


def test_a_job_found_before_gains_the_experience_its_posting_asks_for():
    # As jobs and Orbit jobs were kept before Orbit noted the experience a job asks
    # for, and the Sheet's columns.
    before = store._JOBS.replace("    experience TEXT NOT NULL DEFAULT '',\n", "")
    book = store._BOOK.replace(",\n    columns TEXT NOT NULL DEFAULT '[]'", "")
    assert "experience" not in before and "columns" not in book
    found = (
        # Its address says it.
        (
            "https://www.naukri.com/job-listings-ai-engineer-acme-bengaluru-"
            "2-to-6-years-191026914436",
            "",
            "",
        ),
        # A quote that it fits says it.
        ("https://x/2", "Python; 3+ years of experience with LLMs", ""),
        # Its page does, under a heading.
        (
            "https://x/3",
            "Python",
            "- Page URL: https://x/3\n### Text\nExperience\n4-6 Yrs",
        ),
        ("https://x/4", "Python", "- Page URL: https://x/4\n### Text\nPython"),
    )
    with storage.connect(before) as connection:
        connection.execute(book)
        for number, (url, why, posting) in enumerate(found, start=1):
            connection.execute(
                "INSERT INTO jobs (number, key, site, url, apply_url, applies_on, "
                "company, title, location, remote, posted, salary, fit, why, posting, "
                "search, status, apply, notes, confirmation, in_sheet, found_at, "
                "updated_at) VALUES (?, ?, 'Naukri', ?, '', 'company', 'Acme', "
                "'AI Engineer', 'Bengaluru', '', '', '', 'high', ?, ?, 1, 'Found', 0, "
                "'', '', 1, 1, 1)",
                (number, f"naukri:{number}", url, why, posting),
            )
        connection.execute(
            "INSERT INTO job_book VALUES (1, 'book', 'Orbit jobs', 'https://x', 0, 1, 2)"
        )

    assert [store.job(number).experience for number in (1, 2, 3, 4)] == [
        "2\u20136 years",
        "3+ years",
        "4\u20136 years",
        "",
    ]
    assert store.book().columns == ()


def test_the_one_resume_kept_before_joins_the_others():
    # As Orbit kept its one résumé before it held several.
    with storage.connect(store._RESUME) as connection:
        connection.execute(
            "INSERT INTO job_resume (one, name, path, source, text, kept_at) "
            "VALUES (1, 'CV.pdf', '/resumes/CV.pdf', 'from your computer', 'Python', 1)"
        )

    (moved,) = store.resumes()

    assert (moved.name, moved.text, moved.role) == ("CV.pdf", "Python", "")
    with storage.connect(store._RESUME) as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_resume").fetchone()[0] == 0


def test_the_addresses_behind_a_resumes_links_are_kept_with_its_text():
    resume = _linked(
        _pdf("Nidhish Kumar", "LinkedIn | GitHub | Portfolio"),
        "https://www.linkedin.com/in/nidhishkumar/",
        "https://github.com/nk-droid",
        "https://www.linkedin.com/in/nidhishkumar/",
    )

    kept = profile.keep_resume("CV.pdf", resume, "from your computer")

    assert "LinkedIn | GitHub | Portfolio" in kept.text
    assert kept.text.count("https://www.linkedin.com/in/nidhishkumar/") == 1
    assert "https://github.com/nk-droid" in kept.text


@pytest.mark.parametrize(
    ("name", "data", "refused"),
    [
        ("resume.docx", b"PK", "hand over a PDF"),
        ("resume.pdf", b"not a pdf", "could not be read"),
        ("resume.pdf", _pdf(), "no text"),
        # Links alone are no text to draw on.
        ("resume.pdf", _linked(_pdf(), "https://github.com/nk-droid"), "no text"),
    ],
)
def test_a_resume_that_cannot_be_read_says_why(name, data, refused):
    with pytest.raises(ValueError, match=refused):
        profile.keep_resume(name, data, "from your computer")


# The tools.


class FakeBrowser:
    """The browser in use, as the job tools see it: pages by address."""

    def __init__(self, pages: dict[str, tuple[str, list]]) -> None:
        self.pages = pages
        self.opened: list[str] = []
        self.in_use = True

    async def open_page(self, context, url):
        self.opened.append(url)
        return self.pages[url][0]

    async def run_script(self, context, function):
        return self.pages[self.opened[-1]][1]


def _talk(*said: str) -> SimpleNamespace:
    messages = [
        SimpleNamespace(type="message", role="user", text_content=t) for t in said
    ]
    return SimpleNamespace(
        session=SimpleNamespace(history=SimpleNamespace(items=messages)),
        userdata=OrbitState(gate=ActionGate()),
    )


@pytest.fixture
def model(monkeypatch) -> list[dict]:
    """The drafting model, standing in: it finds the name and title in each source."""
    asked: list[dict] = []

    async def ask(instructions, data, think="high"):
        given = json.loads(data)
        asked.append(given)
        fields: dict[str, list] = {"name": [], "title": []}
        for source, text in given["sources"].items():
            if "Nidhish Kumar" in text:
                fields["name"].append(
                    {
                        "value": "Nidhish Kumar",
                        "source": source,
                        "quote": "Nidhish Kumar",
                    }
                )
            if "SDE (AI/ML)" in text:
                fields["title"].append(
                    {"value": "SDE (AI/ML)", "source": source, "quote": "SDE (AI/ML)"}
                )
        return json.dumps({"fields": fields})

    monkeypatch.setattr(writing, "ask", ask)
    return asked


def _browser(linkedin: str = LINKEDIN_TEXT) -> FakeBrowser:
    return FakeBrowser(
        {
            PORTFOLIO: (PORTFOLIO_TEXT, PORTFOLIO_LINKS),
            "https://github.com/nk-droid": (GITHUB_TEXT, []),
            "https://www.linkedin.com/in/nidhishkumar/": (linkedin, []),
        }
    )


async def test_the_profile_is_built_from_the_users_pages_and_their_links(shown, model):
    browser = _browser()
    tools = job_tools.JobsToolset(
        browser, "You remember: the user wants remote AI roles."
    )
    talk = _talk("Help me apply for jobs. My portfolio is nidhishk.vercel.app")

    told = await tools._job_profile(talk, build=True, pages=[PORTFOLIO])

    # GitHub and LinkedIn are found on the portfolio and read too. A project is not.
    assert browser.opened == [
        PORTFOLIO,
        "https://github.com/nk-droid",
        "https://www.linkedin.com/in/nidhishkumar/",
    ]
    assert set(model[0]["sources"]) == {"portfolio", "GitHub", "LinkedIn", "memory"}
    # The page's links are given with it, so links found on it can be checked.
    assert "https://github.com/nk-droid" in model[0]["sources"]["portfolio"]
    assert told["known"]["title"] == "SDE (AI/ML)"
    assert store.profile()["name"].source == "LinkedIn"
    assert shown[-1]["kind"] == "profile"


async def test_linkedin_behind_a_sign_in_is_left_out_and_said(shown, model):
    tools = job_tools.JobsToolset(_browser(AUTHWALL))

    told = await tools._job_profile(
        _talk("My portfolio is nidhishk.vercel.app"), build=True, pages=[PORTFOLIO]
    )

    assert set(model[0]["sources"]) == {"portfolio", "GitHub"}
    assert any("signed in to LinkedIn" in note for note in told["notes"])


async def test_a_page_nobody_gave_is_not_opened(shown, model):
    browser = _browser()
    tools = job_tools.JobsToolset(browser)

    with pytest.raises(ToolError, match="ask them for the address"):
        await tools._job_profile(
            _talk("Help me with jobs"), build=True, pages=[PORTFOLIO]
        )
    assert browser.opened == []


async def test_pages_need_a_browser_started(shown, model):
    browser = _browser()
    browser.in_use = False
    tools = job_tools.JobsToolset(browser)

    with pytest.raises(ToolError, match="which of their Chrome profiles"):
        await tools._job_profile(
            _talk("nidhishk.vercel.app"), build=True, pages=[PORTFOLIO]
        )


async def test_the_resume_is_taken_from_the_portfolio(shown, monkeypatch):
    fetched = []

    async def download(url):
        fetched.append(url)
        return RESUME

    monkeypatch.setattr(job_tools, "_download", download)
    tools = job_tools.JobsToolset(_browser())

    told = await tools._job_profile(
        _talk("Use the resume on nidhishk.vercel.app"),
        resumes=["portfolio"],
        pages=[PORTFOLIO],
    )

    assert fetched == ["https://nidhishk.vercel.app/resume.pdf"]
    assert told["resumes_kept"] == [
        "resume.pdf, https://nidhishk.vercel.app/resume.pdf"
    ]
    assert "2Sigma School" in store.resume("resume.pdf").text


async def test_the_resume_is_taken_from_a_file_the_user_handed_over(shown):
    attachments.keep(
        attachments.TRAY,
        "Nidhish_CV.pdf",
        "application/pdf",
        RESUME,
        "from your computer",
    )
    tools = job_tools.JobsToolset(None)

    told = await tools._job_profile(_talk("Use my CV"), resumes=["nidhish_cv"])

    assert told["resumes_kept"] == ["Nidhish_CV.pdf, from your computer"]
    with pytest.raises(ToolError, match="no file called 'cover'"):
        await tools._job_profile(_talk("x"), resumes=["cover"])


async def test_changes_by_voice_go_into_the_profile(shown):
    tools = job_tools.JobsToolset(None)

    told = await tools._job_profile(
        _talk("My notice period is 30 days"), change=["notice_period: 30 days"]
    )

    assert told["changed"] == ["notice_period: 30 days"]
    assert told["known"]["notice_period"] == "30 days"
    assert "Notice period" not in told["missing"]
    with pytest.raises(ToolError, match="no field called"):
        await tools._job_profile(_talk("x"), change=["shoe size: 9"])


async def test_settings_change_by_voice(shown):
    tools = job_tools.JobsToolset(None)

    told = await tools._job_settings(
        _talk("Only jobs from the last week"), change=["posted_within_days: 7"]
    )

    assert told["changed"] == {"posted_within_days": 7}
    assert told["settings"]["posted_within_days"] == 7
    with pytest.raises(ToolError, match="from 1 to 90"):
        await tools._job_settings(_talk("x"), change=["posted_within_days: 365"])


def test_the_api_gives_the_profile_its_card_fetches(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LIVEKIT_URL", "wss://example")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret")
    from api.app import app

    profile.change("notice_period: 30 days")
    card = TestClient(app).get("/api/jobs/profile").json()

    fields = {f["key"]: f for group in card["groups"] for f in group["fields"]}
    assert fields["notice_period"]["value"] == "30 days"
    assert fields["notice_period"]["source"] == "you"
    assert fields["name"]["value"] is None and fields["name"]["needed"]
    assert card["resumes"] == []
    assert {s["key"] for s in card["settings"]} >= {"sites", "pages_per_search"}


# Orbit jobs, the Sheet.


def _in_sheets_order(row: list, column: int) -> tuple:
    """Where a row goes when Sheets sorts by a column: empty cells last."""
    value = row[column] if column < len(row) else ""
    blank = value in ("", None)
    return (blank, "" if blank else type(value).__name__, value or 0)


class FakeGoogle:
    """Stands in for Google Sheets: one spreadsheet, its tabs and their cells."""

    def __init__(self) -> None:
        self.tabs: dict[int, dict] = {}
        self.made = 0
        self.gone = False

    async def create(self, session, title, columns, rows):
        self.made += 1
        self.tabs = {0: {"title": "Sheet1", "rows": [list(columns), *rows]}}
        return {
            "spreadsheet_id": "book",
            "title": title,
            "link": "https://docs.google.com/spreadsheets/d/book/edit",
        }

    async def spreadsheet(self, session, spreadsheet_id):
        if self.gone:
            raise GoogleApiError(404, "Requested entity was not found.")
        tabs = [
            {"id": tab_id, "title": tab["title"]} for tab_id, tab in self.tabs.items()
        ]
        return {"id": spreadsheet_id, "title": "Orbit jobs", "tabs": tabs}

    def tab(self, title: str) -> dict:
        return next(tab for tab in self.tabs.values() if tab["title"] == title)

    async def table(self, session, spreadsheet_id, title):
        rows = [list(row) for row in self.tab(title)["rows"]]
        for row in rows:
            while row and row[-1] in ("", None):
                row.pop()
        # Google leaves out the empty rows at the end.
        while rows and not rows[-1]:
            rows.pop()
        return rows

    async def update(self, session, spreadsheet_id, requests):
        for request in requests:
            if "updateSheetProperties" in request:
                properties = request["updateSheetProperties"]["properties"]
                self.tabs[properties["sheetId"]]["title"] = properties["title"]
            elif "addSheet" in request:
                properties = request["addSheet"]["properties"]
                self.tabs[properties["sheetId"]] = {
                    "title": properties["title"],
                    "rows": [],
                }
            elif "updateCells" in request:
                cells = request["updateCells"]
                values = [
                    c["userEnteredValue"]["stringValue"]
                    for c in cells["rows"][0]["values"]
                ]
                rows = self.tabs[cells["start"]["sheetId"]]["rows"]
                first = cells["start"]["columnIndex"]
                if first == 0:
                    rows[:1] = [values]
                else:
                    rows[0][first : first + len(values)] = values
            elif "insertDimension" in request:
                span = request["insertDimension"]["range"]
                rows = self.tabs[span["sheetId"]]["rows"]
                start, count = span["startIndex"], span["endIndex"] - span["startIndex"]
                if span["dimension"] == "COLUMNS":
                    for row in rows:
                        row[start:start] = [""] * count if len(row) >= start else []
                else:
                    rows[start:start] = [[] for _ in range(count)]
            elif "sortRange" in request:
                span = request["sortRange"]["range"]
                rows = self.tabs[span["sheetId"]]["rows"]
                column = request["sortRange"]["sortSpecs"][0]["dimensionIndex"]
                top, end = span["startRowIndex"], span["endRowIndex"]
                rows[top:end] = sorted(
                    rows[top:end], key=lambda row, at=column: _in_sheets_order(row, at)
                )

    async def write(self, session, spreadsheet_id, data):
        for item in data:
            found = re.fullmatch(
                r"'(.+)'!([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?", item["range"]
            )
            rows = self.tab(found[1].replace("''", "'"))["rows"]
            top, left = int(found[3]) - 1, gsheets.column_index(found[2])
            for r, values in enumerate(item["values"]):
                while len(rows) <= top + r:
                    rows.append([])
                row = rows[top + r]
                for c, value in enumerate(values):
                    while len(row) <= left + c:
                        row.append("")
                    row[left + c] = value

    def cell(self, title: str, number: int, column: str):
        rows = self.tab(title)["rows"]
        at = rows[0].index(column)
        row = next(r for r in rows[1:] if r and r[rows[0].index("No.")] == number)
        return row[at] if at < len(row) else ""

    def set(self, title: str, number: int, column: str, value) -> None:
        rows = self.tab(title)["rows"]
        at = rows[0].index(column)
        row = next(r for r in rows[1:] if r and r[rows[0].index("No.")] == number)
        while len(row) <= at:
            row.append("")
        row[at] = value


@pytest.fixture
def google(monkeypatch) -> FakeGoogle:
    fake = FakeGoogle()
    for name in ("create", "spreadsheet", "table", "update", "write"):
        monkeypatch.setattr(gsheets, name, getattr(fake, name))
    monkeypatch.setattr(job_tools, "_http", lambda: None)
    monkeypatch.setattr(action_tools, "_require_write", lambda service: None)
    return fake


def _found(
    company: str, title: str, fit: str = "high", applies_on: str = "company", **more
):
    return store.add_job(
        key=f"{company}|{title}".casefold(),
        site="LinkedIn",
        url=f"https://www.linkedin.com/jobs/view/{abs(hash(company)) % 10**8}/",
        company=company,
        title=title,
        location="Bengaluru",
        fit=fit,
        why=f"{title} asks for Python",
        search=1,
        apply=fit != "low" and applies_on == "company",
        applies_on=applies_on,
        **more,
    )


async def test_orbit_jobs_is_made_with_its_three_tabs(google):
    book = await sheet.make_book(None)

    assert [tab["title"] for tab in google.tabs.values()] == [
        "Jobs",
        "Searches",
        "Applications",
    ]
    assert google.tab("Jobs")["rows"][0] == list(sheet.JOB_COLUMNS)
    assert google.tab("Searches")["rows"][0] == list(sheet.SEARCH_COLUMNS)
    assert store.book() == book
    assert sheet.link(book).endswith(f"#gid={book.jobs_tab}")


async def test_jobs_go_into_the_sheet_in_order_once(google):
    await sheet.make_book(None)
    _found("Acme", "Backend Engineer")
    _found("Globex", "AI Engineer", fit="low")

    synced = await sheet.sync(None)
    again = await sheet.sync(None)

    rows = google.tab("Jobs")["rows"]
    assert [row[2] for row in rows[1:]] == ["Acme", "Globex"]
    assert google.cell("Jobs", 1, "Apply") is True
    assert google.cell("Jobs", 2, "Apply") is False
    assert google.cell("Jobs", 1, "Status") == store.FOUND
    assert (synced.added, again.added) == (2, 0)
    assert all(job.in_sheet for job in store.jobs())


async def test_a_column_orbit_came_to_write_goes_into_an_older_sheet_once(google):
    await sheet.make_book(None)
    # Orbit jobs as Orbit made it before it wrote the experience a job asks for,
    # with a job in it and the user's note beside it.
    rows = google.tab("Jobs")["rows"]
    rows[0] = list(sheet.FIRST_JOB_COLUMNS)
    store.keep_book(replace(store.book(), columns=()))
    acme = _found("Acme", "Backend Engineer", experience="3+ years")
    older = sheet.Tab("Jobs", {sheet._key(name): at for at, name in enumerate(rows[0])})
    rows.append(older.laid_out(sheet.job_row(acme)))
    store.change_job(acme.number, in_sheet=True)
    google.set("Jobs", 1, "Notes", "Ask about the visa")

    await sheet.sync(None)
    _found("Globex", "AI Engineer", experience="2\u20135 years")
    await sheet.sync(None)

    header = google.tab("Jobs")["rows"][0]
    assert header == list(sheet.JOB_COLUMNS)
    assert google.cell("Jobs", 1, "Experience") == "3+ years"
    assert google.cell("Jobs", 1, "Notes") == "Ask about the visa"
    assert google.cell("Jobs", 1, "Posted") == ""
    assert google.cell("Jobs", 2, "Experience") == "2\u20135 years"
    assert store.book().columns == sheet.JOB_COLUMNS

    # A column the user deletes stays deleted.
    at = header.index("Experience")
    for row in google.tab("Jobs")["rows"]:
        del row[at : at + 1]
    _found("Initech", "Backend Engineer", experience="4+ years")
    await sheet.sync(None)

    assert "Experience" not in google.tab("Jobs")["rows"][0]
    assert _numbers(google, "Jobs") == [1, 2, 3]


def _searched(site: str) -> None:
    number = store.start_search(site, "Backend Engineer in Bengaluru")
    store.finish_search(number, read=3, kept=1, left_out={})


def _numbers(google: FakeGoogle, title: str) -> list[int]:
    return [row[0] for row in google.tab(title)["rows"][1:]]


async def test_new_rows_go_in_at_the_end_and_the_rest_stay_put(google):
    await sheet.make_book(None)
    for company in ("Acme", "Globex"):
        _found(company, "Backend Engineer")
    _searched("LinkedIn")
    await sheet.sync(None)
    google.set("Jobs", 1, "Notes", "Ask about the visa")
    _found("Initech", "Backend Engineer")
    _searched("Google")
    store.change_job(1, status=store.SUBMITTED)

    synced = await sheet.sync(None)

    assert _numbers(google, "Jobs") == [1, 2, 3]
    assert _numbers(google, "Searches") == [1, 2]
    # What Orbit writes again lands on the job's own row, the user's notes beside it.
    assert google.cell("Jobs", 1, "Status") == store.SUBMITTED
    assert google.cell("Jobs", 1, "Notes") == "Ask about the visa"
    assert synced.order == [1, 2, 3]


async def test_new_jobs_go_under_the_last_job_not_under_empty_tick_boxes(google):
    await sheet.make_book(None)
    _found("Acme", "Backend Engineer")
    await sheet.sync(None)
    # Google reads an empty tick box as false, so the rows of empty tick boxes the
    # Jobs tab is made with come back as rows too.
    apply = sheet.JOB_COLUMNS.index("Apply")
    google.tab("Jobs")["rows"] += [[""] * apply + [False] for _ in range(5)]
    _found("Globex", "Backend Engineer")

    await sheet.sync(None)

    rows = google.tab("Jobs")["rows"]
    assert [row[0] for row in rows[1:3]] == [1, 2]
    assert len(rows) == 8


async def test_a_sheet_once_written_newest_first_is_put_in_order(google):
    await sheet.make_book(None)
    for company in ("Acme", "Globex"):
        _found(company, "Backend Engineer")
    for site in ("LinkedIn", "Google"):
        _searched(site)
    await sheet.sync(None)
    # As Orbit used to write them: newest first.
    for title in ("Jobs", "Searches"):
        rows = google.tab(title)["rows"]
        rows[1:] = rows[1:][::-1]
    google.set("Jobs", 2, "Notes", "Ask about the visa")
    _found("Initech", "Backend Engineer")
    _searched("Naukri")

    await sheet.sync(None)

    assert _numbers(google, "Jobs") == [1, 2, 3]
    assert _numbers(google, "Searches") == [1, 2, 3]
    # Each row moved whole, the user's notes with it.
    assert google.cell("Jobs", 2, "Notes") == "Ask about the visa"


async def test_rows_the_user_sorted_their_own_way_stay_so(google):
    await sheet.make_book(None)
    for company in ("Acme", "Globex", "Initech"):
        _found(company, "Backend Engineer")
    await sheet.sync(None)
    rows = google.tab("Jobs")["rows"]
    rows[1:] = [rows[2], rows[1], rows[3]]
    _found("Hooli", "Backend Engineer")

    synced = await sheet.sync(None)

    assert _numbers(google, "Jobs") == [2, 1, 3, 4]
    assert synced.order == [2, 1, 3, 4]


async def test_the_users_ticks_notes_and_deletions_are_read_back(google):
    await sheet.make_book(None)
    for company in ("Acme", "Globex", "Initech"):
        _found(company, "Backend Engineer")
    await sheet.sync(None)
    google.set("Jobs", 1, "Apply", False)
    google.set("Jobs", 2, "Notes", "Ask about the visa")
    rows = google.tab("Jobs")["rows"]
    rows[:] = [row for row in rows if not row or row[0] != 3]

    synced = await sheet.sync(None)

    assert store.job(1).apply is False
    assert store.job(2).notes == "Ask about the visa"
    assert store.job(3).status == store.SKIPPED
    assert synced.deleted == [3]
    # A deleted row is not written back.
    assert not any(row and row[0] == 3 for row in google.tab("Jobs")["rows"])


async def test_only_orbits_changing_columns_are_written_again(google):
    await sheet.make_book(None)
    _found("Acme", "Backend Engineer")
    await sheet.sync(None)
    # The user corrects the company's name; Orbit applies to the job.
    google.set("Jobs", 1, "Company", "Acme Labs")
    store.change_job(1, status=store.SUBMITTED, confirmation="Thanks for applying")

    await sheet.sync(None)

    assert google.cell("Jobs", 1, "Company") == "Acme Labs"
    assert google.cell("Jobs", 1, "Status") == store.SUBMITTED
    assert google.cell("Jobs", 1, "Confirmation") == "Thanks for applying"


async def test_new_rows_follow_columns_the_user_moved(google):
    await sheet.make_book(None)
    header = google.tab("Jobs")["rows"][0]
    header[2], header[3] = header[3], header[2]  # Role before Company
    header.append("My rating")

    _found("Acme", "Backend Engineer")
    await sheet.sync(None)

    assert google.cell("Jobs", 1, "Company") == "Acme"
    assert google.cell("Jobs", 1, "Role") == "Backend Engineer"


async def test_changes_by_voice_go_into_the_sheet(google):
    await sheet.make_book(None)
    for company in ("Acme", "Globex", "Initech"):
        _found(company, "Backend Engineer")
    await sheet.sync(None)

    await sheet.sync(None, ["untick 1", "note 2: ask about the visa", "skip 3"])

    assert google.cell("Jobs", 1, "Apply") is False
    assert google.cell("Jobs", 2, "Notes") == "ask about the visa"
    assert google.cell("Jobs", 3, "Status") == store.SKIPPED
    assert store.job(1).apply is False and store.job(3).status == store.SKIPPED


@pytest.mark.parametrize(
    ("said", "refused"),
    [
        (["drop the first one"], "by the job's number"),
        (["untick 9"], "no job number 9"),
    ],
)
async def test_a_change_to_the_list_that_cannot_be_says_why(google, said, refused):
    await sheet.make_book(None)
    _found("Acme", "Backend Engineer")

    with pytest.raises(ValueError, match=refused):
        await sheet.sync(None, said)


async def test_each_search_is_recorded_in_its_tab(google):
    await sheet.make_book(None)
    number = store.start_search("LinkedIn", "Backend Engineer in Bengaluru")
    store.finish_search(
        number, read=40, kept=12, left_out={"too old": 9, "did not fit": 19}
    )

    await sheet.sync(None)

    row = google.tab("Searches")["rows"][1]
    assert row[2:] == [
        "LinkedIn",
        "Backend Engineer in Bengaluru",
        40,
        12,
        "9 too old, 19 did not fit",
    ]


async def test_orbit_jobs_deleted_by_the_user_is_said(google):
    await sheet.make_book(None)
    google.gone = True

    with pytest.raises(sheet.GoneError):
        await sheet.sync(None)


async def test_the_list_needs_jobs_then_orbit_jobs_made_with_a_yes(google, shown):
    tools = job_tools.JobsToolset(None)
    talk = _talk("Show me the jobs")

    with pytest.raises(ToolError, match="No jobs have been found"):
        await tools._jobs_list(talk)
    _found("Acme", "Backend Engineer")
    told = await tools._jobs_list(talk)

    assert told["saving"]["draft"]["where"].startswith("Orbit jobs")
    action = talk.userdata.gate.awaiting()
    assert action.kind == "jobs.make_book"
    result = await effects._make_jobs_book(None, action.payload)
    assert result["spreadsheet"] == "Orbit jobs"
    assert google.cell("Jobs", 1, "Company") == "Acme"


async def test_the_list_is_read_back_and_shown(google, shown):
    await sheet.make_book(None)
    _found("Acme", "Backend Engineer")
    _found("Globex", "AI Engineer")
    await sheet.sync(None)
    google.set("Jobs", 2, "Apply", False)
    tools = job_tools.JobsToolset(None)

    told = await tools._jobs_list(
        _talk("Show me the jobs"), change=["note 1: great team"]
    )

    assert told["jobs"] == 2 and told["ticked_to_apply"] == 1
    assert [job["no"] for job in told["listed"]] == [2, 1]
    assert google.cell("Jobs", 1, "Notes") == "great team"
    assert shown[-1]["kind"] == "sheet" and shown[-1]["tab"] == "Jobs"


async def test_a_list_whose_sheet_is_gone_is_made_again_with_a_yes(google, shown):
    await sheet.make_book(None)
    _found("Acme", "Backend Engineer")
    google.gone = True

    told = await job_tools.JobsToolset(None)._jobs_list(_talk("Show me the jobs"))

    assert "no longer in the user's Drive" in told["gone"]
    assert store.book() is None and not store.job(1).in_sheet


# Searching.


def test_each_site_is_searched_at_its_own_addresses():
    linkedin, naukri = search.SITES["linkedin"], search.SITES["naukri"]

    first = urlsplit(linkedin.search("Backend Engineer", "Bengaluru", 14, 1))
    assert parse_qs(first.query) == {
        "keywords": ["Backend Engineer"],
        "f_TPR": ["r1209600"],
        "location": ["Bengaluru"],
    }
    remote = parse_qs(urlsplit(linkedin.search("AI Engineer", "Remote", 7, 3)).query)
    assert remote["f_WT"] == ["2"] and remote["start"] == ["50"]
    assert naukri.search("Backend Engineer", "Bengaluru", 15, 2) == (
        "https://www.naukri.com/backend-engineer-jobs-in-bengaluru-2?jobAge=15"
    )
    assert "wfhType=2" in naukri.search("AI Engineer", "remote", 7, 1)


def test_all_of_india_is_searched_as_india_not_as_remote():
    linkedin, naukri = search.SITES["linkedin"], search.SITES["naukri"]

    asked = parse_qs(
        urlsplit(linkedin.search("AI Engineer", "Anywhere in India", 7, 1)).query
    )
    assert asked["location"] == ["India"] and "f_WT" not in asked
    assert naukri.search("AI Engineer", "Pan India", 7, 2) == (
        "https://www.naukri.com/ai-engineer-jobs-2?jobAge=7"
    )
    query = job_google.query(
        job_google.BOARDS,
        "AI Engineer",
        "Anywhere in India",
        {},
        14,
        date(2026, 10, 19),
    )
    assert ' "india" (site:' in query and "remote" not in query
    # Anywhere at all is remote work still.
    assert search.is_remote("Anywhere") and not search.is_remote("Anywhere in India")


@pytest.mark.parametrize(
    ("role", "place", "address"),
    [
        # Cutshort lists jobs by field, not by title.
        (
            "AI Engineer",
            "Bengaluru",
            "artificial-intelligence-ai-jobs-in-bangalore-bengaluru",
        ),
        (
            "LLM Engineer",
            "Bangalore, India",
            "artificial-intelligence-ai-jobs-in-bangalore-bengaluru",
        ),
        (
            "Machine Learning Engineer",
            "Gurgaon",
            "machine-learning-ml-jobs-in-delhi-ncr-gurgaon-noida",
        ),
        ("Senior Backend Developer", "Pune", "backend-developer-jobs-in-pune"),
        # A field Orbit does not know is searched in the role's own words.
        ("Senior Graphic Designer", "Mumbai", "graphic-designer-jobs-in-mumbai"),
        # Remote, where Cutshort has a page of the field's remote jobs, and else
        # across India.
        ("ML Engineer", "Remote", "remote-machine-learning-ml-jobs"),
        ("Backend Engineer", "remote", "backend-developer-jobs"),
        # All of India, and a city Cutshort has no page for, across India.
        ("AI Engineer", "Anywhere in India", "artificial-intelligence-ai-jobs"),
        ("AI Engineer", "Bhopal", "artificial-intelligence-ai-jobs"),
    ],
)
def test_cutshort_is_searched_by_field_and_city(role, place, address):
    assert search.SITES["cutshort"].search(role, place, 14, 1) == (
        f"https://cutshort.io/jobs/{address}"
    )


@pytest.mark.parametrize(
    ("url", "job"),
    [
        (
            "https://in.linkedin.com/jobs/view/senior-backend-engineer-at-eltropy-4478477143?position=1",
            "4478477143",
        ),
        ("https://www.linkedin.com/jobs/view/4478477143/?refId=x", "4478477143"),
        (
            "https://www.naukri.com/job-listings-backend-engineer-acme-bengaluru-3-to-5-years-151024500123?src=x",
            "151024500123",
        ),
        (
            "https://cutshort.io/job/Backend-Engineer-Node-JS-Pune-Acme-bGJge8jM?ref=x",
            "bGJge8jM",
        ),
    ],
)
def test_a_postings_address_gives_its_id(url, job):
    matched = [
        re.search(site.posting, url)
        for site in search.SITES.values()
        if re.search(site.posting, url)
    ]
    assert matched[0][1] == job


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (
            [
                "Senior Backend Engineer",
                "Eltropy",
                "Bengaluru, Karnataka, India",
                "3 days ago",
            ],
            ("Eltropy", "Bengaluru, Karnataka, India", "3 days ago"),
        ),
        # A card as LinkedIn shows it to someone signed in.
        (
            [
                "Senior Backend Engineer",
                "Senior Backend Engineer",
                "Eltropy",
                "Bengaluru (Remote)",
                "Promoted",
                "Easy Apply",
                "1 week ago",
            ],
            ("Eltropy", "Bengaluru (Remote)", "1 week ago"),
        ),
    ],
)
def test_a_listing_is_read_from_its_card(lines, expected):
    item = {
        "id": "4478477143",
        "href": "https://www.linkedin.com/jobs/view/4478477143/?x=1",
        "title": "Senior Backend Engineer",
        "lines": lines,
    }

    found = search.listing(item)

    assert (found.company, found.location, found.posted) == expected
    assert found.url == "https://www.linkedin.com/jobs/view/4478477143/"


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (
            [
                "Senior Software Engineer I (AP)",
                "at Acme",
                "3 candid answers",
                "3 recruiters",
                "Posted by Asha Rao",
                "Apply now",
                "Bengaluru (Bangalore) · 7 - 12 years · Profitable · Posted 8 Oct 2026",
                "NodeJS (Node.js)",
            ],
            ("Acme", "Bengaluru (Bangalore)", "8 Oct 2026"),
        ),
        # An agency's job, named by its client.
        (
            [
                "AI Engineer \u2013 Agents & Document Intelligence",
                "Globex",
                "Agency jobvia Initech by Ravi Menon",
                "Apply now",
                "Bengaluru (Bangalore) · 3 - 5 years · ₹20L - ₹30L / yr · "
                "Posted 17 Oct 2026",
                "Python",
                "+3 more",
            ],
            ("Globex", "Bengaluru (Bangalore)", "17 Oct 2026"),
        ),
        (
            [
                "ML Engineer",
                "at Hooli",
                "Posted by Asha Rao",
                "Apply now",
                "Remote only · 3 - 5 years · Profitable · Remote only · "
                "Posted 24 Sep 2026",
                "PyTorch",
            ],
            ("Hooli", "Remote only", "24 Sep 2026"),
        ),
    ],
)
def test_a_cutshort_card_is_read(lines, expected):
    item = {
        "id": "bGJge8jM",
        "href": "https://cutshort.io/job/A-Job-bGJge8jM?ref=search",
        "title": lines[0],
        "lines": lines,
    }

    found = search.SITES["cutshort"].listing(item)

    assert (found.company, found.location, found.posted) == expected
    assert found.title == lines[0]
    assert found.url == "https://cutshort.io/job/A-Job-bGJge8jM"


def test_a_cutshort_card_gives_the_experience_and_pay_it_names():
    lines = [
        "AI Engineer",
        "Globex",
        "Apply now",
        "Bengaluru (Bangalore) · 3 - 5 years · ₹20L - ₹30L / yr · Posted 17 Oct 2026",
    ]
    item = {"id": "bGJge8jM", "href": "https://cutshort.io/job/x", "title": lines[0]}

    found = search.SITES["cutshort"].listing({**item, "lines": lines})

    assert (found.experience, found.salary) == ("3\u20135 years", "₹20L - ₹30L / yr")


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        # A Naukri card: its rating, its reviews, the years and the pay it asks
        # for come before its place.
        (
            [
                "Lead AI Engineer",
                "Ecolab Global Services",
                "3.9",
                "88 Reviews",
                "2-6 Yrs",
                "Not disclosed",
                "Bengaluru",
                "Design and build AI solutions with Python and LLMs for...",
                "Python",
                "Machine Learning",
                "2 days ago",
                "Save",
            ],
            ("Ecolab Global Services", "Bengaluru", "2 days ago", "2\u20136 years", ""),
        ),
        (
            [
                "Lead AI Engineer",
                "Aliqan Services",
                "Posted by Aliqan Services",
                "5-8 Yrs",
                "15-25 Lacs P.A.",
                "Hybrid - Noida, Gurugram",
                "1 week ago",
                "Hide",
            ],
            (
                "Aliqan Services",
                "Hybrid - Noida, Gurugram",
                "1 week ago",
                "5\u20138 years",
                "15-25 Lacs P.A.",
            ),
        ),
        # A card that names no place: its snippet is not taken for one.
        (
            [
                "Lead AI Engineer",
                "Acme",
                "1-4 Yrs",
                "We are looking for an engineer to build agents and retrieval "
                "pipelines for our enterprise customers...",
                "Python",
            ],
            ("Acme", "", "", "1\u20134 years", ""),
        ),
        # The card LinkedIn shows beside its results, its facts on one line.
        (
            [
                "Apple",
                "Lead AI Engineer",
                "Hyderabad, Telangana, India · Reposted 6 days ago · Over 100 people "
                "clicked apply",
                "Responses managed off LinkedIn",
                "Full-time",
                "Apply on company website",
                "Save the job",
            ],
            ("Apple", "Hyderabad, Telangana, India", "6 days ago", "", ""),
        ),
        (
            [
                "Lead AI Engineer",
                "Lead AI Engineer with verification",
                "Globex",
                "Bengaluru, Karnataka, India (On-site)",
                "$120K/yr - $150K/yr",
                "Actively reviewing applicants",
                "Viewed",
                "Easy Apply",
            ],
            (
                "Globex",
                "Bengaluru, Karnataka, India (On-site)",
                "",
                "",
                "$120K/yr - $150K/yr",
            ),
        ),
    ],
)
def test_a_cards_place_is_never_its_experience_its_pay_or_its_poster(lines, expected):
    item = {
        "id": "191026914436",
        "href": "https://www.naukri.com/job-listings-x-191026914436?src=x",
        "title": "Lead AI Engineer",
        "lines": lines,
    }

    found = search.listing(item)

    assert (
        found.company,
        found.location,
        found.posted,
        found.experience,
        found.salary,
    ) == expected


def test_a_linkedin_card_leads_to_its_posting_whatever_it_links_to():
    item = {
        "id": "4479114439",
        "href": "https://www.linkedin.com/jobs/search-results/?currentJobId=4479114439",
        "title": "AI Engineer",
        "lines": ["AI Engineer", "Acme", "Bengaluru"],
    }

    found = search.SITES["linkedin"].listing(item)

    assert found.url == "https://www.linkedin.com/jobs/view/4479114439/"
    assert re.search(search.SITES["linkedin"].posting, item["href"])[1] == "4479114439"


@pytest.mark.parametrize(
    ("said", "experience", "least"),
    [
        ("2-6 Yrs", "2\u20136 years", 2),
        ("3 - 5 years", "3\u20135 years", 3),
        ("Experience Range: 4 to 6 years", "4\u20136 years", 4),
        ("5+ years of backend development experience", "5+ years", 5),
        ("At least 4 years of hands-on experience", "4+ years", 4),
        ("Minimum of 3+ years experience", "3+ years", 3),
        ("1 year", "1 year", 1),
        ("Up to 2 years", "Up to 2 years", 0),
        ("Freshers", "Fresher", 0),
        ("Strong Python skills", "", None),
    ],
)
def test_experience_is_written_one_way_whoever_says_it(said, experience, least):
    assert search.experience_of(said) == experience
    assert search.least_years(said) == least


def test_a_naukri_address_says_the_experience_its_job_asks_for():
    assert (
        search.experience_in(
            "https://www.naukri.com/job-listings-lead-ai-engineer-ecolab-bengaluru-"
            "2-to-6-years-191026914436"
        )
        == "2\u20136 years"
    )
    assert search.experience_in("https://www.linkedin.com/jobs/view/4479114439/") == ""


def test_a_page_says_the_experience_on_a_line_of_its_own_or_with_the_word():
    under_a_heading = "- Page URL: x\n### Text\n# AI Engineer\nExperience\n2-5 Yrs"
    in_a_sentence = (
        "- Page URL: x\n### Text\nWe were founded 20 years ago.\n"
        "- 3+ years of experience with Python"
    )

    assert search.experience_said(under_a_heading) == "2\u20135 years"
    assert search.experience_said(in_a_sentence) == "3+ years"
    assert search.experience_said("- Page URL: x\n### Text\nPython") == ""


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        # LinkedIn's, with a country's code and the experience in months.
        (
            {
                "jobLocation": {
                    "@type": "Place",
                    "address": {
                        "addressCountry": "IN",
                        "addressLocality": "Trivandrum",
                        "addressRegion": None,
                    },
                },
                "datePosted": "2026-10-11T11:27:02.000Z",
                "experienceRequirements": {
                    "@type": "OccupationalExperienceRequirements",
                    "monthsOfExperience": 60,
                },
            },
            search.Facts("Trivandrum, India", "2026-10-11", "5+ years", ""),
        ),
        # Cutshort's, for a remote job open to anyone.
        (
            {
                "jobLocationType": "TELECOMMUTE",
                "applicantLocationRequirements": {"@type": "Country", "name": "NA"},
                "datePosted": "2026-10-14T11:08:08.839Z",
                "experienceRequirements": {"monthsOfExperience": 48},
                "baseSalary": {
                    "currency": "INR",
                    "value": {
                        "minValue": 2200000,
                        "maxValue": 5710000,
                        "unitText": "YEAR",
                    },
                },
            },
            search.Facts(
                "Remote",
                "2026-10-14",
                "4+ years",
                "INR 2,200,000\u20135,710,000 a year",
            ),
        ),
        # Lever's, its whole place as the town.
        (
            {
                "jobLocation": {
                    "address": {
                        "addressLocality": "Bangalore, Karnataka, India",
                        "addressCountry": None,
                    }
                },
                "datePosted": "2026-09-11",
            },
            search.Facts("Bangalore, Karnataka, India", "2026-09-11", "", ""),
        ),
        (
            {
                "jobLocationType": "TELECOMMUTE",
                "applicantLocationRequirements": [{"@type": "Country", "name": "US"}],
                "experienceRequirements": "3-5 years of experience",
            },
            search.Facts("Remote (United States)", "", "3\u20135 years", ""),
        ),
        # A careers site's, its place's address in a list.
        (
            {
                "jobLocation": {
                    "@type": "Place",
                    "address": [
                        {
                            "@type": "PostalAddress",
                            "addressCountry": "India",
                            "addressLocality": "Trivandrum",
                            "addressRegion": "Kerala",
                            "postalCode": "695583",
                            "name": "IND-Trivandrum-Equifax Analytics-PEC",
                        }
                    ],
                },
            },
            search.Facts("Trivandrum, Kerala, India", "", "", ""),
        ),
        (None, search.Facts()),
    ],
)
def test_a_postings_facts_are_read_as_search_engines_read_them(data, expected):
    assert search.facts(data) == expected


def test_a_page_with_nothing_under_where_it_is_is_unread():
    assert search.unread("- Page URL: https://www.naukri.com/x\n### Text\n")
    assert not search.unread("- Page URL: https://www.naukri.com/x\n### Text\n# AI")


@pytest.mark.parametrize(
    ("said", "kept"),
    [
        ("Bengaluru, Karnataka", "Bengaluru, Karnataka"),
        (
            "Cognizant Careers · https://careers.cognizant.com \u203a india-en \u203a jobs",
            "",
        ),
        ("Build agents and pipelines for customers across the world and...", ""),
    ],
)
def test_only_a_place_is_kept_as_one(said, kept):
    assert search.place(said) == kept


@pytest.mark.parametrize(
    ("posted", "days"),
    [
        ("3 days ago", 3),
        ("1 week ago", 7),
        ("2 weeks ago", 14),
        ("30+ days ago", 30),
        ("5 hours ago", 0),
        ("Just now", 0),
        ("Yesterday", 1),
        ("", None),
    ],
)
def test_a_postings_age_is_read_from_its_listing(posted, days):
    assert search.days_old(posted) == days


@pytest.mark.parametrize(
    ("posted", "days"),
    [
        ("8 Oct 2026", 13),
        ("Oct 8, 2026", 13),
        ("21 October 2026", 0),
        # Without its year, the day last came this year or the one before.
        ("8 Oct", 13),
        ("30 Dec", 295),
        ("30 Feb 2026", None),
        # As a page's JobPosting says it.
        ("2026-10-08", 13),
        ("2026-02-30", None),
    ],
)
def test_a_postings_day_gives_its_age(posted, days):
    assert search.days_old(posted, date(2026, 10, 21)) == days


@pytest.mark.parametrize(
    ("asked", "years"),
    [("5-10 years", 5), ("3+ years", 3), ("at least 2 yrs", 2), ("", None)],
)
def test_the_experience_a_posting_asks_for_is_read(asked, years):
    assert search.least_years(asked) == years


def test_a_page_that_refuses_the_browser_is_known():
    assert search.blocked("- Page Title: Access Denied\n# Access Denied")
    assert not search.blocked(PORTFOLIO_TEXT)


async def test_a_fit_keeps_only_what_the_posting_says(monkeypatch):
    posting = (
        "Backend Engineer. We need 5-10 years of Python. Pay: ₹30-40 LPA. Fully remote."
    )

    async def ask(instructions, data, think="high"):
        return json.dumps(
            {
                "fit": "HIGH",
                "why": ["5-10 years of Python", "experience with Kafka"],
                "salary": "₹30-40 LPA",
                "work_mode": "Remote",
                "experience": "8 years",
                "location": "Fully remote",
                "posting": True,
                "title": "Backend Engineer",
                "company": "Initech",
            }
        )

    monkeypatch.setattr(writing, "ask", ask)

    fit = await writing.judge_fit({"skills": ["Python"]}, posting)

    assert fit == {
        "fit": "high",
        "why": ["5-10 years of Python"],
        "salary": "₹30-40 LPA",
        "work_mode": "remote",
        "experience": "",
        "location": "Fully remote",
        "posting": True,
        "title": "Backend Engineer",
        # Not on the page, so not taken.
        "company": "",
    }


# A results page, and the postings it lists.
RESULTS = "- Page URL: https://www.linkedin.com/jobs/search/\n### Text\n# Backend Engineer Jobs"
POSTINGS = {
    "101": (
        "Backend Engineer",
        "Acme",
        "Bengaluru",
        "2 days ago",
        "Python, 3+ years",
        "Apply",
    ),
    "102": (
        "AI Engineer",
        "Globex",
        "Bengaluru",
        "1 day ago",
        "Python and LLMs",
        "Easy Apply",
    ),
    "103": (
        "Sales Manager",
        "Initech",
        "Bengaluru",
        "3 days ago",
        "Sell software",
        "Apply",
    ),
    "104": (
        "Staff Engineer",
        "Umbrella",
        "Bengaluru",
        "4 days ago",
        "12+ years leading teams",
        "Apply",
    ),
    "105": (
        "Backend Engineer",
        "Hooli",
        "Bengaluru",
        "30+ days ago",
        "Python",
        "Apply",
    ),
}


def _posting_url(job: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job}/"


class SearchBrowser:
    """The browser in use for a search: a job site's results page and the postings
    on it, and companies' own job boards, each with its openings. Any other address
    is not found."""

    def __init__(self) -> None:
        self.in_use = True
        self.shown = True
        self.opened: list[str] = []
        self.held: list[str | None] = []
        self.holding: tuple[str | None, bool] = (None, False)
        # How many more reads of a page show a check that the user is a person.
        self.checking = 0
        # Companies' boards by address: their title, and each opening's id, title,
        # place and posting.
        self.boards: dict[str, tuple[str, list[tuple[str, str, str, str]]]] = {}
        # Google's results, page by page: each posting's address, the title Google
        # shows for it, and its snippet, for a search of companies' job boards and
        # one of their careers pages. And companies' postings by address: the title
        # of the page, its place and what it asks.
        self.google: list[list[tuple[str, str, str]]] = []
        self.careers: list[list[tuple[str, str, str]]] = []
        self.postings: dict[str, tuple[str, str, str]] = {}
        # LinkedIn postings' pages by job: their title, and the company they name,
        # which may not be what the job's card said.
        self.named: dict[str, tuple[str, str]] = {}
        # What postings' pages say in JSON-LD, by address, and the pages that were
        # waited on until they stopped changing, and those that show nothing until
        # they are.
        self.structured: dict[str, dict] = {}
        self.settled: list[str] = []
        self.filled_late: set[str] = set()
        self.blank: set[str] = set()
        # Pages that never finish loading, so the browser does not answer in time.
        self.unanswered: set[str] = set()

    def hold(self, what, showing=False):
        self.held.append(what)
        self.holding = (what, showing)

    def release(self):
        self.hold(None)

    async def open_page(self, context, url, settle=""):
        if url in self.unanswered:
            raise McpError(
                ErrorData(code=408, message="Timed out while waiting for response")
            )
        self.opened.append(url)
        if settle:
            await self.run_script(context, settle)
        return await self.read_page(context)

    def _opening(self, url: str) -> tuple[str, str, str, str] | None:
        for board, (_, openings) in self.boards.items():
            for opening in openings:
                if (
                    url == f"{board}/jobs/{opening[0]}"
                    or url == f"{board}/{opening[0]}"
                ):
                    return opening
        return None

    async def read_page(self, context):
        url = self.opened[-1]
        # A site may ask for a check that the user is a person, until they do it.
        if self.checking:
            self.checking -= 1
            return f"- Page URL: {url}\n### Text\n# Let's do a quick security check"
        if url in self.blank or (url in self.filled_late and url not in self.settled):
            return f"- Page URL: {url}\n### Text\n"
        if "/jobs/search/" in url:
            return RESULTS
        if "google.com/search" in url:
            return (
                f"- Page URL: {url}\n- Page Title: Google Search\n### Text\n# Results"
            )
        if url in self.postings:
            title, place, asks = self.postings[url]
            return (
                f"- Page URL: {url}\n- Page Title: {title}\n### Text\n# {title}\n"
                f"{place}\n{asks}"
            )
        board = url.split("?page=")[0]
        if board in self.boards:
            return f"- Page URL: {url}\n- Page Title: {self.boards[board][0]}\n### Text"
        if opening := self._opening(url):
            _, title, place, asks = opening
            return f"- Page URL: {url}\n### Text\n# {title}\n{place}\n{asks}"
        job = url.rstrip("/").rsplit("/", 1)[1]
        if "linkedin.com/jobs/view/" not in url or job not in POSTINGS:
            return f"- Page URL: {url}\n- Page Title: Page not found\n### Text\n404"
        title, company, place, _, asks, _ = POSTINGS[job]
        if job in self.named:
            page_title, company = self.named[job]
            return (
                f"- Page URL: {url}\n- Page Title: {page_title}\n### Text\n# {title}\n"
                f"{company} · {place}\n{asks}"
            )
        return f"- Page URL: {url}\n### Text\n# {title}\n{company} · {place}\n{asks}"

    async def run_script(self, context, function):
        url = self.opened[-1]
        if function == scripts.FORM:
            return {"url": url, "fields": [], "challenge": False}
        if function == scripts.SETTLED:
            self.settled.append(url)
            return 1000
        if function == scripts.JOB_POSTING:
            return self.structured.get(url)
        if "tries" in function:
            return len(self.boards[url][1]) if url in self.boards else 0
        if "new RegExp" in function and "google.com/search" in url:
            asked = parse_qs(urlsplit(url).query)
            page = int(asked.get("start", ["0"])[0]) // 10
            pages = self.careers if "inurl:careers" in asked["q"][0] else self.google
            results = pages[page] if page < len(pages) else []
            return [
                {
                    "id": address,
                    "href": address,
                    "title": shown,
                    "lines": [shown, snippet],
                }
                for address, shown, snippet in results
            ]
        board, _, page = url.partition("?page=")
        if "new RegExp" in function and board in self.boards:
            # Fifty openings to a page, as Greenhouse shows them.
            first = 50 * (int(page or 1) - 1)
            return [
                {
                    "id": number,
                    "href": f"{board}/jobs/{number}",
                    "title": title,
                    "lines": [title, place],
                }
                for number, title, place, _ in self.boards[board][1][first : first + 50]
            ]
        if "new RegExp" in function and "/jobs/search/" not in url:
            return []
        if "new RegExp" in function:
            return [
                {
                    "id": job,
                    "href": _posting_url(job) + "?trk=1",
                    "title": title,
                    "lines": [title, company, place, posted],
                }
                for job, (title, company, place, posted, _, _) in POSTINGS.items()
            ]
        job = url.rstrip("/").rsplit("/", 1)[1]
        said = POSTINGS[job][5] if job in POSTINGS else "Apply"
        return [{"said": said, "href": ""}]


@pytest.fixture
def judged(monkeypatch) -> None:
    """The drafting model, standing in: sales is not a fit, the rest are."""

    async def ask(instructions, data, think="high"):
        posting = json.loads(data)["posting"]
        if "Sell software" in posting:
            return json.dumps({"fit": "none", "why": ["Sell software"]})
        if "All open roles" in posting:
            return json.dumps(
                {"fit": "high", "why": ["All open roles"], "posting": False}
            )
        asks = next(
            line for line in posting.splitlines() if "Python" in line or "years" in line
        )
        return json.dumps(
            {
                "fit": "high",
                "why": [asks],
                "experience": asks if "years" in asks else "",
            }
        )

    monkeypatch.setattr(writing, "ask", ask)


def _searching(*said: str) -> SimpleNamespace:
    talk = _talk(*said)
    told: list[str] = []

    async def generate_reply(instructions=""):
        told.append(instructions)

    talk.session.generate_reply = generate_reply
    talk.told = told
    return talk


async def test_a_search_reads_only_what_may_fit_and_keeps_what_does(
    google, judged, shown
):
    profile.change("roles: Backend Engineer, AI Engineer")
    profile.change("locations: Bengaluru")
    profile.change("years: 4")
    settings.change("pages_per_search: 1")
    await sheet.make_book(None)
    browser = SearchBrowser()
    tools = job_tools.JobsToolset(browser)
    talk = _searching("Find me backend jobs in Bengaluru")

    told = await tools._jobs_search(talk)
    await tools._searching

    assert told["started"] == [
        "Company job boards: none known yet",
        "Google, company job boards: Backend Engineer in Bengaluru",
        "Google, company job boards: AI Engineer in Bengaluru",
        "Google, companies' careers pages: Backend Engineer in Bengaluru",
        "Google, companies' careers pages: AI Engineer in Bengaluru",
        "LinkedIn: Backend Engineer in Bengaluru",
        "LinkedIn: AI Engineer in Bengaluru",
        "Cutshort: Backend Engineer in Bengaluru",
        "Cutshort: AI Engineer in Bengaluru",
        "Company job boards of the companies found on Google, LinkedIn and Cutshort",
    ]
    kept, easy = sorted(store.jobs(), key=lambda job: job.number)
    assert (kept.company, kept.apply, kept.applies_on) == ("Acme", True, "company")
    assert kept.why == "Python, 3+ years" and kept.posting
    # One that applies only on LinkedIn, its company's own page having no such job,
    # is kept to apply to there.
    assert (easy.company, easy.apply, easy.applies_on) == ("Globex", True, "LinkedIn")
    assert (kept.location, kept.posted, kept.experience) == (
        "Bengaluru",
        "2 days ago",
        "3+ years",
    )
    # Each posting was read once its page had stopped changing.
    assert all(url in browser.settled for url in browser.opened if "/view/" in url)
    first = next(done for done in reversed(store.searches()) if done.site == "LinkedIn")
    # Only the postings that may fit were opened: not sales, a staff role or one
    # too old.
    assert (first.read, first.kept) == (2, 2)
    assert first.left_out == {"not one of the roles": 2, "too old": 1}
    assert not any("/jobs/view/103/" in url for url in browser.opened)
    # The boards of the companies found were looked for: they have none.
    assert store.board("Acme").system == ""
    assert "No job board of their own was found for Acme, Globex." in talk.told[0]
    assert browser.held == ["searching for jobs", None]
    assert "2 postings were read and 2 kept" in talk.told[0]


async def test_a_search_finds_nothing_twice(google, judged, shown):
    profile.change("roles: Backend Engineer")
    profile.change("locations: Bengaluru")
    settings.change("pages_per_search: 1")
    tools = job_tools.JobsToolset(SearchBrowser())

    first = await tools._jobs_search(_searching("Find jobs"))
    await tools._searching
    await tools._jobs_search(_searching("Find jobs again"))
    await tools._searching

    assert "saving" in first
    assert len(store.jobs()) == 1
    again = next(done for done in store.searches() if done.site == "LinkedIn")
    assert again.left_out["already found"] == 1


def _days_ago(days: int) -> str:
    """A day as Cutshort writes it, such as "8 Oct 2026"."""
    when = date.today() - timedelta(days=days)
    return f"{when.day} {when:%b %Y}"


# Cutshort's AI jobs across India, as its results page lists them: each card's id and
# lines.
CUTSHORT_CARDS = {
    "AbCd1234": [
        "AI Engineer",
        "at Acme",
        "3 recruiters",
        "Posted by Asha Rao",
        "Apply now",
        f"Bengaluru (Bangalore) · 3 - 5 years · Profitable · Posted {_days_ago(4)}",
        "Python",
    ],
    "EfGh5678": [
        "LLM Engineer",
        "at Globex",
        "Posted by Ravi Menon",
        "Apply now",
        f"Pune · 2 - 4 years · Posted {_days_ago(40)}",
    ],
    "IjKl9012": [
        "Sales Manager - AI",
        "at Initech",
        "Posted by Asha Rao",
        "Apply now",
        f"Mumbai · 3 - 6 years · Posted {_days_ago(2)}",
    ],
    "MnOp3456": [
        "AI Engineer",
        "at Hooli",
        "Posted by Asha Rao",
        "Apply now",
        f"Pune · 8 - 12 years · Posted {_days_ago(3)}",
    ],
}


class CutshortBrowser(SearchBrowser):
    """The browser in use for a search of Cutshort: its results page, and postings
    with the posting's own Apply button and others for jobs like it."""

    async def read_page(self, context):
        url = self.opened[-1]
        if url.startswith("https://cutshort.io/jobs/"):
            return f"- Page URL: {url}\n- Page Title: AI Jobs | Cutshort\n### Text"
        if url == "https://cutshort.io/job/AI-Engineer-Acme-AbCd1234":
            return (
                f"- Page URL: {url}\n- Page Title: Acme is hiring AI Engineer job in "
                "Bengaluru (Bangalore) | Cutshort\n### Text\n# AI Engineer\nat Acme\n"
                "Python, 3+ years\nApply to this job"
            )
        return await super().read_page(context)

    async def run_script(self, context, function):
        url = self.opened[-1]
        if "new RegExp" in function and url.startswith("https://cutshort.io/jobs/"):
            return [
                {
                    "id": job,
                    "href": f"https://cutshort.io/job/{lines[0].split()[0]}-Engineer-"
                    f"{lines[1].removeprefix('at ')}-{job}?ref=search",
                    "title": lines[0],
                    "lines": lines,
                }
                for job, lines in CUTSHORT_CARDS.items()
            ]
        if url.startswith("https://cutshort.io/job/"):
            return [{"said": "Apply to this job"}, {"said": "Apply now"}]
        return await super().run_script(context, function)


async def test_cutshort_jobs_are_kept_though_they_apply_only_there(
    google, judged, shown
):
    profile.change("roles: AI Engineer, LLM Engineer")
    profile.change("locations: Anywhere in India")
    profile.change("years: 4")
    settings.change("sites: cutshort")
    settings.change("board only: leave out")
    browser = CutshortBrowser()
    tools = job_tools.JobsToolset(browser)

    told = await tools._jobs_search(_searching("Find me AI jobs"))
    await tools._searching

    # Cutshort lists both roles' jobs as one field's, on one page, across India.
    assert told["started"] == ["Cutshort: AI Engineer in Anywhere in India"]
    assert [url for url in browser.opened if "/jobs/" in url] == [
        "https://cutshort.io/jobs/artificial-intelligence-ai-jobs"
    ]
    # Its jobs all apply on Cutshort: kept although board-only jobs are not, and
    # left unticked, as those are, for the user to choose.
    assert not settings.current()["easy_apply"]
    (kept,) = store.jobs()
    assert (kept.site, kept.title, kept.company, kept.location) == (
        "Cutshort",
        "AI Engineer",
        "Acme",
        "Bengaluru (Bangalore)",
    )
    assert (kept.applies_on, kept.apply) == ("Cutshort", False)
    assert kept.url == "https://cutshort.io/job/AI-Engineer-Acme-AbCd1234"
    # As its posting says it, before its card's "3 - 5 years".
    assert kept.experience == "3+ years"
    done = store.searches()[0]
    assert (done.read, done.kept) == (1, 1)
    # One asking for twice the user's years was not even opened.
    assert done.left_out == {
        "too old": 1,
        "not one of the roles": 1,
        "asks for more experience": 1,
    }
    assert not any("MnOp3456" in url for url in browser.opened)


ACME = "https://job-boards.greenhouse.io/acme"


def _acme_board() -> tuple[str, list[tuple[str, str, str, str]]]:
    return (
        "Jobs at Acme",
        [
            ("900", "Backend Engineer", "Bengaluru, India", "Python, 3+ years"),
            ("901", "Backend Engineer, Payments", "Remote, United States", "Python"),
            ("902", "Account Executive", "Bengaluru, India", "Sell software"),
            (
                "903",
                "Director of Engineering, Backend",
                "Bengaluru",
                "Python, 12+ years",
            ),
            ("904", "AI Engineer New", "Hybrid - Bangalore, India", "Python and LLMs"),
        ],
    )


async def test_company_boards_come_first_and_only_their_jobs_that_fit_are_kept(
    google, judged, shown
):
    profile.change("roles: Backend Engineer, AI Engineer")
    profile.change("locations: Bengaluru")
    profile.change("years: 4")
    settings.change("pages_per_search: 1")
    settings.change("companies: add Acme")
    await sheet.make_book(None)
    browser = SearchBrowser()
    browser.boards[ACME] = _acme_board()
    tools = job_tools.JobsToolset(browser)
    talk = _searching("Find me jobs")

    told = await tools._jobs_search(talk)
    await tools._searching

    assert told["started"][0] == "Company job boards: Acme"
    # Acme's board was found under its name on Greenhouse, and read first.
    board = store.board("Acme")
    assert (board.system, board.slug, board.source) == ("greenhouse", "acme", "you")
    company = {job.title: job for job in store.jobs() if job.site == "Greenhouse"}
    assert set(company) == {"Backend Engineer", "AI Engineer"}
    backend = company["Backend Engineer"]
    assert backend.url == backend.apply_url == f"{ACME}/jobs/900"
    assert backend.applies_on == "company" and backend.apply
    boards_search = store.searches()[-1]
    assert boards_search.site == "Company job boards"
    assert boards_search.left_out == {
        "somewhere else": 1,
        "not one of the roles": 1,
        "too senior": 1,
    }
    # The same job on LinkedIn is the one already found on Acme's own board.
    assert not any(job.key == "linkedin:101" for job in store.jobs())
    linkedin = next(row for row in reversed(store.searches()) if row.site == "LinkedIn")
    assert linkedin.left_out["already found"] == 1


async def test_what_a_postings_page_says_as_search_engines_read_it_is_used(
    google, judged, shown
):
    profile.change("roles: Backend Engineer, AI Engineer")
    profile.change("locations: Bengaluru, remote")
    profile.change("years: 4")
    settings.change("pages_per_search: 1")
    settings.change("companies: add Acme")
    browser = SearchBrowser()
    browser.boards[ACME] = (
        "Jobs at Acme",
        [
            ("900", "Backend Engineer", "Bengaluru, India", "Python, 3+ years"),
            # Its card runs its words together; its page says who may apply.
            ("905", "AI Engineer", "REMOTE \u2014 FULL-TIMEUS", "Python and LLMs"),
        ],
    )
    browser.structured[f"{ACME}/jobs/900"] = {
        "jobLocation": {
            "address": {
                "addressLocality": "Bengaluru",
                "addressRegion": "Karnataka",
                "addressCountry": "IN",
            }
        },
        "datePosted": "2026-10-15T09:00:00Z",
        "experienceRequirements": {"monthsOfExperience": 24},
        "baseSalary": {
            "currency": "INR",
            "value": {"minValue": 1500000, "maxValue": 2500000, "unitText": "YEAR"},
        },
    }
    browser.structured[f"{ACME}/jobs/905"] = {
        "jobLocationType": "TELECOMMUTE",
        "applicantLocationRequirements": {"@type": "Country", "name": "United States"},
    }
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(_searching("Find me jobs"))
    await tools._searching

    backend = next(job for job in store.jobs() if job.url == f"{ACME}/jobs/900")
    # Where it is, when it went up and its pay, as its page says them; the years
    # as the posting's own words say them, before its page's months.
    assert (backend.location, backend.posted, backend.experience, backend.salary) == (
        "Bengaluru, Karnataka, India",
        "2026-10-15",
        "3+ years",
        "INR 1,500,000\u20132,500,000 a year",
    )
    assert not any(job.url == f"{ACME}/jobs/905" for job in store.jobs())
    boards_search = next(
        row for row in reversed(store.searches()) if row.site == "Company job boards"
    )
    assert boards_search.left_out == {"somewhere else": 1}


async def test_a_posting_is_judged_only_once_its_page_shows_it(
    google, judged, shown, monkeypatch
):
    _backend_in_bengaluru()
    settings.change("pages_per_search: 1")
    browser = SearchBrowser()
    late, blank = (
        f"{board}/jobs/{8000 + n}" for n, board in enumerate(_on_google(browser, 2))
    )
    # One page fills in a moment after it opens; the other never does.
    browser.filled_late.add(late)
    browser.blank.add(blank)
    judged_pages = []
    ask = writing.ask

    async def asked(instructions, data, think="high"):
        judged_pages.append(json.loads(data)["posting"])
        return await ask(instructions, data, think)

    monkeypatch.setattr(writing, "ask", asked)
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(_searching("Find me jobs"))
    await tools._searching

    assert [job.url for job in store.jobs() if job.site == "Greenhouse"] == [late]
    assert not any(blank in page for page in judged_pages)
    row = next(row for row in reversed(store.searches()) if row.site == "Google")
    assert (row.read, row.kept) == (2, 1)
    assert row.left_out == {"could not be read": 1}


async def test_every_page_of_a_companys_board_is_read(google, judged, shown):
    _backend_in_bengaluru()
    settings.change("pages_per_search: 1")
    settings.change("companies: add Acme")
    browser = SearchBrowser()
    browser.boards[ACME] = (
        "Jobs at Acme",
        [
            (str(1000 + n), f"Account Executive {n}", "Bengaluru, India", "Sell")
            for n in range(120)
        ]
        + [("2000", "Backend Engineer", "Bengaluru, India", "Python, 3+ years")],
    )
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(_searching("Find me jobs"))
    await tools._searching

    # Until a page lists nothing new.
    assert [url for url in browser.opened if url.startswith(f"{ACME}?")] == [
        f"{ACME}?page=2",
        f"{ACME}?page=3",
        f"{ACME}?page=4",
    ]
    assert any(job.url == f"{ACME}/jobs/2000" for job in store.jobs())
    boards_search = next(
        row for row in store.searches() if row.site == "Company job boards"
    )
    assert boards_search.left_out == {"not one of the roles": 120}


async def test_a_fit_its_postings_own_words_do_not_show_is_not_kept(
    google, shown, monkeypatch
):
    _backend_in_bengaluru()
    browser = SearchBrowser()
    (board,) = _on_google(browser, 1)
    # Its page, read before its description showed: no more than its heading.
    title = "Job Application for Backend Engineer at Company0"
    browser.postings[f"{board}/jobs/8000"] = (title, "Bengaluru, India", "Apply")

    async def ask(instructions, data, think="high"):
        # All the drafting model can quote is the heading.
        return json.dumps(
            {
                "fit": "high",
                "why": ["Backend Engineer", "Bengaluru, India"],
                "location": "Bengaluru, India",
            }
        )

    monkeypatch.setattr(writing, "ask", ask)
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(_searching("Find me jobs"))
    await tools._searching

    assert not store.jobs()
    row = next(row for row in reversed(store.searches()) if row.site == "Google")
    assert row.left_out == {"said too little to judge": 1}


async def test_a_job_only_on_linkedin_is_applied_to_on_its_companys_page_if_there(
    google, judged, shown
):
    profile.change("roles: Backend Engineer, AI Engineer")
    profile.change("locations: Bengaluru")
    profile.change("years: 4")
    settings.change("pages_per_search: 1")
    browser = SearchBrowser()
    # Globex's AI Engineer applies only on LinkedIn, but its own board has it too.
    globex = "https://job-boards.greenhouse.io/globex"
    browser.boards[globex] = (
        "Jobs at Globex",
        [("950", "AI Engineer", "Bengaluru, India", "Python and LLMs")],
    )
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(_searching("Find me jobs"))
    await tools._searching

    (job,) = [job for job in store.jobs() if job.company == "Globex"]
    assert job.key == "linkedin:102"
    assert (job.applies_on, job.apply_url) == ("company", f"{globex}/jobs/950")


async def test_jobs_that_apply_only_on_linkedin_are_left_out_when_the_user_says(
    google, judged, shown
):
    _backend_in_bengaluru()
    profile.change("roles: Backend Engineer, AI Engineer")
    settings.change("pages_per_search: 1")
    settings.change("board only: leave out")
    browser = SearchBrowser()
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(_searching("Find me jobs"))
    await tools._searching

    assert not any(job.company == "Globex" for job in store.jobs())
    first = next(done for done in reversed(store.searches()) if done.site == "LinkedIn")
    assert first.left_out["applied to only on LinkedIn"] == 1


async def test_a_posting_that_does_not_open_in_time_is_passed_over(
    google, judged, shown
):
    _backend_in_bengaluru()
    settings.change("pages_per_search: 1")
    browser = SearchBrowser()
    slow, fine = (
        f"{board}/jobs/{8000 + n}" for n, board in enumerate(_on_google(browser, 2))
    )
    browser.unanswered.add(slow)
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(_searching("Find me jobs"))
    await tools._searching

    assert [job.url for job in store.jobs() if job.site == "Greenhouse"] == [fine]
    row = next(row for row in reversed(store.searches()) if row.site == "Google")
    assert row.left_out == {"could not be read": 1}


async def test_a_search_stops_once_the_browser_stops_answering(google, judged, shown):
    _backend_in_bengaluru()
    settings.change("pages_per_search: 1")
    browser = SearchBrowser()
    browser.unanswered |= {
        f"{board}/jobs/{8000 + n}" for n, board in enumerate(_on_google(browser, 5))
    }
    talk = _searching("Find me jobs")
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(talk)
    await tools._searching

    assert job_tools.STUCK in talk.told[-1]
    # It stopped there: no later part of the search ran.
    assert not any(row.site == "LinkedIn" for row in store.searches())


async def test_a_results_page_that_does_not_open_in_time_ends_only_its_part(
    google, judged, shown
):
    profile.change("roles: Backend Engineer, AI Engineer")
    profile.change("locations: Bengaluru")
    profile.change("years: 4")
    settings.change("pages_per_search: 1")
    browser = SearchBrowser()
    linkedin = search.SITES["linkedin"]
    browser.unanswered.add(linkedin.search("Backend Engineer", "Bengaluru", 14, 1))
    talk = _searching("Find me jobs")
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(talk)
    await tools._searching

    assert "LinkedIn's page did not open in time." in talk.told[-1]
    # The next part went on.
    assert any(
        row.site == "LinkedIn" and row.query == "AI Engineer in Bengaluru"
        for row in store.searches()
    )


async def test_a_search_that_outlives_its_conversation_ends_quietly(
    google, judged, shown, caplog
):
    _backend_in_bengaluru()
    browser = SearchBrowser()
    talk = _searching("Find me jobs")

    async def ended(instructions=""):
        raise RuntimeError("AgentSession isn't running")

    talk.session.generate_reply = ended
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(talk)
    await tools._searching

    assert any(job.company == "Acme" for job in store.jobs())
    assert "could not be told" not in caplog.text


async def test_a_companys_board_is_looked_for_once(google, judged, shown):
    profile.change("roles: Backend Engineer")
    profile.change("locations: Bengaluru")
    settings.change("pages_per_search: 1")
    settings.change("companies: Initrode")
    browser = SearchBrowser()
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(_searching("Find jobs"))
    await tools._searching
    tried = [url for url in browser.opened if "initrode" in url]
    await tools._jobs_search(_searching("Find jobs again"))
    await tools._searching

    assert tried == [
        "https://job-boards.greenhouse.io/initrode",
        "https://jobs.lever.co/initrode",
        "https://jobs.ashbyhq.com/initrode",
    ]
    assert [url for url in browser.opened if "initrode" in url] == tried
    assert store.board("Initrode").system == ""


@pytest.mark.parametrize(
    ("title", "fits"),
    [
        ("Senior Backend Engineer", True),
        ("Software Engineer II (Backend)", True),
        ("Back-end Developer - Java", True),
        ("Machine Learning Engineer", True),
        ("Frontend Engineer", False),
        ("AI Product Manager", False),
        ("Data Analyst", False),
    ],
)
def test_a_title_is_one_of_the_roles_by_its_work_and_field(title, fits):
    assert relevance.is_role(title, ["Backend Engineer", "AI Engineer"]) is fits


@pytest.mark.parametrize(
    ("where", "fits"),
    [
        ("Bengaluru, Karnataka, India", True),
        ("Hybrid - Full-timeBangalore, India", True),
        ("Remote - India", True),
        ("Remote, United States", False),
        ("Applied AI • San Francisco • Full time", False),
        ("Pune, India", False),
        ("", None),
    ],
)
def test_a_place_is_one_of_the_users(where, fits):
    assert relevance.in_place(where, ["Bengaluru", "remote"]) is fits


@pytest.mark.parametrize(
    ("where", "fits"),
    [
        ("Pune", True),
        ("Bengaluru (Bangalore), Mumbai", True),
        ("Remote only", True),
        ("Remote, United States", False),
        ("London, United Kingdom", False),
        ("Bhopal", None),
    ],
)
def test_all_of_india_is_any_place_in_it(where, fits):
    assert relevance.in_place(where, ["Anywhere in India"]) is fits
    assert relevance.in_place(where, ["Pan India"]) is fits


@pytest.mark.parametrize(
    "where",
    [
        "Colombo, Sri Lanka",
        "Slovakia",
        "Buenos Aires",
        "United Arab Emirates",
        "Riyadh, Saudi Arabia",
        "Warsaw, Masovian Voivodeship, Poland",
        "Toronto, ON",
        "Austin, TX",
        "Kraków",
        "Remote, Massachusetts",
        "Remote (US only)",
        "Anywhere in the US and/or Remote",
        "Remote \u2014 EMEA",
        "Hybrid - Full-timeSão Paulo",
    ],
)
def test_a_place_in_another_country_is_somewhere_else(where):
    assert relevance.in_place(where, ["Anywhere in India"]) is False
    assert relevance.in_place(where, ["Bengaluru", "remote"]) is False


@pytest.mark.parametrize(
    ("where", "fits"),
    [
        # A word that is no place, though a place's name is in it, or a place's code
        # in another case.
        ("Join us as a Woman in Tech · Bengaluru", True),
        ("Remote · Worldwide", True),
        ("Remote (Anywhere)", True),
        ("New Delhi, India · Virginia, USA", True),
        ("Salem, Tamil Nadu", None),
    ],
)
def test_only_a_place_abroad_is_taken_for_one(where, fits):
    assert relevance.in_place(where, ["Anywhere in India"]) is fits


def test_a_job_somewhere_else_is_left_out_by_its_title_too():
    known = {"roles": ["AI Engineer"], "locations": ["Bengaluru", "remote"]}

    assert (
        relevance.left_out(
            "AI Support Engineer - Toronto", "OpenAI", "Ontario - Remote", known
        )
        == relevance.ELSEWHERE
    )
    assert (
        relevance.left_out(
            "AI Deployment Engineer",
            "OpenAI",
            "Bangalore - Remote; Delhi, India",
            known,
        )
        == ""
    )


def test_a_level_far_from_the_users_is_told():
    assert relevance.level("Director, Engineering", 4) == relevance.TOO_SENIOR
    assert relevance.level("Staff Backend Engineer", 9) == ""
    assert relevance.level("Backend Intern", 3) == relevance.AN_INTERNSHIP
    assert relevance.level("Senior Backend Engineer", 4) == ""


def test_the_same_job_on_two_sites_is_known():
    assert relevance.same_job(
        "Backend Engineer - Bengaluru",
        "Razorpay",
        "Backend Engineer",
        "Razorpay Pvt Ltd",
    )
    assert not relevance.same_job(
        "Senior Backend Engineer", "Razorpay", "Backend Engineer", "Razorpay"
    )


def test_companies_to_watch_and_board_only_jobs_change_by_voice():
    assert settings.change("companies: add Razorpay and Zerodha") == (
        "companies",
        ["Razorpay", "Zerodha"],
    )
    assert settings.change("companies: remove zerodha")[1] == ["Razorpay"]
    assert settings.change("easy apply: yes") == ("easy_apply", True)
    assert settings.change("board only: leave out") == ("easy_apply", False)


def test_sites_new_to_orbit_join_sites_set_before_once():
    store.keep_setting("sites", ["linkedin", "naukri"])

    assert settings.current()["sites"] == [
        "companies",
        "google",
        "linkedin",
        "cutshort",
        "naukri",
    ]
    settings.change("sites: remove companies, google and cutshort")
    assert settings.current()["sites"] == ["linkedin", "naukri"]


def test_sites_chosen_by_voice_stay_as_chosen():
    settings.change("sites: linkedin")

    assert settings.current()["sites"] == ["linkedin"]


def test_an_easy_apply_card_is_known():
    item = {
        "id": "7",
        "href": "https://www.linkedin.com/jobs/view/7/",
        "title": "Backend Engineer",
        "lines": ["Backend Engineer", "Acme", "Bengaluru", "Easy Apply"],
    }
    assert search.listing(item).easy_apply


def test_a_card_marked_applied_is_known():
    def card(*lines: str) -> search.Listing:
        return search.listing(
            {"id": "8", "href": "https://www.linkedin.com/jobs/view/8/", "lines": lines}
        )

    applied = card("Backend Engineer", "Acme", "Bengaluru", "Applied 2 weeks ago")
    # When the user applied is not how old the posting is.
    assert applied.applied and applied.posted == ""
    assert applied.company == "Acme" and applied.location == "Bengaluru"
    assert card("Backend Engineer", "Acme", "Bengaluru", "1 day ago", "Applied").applied
    assert not card("Applied AI Engineer", "Applied Intuition", "Bengaluru").applied


@pytest.mark.parametrize(
    ("page_title", "named"),
    [
        (
            "Backend Engineer | Wayne Enterprises | LinkedIn",
            ("Backend Engineer", "Wayne Enterprises"),
        ),
        # With notifications waiting, and a role that has a bar of its own.
        (
            "(3) Senior Engineer | Payments | Acme | LinkedIn",
            ("Senior Engineer | Payments", "Acme"),
        ),
        # As LinkedIn titles it for someone not signed in.
        (
            "Acme hiring Backend Engineer in Bengaluru, Karnataka, India | LinkedIn",
            ("Backend Engineer", "Acme"),
        ),
        ("Jobs | LinkedIn", None),
    ],
)
def test_a_linkedin_posting_names_its_role_and_company_in_its_title(page_title, named):
    assert search.titled(page_title, search.SITES["linkedin"]) == named


@pytest.mark.parametrize(
    ("page_title", "named"),
    [
        (
            "Acme is hiring Senior Software Engineer I (AP) job in Bengaluru "
            "(Bangalore) | Cutshort",
            ("Senior Software Engineer I (AP)", "Acme"),
        ),
        (
            "Initech is hiring Engineer in Test job in Pune | Cutshort",
            ("Engineer in Test", "Initech"),
        ),
        (
            "Hooli is hiring ML Engineer job (Remote only) | Cutshort",
            ("ML Engineer", "Hooli"),
        ),
        # Through an agency, whose name runs into the role's.
        (
            "Globex is hiring via Initech Staffing AI Engineer job in Pune | Cutshort",
            None,
        ),
        ("Cutshort", None),
    ],
)
def test_a_cutshort_posting_names_its_role_and_company_in_its_title(page_title, named):
    assert search.titled(page_title, search.SITES["cutshort"]) == named


@pytest.mark.parametrize(
    ("said", "applied"),
    [
        ("Applied 3 days ago", True),
        ("- Applied on 12 Oct 2026 | See application", True),
        ("You applied", True),
        ("You\u2019ve already applied to this job.", True),
        ("You have already applied for this position", True),
        ("Application submitted", True),
        # A menu or filter may say "Applied" on any page: alone it says nothing.
        ("Applied", False),
        ("# Applied AI Engineer", False),
        ("Applied Scientist, LLM", False),
        ("Easy Apply", False),
    ],
)
def test_a_page_says_when_the_user_applied(said, applied):
    page = f"- Page Title: A job\n### Text\n# Backend Engineer\n{said}\nPython"
    assert search.applied(page) == applied


async def test_a_search_needs_roles_places_and_a_browser(shown):
    browser = SearchBrowser()
    tools = job_tools.JobsToolset(browser)

    with pytest.raises(ToolError, match="no roles"):
        await tools._jobs_search(_searching("Find jobs"))
    profile.change("roles: Backend Engineer")
    with pytest.raises(ToolError, match="no places"):
        await tools._jobs_search(_searching("Find jobs"))
    browser.in_use = False
    with pytest.raises(ToolError, match="Chrome profiles"):
        await tools._jobs_search(_searching("Find jobs"), place="Pune")


async def test_a_running_search_says_how_it_is_going_and_can_be_stopped(judged, shown):
    profile.change("roles: Backend Engineer")
    profile.change("locations: Bengaluru")
    gate = asyncio.Event()
    browser = SearchBrowser()
    opened = browser.open_page

    async def slow(context, url, settle=""):
        if "/jobs/view/" in url:
            await gate.wait()
        return await opened(context, url, settle)

    browser.open_page = slow
    tools = job_tools.JobsToolset(browser)
    talk = _searching("Find jobs")
    started = await tools._jobs_search(talk)
    await asyncio.sleep(0.05)

    status = await tools._jobs_search(talk)
    stopped = await tools._jobs_search(talk, stop=True)
    with pytest.raises(asyncio.CancelledError):
        await tools._searching

    # Without write access Orbit jobs cannot be made yet: the search goes on anyway.
    assert "cannot be made yet" in started["orbit_jobs"]
    assert "LinkedIn: Backend Engineer in Bengaluru" in status["running"]
    assert "stopped" in stopped["stopped"]
    assert browser.held[-1] is None
    assert store.searches()[0].finished_at is not None
    assert talk.told == []


async def test_the_users_browser_tools_wait_while_orbit_holds_the_browser():
    browser = BrowserToolset(lambda endpoint, out: None)
    browser.hold("searching for jobs")

    with pytest.raises(ToolError, match="busy searching for jobs"):
        await browser._close("")
    browser.release()
    browser._check_free()


async def _searched_with_a_check(
    checking: int,
) -> tuple[SearchBrowser, SimpleNamespace]:
    profile.change("roles: Backend Engineer")
    profile.change("locations: Bengaluru")
    profile.change("years: 4")
    settings.change("pages_per_search: 1")
    settings.change("sites: linkedin")
    await sheet.make_book(None)
    browser = SearchBrowser()
    browser.checking = checking
    tools = job_tools.JobsToolset(browser)
    talk = _searching("Find me backend jobs in Bengaluru")
    await tools._jobs_search(talk)
    await tools._searching
    return browser, talk


async def test_a_check_that_the_user_is_a_person_is_theirs_and_the_search_goes_on(
    google, judged, shown, monkeypatch
):
    monkeypatch.setattr(applying, "LOOK_EVERY_SECONDS", 0.01)

    # The results page asks, and goes on asking while the user does the check.
    browser, talk = await _searched_with_a_check(checking=3)

    handed, finished = talk.told
    assert handed.startswith(
        "LinkedIn, during the job search, asks the user for a check that they are a "
        "person"
    )
    assert "never click, type into or try to get past it yourself" in handed
    # Meanwhile Orbit only waited, and the browser could be shown.
    assert ("waiting for the user to do a check that they are a person") in browser.held
    assert {job.company for job in store.jobs()} == {"Acme"}
    assert "1 kept" in finished


async def test_a_search_stops_when_the_check_is_not_done_in_time(
    google, judged, shown, monkeypatch
):
    monkeypatch.setattr(applying, "LOOK_EVERY_SECONDS", 0.01)
    monkeypatch.setattr(applying, "WAIT_SECONDS", 0.05)

    browser, talk = await _searched_with_a_check(checking=1000)

    assert store.jobs() == []
    assert (
        "was not done within ten minutes, so Orbit stopped searching it"
        in (talk.told[-1])
    )
    assert browser.holding == (None, False)


@pytest.mark.parametrize(
    "page, kind",
    [
        ("- Page Title: Security Verification | LinkedIn", search.A_PERSON),
        ("# Let\u2019s do a quick security check", search.A_PERSON),
        ('- checkbox "I\u2019m not a robot" [ref=f1e3]', search.A_PERSON),
        (
            "Our systems have detected unusual traffic from your network",
            search.A_PERSON,
        ),
        ("- Page URL: https://www.linkedin.com/authwall?trk=x", search.SIGNING_IN),
        ("# Access Denied\nYou don't have permission", search.REFUSED),
        ("# Backend Engineer\nAcme · Bengaluru", ""),
        # A form protected by a CAPTCHA it runs unseen asks nothing.
        ("# Apply\n" + "x" * 1300 + "\nprotected by reCAPTCHA Privacy - Terms", ""),
    ],
)
def test_what_stops_a_page_is_told_apart(page, kind):
    assert search.block(page) == kind


RAZORPAY = "https://job-boards.greenhouse.io/razorpay"
ACMEPAY = "https://careers.acmepay.com"
CRED = "https://jobs.lever.co/cred/0a1b2c3d-1111-2222-3333-444455556666"


async def test_google_finds_companies_own_postings_and_their_boards(
    google, judged, shown
):
    profile.change("roles: Backend Engineer")
    profile.change("locations: Bengaluru")
    profile.change("years: 4")
    settings.change("pages_per_search: 2")
    await sheet.make_book(None)
    browser = SearchBrowser()
    browser.google = [
        [
            (
                f"{RAZORPAY}/jobs/7001",
                "Job Application for Backend Engineer at Razorpay",
                "Bengaluru, Karnataka, India · 2 days ago",
            ),
            (CRED + "/apply", "CRED - Senior Backend Engineer (Pla...", "Bangalore"),
            (
                "https://job-boards.greenhouse.io/stripe/jobs/7002",
                "Job Application for Backend Engineer at Stripe",
                "Remote, United States",
            ),
            (
                "https://jobs.ashbyhq.com/acme/1b2c3d4e-1111-2222-3333-444455556666",
                "Account Executive @ Acme",
                "Bengaluru",
            ),
        ]
    ]
    browser.postings[f"{RAZORPAY}/jobs/7001"] = (
        "Job Application for Backend Engineer at Razorpay",
        "Bengaluru, India",
        "Python, 3+ years",
    )
    browser.postings[CRED] = (
        "CRED - Senior Backend Engineer (Platform)",
        "Bangalore",
        "Python and Go",
    )
    browser.boards[RAZORPAY] = (
        "Jobs at Razorpay",
        [("7003", "Backend Engineer, Payments", "Bengaluru", "Python, 2+ years")],
    )
    browser.careers = [
        [
            (
                f"{ACMEPAY}/jobs/backend-engineer-42",
                "Backend Engineer - Bengaluru | Acmepay Careers",
                "Bengaluru · 1 day ago",
            ),
            (
                f"{ACMEPAY}/jobs/backend",
                "Backend Engineer jobs at Acmepay",
                "Bengaluru, Pune and remote",
            ),
            ("https://www.naukri.com/job-listings-backend-1", "Backend Engineer", ""),
        ]
    ]
    browser.postings[f"{ACMEPAY}/jobs/backend-engineer-42"] = (
        "Backend Engineer | Acmepay",
        "Bengaluru, India",
        "Python, 3+ years",
    )
    browser.postings[f"{ACMEPAY}/jobs/backend"] = (
        "Backend Engineer jobs at Acmepay",
        "Bengaluru",
        "All open roles. Python",
    )
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(_searching("Find me jobs"))
    await tools._searching

    searched = next(url for url in browser.opened if "google.com/search" in url)
    query = parse_qs(urlsplit(searched).query)["q"][0]
    assert query.startswith(
        '("Backend Engineer" OR "Backend Developer") ("bengaluru" OR "bangalore")'
    )
    assert "(site:boards.greenhouse.io OR" in query and "after:20" in query
    assert "-intitle:intern" in query
    # A search of the job boards, and one of careers pages: a page short of results
    # is each one's last.
    assert sum("google.com/search" in url for url in browser.opened) == 2
    found = {(job.site, job.company, job.title) for job in store.jobs()}
    assert ("Greenhouse", "Razorpay", "Backend Engineer") in found
    # The title Google cut short is the posting's own.
    assert ("Lever", "CRED", "Senior Backend Engineer (Platform)") in found
    # Razorpay's whole board was searched after, for its other openings.
    assert ("Greenhouse", "Razorpay", "Backend Engineer, Payments") in found
    cred = next(job for job in store.jobs() if job.company == "CRED")
    assert cred.url == cred.apply_url == CRED and cred.applies_on == "company"
    assert (store.board("CRED").system, store.board("CRED").slug) == ("lever", "cred")
    boards_row, careers_row = [
        row for row in reversed(store.searches()) if row.site == "Google"
    ]
    assert boards_row.left_out == {"somewhere else": 1, "not one of the roles": 1}
    assert boards_row.kept == 2
    # A company's own careers page is kept as a company site's job; a page listing
    # its jobs is not one job; a job site's page is no company's own.
    acmepay = next(job for job in store.jobs() if job.company == "Acmepay")
    assert (acmepay.site, acmepay.title) == (
        "Company site",
        "Backend Engineer - Bengaluru",
    )
    assert acmepay.apply_url == f"{ACMEPAY}/jobs/backend-engineer-42"
    assert careers_row.left_out == {"not one job's posting": 1}
    assert careers_row.kept == 1


async def test_a_job_is_named_by_its_page_not_by_a_card_read_wrong(
    google, judged, shown, monkeypatch
):
    _backend_in_bengaluru()
    settings.change("sites: linkedin")
    # Read with the page around it, a card once gave LinkedIn's top bar as the company.
    monkeypatch.setitem(
        POSTINGS,
        "107",
        ("Backend Engineer", "0 notifications", "Bengaluru", "1 day ago", "Python", ""),
    )
    browser = SearchBrowser()
    browser.named["107"] = (
        "Backend Engineer | Wayne Enterprises | LinkedIn",
        "Wayne Enterprises",
    )
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(_searching("Find me jobs"))
    await tools._searching

    job = next(job for job in store.jobs() if job.key == "linkedin:107")
    assert (job.company, job.title) == ("Wayne Enterprises", "Backend Engineer")


async def test_a_company_to_avoid_is_left_out_once_its_page_names_it(
    google, judged, shown, monkeypatch
):
    _backend_in_bengaluru()
    profile.change("avoid: Wayne Enterprises")
    settings.change("sites: linkedin")
    monkeypatch.setitem(
        POSTINGS,
        "107",
        ("Backend Engineer", "0 notifications", "Bengaluru", "1 day ago", "Python", ""),
    )
    browser = SearchBrowser()
    browser.named["107"] = (
        "Backend Engineer | Wayne Enterprises | LinkedIn",
        "Wayne Enterprises",
    )
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(_searching("Find me jobs"))
    await tools._searching

    assert not any(job.key == "linkedin:107" for job in store.jobs())
    linkedin = next(row for row in reversed(store.searches()) if row.site == "LinkedIn")
    assert linkedin.left_out["a company to avoid"] == 1


def _on_google(browser: SearchBrowser, count: int) -> list[str]:
    """Google's results for a search of companies' job boards: a Backend Engineer
    posting in Bengaluru that fits at each of `count` companies, ten to a page.
    Returns the companies' boards."""
    shown = []
    for n in range(count):
        board = f"https://job-boards.greenhouse.io/company{n}"
        title = f"Job Application for Backend Engineer at Company{n}"
        shown.append((f"{board}/jobs/{8000 + n}", title, "Bengaluru · 2 days ago"))
        browser.postings[f"{board}/jobs/{8000 + n}"] = (
            title,
            "Bengaluru, India",
            "Python, 3+ years",
        )
    browser.google = [shown[at : at + 10] for at in range(0, count, 10)]
    return [f"https://job-boards.greenhouse.io/company{n}" for n in range(count)]


def _backend_in_bengaluru() -> None:
    profile.change("roles: Backend Engineer")
    profile.change("locations: Bengaluru")
    profile.change("years: 4")


async def test_every_posting_that_may_fit_is_read_and_every_company_searched(
    google, judged, shown
):
    _backend_in_bengaluru()
    browser = SearchBrowser()
    companies = _on_google(browser, 15)
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(_searching("Find me every job that fits"))
    await tools._searching

    boards_row = next(row for row in reversed(store.searches()) if row.site == "Google")
    # More than a share of a whole search once was: each one is read and kept.
    assert (boards_row.read, boards_row.kept) == (15, 15)
    assert boards_row.left_out == {}
    # And every company a job was kept at has its own board searched after.
    assert all(board in browser.opened for board in companies)


async def test_a_search_stops_once_it_has_found_the_most_jobs_one_search_keeps(
    google, judged, shown, monkeypatch
):
    monkeypatch.setattr(settings, "MOST_KEPT", 4)
    _backend_in_bengaluru()
    browser = SearchBrowser()
    _on_google(browser, 6)
    opened = browser.open_page

    async def as_a_page_takes(context, url, settle=""):
        # Judging goes on while the browser opens the next page.
        await asyncio.sleep(0.01)
        return await opened(context, url, settle)

    browser.open_page = as_a_page_takes
    tools = job_tools.JobsToolset(browser)
    talk = _searching("Find me jobs")

    await tools._jobs_search(talk)
    await tools._searching

    assert len(store.jobs()) == 4
    boards_row = next(row for row in reversed(store.searches()) if row.site == "Google")
    assert boards_row.kept == 4 and boards_row.read < 6
    assert boards_row.left_out == {"left once 4 jobs were found": 2}
    # Nothing after it was searched: not careers pages, LinkedIn or companies' boards.
    assert sum("google.com/search" in url for url in browser.opened) == 2
    assert not any("linkedin.com" in url for url in browser.opened)
    assert "It stopped once it had found 4 jobs" in talk.told[0]


async def test_jobs_already_applied_to_are_left_out(google, judged, shown, monkeypatch):
    _backend_in_bengaluru()
    # Applied to through Orbit on LinkedIn, and found now on Razorpay's own board.
    store.add_job(
        key="linkedin:555",
        site="LinkedIn",
        url=_posting_url("555"),
        company="Razorpay",
        title="Backend Engineer",
        location="Bengaluru",
        fit="high",
        why="",
        search=0,
        status=store.SUBMITTED,
    )
    browser = SearchBrowser()
    browser.google = [
        [
            (
                f"{RAZORPAY}/jobs/7001",
                "Job Application for Backend Engineer at Razorpay",
                "Bengaluru · 2 days ago",
            ),
            (CRED, "CRED - Backend Engineer", "Bangalore"),
        ]
    ]
    # Applied to elsewhere, as CRED's page and a LinkedIn card say.
    browser.postings[CRED] = ("CRED - Backend Engineer", "Bangalore", "You applied")
    monkeypatch.setitem(
        POSTINGS,
        "106",
        ("Backend Engineer", "Wayne", "Bengaluru", "Applied 2 weeks ago", "Python", ""),
    )
    tools = job_tools.JobsToolset(browser)

    await tools._jobs_search(_searching("Find me jobs"))
    await tools._searching

    found = {(job.company, job.status) for job in store.jobs()}
    assert ("Razorpay", store.SUBMITTED) in found
    assert not {"CRED", "Wayne"} & {company for company, _ in found}
    assert sum(company == "Razorpay" for company, _ in found) == 1
    boards_row = next(row for row in reversed(store.searches()) if row.site == "Google")
    assert boards_row.left_out == {"already applied to": 2}
    linkedin = next(row for row in reversed(store.searches()) if row.site == "LinkedIn")
    assert linkedin.left_out["already applied to"] == 1
    assert _posting_url("106") not in browser.opened


def test_googles_advanced_search_is_written_from_the_profile():
    known = {
        "skills": ["Python | AI/ML | 5y+", "RAG", "LangGraph", "SQL"],
        "years": "4",
    }

    boards_query = job_google.query(
        job_google.BOARDS, "AI Engineer", "Bengaluru", known, 14, date(2026, 10, 19)
    )
    careers_query = job_google.query(
        job_google.CAREERS, "AI Engineer", "remote", known, 7, date(2026, 10, 19)
    )

    assert boards_query.startswith('("AI Engineer" OR "ML Engineer" OR "LLM Engineer")')
    assert '("bengaluru" OR "bangalore") (Python OR RAG OR LangGraph)' in boards_query
    assert "site:jobs.lever.co" in boards_query and "after:2026-10-05" in boards_query
    assert "-intitle:intern -intitle:internship -intitle:course" in boards_query
    assert "(inurl:careers OR inurl:jobs OR intitle:careers)" in careers_query
    assert "-site:linkedin.com -site:naukri.com" in careers_query
    assert '"remote" ("india" OR "anywhere" OR "worldwide")' in careers_query
    assert "after:2026-10-12" in careers_query
    # Google reads no more than 32 words: the least telling are dropped first,
    # never the role.
    for query in (boards_query, careers_query):
        assert job_google._size(query) <= job_google.MOST_WORDS
        assert '"AI Engineer"' in query


def test_internships_are_searched_for_by_someone_with_no_experience_yet():
    query = job_google.query(
        job_google.BOARDS, "AI Engineer", "Pune", {"years": "0"}, 14, date(2026, 10, 19)
    )

    assert "-intitle:intern " not in query and "-intitle:course" in query


@pytest.mark.parametrize(
    ("url", "company"),
    [
        ("https://careers.acmepay.com/jobs/backend-engineer-42", "Acmepay"),
        ("https://acme.keka.com/careers/jobdetails/4567", "Acme"),
        ("https://www.initech.in/careers/openings/backend", "Initech"),
    ],
)
def test_a_companys_own_careers_page_is_a_result(url, company):
    item = {"href": url, "title": "Backend Engineer | Careers", "lines": []}

    result = job_google.page(item, {})

    assert result is not None and result.system is None
    assert result.listing.company == company
    assert result.listing.title == "Backend Engineer"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/view/123",
        "https://www.naukri.com/job-listings-backend-1",
        "https://www.acmepay.com/blog/we-are-hiring",
    ],
)
def test_job_sites_and_other_pages_are_no_companys_posting(url):
    assert job_google.page({"href": url, "title": "Backend Engineer"}, {}) is None
