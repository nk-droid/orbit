"""How Orbit's job searches run: which sites, and how much at a time. The user changes
these by voice, and they apply from the next search."""

import re
from dataclasses import dataclass
from typing import Any

from . import store

# The job sites Orbit searches, as the user says them. "companies" is companies' own
# job boards, searched first, and "google" is a Google search of them.
SITES = {
    "companies": "Company job boards",
    "google": "Google",
    "linkedin": "LinkedIn",
    "naukri": "Naukri",
    "cutshort": "Cutshort",
}
# Sites that came to Orbit after the user may have chosen theirs: each joins the
# sites they chose once, after which their choice stands.
_NEW_SITES = ("companies", "google", "cutshort")
_SITE_NAMES = {
    "cut short": "cutshort",
    "cutshort.io": "cutshort",
    "company": "companies",
    "company sites": "companies",
    "company boards": "companies",
    "company job boards": "companies",
    "company pages": "companies",
    "career pages": "companies",
    "careers pages": "companies",
    "careers": "companies",
    "google search": "google",
    "search engine": "google",
}

NOT_A_SETTING = "There is no setting called {asked!r}. The settings are: {names}."
NOT_A_NUMBER = "{label} takes a whole number from {low} to {high}, such as {example}."
NOT_A_SITE = "Orbit searches these sites: {sites}. {asked!r} is not one of them."
NO_SITES = "At least one site has to be searched."
NOT_A_SWITCH = "{label} is on or off: say yes or no."
WRITTEN = 'Write each change as "setting: value", such as "posted_within_days: 7".'

# The most jobs one search keeps. Once it has found this many it stops, and the next
# search goes on past them: the jobs it found are not read again.
MOST_KEPT = 250
# How many postings a search reads was a setting once. It reads every one that may
# fit, until it has found the most jobs it keeps.
_ONCE_A_LIMIT = (
    "postings per run", "postings", "postings per search", "how many postings",
    "jobs per search", "how many jobs",
)  # fmt: skip
ALL_READ = (
    "A search reads every posting that may fit until it has found {most} jobs, the "
    "most one search keeps. Neither is a setting."
)


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    default: Any
    # Other ways the user says it.
    also: tuple[str, ...] = ()
    low: int = 1
    high: int = 1
    # "number", "sites", "names" for a list of names, or "switch" for on or off.
    kind: str = "number"


SETTINGS = (
    Setting(
        "sites",
        "Sites",
        ["companies", "google", "linkedin", "cutshort"],
        ("site", "job sites", "boards"),
        kind="sites",
    ),
    Setting(
        "companies",
        "Companies to watch",
        [],
        ("company", "watch", "watched companies", "companies to watch"),
        kind="names",
    ),
    # Jobs that apply only on LinkedIn or Naukri, when the company's own page does
    # not list them: kept, and applied to there. A job its company's own page lists
    # is applied to on that page. Cutshort's jobs all apply on Cutshort: searching
    # it is choosing them.
    Setting(
        "easy_apply",
        "LinkedIn Easy Apply and Naukri jobs not on the company's own page",
        True,
        (
            "easy apply",
            "board only",
            "apply on the board",
            "linkedin easy apply",
            "naukri apply",
            "direct apply",
            "jobs that apply only on a job board",
        ),
        kind="switch",
    ),
    Setting(
        "pages_per_search",
        "Results pages per search",
        3,
        ("pages", "results pages", "pages of results"),
        1,
        10,
    ),
    Setting(
        "posted_within_days",
        "Posted within, in days",
        14,
        ("days", "how recent", "posted within", "posted in the last", "recent"),
        1,
        90,
    ),
    Setting(
        "applications_per_sitting",
        "Applications per sitting",
        10,
        ("applications", "per sitting", "applications per day", "per day", "today"),
        1,
        50,
    ),
)
_BY_KEY = {setting.key: setting for setting in SETTINGS}


def current() -> dict[str, Any]:
    """Every setting, as the user set it or as it starts. A site that came to Orbit
    after the user chose their sites joins them, once, in its usual place."""
    kept = store.settings()
    for site in _NEW_SITES:
        if "sites" in kept and not kept.get(f"{site}_offered"):
            if site not in kept["sites"]:
                usual = _BY_KEY["sites"].default
                at = sum(
                    1
                    for other in kept["sites"]
                    if usual.index(site)
                    > (usual.index(other) if other in usual else len(usual))
                )
                kept["sites"] = [*kept["sites"][:at], site, *kept["sites"][at:]]
                store.keep_setting("sites", kept["sites"])
            store.keep_setting(f"{site}_offered", True)
    return {setting.key: kept.get(setting.key, setting.default) for setting in SETTINGS}


def _named(asked: str) -> Setting:
    said = " ".join(re.sub(r"[_-]", " ", asked).casefold().split())
    for setting in SETTINGS:
        if said in (
            setting.key.replace("_", " "),
            setting.label.casefold(),
            *setting.also,
        ):
            return setting
    if said in _ONCE_A_LIMIT:
        raise ValueError(ALL_READ.format(most=MOST_KEPT))
    names = ", ".join(setting.key for setting in SETTINGS)
    raise ValueError(NOT_A_SETTING.format(asked=asked, names=names))


def change(written: str) -> tuple[str, Any]:
    """Changes one setting, written as "setting: value". Returns the setting and its
    new value, or raises ValueError saying what to give instead."""
    asked, colon, value = written.partition(":")
    if not colon:
        raise ValueError(WRITTEN)
    setting = _named(asked)
    if setting.kind == "sites":
        new = _sites(value, current()["sites"])
        # The user chose with every site there is before them.
        for site in _NEW_SITES:
            store.keep_setting(f"{site}_offered", True)
    elif setting.kind == "names":
        new = _names(value, current()[setting.key])
    elif setting.kind == "switch":
        new = _switch(value, setting)
    else:
        found = re.fullmatch(r"\s*(\d+)\s*", value)
        number = int(found[1]) if found else None
        if number is None or not setting.low <= number <= setting.high:
            raise ValueError(
                NOT_A_NUMBER.format(
                    label=setting.label,
                    low=setting.low,
                    high=setting.high,
                    example=setting.default,
                )
            )
        new = number
    store.keep_setting(setting.key, new)
    return setting.key, new


def _sites(value: str, was: list[str]) -> list[str]:
    """The sites after a change: a list of them, or one added or taken away."""
    said = " ".join(value.casefold().split())
    adding = said.startswith(("add ", "also "))
    removing = said.startswith(("remove ", "drop ", "not ", "no "))
    if adding or removing or said.startswith("only "):
        said = said.split(" ", 1)[1]
    named = [
        _SITE_NAMES.get(name.strip(), name.strip())
        for name in re.split(r",|\band\b", said)
        if name.strip()
    ]
    for name in named:
        if name not in SITES:
            sites = ", ".join(SITES)
            raise ValueError(NOT_A_SITE.format(sites=sites, asked=name))
    if adding:
        sites = [*was, *(name for name in named if name not in was)]
    elif removing:
        sites = [site for site in was if site not in named]
    else:
        sites = list(dict.fromkeys(named))
    if not sites:
        raise ValueError(NO_SITES)
    return sites


def _names(value: str, was: list[str]) -> list[str]:
    """A list of names after a change: names added or taken away, or the whole list."""
    said = " ".join(value.split())
    how = said.split(" ", 1)[0].casefold()
    rest = said.split(" ", 1)[1] if " " in said else ""
    if how in ("add", "also", "remove", "drop", "only") and rest:
        said = rest
    named = [name.strip() for name in re.split(r",|\band\b|&", said) if name.strip()]
    if how in ("add", "also"):
        known = {name.casefold() for name in was}
        return [*was, *(name for name in named if name.casefold() not in known)]
    if how in ("remove", "drop"):
        gone = {name.casefold() for name in named}
        return [name for name in was if name.casefold() not in gone]
    if how in ("none", "nothing", "clear"):
        return []
    return list(dict.fromkeys(named))


def _switch(value: str, setting: Setting) -> bool:
    said = " ".join(value.casefold().split())
    if re.match(r"(?:yes|on|true|include|included|search|allow|with)\b", said):
        return True
    if re.match(r"(?:no|off|false|exclude|leave out|skip|without|don't)\b", said):
        return False
    raise ValueError(NOT_A_SWITCH.format(label=setting.label))


def _shown(setting: Setting, value: Any) -> str:
    if setting.kind == "sites":
        return ", ".join(SITES[site] for site in value)
    if setting.kind == "names":
        return ", ".join(value) or "none yet"
    if setting.kind == "switch":
        return "included" if value else "left out"
    return str(value)


def described() -> list[dict[str, str]]:
    """The settings as the Profile card shows them."""
    now = current()
    return [
        {
            "key": setting.key,
            "label": setting.label,
            "value": _shown(setting, now[setting.key]),
        }
        for setting in SETTINGS
    ]
