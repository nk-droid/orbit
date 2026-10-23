"""Whether a job is one the user would want, told from what a listing says before its
posting is opened: one of their roles, in one of their places, at their level, and not
at a company they avoid. A search reads only the postings that pass, so the list holds
the jobs that fit, not everything a site shows.

Code decides all of this, from the profile: the drafting model then judges how well
each posting that passed fits.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .boards import slugs
from .search import all_india

# Why a listing is left out, as the Searches tab counts them.
NOT_A_ROLE = "not one of the roles"
ELSEWHERE = "somewhere else"
TOO_SENIOR = "too senior"
AN_INTERNSHIP = "an internship"
AVOIDED = "a company to avoid"

# Ways of writing the same thing, made one before words are compared.
_SAME = (
    (r"\bback[\s-]+end\b", "backend"),
    (r"\bfront[\s-]+end\b", "frontend"),
    (r"\bfull[\s-]+stack\b", "fullstack"),
    (r"\bmachine[\s-]+learning\b", "ml"),
    (r"\bartificial[\s-]+intelligence\b", "ai"),
    (r"\bgen(?:erative)?[\s-]*ai\b", "ai"),
    (r"\bdev[\s-]?ops\b", "devops"),
    (r"\bsite[\s-]+reliability\b", "sre"),
    (r"\bdata[\s-]+science\b", "data scientist"),
)
# Words that name the same field of work.
_FAMILIES = (
    frozenset({"ai", "ml", "llm", "llms", "genai", "mlops"}),
    frozenset({"devops", "sre", "platform", "infrastructure", "infra"}),
)
# What a role is: the work it names, by its last word.
_CORES = (
    frozenset({"engineer", "engineering", "developer", "development", "sde", "swe",
               "programmer", "coder"}),
    frozenset({"scientist", "science"}),
    frozenset({"analyst", "analytics"}),
    frozenset({"architect", "architecture"}),
    frozenset({"designer", "design"}),
    frozenset({"manager", "management"}),
    frozenset({"researcher", "research"}),
)  # fmt: skip
# Words that say nothing of the field: levels, and words every such title has.
_PLAIN = frozenset(
    {
        "senior", "sr", "junior", "jr", "lead", "staff", "principal", "associate",
        "member", "technical", "mts", "software", "i", "ii", "iii", "iv", "v", "1",
        "2", "3", "4", "5", "of", "the", "and", "for", "at", "in", "a", "an", "with",
    }
)  # fmt: skip
# Levels above the user's, by the fewest years of experience they usually take.
_SENIOR = (
    (re.compile(r"\b(?:director|vp|vice president|head of|chief|cto|ceo)\b", re.I), 10),
    (re.compile(r"\bprincipal\b", re.I), 8),
    (re.compile(r"\bstaff\b", re.I), 6),
)
_INTERN = re.compile(r"\b(?:intern|internship|trainee|apprentice)\b", re.I)

# The same place written differently.
_PLACES = (
    frozenset({"bengaluru", "bangalore", "blr"}),
    frozenset({"gurugram", "gurgaon"}),
    frozenset({"mumbai", "bombay", "navi mumbai", "thane"}),
    frozenset({"chennai", "madras"}),
    frozenset({"kolkata", "calcutta"}),
    frozenset(
        {"delhi", "new delhi", "ncr", "delhi ncr", "noida", "gurugram", "gurgaon"}
    ),
    frozenset({"hyderabad", "secunderabad"}),
    frozenset({"pune"}),
)
_INDIA = frozenset().union(*_PLACES) | {"india", "ahmedabad", "jaipur", "kochi",
                                        "chandigarh", "indore", "coimbatore"}  # fmt: skip
_REMOTE = re.compile(r"\b(?:remote|work from home|wfh|anywhere|distributed)\b", re.I)
# Places in other countries, as listings say them: every other country, the regions
# jobs abroad are listed in, the cities they are in most, and the states of the
# United States, Canada and Australia. A long name may run into the word before it,
# as in "Full-timeNew York"; a short one is a word of its own. A name that is a
# place in India too, such as Salem, or a word, such as Reading, is left out.
_COUNTRIES = (
    "afghanistan",
    "albania",
    "algeria",
    "andorra",
    "angola",
    "antigua",
    "argentina",
    "armenia",
    "australia",
    "austria",
    "azerbaijan",
    "bahamas",
    "bahrain",
    "bangladesh",
    "barbados",
    "belarus",
    "belgium",
    "belize",
    "benin",
    "bhutan",
    "bolivia",
    "bosnia",
    "botswana",
    "brazil",
    "brunei",
    "bulgaria",
    "burkina faso",
    "burundi",
    "cabo verde",
    "cape verde",
    "cambodia",
    "cameroon",
    "canada",
    "central african republic",
    "chad",
    "chile",
    "china",
    "colombia",
    "comoros",
    "congo",
    "costa rica",
    "c\u00f4te d'ivoire",
    "cote d'ivoire",
    "ivory coast",
    "croatia",
    "cuba",
    "cyprus",
    "czechia",
    "czech republic",
    "denmark",
    "djibouti",
    "dominica",
    "dominican republic",
    "ecuador",
    "egypt",
    "el salvador",
    "equatorial guinea",
    "eritrea",
    "estonia",
    "eswatini",
    "swaziland",
    "ethiopia",
    "fiji",
    "finland",
    "france",
    "gabon",
    "gambia",
    "georgia",
    "germany",
    "ghana",
    "greece",
    "grenada",
    "guatemala",
    "guinea",
    "guyana",
    "haiti",
    "honduras",
    "hungary",
    "iceland",
    "indonesia",
    "iran",
    "iraq",
    "ireland",
    "israel",
    "italy",
    "jamaica",
    "japan",
    "jordan",
    "kazakhstan",
    "kenya",
    "kiribati",
    "kosovo",
    "kuwait",
    "kyrgyzstan",
    "laos",
    "latvia",
    "lebanon",
    "lesotho",
    "liberia",
    "libya",
    "liechtenstein",
    "lithuania",
    "luxembourg",
    "madagascar",
    "malawi",
    "malaysia",
    "maldives",
    "mali",
    "malta",
    "marshall islands",
    "mauritania",
    "mauritius",
    "mexico",
    "micronesia",
    "moldova",
    "monaco",
    "mongolia",
    "montenegro",
    "morocco",
    "mozambique",
    "myanmar",
    "burma",
    "namibia",
    "nauru",
    "nepal",
    "netherlands",
    "holland",
    "new zealand",
    "nicaragua",
    "niger",
    "nigeria",
    "north korea",
    "north macedonia",
    "macedonia",
    "norway",
    "oman",
    "pakistan",
    "palau",
    "palestine",
    "panama",
    "papua new guinea",
    "paraguay",
    "peru",
    "philippines",
    "poland",
    "portugal",
    "qatar",
    "romania",
    "russia",
    "rwanda",
    "saint kitts",
    "saint lucia",
    "saint vincent",
    "samoa",
    "san marino",
    "sao tome",
    "saudi arabia",
    "senegal",
    "serbia",
    "seychelles",
    "sierra leone",
    "singapore",
    "slovakia",
    "slovenia",
    "solomon islands",
    "somalia",
    "south africa",
    "south korea",
    "korea",
    "south sudan",
    "spain",
    "sri lanka",
    "sudan",
    "suriname",
    "sweden",
    "switzerland",
    "syria",
    "taiwan",
    "tajikistan",
    "tanzania",
    "thailand",
    "timor-leste",
    "east timor",
    "togo",
    "tonga",
    "trinidad",
    "tunisia",
    "turkey",
    "t\u00fcrkiye",
    "turkiye",
    "turkmenistan",
    "tuvalu",
    "uganda",
    "ukraine",
    "united arab emirates",
    "emirates",
    "united kingdom",
    "great britain",
    "britain",
    "england",
    "scotland",
    "wales",
    "northern ireland",
    "united states",
    "america",
    "americas",
    "uruguay",
    "uzbekistan",
    "vanuatu",
    "vatican",
    "venezuela",
    "vietnam",
    "viet nam",
    "yemen",
    "zambia",
    "zimbabwe",
    "hong kong",
    "macau",
    "macao",
    "puerto rico",
    "greenland",
    "bermuda",
    "cayman islands",
    "gibraltar",
    "isle of man",
)
_REGIONS = (
    "europe",
    "european union",
    "middle east",
    "africa",
    "latin america",
    "north america",
    "south america",
    "central america",
    "caribbean",
    "oceania",
    "scandinavia",
    "nordics",
    "benelux",
    "balkans",
    "baltics",
    "gulf region",
)
_STATES = (
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "wisconsin",
    "wyoming",
    "district of columbia",
    "ontario",
    "quebec",
    "qu\u00e9bec",
    "british columbia",
    "alberta",
    "manitoba",
    "saskatchewan",
    "nova scotia",
    "new brunswick",
    "newfoundland",
    "prince edward island",
    "yukon",
    "new south wales",
    "queensland",
    "tasmania",
    "western australia",
)
_CITIES = (
    # The United States
    "nyc",
    "manhattan",
    "brooklyn",
    "san francisco",
    "bay area",
    "silicon valley",
    "palo alto",
    "mountain view",
    "sunnyvale",
    "san jose",
    "santa clara",
    "cupertino",
    "menlo park",
    "redwood city",
    "san mateo",
    "fremont",
    "oakland",
    "berkeley",
    "los angeles",
    "santa monica",
    "san diego",
    "irvine",
    "sacramento",
    "seattle",
    "bellevue",
    "redmond",
    "kirkland",
    "portland",
    "austin",
    "dallas",
    "houston",
    "san antonio",
    "plano",
    "boston",
    "cambridge",
    "chicago",
    "denver",
    "atlanta",
    "miami",
    "tampa",
    "orlando",
    "jacksonville",
    "fort lauderdale",
    "raleigh",
    "durham",
    "nashville",
    "pittsburgh",
    "philadelphia",
    "baltimore",
    "detroit",
    "minneapolis",
    "milwaukee",
    "cleveland",
    "cincinnati",
    "columbus",
    "indianapolis",
    "st. louis",
    "saint louis",
    "kansas city",
    "salt lake city",
    "las vegas",
    "honolulu",
    "albuquerque",
    "arlington",
    "reston",
    "herndon",
    "mclean",
    "bethesda",
    "princeton",
    "hoboken",
    "jersey city",
    "stamford",
    "hartford",
    "new orleans",
    "tucson",
    "tempe",
    "scottsdale",
    "boulder",
    # Canada
    "toronto",
    "vancouver",
    "montreal",
    "montr\u00e9al",
    "ottawa",
    "calgary",
    "edmonton",
    "waterloo",
    "kitchener",
    "winnipeg",
    "halifax",
    "mississauga",
    # Europe and the United Kingdom
    "london",
    "manchester",
    "birmingham",
    "edinburgh",
    "glasgow",
    "leeds",
    "bristol",
    "belfast",
    "cardiff",
    "liverpool",
    "newcastle",
    "nottingham",
    "sheffield",
    "oxford",
    "brighton",
    "southampton",
    "milton keynes",
    "dublin",
    "cork",
    "galway",
    "berlin",
    "munich",
    "m\u00fcnchen",
    "hamburg",
    "frankfurt",
    "cologne",
    "stuttgart",
    "d\u00fcsseldorf",
    "dusseldorf",
    "leipzig",
    "dresden",
    "nuremberg",
    "karlsruhe",
    "heidelberg",
    "paris",
    "lyon",
    "toulouse",
    "marseille",
    "bordeaux",
    "lille",
    "nantes",
    "grenoble",
    "amsterdam",
    "rotterdam",
    "eindhoven",
    "utrecht",
    "the hague",
    "brussels",
    "antwerp",
    "ghent",
    "zurich",
    "z\u00fcrich",
    "geneva",
    "basel",
    "lausanne",
    "bern",
    "vienna",
    "prague",
    "brno",
    "budapest",
    "bucharest",
    "cluj",
    "sofia",
    "athens",
    "istanbul",
    "ankara",
    "izmir",
    "helsinki",
    "espoo",
    "oslo",
    "stockholm",
    "gothenburg",
    "malm\u00f6",
    "malmo",
    "copenhagen",
    "aarhus",
    "warsaw",
    "krak\u00f3w",
    "krakow",
    "wroc\u0142aw",
    "wroclaw",
    "gda\u0144sk",
    "gdansk",
    "pozna\u0144",
    "poznan",
    "vilnius",
    "riga",
    "tallinn",
    "kyiv",
    "kiev",
    "lviv",
    "kharkiv",
    "minsk",
    "moscow",
    "st. petersburg",
    "saint petersburg",
    "belgrade",
    "zagreb",
    "ljubljana",
    "bratislava",
    "sarajevo",
    "skopje",
    "tirana",
    "tbilisi",
    "yerevan",
    "baku",
    "madrid",
    "barcelona",
    "valencia",
    "seville",
    "malaga",
    "m\u00e1laga",
    "bilbao",
    "lisbon",
    "porto",
    "milan",
    "rome",
    "turin",
    "naples",
    "bologna",
    "florence",
    "reykjavik",
    "valletta",
    "nicosia",
    "limassol",
    # The Middle East and Africa
    "dubai",
    "abu dhabi",
    "sharjah",
    "riyadh",
    "jeddah",
    "dammam",
    "doha",
    "kuwait city",
    "manama",
    "muscat",
    "amman",
    "beirut",
    "tel aviv",
    "jerusalem",
    "haifa",
    "tehran",
    "cairo",
    "alexandria",
    "casablanca",
    "tunis",
    "algiers",
    "lagos",
    "abuja",
    "accra",
    "nairobi",
    "kampala",
    "kigali",
    "addis ababa",
    "dar es salaam",
    "johannesburg",
    "cape town",
    "durban",
    "pretoria",
    "lusaka",
    "harare",
    "dakar",
    # Asia and Oceania
    "colombo",
    "kandy",
    "dhaka",
    "chittagong",
    "karachi",
    "lahore",
    "islamabad",
    "rawalpindi",
    "kathmandu",
    "thimphu",
    "kabul",
    "tashkent",
    "almaty",
    "astana",
    "singapore",
    "kuala lumpur",
    "penang",
    "jakarta",
    "bali",
    "manila",
    "cebu",
    "makati",
    "taguig",
    "bangkok",
    "ho chi minh",
    "saigon",
    "hanoi",
    "da nang",
    "phnom penh",
    "yangon",
    "tokyo",
    "osaka",
    "kyoto",
    "fukuoka",
    "seoul",
    "busan",
    "beijing",
    "shanghai",
    "shenzhen",
    "guangzhou",
    "hangzhou",
    "chengdu",
    "taipei",
    "ulaanbaatar",
    "sydney",
    "melbourne",
    "brisbane",
    "perth",
    "adelaide",
    "canberra",
    "auckland",
    "wellington",
    "christchurch",
    # Latin America
    "mexico city",
    "guadalajara",
    "monterrey",
    "buenos aires",
    "c\u00f3rdoba",
    "cordoba",
    "rosario",
    "s\u00e3o paulo",
    "sao paulo",
    "rio de janeiro",
    "belo horizonte",
    "florian\u00f3polis",
    "florianopolis",
    "curitiba",
    "porto alegre",
    "recife",
    "santiago",
    "bogot\u00e1",
    "bogota",
    "medell\u00edn",
    "medellin",
    "cali",
    "lima",
    "quito",
    "montevideo",
    "asunci\u00f3n",
    "asuncion",
    "la paz",
    "caracas",
    "panama city",
    "san juan",
    "havana",
    "santo domingo",
    "kingston",
    "san jos\u00e9",
)
# Codes for places abroad, written as they are, in capitals: "US", "UK", "EMEA".
_CODES = (
    "US",
    "U.S.",
    "USA",
    "U.S.A.",
    "UK",
    "U.K.",
    "EU",
    "UAE",
    "KSA",
    "NYC",
    "SF",
    "EMEA",
    "LATAM",
    "MENA",
    "DACH",
    "ANZ",
    "AMER",
    "NORAM",
    "NSW",
)


def _names_pattern(names: tuple[str, ...]) -> str:
    short = sorted({name for name in names if len(name) <= 5}, key=len, reverse=True)
    long = sorted({name for name in names if len(name) > 5}, key=len, reverse=True)
    whole = "|".join(re.escape(name) for name in short)
    joined = "|".join(re.escape(name) for name in long)
    return rf"\b(?:{whole})\b|(?:{joined})\b"


_ABROAD = re.compile(
    _names_pattern(_COUNTRIES + _REGIONS + _STATES + _CITIES), re.IGNORECASE
)
_ABROAD_CODES = re.compile(
    r"(?<![\w.])(?:" + "|".join(re.escape(code) for code in _CODES) + r")(?![\w])"
)
# A place in the United States, written with its state, as in "Austin, TX".
_US_STATE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|"
    r"MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|"
    r"DC)\b"
)


def _abroad(text: str) -> bool:
    return bool(
        _ABROAD.search(text) or _ABROAD_CODES.search(text) or _US_STATE.search(text)
    )


# Open to anywhere, as "Worldwide" says: not "Anywhere in the US".
_ANYWHERE = re.compile(
    r"\b(?:anywhere|worldwide|global|any location)\b"
    r"(?!\s+(?:in|within|across|from|inside)\b)",
    re.I,
)


def words(text: str) -> set[str]:
    """A title's words, each way of writing a thing made one."""
    said = text.casefold()
    for pattern, same in _SAME:
        said = re.sub(pattern, same, said)
    return set(re.findall(r"[a-z0-9+#]+", said))


def _family(word: str) -> frozenset[str]:
    return next((family for family in _FAMILIES if word in family), frozenset({word}))


def _core(found: set[str]) -> frozenset[str] | None:
    return next((core for core in _CORES if found & core), None)


@dataclass(frozen=True)
class Role:
    """A role the user wants, as titles are compared with it."""

    core: frozenset[str] | None
    focus: frozenset[str]


@lru_cache(maxsize=256)
def role(text: str) -> Role:
    """What a role names: its kind of work, and the field it is in."""
    found = words(text)
    core = _core(found)
    focus = frozenset(found - _PLAIN - (core or frozenset()))
    return Role(core, focus)


def is_role(title: str, wanted: list[str]) -> bool:
    """Whether a title is one of the roles the user wants: the same kind of work, in
    the same field, such as "Senior Software Engineer (Backend)" for "Backend
    Engineer". With no roles given, every title is."""
    if not wanted:
        return True
    found = words(title)
    for asked in map(role, wanted):
        if asked.core is not None and not found & asked.core:
            continue
        if not asked.focus or any(found & _family(word) for word in asked.focus):
            return True
    return False


def level(title: str, years: float | None) -> str:
    """Why a title is not at the user's level, or "": far above it, or an internship
    for someone with years of work behind them."""
    if years is None:
        return ""
    for pattern, least in _SENIOR:
        if pattern.search(title) and years < least:
            return TOO_SENIOR
    if _INTERN.search(title) and years >= 1:
        return AN_INTERNSHIP
    return ""


def _named(place: str, said: str) -> bool:
    # A place ends a word, though a page may run it into the word before, as in
    # "Full-timeBangalore".
    return bool(re.search(rf"{re.escape(place)}\b", said))


def _places(text: str) -> set[str]:
    said = " ".join(re.findall(r"[a-z]+", text.casefold()))
    found: set[str] = set()
    for group in _PLACES:
        if any(_named(place, said) for place in group):
            found |= group
    return found | {place for place in _INDIA if _named(place, said)}


def in_place(where: str, places: list[str]) -> bool | None:
    """Whether a job's location is one of the user's places: a city of theirs, any
    in India when they said all of it, or remote when they want remote and it is open
    to where they are. Any other place named is not. None when no place is named to
    tell by."""
    if not where.strip() or not places:
        return None
    theirs = set().union(*(_places(place) for place in places))
    named = _places(where)
    if theirs & named or (named and any(all_india(place) for place in places)):
        return True
    if not _REMOTE.search(where):
        return False if named or _abroad(where) else None
    # Remote work in India is anywhere in it.
    if not any(_REMOTE.search(place) or all_india(place) for place in places):
        return False
    # Remote, and open to where the user is: their country, anywhere, or nowhere
    # in particular.
    in_india = bool(theirs & _INDIA)
    if _ANYWHERE.search(where) or (in_india and "india" in named):
        return True
    return not (_abroad(where) or named - theirs)


def names_of(place: str) -> list[str]:
    """The ways a place is written, such as Bengaluru and Bangalore."""
    said = " ".join(place.casefold().split())
    group = next((g for g in _PLACES if said in g and len(g) < 5), frozenset({said}))
    return sorted(group - {"blr"}, key=lambda name: (name != said, name))


def avoided(company: str, avoid: list[str]) -> bool:
    name = " ".join(re.findall(r"[a-z0-9]+", company.casefold()))
    return any(
        (said := " ".join(re.findall(r"[a-z0-9]+", item.casefold())))
        and re.search(rf"\b{re.escape(said)}\b", name)
        for item in avoid
    )


def left_out(title: str, company: str, where: str, known: dict[str, Any]) -> str:
    """Why a listing is left out before its posting is opened, or "" when it is read:
    by its title, its place and its company, against the profile (`known`)."""
    roles = _listed(known.get("roles"))
    if not is_role(title, roles):
        return NOT_A_ROLE
    if why := level(title, years_of(known)):
        return why
    # A title may say where the job is too, as in "Support Engineer - Toronto".
    if in_place(f"{where} · {title}", _listed(known.get("locations"))) is False:
        return ELSEWHERE
    if company and avoided(company, _listed(known.get("avoid"))):
        return AVOIDED
    return ""


def years_of(known: dict[str, Any]) -> float | None:
    found = re.search(r"\d+(?:\.\d+)?", str(known.get("years") or ""))
    return float(found[0]) if found else None


def _listed(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


# What a listing may add to a role's title that is not the role.
_TAGS = frozenset(
    {"new", "urgent", "hiring", "immediate", "joiner", "joiners", "remote", "hybrid",
     "onsite", "on", "site", "office", "wfh", "full", "time", "fulltime", "contract"}
)  # fmt: skip


def job_key(title: str, company: str) -> tuple[str, frozenset[str]] | None:
    """What makes listings the same job, as a job site and the company's own board
    may each list it: the same company, and the same role whatever place or tag
    either adds to the title. None without a company to tell by."""
    if not company.strip():
        return None
    first = slugs(company)[:1]
    return (first[0] if first else "", frozenset(words(title) - _TAGS - _places(title)))


def same_job(title: str, company: str, other_title: str, other_company: str) -> bool:
    """Whether two listings are the same job (see job_key)."""
    mine = job_key(title, company)
    return mine is not None and mine == job_key(other_title, other_company)
