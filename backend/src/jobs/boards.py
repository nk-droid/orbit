"""Companies' own job boards, where they post their jobs themselves: on Greenhouse,
Lever or Ashby, the hiring systems most companies use. A board is a public page that
lists every opening a company has, each linking to its posting and application.

Orbit learns a company's board from any address on it, such as the application a
posting leads to, or finds it by trying the board addresses a company's name gives.
Reading a board is done in the browser, as any page is.
"""

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .search import Listing


@dataclass(frozen=True)
class System:
    """A hiring system companies keep their job boards on."""

    key: str
    name: str
    # A company's board on it, from the company's name there.
    board: str
    # A posting's address, with its id as the first group.
    posting: str
    # The company's name there, from any address on the system.
    owner: str
    # A posting's title and its company, from the title of its page.
    titled: str
    # A later page of a company's board, on a system that shows a board's openings
    # a page at a time.
    paged: str = ""


SYSTEMS = (
    System(
        "greenhouse",
        "Greenhouse",
        "https://job-boards.greenhouse.io/{slug}",
        r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/[\w-]+/jobs/(\d+)",
        r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?!embed\b)([\w-]+)",
        r"^job application for (?P<title>.+) at (?P<company>.+?)$",
        # Fifty openings to a page.
        "https://job-boards.greenhouse.io/{slug}?page={page}",
    ),
    System(
        "lever",
        "Lever",
        "https://jobs.lever.co/{slug}",
        r"jobs(?:\.eu)?\.lever\.co/[\w.-]+/([0-9a-f]{8}-[0-9a-f-]{27})",
        r"jobs(?:\.eu)?\.lever\.co/([\w.-]+)",
        r"^(?P<company>.+?) - (?P<title>.+)$",
    ),
    System(
        "ashby",
        "Ashby",
        "https://jobs.ashbyhq.com/{slug}",
        r"jobs\.ashbyhq\.com/[\w.%-]+/([0-9a-f]{8}-[0-9a-f-]{27})",
        r"jobs\.ashbyhq\.com/([\w.%-]+)",
        r"^(?P<title>.+) @ (?P<company>.+?)$",
    ),
)
BY_KEY = {system.key: system for system in SYSTEMS}
# Any company's posting on any of these systems, as one pattern whose one group is
# the posting's whole address, so that postings on different systems never clash.
ANY_POSTING = (
    "(" + "|".join(re.sub(r"\((?!\?)", "(?:", s.posting) for s in SYSTEMS) + ")"
)


def system_of(url: str) -> tuple[System, str, str] | None:
    """The system a posting's address is on, the company's name there, and the
    posting's id. None for any other address."""
    for system in SYSTEMS:
        posting = re.search(system.posting, url, re.IGNORECASE)
        owner = re.search(system.owner, url, re.IGNORECASE)
        if posting and owner:
            return system, owner[1], posting[1]
    return None


def titled(title: str, system: System) -> tuple[str, str] | None:
    """A posting's title and its company from the title of its page, as the system
    writes it, such as "Job Application for Backend Engineer at Acme"."""
    found = re.match(system.titled, " ".join(title.split()), re.IGNORECASE)
    return (found["title"].strip(), found["company"].strip()) if found else None


# Words a company's legal name has that its board's name seldom does.
_SUFFIXES = frozenset(
    {
        "inc", "incorporated", "ltd", "limited", "llc", "llp", "pvt", "private",
        "plc", "gmbh", "corp", "corporation", "co", "company", "group", "holdings",
        "technologies", "technology", "tech", "labs", "lab", "software",
        "solutions", "systems", "services", "global", "india", "hq", "the",
    }
)  # fmt: skip


def board_of(url: str) -> tuple[System, str] | None:
    """The system and the company's name on it for any address on a company's
    board, such as a posting or an application. None for any other address."""
    parts = urlsplit(url)
    if parts.netloc.endswith("greenhouse.io") and "for" in parse_qs(parts.query):
        # An application embedded in a company's own site names its board.
        return BY_KEY["greenhouse"], parse_qs(parts.query)["for"][0]
    for system in SYSTEMS:
        if found := re.search(system.owner, url, re.IGNORECASE):
            return system, found[1]
    return None


def slugs(company: str) -> list[str]:
    """The names a company's board most likely has, the likeliest first: its name
    run together, without words such as "Technologies" or "Pvt Ltd", then its first
    word, then its name with hyphens."""
    words = re.findall(r"[a-z0-9]+", company.casefold().replace("&", " and "))
    plain = [word for word in words if word not in _SUFFIXES] or words
    found = ["".join(plain), plain[0] if plain else "", "-".join(plain), "".join(words)]
    return [slug for slug in dict.fromkeys(found) if slug]


def company_from_title(title: str) -> str:
    """A company's name from its board's title, such as "Jobs at GitLab", "OpenAI
    Jobs" or "Palantir Technologies"."""
    said = " ".join(title.split())
    said = re.sub(
        r"^(?:jobs|careers|current openings)\s+(?:at|@)\s+", "", said, flags=re.I
    )
    said = re.sub(r"\s+(?:jobs|careers)$", "", said, flags=re.I)
    return said.strip(" -\u2013|")


_BADGES = re.compile(r"\s+(?:new|hot|featured)$", re.IGNORECASE)
# A card's buttons, which say nothing of the job.
_BUTTONS = re.compile(r"(?:apply|apply now|apply for this job|save|share)", re.I)


def listing(item: dict[str, Any], company: str) -> Listing:
    """A posting as a company's board lists it: its title, and the rest of what its
    card says but its buttons, which holds its place."""
    lines = [" ".join(str(line).split()) for line in item.get("lines", [])]
    title = _BADGES.sub("", " ".join(str(item.get("title") or "").split()))
    rest = [
        line
        for line in lines
        if _BADGES.sub("", line) != title and not _BUTTONS.fullmatch(line)
    ]
    return Listing(
        id=str(item["id"]),
        # The posting itself, not its application, which a board's Apply links to.
        url=re.sub(
            r"/(?:apply|application)$", "", str(item["href"]).split("?")[0].rstrip("/")
        ),
        title=title,
        company=company,
        location=" · ".join(rest),
        posted="",
    )
