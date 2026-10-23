"""Searching Google for companies' own job postings, with Google's advanced search.

Each query is built from the user's profile the way a careful person writes one:
their roles and the titles that mean the same, as exact phrases joined by OR; their
top skills joined by OR; their place as each way it is written; after: the earliest
day a posting may be from; and -intitle: for what is never theirs, such as an
internship or a course. Two kinds of query are made:

- BOARDS: site: the job boards companies keep on Greenhouse, Lever and Ashby, whose
  postings Orbit reads and applies to best, and whose boards it then searches whole.
- CAREERS: inurl: or intitle: careers or jobs, anywhere but the job sites, -site:
  each of them, for companies with careers pages of their own.

It runs in the user's own browser, a page at a time, as their own search would; a
check that they are a person is theirs to do.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

from . import boards, relevance, search

BOARDS = "boards"
CAREERS = "careers"

# The job boards searched for companies' own postings.
BOARD_SITES = (
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
)
# Job sites and others whose pages are not a company's own postings. The first few
# are left out of a search of careers pages by name too.
NOT_COMPANIES = (
    "linkedin.com", "naukri.com", "indeed.com", "glassdoor.co.in", "foundit.in",
    "instahyre.com", "glassdoor.com", "cutshort.io", "wellfound.com", "hirist.tech",
    "shine.com", "timesjobs.com", "apna.co", "internshala.com", "ambitionbox.com",
    "simplyhired.com", "ziprecruiter.com", "monster.com", "google.com",
    "youtube.com", "facebook.com", "x.com", "twitter.com", "reddit.com",
    "medium.com", "quora.com",
)  # fmt: skip
NAMED_OUT = 6
# Google reads no more than this many words of a query.
MOST_WORDS = 32
# How many results a page of Google's has.
PER_PAGE = 10
# Titles that ask for other things than the user's work, left out by their titles.
NEVER = ("course", "training", "bootcamp", "certification")

# Titles that mean the same work as a role, by the field it is in.
_ALSO = (
    (
        frozenset({"ai", "ml", "llm", "llms", "genai"}),
        ("AI Engineer", "ML Engineer", "LLM Engineer", "GenAI Engineer",
         "Machine Learning Engineer"),
    ),
    (frozenset({"backend"}), ("Backend Engineer", "Backend Developer")),
    (frozenset({"frontend"}), ("Frontend Engineer", "Frontend Developer")),
    (frozenset({"fullstack"}), ("Full Stack Engineer", "Full Stack Developer")),
    (
        frozenset({"devops", "sre", "platform"}),
        ("DevOps Engineer", "Site Reliability Engineer", "Platform Engineer"),
    ),
)  # fmt: skip


def titles(role: str) -> list[str]:
    """The titles searched for a role: the role itself first, then the titles that
    mean the same work, such as "ML Engineer" for "AI Engineer"."""
    found = [" ".join(role.split())]
    said = relevance.words(role)
    for field, same in _ALSO:
        if said & field:
            found += same
    found.append(re.sub(r"\bEngineer\b", "Developer", found[0]))
    seen: set[str] = set()
    return [t for t in found if not (t.casefold() in seen or seen.add(t.casefold()))]


def skills(known: dict[str, Any]) -> list[str]:
    """The user's top skills, by name only, as a posting would name them."""
    listed = known.get("skills") or []
    if isinstance(listed, str):
        listed = listed.split(",")
    named: list[str] = []
    for skill in listed:
        name = re.split(r"[|(:\u2013-]", str(skill))[0].strip()
        if (
            name
            and len(name.split()) <= 3
            and name.casefold() not in (n.casefold() for n in named)
        ):
            named.append(name)
    return named[:3]


def _where(place: str) -> str:
    """A place as Google is asked for it: each way it is written, India for all of
    it, or for remote, remote and open to India or anywhere."""
    if search.is_remote(place):
        return '"remote" ("india" OR "anywhere" OR "worldwide")'
    if search.all_india(place):
        return '"india"'
    return "(" + " OR ".join(f'"{name}"' for name in relevance.names_of(place)) + ")"


def _any(items: list[str], quoted: bool = True) -> str:
    said = [f'"{item}"' if quoted else item for item in items]
    return said[0] if len(said) == 1 else "(" + " OR ".join(said) + ")"


def _size(query: str) -> int:
    return len(query.replace("(", " ").replace(")", " ").split())


def query(
    kind: str,
    role: str,
    place: str,
    known: dict[str, Any],
    days: int,
    today: date,
) -> str:
    """A Google search for companies' own postings of one of the user's roles in a
    place, posted within so many days, written with Google's operators. To keep
    within the words Google reads, what says least goes first: other titles beyond
    two, then what titles leave out beyond internships, then the other titles, the
    skills, and the job sites named; never the role itself."""
    wanted = titles(role)
    years = relevance.years_of(known)
    never = [*(("intern", "internship") if years is None or years >= 1 else ())]
    never += ["course", "training"]
    if years is not None and years < 10:
        never.append("director")
    never += ["bootcamp", "certification"]
    top = skills(known)
    named_out = list(NOT_COMPANIES[:NAMED_OUT])
    since = f"after:{(today - timedelta(days=days)).isoformat()}"

    def written() -> str:
        if kind == BOARDS:
            where = _any([f"site:{site}" for site in BOARD_SITES], quoted=False)
        else:
            where = "(inurl:careers OR inurl:jobs OR intitle:careers) " + " ".join(
                f"-site:{site}" for site in named_out
            )
        parts = [_any(wanted), _where(place)]
        if top:
            parts.append(_any(top, quoted=False))
        parts += [where, since, *(f"-intitle:{word}" for word in never)]
        return " ".join(parts)

    trims = (
        lambda: len(wanted) > 3 and bool(wanted.pop()),
        lambda: len(never) > 2 and bool(never.pop()),
        lambda: len(wanted) > 1 and bool(wanted.pop()),
        lambda: bool(top) and bool(top.pop()),
        lambda: bool(never) and bool(never.pop()),
        lambda: len(named_out) > 3 and bool(named_out.pop()),
    )
    while _size(written()) > MOST_WORDS and any(trim() for trim in trims):
        pass
    return written()


def address(
    kind: str,
    role: str,
    place: str,
    known: dict[str, Any],
    days: int,
    page: int,
    today: date,
) -> str:
    """One page of Google's results for the search."""
    params = {"q": query(kind, role, place, known, days, today)}
    if page > 1:
        params["start"] = str(PER_PAGE * (page - 1))
    return "https://www.google.com/search?" + urlencode(params)


def _host_pattern(hosts: tuple[str, ...]) -> str:
    return "|".join(re.escape(host) for host in hosts)


# A careers page Google may list: on no job site, with careers or jobs in its
# address. And a result of a search of careers pages, which may be a posting on a
# job board too. Each pattern's one group is a page's address after its scheme.
_CAREERS_ADDRESS = (
    r"(?!(?:[\w-]+\.)*(?:"
    + _host_pattern(NOT_COMPANIES)
    + r")(?:[/:]|$))[\w.-]+\.[a-z]{2,}(?::\d+)?/[^?#\s]*"
    r"(?:careers?|jobs?|positions?|openings?|vacanc|requisition)[^?#\s]*"
)
CAREERS_PAGE = r"https?://(" + _CAREERS_ADDRESS + ")"
CAREERS_RESULT = (
    r"https?://(?:www\.)?("
    + "|".join(re.sub(r"\((?!\?)", "(?:", s.posting) for s in boards.SYSTEMS)
    + "|"
    + _CAREERS_ADDRESS
    + ")"
)
_JOBLIKE = re.compile(
    r"/(?:[\w-]*[-_])?(?:careers?|jobs?|positions?|openings?|vacancies|vacancy|"
    r"requisitions?|jobdetails)(?:[-_/]|$)",
    re.IGNORECASE,
)
# The words of a careers page's title that name the site rather than the job.
_SITE_WORDS = re.compile(
    r"\s*[|\u00b7\u2013-]\s*[^|\u00b7\u2013-]*\b(?:careers?|jobs?)\b[^|\u00b7\u2013-]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Found:
    """A company's posting as Google lists it, and the board it is on, if it is on
    one of the systems Orbit knows."""

    listing: search.Listing
    system: boards.System | None
    slug: str


_CUT = re.compile(r"\s*(?:\.\.\.|…)$")


def _shown(item: dict[str, Any]) -> tuple[str, bool]:
    shown = " ".join(str(item.get("title") or "").split())
    return _CUT.sub("", shown), bool(_CUT.search(shown))


def _rest(item: dict[str, Any], shown: str) -> list[str]:
    lines = [" ".join(str(line).split()) for line in item.get("lines", [])]
    return [line for line in lines if line and _CUT.sub("", line) != shown]


def found(item: dict[str, Any], names: dict[tuple[str, str], str]) -> Found | None:
    """A posting from a result the listings script found on a job board: its
    address, its title and company from the title Google shows, and what its
    snippet says, which may hold its place and age. `names` are the companies Orbit
    knows, by system and board name. None for a result that is no such posting."""
    href = str(item.get("href") or "")
    on = boards.system_of(href)
    if on is None:
        return None
    system, slug, number = on
    shown, cut = _shown(item)
    parsed = boards.titled(shown, system)
    title = parsed[0] if parsed else shown
    company = (
        names.get((system.key, slug.casefold()))
        or (parsed[1] if parsed and not cut else "")
        or slug.replace("-", " ").title()
    )
    rest = _rest(item, shown)
    url = re.sub(
        r"/(?:apply|application)$", "", href.split("?")[0].split("#")[0].rstrip("/")
    )
    listing = search.Listing(
        id=number,
        url=url,
        title=title,
        company=company,
        location=" · ".join(rest),
        posted=search.posted_in(rest),
    )
    return Found(listing, system, slug)


def _company_of(host: str) -> str:
    """A company's name from its own site's address, such as Razorpay for
    careers.razorpay.com."""
    labels = [
        label
        for label in host.casefold().removeprefix("www.").split(".")
        if label not in ("careers", "career", "jobs", "job", "apply", "join", "work")
    ]
    name = labels[0] if len(labels) > 1 else (labels[0] if labels else host)
    return name.replace("-", " ").title()


def page(item: dict[str, Any], names: dict[tuple[str, str], str]) -> Found | None:
    """A posting from a result of a search of careers pages: one on a job board of a
    system Orbit knows, or any other page of a company's own whose address says it
    is about careers or jobs. None for a job site's page, or any other page."""
    href = str(item.get("href") or "")
    if boards.system_of(href) is not None:
        return found(item, names)
    parts = urlsplit(href)
    host = parts.netloc.casefold().removeprefix("www.")
    if not host or any(
        host == other or host.endswith("." + other) for other in NOT_COMPANIES
    ):
        return None
    if not _JOBLIKE.search(parts.path):
        return None
    shown, _ = _shown(item)
    rest = _rest(item, shown)
    url = f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")
    listing = search.Listing(
        id=hashlib.sha1(url.casefold().encode()).hexdigest()[:16],
        url=url,
        title=_SITE_WORDS.sub("", shown),
        company=_company_of(host),
        location=" · ".join(rest),
        posted=search.posted_in(rest),
    )
    return Found(listing, None, "")
