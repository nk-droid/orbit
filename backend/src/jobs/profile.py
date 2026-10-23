"""The user's profile for job applications: what every form is filled from.

It is built from their résumé, their own pages (portfolio, GitHub, LinkedIn) and what
Orbit remembers about them. The drafting model reads those, and code keeps only what
a source really says: a finding's words must be in its source, and contact details
and links must be there as they are written. Where sources disagree, every version is
kept, so the user can choose. What the user says themselves outranks any source.
"""

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from attachments import safe_name
from storage import data_dir

from . import resumes, settings, store, writing

# Where a value can come from, the most trusted first.
YOU = "you"
SOURCES = (YOU, "résumé", "LinkedIn", "portfolio", "GitHub", "memory")

NOT_A_FIELD = "There is no field called {asked!r}. The fields are: {keys}."
WRITTEN = (
    'Write each change as "field: value", such as "notice_period: 30 days". For a '
    'list, "skills: add Kubernetes" or "skills: remove Java".'
)
NO_VALUE = "Say what {label} is."
NOT_A_RESUME = "{name} is not a résumé Orbit can read: hand over a PDF."
NO_TEXT = (
    "{name} has no text Orbit can read, as a scanned résumé has none: hand over one "
    "saved as a PDF from a document."
)
UNREADABLE = "{name} could not be read as a PDF."
NO_SUCH_RESUME = "There is no résumé called {asked!r}. The résumés are: {names}."


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    many: bool = False
    # Asked for by most applications: the user is asked when no source gives it.
    needed: bool = False
    # Checked against its source as it is written: contact details and links.
    exact: bool = False


FIELDS = (
    Field("name", "Name", "About you", needed=True, exact=True),
    Field("email", "Email", "About you", needed=True, exact=True),
    Field("phone", "Phone", "About you", needed=True, exact=True),
    Field("city", "City", "About you", needed=True),
    Field("github", "GitHub", "About you", exact=True),
    Field("linkedin", "LinkedIn", "About you", exact=True),
    Field("portfolio", "Portfolio", "About you", exact=True),
    Field("title", "Current role", "Work", needed=True),
    Field("company", "Current company", "Work", needed=True),
    Field("years", "Years of experience", "Work", needed=True),
    Field("highlights", "Highlights", "Work", many=True),
    Field("education", "Education", "Work", many=True),
    Field("skills", "Skills", "Work", many=True),
    Field("authorization", "Work authorization", "Where you can work", needed=True),
    Field("relocation", "Open to relocation", "Where you can work"),
    Field("notice_period", "Notice period", "Where you can work", needed=True),
    Field("roles", "Roles", "What you want", many=True, needed=True),
    Field("seniority", "Seniority", "What you want"),
    Field("locations", "Locations", "What you want", many=True, needed=True),
    Field("work_mode", "Remote or on site", "What you want"),
    Field("job_types", "Job types", "What you want", many=True),
    Field("seek", "Companies to seek", "What you want", many=True),
    Field("avoid", "Companies to avoid", "What you want", many=True),
)
GROUPS = ("About you", "Work", "Where you can work", "What you want")
_BY_KEY = {field.key: field for field in FIELDS}
# What each field holds, as the drafting model is told.
_HOLDS = {
    "name": "full name",
    "email": "email address",
    "phone": "phone number, with its country code if given",
    "city": "the city they live in",
    "github": "link to their GitHub profile",
    "linkedin": "link to their LinkedIn profile",
    "portfolio": "link to their own website or portfolio",
    "title": "their current job title",
    "company": "the company they work for now",
    "years": "years of work experience, as a number",
    "highlights": "(a list) achievements, each in one line, with numbers where given",
    "education": "(a list) each degree with its institution and year",
    "skills": "(a list) each technology or skill, with its level if given",
    "authorization": "where they may work, such as a citizenship or visa",
    "relocation": "whether they would move for a job",
    "notice_period": "how soon they can start",
    "roles": "(a list) the roles they want",
    "seniority": "the seniority they want",
    "locations": "(a list) where they want to work",
    "work_mode": "remote, hybrid or on site",
    "job_types": "(a list) full time, contract, internship and so on",
    "seek": "(a list) companies or industries they want",
    "avoid": "(a list) companies or industries to avoid",
}


@dataclass(frozen=True)
class Built:
    """What building the profile came to, for the model to tell the user."""

    sources: list[str]
    filled: int
    missing: list[str]
    disagree: list[dict[str, Any]]


def _plain(text: str) -> str:
    """Text as it is compared: one case, single spaces, plain quotes and dashes."""
    text = text.translate(
        {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x00A0: " "}
    )
    text = re.sub("[\\u2010-\\u2015]", "-", text)
    return " ".join(text.casefold().split())


def _bare(text: str) -> str:
    """Text with its links written without their beginning, as "github.com/x"."""
    return re.sub(r"https?://(?:www\.)?", "", _plain(text)).rstrip("/")


# What each of the user's links looks like, written bare: an address, never the word
# a résumé shows for it, such as "LinkedIn".
_LINKS = {
    "linkedin": r"(?:[a-z]{2,3}\.)?linkedin\.com/in/[^/\s?#]+(?:[/?#]\S*)?",
    "github": r"github\.com/[\w.-]+(?:[/?#]\S*)?",
    "portfolio": r"[\w-]+(?:\.[\w-]+)+(?:[/?#]\S*)?",
}


def supported(field: Field, finding: dict[str, str], sources: dict[str, str]) -> bool:
    """Whether a source really says what a finding says it does."""
    text = sources.get(finding["source"])
    quote = _plain(finding["quote"])
    if text is None or not quote or quote not in _plain(text):
        return False
    if not field.exact:
        return True
    value = finding["value"]
    if field.key == "phone":
        digits = re.sub(r"\D", "", value)
        return len(digits) >= 7 and digits in re.sub(r"\D", "", text)
    if field.key in _LINKS:
        bare = _bare(value)
        return bool(re.fullmatch(_LINKS[field.key], bare)) and bare in _bare(text)
    return _plain(value) in _plain(text)


def _rank(finding: dict[str, str]) -> int:
    source = finding["source"]
    return SOURCES.index(source) if source in SOURCES else len(SOURCES)


def merge(
    found: dict[str, list[dict[str, str]]],
    sources: dict[str, str],
    kept: dict[str, store.Known],
) -> list[store.Known]:
    """The profile from what was found, keeping only what the sources say, the most
    trusted source first. What the user said themselves stays as they said it."""
    known = []
    for field in FIELDS:
        findings = sorted(
            (f for f in found.get(field.key, []) if supported(field, f, sources)),
            key=_rank,
        )
        said = kept.get(field.key)
        if said is not None and said.source == YOU:
            known.append(said)
            continue
        if not findings:
            continue
        if field.many:
            items: dict[str, str] = {}
            used: list[str] = []
            for finding in findings:
                item = " ".join(finding["value"].split())
                if item and _plain(item) not in items:
                    items[_plain(item)] = item
                    used.append(finding["source"])
            if items:
                source = ", ".join(dict.fromkeys(used))
                known.append(store.Known(field.key, list(items.values()), source, []))
            continue
        first = findings[0]
        value = " ".join(first["value"].split())
        others = []
        for finding in findings[1:]:
            other = " ".join(finding["value"].split())
            seen = {_plain(value), *(_plain(o["value"]) for o in others)}
            if _plain(other) not in seen:
                others.append({"value": other, "source": finding["source"]})
        known.append(store.Known(field.key, value, first["source"], others))
    return known


async def build(sources: dict[str, str]) -> Built:
    """Builds the profile, or builds it again, from what the sources say."""
    holds = {field.key: f"{field.label}: {_HOLDS[field.key]}" for field in FIELDS}
    found = await writing.read_profile(holds, sources)
    known = merge(found, sources, store.profile())
    store.keep(known, replace_all=True)
    return Built(
        sources=list(sources),
        filled=len(known),
        missing=missing(),
        disagree=disagreements(),
    )


def missing() -> list[str]:
    """The fields most forms ask for that the profile does not have."""
    known = store.profile()
    return [
        field.label
        for field in FIELDS
        if field.needed and (field.key not in known or not known[field.key].value)
    ]


def disagreements() -> list[dict[str, Any]]:
    """The fields where sources said different things, each with every version."""
    return [
        {
            "field": _BY_KEY[known.key].label,
            "values": [
                f"{known.value} ({known.source})",
                *(f"{other['value']} ({other['source']})" for other in known.others),
            ],
        }
        for known in store.profile().values()
        if known.others and known.key in _BY_KEY
    ]


def _named(asked: str) -> Field:
    said = " ".join(re.sub(r"[_-]", " ", asked).casefold().split())
    for field in FIELDS:
        if said in (field.key.replace("_", " "), field.label.casefold()):
            return field
    keys = ", ".join(field.key for field in FIELDS)
    raise ValueError(NOT_A_FIELD.format(asked=asked, keys=keys))


def _items(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def change(written: str) -> store.Known:
    """Changes a field as the user said it should be, written "field: value". A
    list takes "add X", "remove X", or the whole list."""
    asked, colon, value = written.partition(":")
    if not colon:
        raise ValueError(WRITTEN)
    field = _named(asked)
    value = " ".join(value.split())
    if not value:
        raise ValueError(NO_VALUE.format(label=field.label))
    if not field.many:
        new = store.Known(field.key, value, YOU, [])
    else:
        was = store.profile().get(field.key)
        items = list(was.value) if was and isinstance(was.value, list) else []
        words = value.split(" ", 1)
        how = words[0].casefold()
        if how in ("add", "plus") and len(words) > 1:
            taken = {_plain(item) for item in items}
            items += [item for item in _items(words[1]) if _plain(item) not in taken]
        elif how in ("remove", "drop", "without") and len(words) > 1:
            gone = {_plain(item) for item in _items(words[1])}
            items = [item for item in items if _plain(item) not in gone]
        else:
            items = _items(value)
        new = store.Known(field.key, items, YOU, [])
    store.keep([new])
    return new


def value(key: str) -> str | list[str] | None:
    known = store.profile().get(key)
    return known.value if known else None


def card() -> dict[str, Any]:
    """The profile as its card shows it: each field by group, where its value came
    from and what other sources said, the résumé, kept answers and the settings."""
    known = store.profile()
    owner = str(known["name"].value) if "name" in known else ""
    return {
        "groups": [
            {
                "title": group,
                "fields": [
                    {
                        "key": field.key,
                        "label": field.label,
                        "value": known[field.key].value if field.key in known else None,
                        "source": known[field.key].source if field.key in known else "",
                        "others": known[field.key].others if field.key in known else [],
                        "needed": field.needed,
                        "many": field.many,
                    }
                    for field in FIELDS
                    if field.group == group
                ],
            }
            for group in GROUPS
        ],
        "resumes": [
            {
                "name": resume.name,
                "source": resume.source,
                "role": resumes.role_of(resume, owner),
            }
            for resume in store.resumes()
        ],
        "answers": [
            {"question": question, "answer": answer}
            for question, answer in store.answers().items()
        ],
        "settings": settings.described(),
    }


def _pdf_text(name: str, data: bytes) -> str:
    """A PDF's text, and after it the addresses its links go to: a résumé may show
    only "LinkedIn" or "GitHub" for its links."""
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except (PdfReadError, ValueError, OSError) as e:
        raise ValueError(UNREADABLE.format(name=name)) from e
    links = _pdf_links(reader)
    return "\n".join([text, *links]) if text.strip() and links else text


def _pdf_links(reader: PdfReader) -> list[str]:
    """The web addresses a PDF's links go to, in order, each once. A link that
    cannot be read is passed over: they only add to the text."""
    found: list[str] = []
    for page in reader.pages:
        try:
            annotations = page.get("/Annots")
            for annotation in annotations.get_object() if annotations else []:
                action = annotation.get_object().get("/A")
                uri = action.get_object().get("/URI") if action is not None else None
                if (
                    isinstance(uri, str)
                    and uri.startswith(("http://", "https://"))
                    and uri not in found
                ):
                    found.append(str(uri))
        except (PdfReadError, ValueError, KeyError, AttributeError, TypeError):
            continue
    return found


def keep_resume(name: str, data: bytes, source: str, role: str = "") -> store.Resume:
    """Keeps a résumé, and its text, with the user's others: one of the same name
    takes its place. `role` is the work it is for, if the user said. Only a PDF or
    plain text is read: a form takes a PDF, and its text is what answers draw on."""
    suffix = Path(name).suffix.casefold()
    if suffix == ".pdf":
        text = _pdf_text(name, data)
    elif suffix in (".txt", ".md"):
        text = data.decode("utf-8", "replace")
    else:
        raise ValueError(NOT_A_RESUME.format(name=name))
    if not text.strip():
        raise ValueError(NO_TEXT.format(name=name))
    folder = data_dir() / "jobs"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / safe_name(name)
    was = store.resume(path.name)
    path.write_bytes(data)
    store.keep_resume(
        path.name, str(path), source, text, role or (was.role if was else "")
    )
    resume = store.resume(path.name)
    assert resume is not None
    return resume


def named_resume(asked: str) -> store.Resume:
    """The résumé the user means: by its name, or by the work it is for."""
    kept = store.resumes()
    owner = str(value("name") or "")
    said = (
        " ".join(asked.casefold().split())
        .removesuffix(" résumé")
        .removesuffix(" resume")
    )
    for found in (
        [r for r in kept if r.name.casefold() == said],
        [r for r in kept if said and said in r.name.casefold()],
        [r for r in kept if said and said in resumes.role_of(r, owner).casefold()],
    ):
        if len(found) == 1:
            return found[0]
    names = ", ".join(r.name for r in kept) or "none yet"
    raise ValueError(NO_SUCH_RESUME.format(asked=asked, names=names))


def resume_for(asked: str, role: str) -> store.Resume:
    """Says what work a résumé is for, such as "Backend Developer"."""
    resume = named_resume(asked)
    store.resume_for(resume.name, " ".join(role.split()))
    found = store.resume(resume.name)
    assert found is not None
    return found


def forget_resume(asked: str) -> store.Resume:
    """Stops using a résumé: it is no longer kept, nor sent with any application."""
    resume = named_resume(asked)
    store.forget_resume(resume.name)
    if not any(other.path == resume.path for other in store.resumes()):
        Path(resume.path).unlink(missing_ok=True)
    return resume
