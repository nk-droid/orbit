"""Searching job sites, the parts that do not need the browser: where a site's search
for a role and place is, how its postings' addresses look, what a listing's card says,
and how old a posting is.

The browser itself is driven by the job tools (agent_tools/jobs.py), one page at a
time, the way a person would.
"""

import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Any
from urllib.parse import urlencode, urlsplit

import human_checks


@dataclass(frozen=True)
class Listing:
    """A posting as a results page lists it."""

    id: str
    url: str
    title: str
    company: str
    # Where its card says the job is, or, on a card that does not say it apart,
    # the card's words that may.
    location: str
    posted: str
    # Its card says it is applied to on the job board itself, with Easy Apply.
    easy_apply: bool = False
    # Its card says the user has applied to it, as a job site they are signed in to
    # marks it.
    applied: bool = False
    # The years of experience its card says it asks for, as experience_of() writes
    # them, and the pay its card names.
    experience: str = ""
    salary: str = ""


@dataclass(frozen=True)
class Site:
    key: str
    name: str
    # A posting's address, with the posting's id as its first group.
    posting: str
    # What an Apply button says when the job can be applied to only on the site.
    on_site_only: str
    # How a posting's page is titled, naming its role and its company: each a
    # pattern with those two groups.
    titles: tuple[str, ...] = ()
    # Every job on the site is applied to there, so choosing the site is choosing
    # its jobs: they are kept whether or not board-only jobs are.
    applied_on_site: bool = False
    # How many pages of results the site shows a search, when it shows fewer than a
    # search may read.
    pages: int | None = None
    # A posting's own page, by its id, where the site's results may link to a
    # posting through another page.
    address: str = ""
    # Its own way to apply sends the application at once, with the user's profile
    # on the site, as Naukri's Apply does: there is no form to fill in first, so it
    # is pressed only on the user's yes to send it.
    sends_on_press: bool = False

    def search(self, role: str, place: str, days: int, page: int) -> str:
        """The address of one page of the site's results for a role and a place."""
        if self.key == "linkedin":
            params = {"keywords": role, "f_TPR": f"r{days * 86400}"}
            if is_remote(place):
                params |= {"f_WT": "2", "location": "India"}
            else:
                params["location"] = "India" if all_india(place) else place
            if page > 1:
                params["start"] = str(25 * (page - 1))
            return "https://www.linkedin.com/jobs/search/?" + urlencode(params)
        if self.key == "cutshort":
            return _cutshort(role, place)
        more = f"-{page}" if page > 1 else ""
        if is_remote(place):
            return f"https://www.naukri.com/{_slug(role)}-jobs{more}?" + urlencode(
                {"jobAge": days, "wfhType": 2}
            )
        if all_india(place):
            return f"https://www.naukri.com/{_slug(role)}-jobs{more}?" + urlencode(
                {"jobAge": days}
            )
        return f"https://www.naukri.com/{_slug(role)}-jobs-in-{_slug(place)}{more}?" + (
            urlencode({"jobAge": days})
        )

    def listing(self, item: dict[str, Any]) -> Listing:
        """A listing from what the listings script found on one of the site's cards."""
        found = _cutshort_listing(item) if self.key == "cutshort" else listing(item)
        if self.address:
            return replace(found, url=self.address.format(id=found.id))
        return found


SITES = {
    "linkedin": Site(
        "linkedin",
        "LinkedIn",
        # Its results link a card to its posting, or to the results with the posting
        # open beside them.
        r"(?:/jobs/view/(?:[^/?#]*-)?|[?&]currentJobId=)(\d{6,})",
        r"\beasy apply\b",
        (
            # Signed in, with a count of notifications first when there are any.
            r"(?:\(\d+\+?\)\s*)?(?P<role>.+?) \| (?P<company>[^|]+?) \| LinkedIn",
            r"(?P<company>.+?) hiring (?P<role>.+?)(?: in [^|]+)? \| LinkedIn",
        ),
        address="https://www.linkedin.com/jobs/view/{id}/",
    ),
    "naukri": Site(
        "naukri",
        "Naukri",
        r"/job-listings-[^?#]*?-(\d{6,})(?:[?#/]|$)",
        r"^\s*apply\s*$|\bapply on naukri\b",
        sends_on_press=True,
    ),
    "cutshort": Site(
        "cutshort",
        "Cutshort",
        r"/job/[^/?#]*-([A-Za-z0-9]{8})(?:[?#/]|$)",
        # A posting's own button: its "Apply now" ones are for other jobs like it.
        r"^\s*apply to this job\s*$",
        # Through an agency, "via" its name runs into the role's, so the card's
        # role stands.
        (r"(?P<company>.+?) is hiring (?!via )(?P<role>.+?) job(?: .+?)? \| Cutshort",),
        applied_on_site=True,
        # Its newest 50 jobs, and no more: another page shows them again.
        pages=1,
    ),
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


# Cutshort lists jobs by field, each in its own words, rather than by what a role is
# called: the first field a role names is searched, and else the role's own words
# without its level, as Cutshort names some fields after a role ("graphic-designer").
_CUTSHORT_FIELDS = (
    (r"\b(?:nlp|natural language)\b", "natural-language-processing-nlp"),
    (r"\bcomputer vision\b", "computer-vision"),
    (r"\bdeep learning\b", "deep-learning"),
    (r"\b(?:ml|machine learning|mlops)\b", "machine-learning-ml"),
    (
        r"\b(?:ai|artificial intelligence|llms?|gen ?ai|generative|agentic)\b",
        "artificial-intelligence-ai",
    ),
    (r"\bdata scien", "datascience"),
    (r"\bdata engineer", "data-engineering"),
    (r"\b(?:data|business) analy", "data-analytics"),
    (r"\bfull[ -]?stack\b", "fullstack-developer"),
    (r"\bback[ -]?end\b", "backend-developer"),
    (r"\bfront[ -]?end\b", "frontend-developer"),
    (r"\b(?:devops|sre|site reliability|platform)\b", "devops"),
    (r"\bandroid\b", "android-developer"),
    (r"\bios\b", "ios-developer"),
    (r"\bproduct manager\b", "product-manager"),
    (r"\b(?:qa|sdet|testing|test automation)\b", "qa-software-testing"),
    (r"\b(?:ux|ui)\b", "ux-designer"),
    (r"\bpython\b", "python"),
    (r"\bjava\b", "java"),
    (r"\bnode(?:\.?js)?\b", "nodejs"),
    (r"\b(?:go|golang)\b", "golang"),
    (r"\breact(?:\.?js)?\b", "reactjs"),
)
_LEVEL = re.compile(
    r"\b(?:senior|sr|junior|jr|lead|staff|principal|associate|head|i{1,3}|iv|\d)\b\.?"
)
# The fields Cutshort has a page of remote jobs for. The rest are searched across
# India, and their remote jobs kept as their places say.
_CUTSHORT_REMOTE = frozenset({
    "android-developer", "data-analytics", "datascience", "devops",
    "frontend-developer", "golang", "ios-developer", "java", "machine-learning-ml",
    "nodejs", "python", "qa-software-testing", "reactjs",
})  # fmt: skip
# The cities Cutshort lists jobs in, by its name for each, and how else each is said.
# Other places are searched across India, and their jobs kept as their places say.
_CUTSHORT_CITIES = {
    "bangalore-bengaluru": ("bengaluru", "bangalore", "blr"),
    "delhi-ncr-gurgaon-noida": (
        "delhi", "new delhi", "ncr", "gurugram", "gurgaon", "noida",
    ),
    "mumbai": ("mumbai", "bombay", "navi mumbai", "thane"),
    "pune": ("pune",),
    "hyderabad": ("hyderabad", "secunderabad"),
    "chennai": ("chennai", "madras"),
    "kolkata": ("kolkata", "calcutta"),
    "kochi-cochin": ("kochi", "cochin"),
    "ahmedabad": ("ahmedabad",),
    "jaipur": ("jaipur",),
    "chandigarh": ("chandigarh",),
    "coimbatore": ("coimbatore",),
    "indore": ("indore",),
    "bhubaneswar": ("bhubaneswar",),
}  # fmt: skip


def _cutshort(role: str, place: str) -> str:
    said = " ".join(re.findall(r"[a-z0-9.]+", role.casefold()))
    field = next(
        (field for pattern, field in _CUTSHORT_FIELDS if re.search(pattern, said)),
        _slug(_LEVEL.sub(" ", said)),
    )
    where = " ".join(re.findall(r"[a-z]+", place.casefold()))
    city = next(
        (
            city
            for city, names in _CUTSHORT_CITIES.items()
            if any(re.search(rf"\b{name}\b", where) for name in names)
        ),
        None,
    )
    if is_remote(place) and field in _CUTSHORT_REMOTE:
        return f"https://cutshort.io/jobs/remote-{field}-jobs"
    if city is None or is_remote(place):
        return f"https://cutshort.io/jobs/{field}-jobs"
    return f"https://cutshort.io/jobs/{field}-jobs-in-{city}"


def titled(page_title: str, site: Site) -> tuple[str, str] | None:
    """The role and the company a posting's page names in its title, as the site
    titles it, or None when it does not."""
    said = " ".join(page_title.split())
    for pattern in site.titles:
        if found := re.fullmatch(pattern, said):
            return found["role"].strip(), found["company"].strip()
    return None


# A place that is all of India, as people say it: "Anywhere in India", "Pan India".
_ALL_INDIA = re.compile(
    r"(?:(?:any|anywhere|where|location|all|over|of|pan|across|whole|throughout|"
    r"everywhere|in)\s+)*india"
)


def all_india(place: str) -> bool:
    """Whether a place is all of India, rather than a city in it or remote work."""
    return bool(_ALL_INDIA.fullmatch(" ".join(re.findall(r"[a-z]+", place.casefold()))))


def is_remote(place: str) -> bool:
    """Whether a place is remote work, as "Remote" or "Anywhere" says: not "Anywhere
    in India", which is every place in India."""
    return not all_india(place) and bool(
        re.search(r"\b(remote|work from home|wfh|anywhere)\b", place, re.I)
    )


_AGO = re.compile(
    r"\b(?:\d+\+?\s*(?:minute|min|hour|hr|day|week|month)s?\s+ago|just now|today|"
    r"yesterday|few hours ago)\b",
    re.IGNORECASE,
)
# The day a card says a job was posted on, as in "Posted 17 Oct 2026".
_POSTED_ON = re.compile(r"(?:re)?posted\s+(?:on\s+)?(?P<day>.+)", re.IGNORECASE)
# What a card says that is none of its title, company, place or age: its badges,
# counts and buttons, who posted it, and the kind of job it is.
_NOISE = re.compile(
    r"^(?:promoted|easy apply|actively (?:hiring|recruiting|reviewing applicants)|"
    r"be an early applicant|(?:over )?\d[\d,]*\+?\s+(?:applicants?|views?|"
    r"people clicked apply)|viewed|saved?|save the job|hide|new|verified|"
    r"with verification|reposted|top applicant|\d+ connections? works? here|"
    r"\d+ alumni works? here|.*school alumni.*|applied|apply(?: now)?|"
    r"apply on company (?:website|site)|responses managed off linkedin|"
    r"no longer accepting applications|your profile matches.*|posted by .*|"
    r"full[- ]time|part[- ]time|contract|temporary|freelance|"
    r"\d+(\.\d+)?\s*reviews?|\d(\.\d)?)$",
    re.IGNORECASE,
)
# A line of a card in parts, as in "Bengaluru · 3 days ago · Over 100 applicants".
_PARTS = re.compile(r"\s+[·•]\s+")
# Years of experience, as cards and postings say them: "2-6 Yrs", "3 - 5 years",
# "5+ years", "at least 4 years".
_RANGE = (
    r"(?P<least>\d{1,2}(?:\.\d)?)\s*(?:(?P<plus>\+)|(?:-|\u2013|\u2014|to)\s*"
    r"(?P<most>\d{1,2}(?:\.\d)?)\s*\+?)?\s*(?:years?|yrs?)\b"
)
_YEARS = re.compile(
    rf"(?:\b(?P<said>at least|minimum(?: of)?|min\.?|over|more than|up ?to)\s+)?"
    rf"{_RANGE}",
    re.IGNORECASE,
)
# A card's part that says only that, as in "Exp: 2-5 Yrs", or that a job is for
# freshers.
_EXPERIENCE = re.compile(
    rf"(?:exp(?:erience)?\s*[:\-\u2013]?\s*)?{_RANGE}(?:\s+(?:of\s+)?exp(?:erience)?)?"
    r"|freshers?",
    re.IGNORECASE,
)
# What a posting's address may say of the experience it asks for, as Naukri's do:
# "...-bengaluru-2-to-6-years-191026914436".
_ADDRESS_YEARS = re.compile(r"[-_/](\d{1,2})-to-(\d{1,2})-years?(?=[-_/.?#]|$)", re.I)
# Money, as a card says what a job pays: "₹ 10-20 Lacs P.A.", "$120K/yr".
_MONEY = re.compile(
    r"[₹$€£]|\b(?:inr|usd|eur|gbp|lpa|lacs?|lakhs?|ctc|"
    r"per (?:annum|year|month|hour))\b|/\s?(?:yr|year|mo|month|hr|hour)\b|\bp\.\s?a\b",
    re.IGNORECASE,
)


def experience_of(text: str) -> str:
    """The years of experience a card or a posting says a job asks for, always written
    the same way: "2\u20136 years", "5+ years", "3 years", "Up to 2 years" or
    "Fresher". "" when it says none."""
    said = " ".join(text.split())
    if re.fullmatch(r"freshers?(?: only)?", said, re.IGNORECASE):
        return "Fresher"
    if not (found := _YEARS.search(said)):
        return ""
    least, most = found["least"], found["most"]
    if most:
        return f"{least}\u2013{most} years"
    qualified = (found["said"] or "").casefold()
    if qualified.replace(" ", "") == "upto":
        return f"Up to {least} years"
    if found["plus"] or qualified:
        return f"{least}+ years"
    return f"{least} year" if least == "1" else f"{least} years"


def experience_in(url: str) -> str:
    """The years of experience a posting's address says it asks for, as Naukri's do:
    "...-bengaluru-2-to-6-years-191026914436". "" when it does not say."""
    found = _ADDRESS_YEARS.search(urlsplit(url).path)
    return f"{found[1]}\u2013{found[2]} years" if found else ""


def experience_said(page: str) -> str:
    """The years of experience a posting's page says it asks for: from the first of
    its lines that names both years and experience, as "3+ years of experience in
    Python" or "Exp: 2-5 Yrs" do, or that says nothing else, as "2-5 Yrs" under an
    "Experience" heading does. "" when none does."""
    for line in page.split("### Text", 1)[-1].splitlines():
        said = line.strip(" -*•●|")
        if _EXPERIENCE.fullmatch(said) or (
            re.search(r"\bexp(?:erience)?\b", said, re.IGNORECASE)
            and _YEARS.search(said)
        ):
            return experience_of(said)
    return ""


def least_years(asked: str) -> int | None:
    """The fewest years of experience a posting asks for, from how it says it, such
    as "5-10 years" or "at least 3 years"."""
    said = experience_of(asked)
    if said in ("Fresher", "") or said.startswith("Up to"):
        return 0 if said else None
    return int(float(re.match(r"[\d.]+", said)[0]))


def _pay(part: str) -> bool:
    """Whether a card's part says what the job pays, or that it does not say."""
    return part.casefold() == "not disclosed" or (
        len(part) <= 60 and bool(re.search(r"\d", part)) and bool(_MONEY.search(part))
    )


def _age(part: str) -> str:
    """How long ago a card's part says the job was posted, or the day it says it
    was: "3 days ago", "17 Oct 2026". "" when it says neither."""
    if found := _AGO.search(part):
        return found[0]
    if (found := _POSTED_ON.fullmatch(part)) and _DAY.search(found["day"].casefold()):
        return found["day"]
    return ""


def place(text: str) -> str:
    """A place as a card or a page says it, fit to keep: "" for words that are no
    place, such as a search result's snippet or a link, where a card says them
    instead."""
    said = " ".join(text.split())
    if len(said) > 80 or re.search(r"https?:|www\.|\u203a|(?:\.\.\.|…)$", said):
        return ""
    return said


# When, as a site says the user applied: "on 12 Oct", "3 days ago" or "today".
_WHEN = r"(?:\s+(?:on\s+[\w ,.]{1,30}|\d+\+?\s*\w+\s+ago|just now|today|yesterday))"
# What a site says once the user has applied to a job, in a line of its own.
_APPLIED = re.compile(
    rf"(?:you(?:'ve|\u2019ve| have)?\s+(?:already\s+)?applied|already applied){_WHEN}?"
    r"(?:\s+(?:for|to)\s+(?:this|the)\s+(?:job|position|role|opening))?[.!]?"
    rf"|applied{_WHEN}|application (?:submitted|sent){_WHEN}?[.!]?",
    re.IGNORECASE,
)


def listing(item: dict[str, Any]) -> Listing:
    """A listing from what the listings script found on its card: its title, then its
    company and its place, the first two lines that are none of what else a card
    says: how long ago the job was posted, the years of experience and the pay it
    names, and its badges, counts and buttons. A line in parts, as in "Bengaluru · 3
    days ago · Over 100 applicants", is read part by part."""
    lines = [" ".join(str(line).split()) for line in item.get("lines", [])]
    title = " ".join(str(item.get("title") or (lines[0] if lines else "")).split())
    parts = [
        part
        for line in lines
        if re.sub(r"\s+with verification$", "", line, flags=re.I).casefold()
        != title.casefold()
        for part in _PARTS.split(line)
        if part
    ]
    # On a card, "Applied" alone says it too: "Applied 3 days ago" is no posting's age.
    marks = [
        part
        for part in parts
        if part.casefold() == "applied" or _APPLIED.fullmatch(part)
    ]
    rest = [part for part in parts if part not in marks]
    posted = next((age for part in rest if (age := _age(part))), "")
    experience = next(
        (experience_of(part) for part in rest if _EXPERIENCE.fullmatch(part)), ""
    )
    pay = next((part for part in rest if _pay(part)), "")
    easy = any(re.fullmatch(r"easy apply", part, re.IGNORECASE) for part in rest)
    named = [
        part
        for part in rest
        if not (
            _age(part)
            or _EXPERIENCE.fullmatch(part)
            or _pay(part)
            or _NOISE.match(part)
        )
    ]
    return Listing(
        id=str(item["id"]),
        url=str(item["href"]).split("?")[0],
        title=title,
        company=named[0] if named else "",
        location=place(named[1]) if len(named) > 1 else "",
        posted=posted,
        easy_apply=easy,
        applied=bool(marks),
        experience=experience,
        salary="" if pay.casefold() == "not disclosed" else pay,
    )


# What a Cutshort card says besides its title, company, place and age: who posted it,
# how many answered or hire for it, and its button.
_CUTSHORT_NOISE = re.compile(
    r"^(?:posted by .*|agency job.*|apply now|apply to this job|"
    r"\d+ (?:candid answers?|recruiters?)|\+\d+ more)$",
    re.IGNORECASE,
)


def _cutshort_listing(item: dict[str, Any]) -> Listing:
    """A listing from a Cutshort card: its title, its company ("at Acme", or an
    agency's client), who posted it, then its facts on one line, as in "Bengaluru
    (Bangalore) · 3 - 5 years · ₹20L - ₹30L / yr · Posted 17 Oct 2026", and its
    skills."""
    lines = [" ".join(str(line).split()) for line in item.get("lines", [])]
    title = " ".join(str(item.get("title") or (lines[0] if lines else "")).split())
    rest = [line for line in lines if line.casefold() != title.casefold()]
    marks = [
        line
        for line in rest
        if line.casefold() == "applied" or _APPLIED.fullmatch(line)
    ]
    at = next((n for n, line in enumerate(rest) if " · " in line), len(rest))
    facts = [part.strip() for part in rest[at].split(" · ")] if at < len(rest) else []
    company = next(
        (
            line
            for line in rest[:at]
            if line not in marks and not _CUTSHORT_NOISE.match(line)
        ),
        "",
    )
    posted = next((part for part in facts if part.casefold().startswith("posted ")), "")
    pay = next((part for part in facts[1:] if _pay(part)), "")
    return Listing(
        id=str(item["id"]),
        url=str(item["href"]).split("?")[0],
        title=title,
        company=re.sub(r"^at\s+", "", company),
        location=facts[0] if facts else "",
        posted=posted[len("posted ") :],
        applied=bool(marks),
        experience=next(
            (experience_of(part) for part in facts[1:] if _EXPERIENCE.fullmatch(part)),
            "",
        ),
        salary="" if pay.casefold() == "not disclosed" else pay,
    )


def applied(page: str) -> bool:
    """Whether a posting's page says the user has applied to it, as a job site they
    are signed in to shows it: "Applied 3 days ago", "You applied", "Application
    submitted". "Applied" alone is not taken for it: a page's menus may say it."""
    for line in page.splitlines():
        for part in line.lstrip("-# ").split(" | "):
            if _APPLIED.fullmatch(part.strip()):
                return True
    return False


def posted_in(lines: list[str]) -> str:
    """What a listing's lines say of how long ago it was posted, such as "3 days
    ago", or "" when they do not say."""
    for line in lines:
        if found := _AGO.search(line):
            return found[0]
    return ""


_UNITS = {"minute": 0, "min": 0, "hour": 0, "hr": 0, "day": 1, "week": 7, "month": 30}
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
           "dec")  # fmt: skip
_MONTH = rf"({'|'.join(_MONTHS)})[a-z]*\.?"
# A day, as in "8 Oct 2026" or "Oct 8, 2026", its year left out in this one's.
_DAY = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH}(?:,?\s+(\d{{4}}))?\b|"
    rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b"
)


def days_old(posted: str, today: date | None = None) -> int | None:
    """How many days ago a posting went up, from what its listing says: how long ago,
    as in "3 days ago", or the day, as in "8 Oct 2026" or "2026-10-08". None when it
    does not say."""
    said = posted.casefold()
    if re.search(r"just now|today|few hours ago", said):
        return 0
    if "yesterday" in said:
        return 1
    today = today or date.today()
    if found := re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", said):
        try:
            return max((today - date(*map(int, found.groups()))).days, 0)
        except ValueError:
            return None
    if found := re.search(r"(\d+)\+?\s*(minute|min|hour|hr|day|week|month)s?", said):
        return int(found[1]) * _UNITS[found[2]]
    if not (found := _DAY.search(said)):
        return None
    day, month, year = found.group(1, 2, 3) if found[1] else found.group(5, 4, 6)
    try:
        when = date(
            int(year) if year else today.year, _MONTHS.index(month) + 1, int(day)
        )
    except ValueError:
        return None
    if not year and when > today:
        when = when.replace(year=today.year - 1)
    return max((today - when).days, 0)


@dataclass(frozen=True)
class Facts:
    """What a posting's page says of the job in the shape search engines read, a
    JobPosting: where it is, when it went up, the years of experience it asks for and
    its pay, each "" where the page does not say."""

    location: str = ""
    posted: str = ""
    experience: str = ""
    salary: str = ""


# Countries as postings name them by their codes, written out where they are the
# whole of a place.
_COUNTRIES = {
    "IN": "India", "US": "United States", "USA": "United States",
    "GB": "United Kingdom", "UK": "United Kingdom", "CA": "Canada",
    "AU": "Australia", "NZ": "New Zealand", "DE": "Germany", "FR": "France",
    "NL": "Netherlands", "IE": "Ireland", "ES": "Spain", "PT": "Portugal",
    "IT": "Italy", "PL": "Poland", "SE": "Sweden", "CH": "Switzerland",
    "AT": "Austria", "BE": "Belgium", "DK": "Denmark", "FI": "Finland",
    "NO": "Norway", "CZ": "Czechia", "SK": "Slovakia", "HU": "Hungary",
    "RO": "Romania", "BG": "Bulgaria", "GR": "Greece", "TR": "Turkey",
    "UA": "Ukraine", "IL": "Israel", "AE": "United Arab Emirates",
    "SA": "Saudi Arabia", "QA": "Qatar", "EG": "Egypt", "ZA": "South Africa",
    "NG": "Nigeria", "KE": "Kenya", "SG": "Singapore", "MY": "Malaysia",
    "ID": "Indonesia", "PH": "Philippines", "VN": "Vietnam", "TH": "Thailand",
    "JP": "Japan", "KR": "South Korea", "CN": "China", "HK": "Hong Kong",
    "TW": "Taiwan", "LK": "Sri Lanka", "BD": "Bangladesh", "PK": "Pakistan",
    "NP": "Nepal", "BR": "Brazil", "MX": "Mexico", "AR": "Argentina",
    "CO": "Colombia", "CL": "Chile", "PE": "Peru", "UY": "Uruguay",
    "CR": "Costa Rica", "EE": "Estonia", "LV": "Latvia", "LT": "Lithuania",
    "RS": "Serbia", "HR": "Croatia", "SI": "Slovenia", "LU": "Luxembourg",
}  # fmt: skip
_PER = {"HOUR": "an hour", "DAY": "a day", "WEEK": "a week", "MONTH": "a month",
        "YEAR": "a year"}  # fmt: skip


def _many(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [] if value is None else [value]


def _name(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("name") or value.get("addressCountry") or ""
    said = " ".join(str(value or "").split())
    return "" if said.casefold() in ("na", "n/a", "none", "null") else said


def _address(where: Any) -> str:
    """A JobPosting's place, as its town, region and country, or its name. A place
    may give several addresses."""
    address = where.get("address", where) if isinstance(where, dict) else where
    if isinstance(address, list):
        said = (_address(part) for part in address)
        return " · ".join(dict.fromkeys(part for part in said if part))
    if not isinstance(address, dict):
        return _name(address)
    found: list[str] = []
    for key in ("addressLocality", "addressRegion", "addressCountry"):
        said = _name(address.get(key))
        said = _COUNTRIES.get(said.upper(), said) if key == "addressCountry" else said
        if said and not any(said.casefold() in part.casefold() for part in found):
            found.append(said)
    return ", ".join(found) or _name(where)


def _money(amount: Any) -> str:
    """A JobPosting's pay, as in "INR 2,200,000\u20135,710,000 a year"."""
    if not isinstance(amount, dict):
        return ""
    value = amount.get("value")
    if not isinstance(value, dict):
        value = {"value": value}

    def number(key: str) -> str:
        try:
            return f"{float(value.get(key)):,.0f}"
        except (TypeError, ValueError):
            return ""

    least, most, only = number("minValue"), number("maxValue"), number("value")
    said = (
        f"{least}\u2013{most}"
        if least and most and least != most
        else (least or most or only)
    )
    if not said:
        return ""
    unit = _PER.get(str(value.get("unitText") or "").upper(), "")
    return " ".join(
        part for part in (str(amount.get("currency") or ""), said, unit) if part
    )


def _asked(requirements: Any) -> str:
    """The years of experience a JobPosting asks for: in words, or in months."""
    for asked in _many(requirements):
        if isinstance(asked, dict) and asked.get("monthsOfExperience") is not None:
            try:
                years = float(asked["monthsOfExperience"]) / 12
            except (TypeError, ValueError):
                continue
            return "Fresher" if years == 0 else f"{years:g}+ years"
        if said := experience_of(
            str(asked.get("description", "") if isinstance(asked, dict) else asked)
        ):
            return said
    return ""


def facts(data: Any) -> Facts:
    """The facts a JobPosting holds, as the job posting script found it on a page:
    its places (and that it is remote, and who may apply from where), the day it
    went up, the experience it asks for and its pay."""
    if not isinstance(data, dict):
        return Facts()
    places = [_address(where) for where in _many(data.get("jobLocation"))]
    where = [said for said in dict.fromkeys(places) if said]
    if str(data.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        open_to = [
            _COUNTRIES.get(said.upper(), said)
            for area in _many(data.get("applicantLocationRequirements"))
            if (said := _name(area))
        ]
        where.insert(0, f"Remote ({', '.join(open_to)})" if open_to else "Remote")
    posted = re.match(r"\d{4}-\d{2}-\d{2}", str(data.get("datePosted") or ""))
    return Facts(
        location=" · ".join(where),
        posted=posted[0] if posted else "",
        experience=_asked(data.get("experienceRequirements")),
        salary=_money(data.get("baseSalary")),
    )


def unread(page: str) -> bool:
    """Whether a page shows nothing to read under where it is, as one read before
    its site has filled it in does not."""
    return not re.search(r"[^\W\d_]{2,}", page.split("### Text", 1)[-1])


# What stops a page from being the one asked for.
REFUSED = "refused"
A_PERSON = "a person"
SIGNING_IN = "signing in"


def block(text: str) -> str:
    """What stops a page from being what was asked for: the site refusing the
    browser outright (REFUSED), a check that the user is a person (A_PERSON), or a
    sign-in (SIGNING_IN). The last two are the user's to do. "" when nothing does."""
    head = text[: human_checks.TOP_CHARS].casefold()
    if "access denied" in head:
        return REFUSED
    if human_checks.asks(text):
        return A_PERSON
    if "authwall" in head:
        return SIGNING_IN
    return ""


def blocked(text: str) -> bool:
    """Whether a page is a site refusing the browser, or asking for a sign-in or a
    check that the user is a person, rather than what was asked for."""
    return bool(block(text))
