"""The checks on an application's details and formatting, made on every draft and
again on what the page holds once it is filled in, before it is submitted.

Each finding names the question it is about and what is wrong. A blocking finding
stops the form from being filled in or submitted until it is put right; the others
are for the user to hear. Writing is tidied first (tidy), so that what code can put
right itself, such as spacing or a company's name in the wrong case, never reaches
the user as a finding.
"""

import mimetypes
import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from . import forms
from .forms import Answer, Field

# The contact details and links, which must be as the profile has them.
CONTACT = (
    "name",
    "first_name",
    "last_name",
    "email",
    "phone",
    "linkedin",
    "github",
    "portfolio",
)
# A sentence at least this many words long said twice is a repeat.
REPEAT_WORDS = 6
# The characters a cover letter put into a PDF can be written in.
PDF_ENCODING = "cp1252"


@dataclass(frozen=True)
class Finding:
    """Something wrong with an answer, or with the application as a whole when
    `question` is empty."""

    question: str
    problem: str
    blocking: bool = True

    def said(self) -> str:
        return f"{self.question}: {self.problem}" if self.question else self.problem


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_PHONE = re.compile(r"^\+?[\d][\d\s().-]{5,}$")
_LINK = re.compile(r"^https?://(?:[\w-]+\.)+[a-z]{2,}(?:[/?#]\S*)?$", re.IGNORECASE)
_PLACEHOLDER = re.compile(
    r"\[[^\]\n]{1,40}\]|\{[^}\n]{1,40}\}|<[^>\n]{1,40}>|\b(?:XYZ|TBD|TODO|lorem ipsum)\b|"
    r"\b(?:your name|company name|job title|hiring manager name)\b",
)
_MARKUP = re.compile(
    r"\*\*[^*\n]+\*\*|__[^_\n]+__|(?m:^#{1,6} )|\[[^\]\n]+\]\([^)\n]+\)|`|"
    r"</?(?:p|br|b|i|strong|em|ul|ol|li|div|span|a|h\d)\b[^>]*>",
    re.IGNORECASE,
)
_LIMIT = re.compile(
    r"\b(?:max(?:imum)?|up to|no more than|at most|within|under|less than|"
    r"limit(?:ed)? to|in)\s+(\d[\d,]*)\s*(words?|characters?|chars?)\b|"
    r"\b(\d[\d,]*)\s*-?\s*(words?|characters?|chars?)\s*(?:max(?:imum)?|limit|"
    r"or (?:less|fewer))\b",
    re.IGNORECASE,
)
_GREETING = re.compile(r"^\s*(?:dear|hello|hi|to whom|greetings)\b", re.IGNORECASE)


def tidy(text: str, company: str, role: str) -> str:
    """Writing as it goes into a form: Markdown marks taken out, spacing cleaned,
    and the company and role spelled as the posting spells them."""
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m[1] or m[2], text)
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", text)
    text = text.replace("`", "")
    text = "\n".join(" ".join(line.split()) for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = re.sub(r"(?<=\S) +([,.;:!?])", r"\1", text)
    for name in (company.strip(), role.strip()):
        if name:
            spelled = name.replace("\\", "\\\\")
            text = re.sub(
                rf"(?<![\w]){re.escape(name)}(?![\w])", spelled, text, flags=re.I
            )
    return text


def limits(item: Field) -> tuple[int | None, int | None]:
    """The most characters and the most words a field takes: its page's limit, and
    one its question states, such as "in 200 words"."""
    characters = item.max_length
    words = None
    for found in _LIMIT.finditer(f"{item.question} {item.hint}"):
        number = int((found[1] or found[3]).replace(",", ""))
        unit = (found[2] or found[4]).casefold()
        if unit.startswith("word"):
            words = number if words is None else min(words, number)
        else:
            characters = number if characters is None else min(characters, number)
    return characters, words


def _sentences(text: str) -> list[str]:
    return [
        " ".join(part.split())
        for part in re.split(r"(?<=[.!?])\s+|\n+", text)
        if len(part.split()) >= REPEAT_WORDS
    ]


def _expected(what: str, known: dict[str, Any]) -> str:
    """A contact detail or link as the profile has it."""
    value = str(
        known.get("name" if what in ("first_name", "last_name") else what) or ""
    )
    if what == "first_name":
        return value.split()[0] if value.split() else ""
    if what == "last_name":
        return " ".join(value.split()[1:])
    return value.strip()


def _same_contact(what: str, value: str, expected: str) -> bool:
    if what == "phone":
        return re.sub(r"\D", "", value) == re.sub(r"\D", "", expected)
    if what in ("linkedin", "github", "portfolio"):

        def bare(link: str) -> str:
            return re.sub(r"^https?://(?:www\.)?", "", link.strip().casefold()).rstrip(
                "/"
            )

        return bare(value) == bare(expected)
    return " ".join(value.split()) == " ".join(expected.split())


# What each link must be a link to.
_SITES = {
    "linkedin": ("linkedin.com/", "LinkedIn"),
    "github": ("github.com/", "GitHub"),
}


def _contact(answer: Answer, known: dict[str, Any]) -> Finding | None:
    """What is wrong with a contact detail or a link, if anything: first whether it
    is one at all, then whether it is as the profile has it."""
    value = str(answer.value).strip()
    question = answer.question
    if answer.asks == "email" and not _EMAIL.match(value):
        return Finding(question, f"{value!r} is not an email address.")
    if answer.asks == "phone" and (
        not _PHONE.match(value) or len(re.sub(r"\D", "", value)) < 7
    ):
        return Finding(question, f"{value!r} is not a phone number.")
    if answer.asks in ("linkedin", "github", "portfolio"):
        site, name = _SITES.get(answer.asks, ("", "portfolio"))
        if not _LINK.match(value) or site not in value.casefold():
            return Finding(question, f"{value!r} is not a link to your {name}.")
    expected = _expected(answer.asks, known)
    if expected and not _same_contact(answer.asks, value, expected):
        return Finding(
            question,
            f"{value!r} is not as in your profile: {expected!r}.",
            blocking=answer.source not in (forms.FROM_YOU, forms.ON_THE_PAGE),
        )
    return None


def _format(answer: Answer, item: Field) -> list[Finding]:
    """An answer in the form its field takes it."""
    value = answer.value
    text = str(value).strip()
    question = answer.question
    if item.kind == "email" and not _EMAIL.match(text):
        return [Finding(question, f"{text!r} is not an email address.")]
    if item.kind == "url" and not _LINK.match(text):
        return [Finding(question, f"{text!r} is not a full link.")]
    if item.kind == "number" and not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return [
            Finding(question, f"{text!r} is not a number, and the field takes one.")
        ]
    if item.kind == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return [Finding(question, f"{text!r} is not a date the field takes.")]
    if item.offers and item.kind != "file":
        given = value if isinstance(value, tuple) else (text,)
        offered = {forms.plain(option) for option in item.offers}
        wrong = [part for part in given if forms.plain(part) not in offered]
        if wrong:
            return [Finding(question, f"{wrong[0]!r} is not one of its options.")]
    return []


def _writing(
    answer: Answer, item: Field, company: str, others: list[str]
) -> list[Finding]:
    """A written answer: within its limits, without placeholders or markup, naming
    the company as the posting does and no other company applied to."""
    text = str(answer.value)
    question = answer.question
    found = []
    characters, words = limits(item)
    if characters is not None and len(text) > characters:
        found.append(
            Finding(
                question, f"is {len(text)} characters, over its limit of {characters}."
            )
        )
    if words is not None and len(text.split()) > words:
        found.append(
            Finding(
                question, f"is {len(text.split())} words, over its limit of {words}."
            )
        )
    if placeholder := _PLACEHOLDER.search(text):
        found.append(Finding(question, f"has a placeholder: {placeholder[0]!r}."))
    if markup := _MARKUP.search(text):
        found.append(Finding(question, f"has a Markdown or HTML mark: {markup[0]!r}."))
    if company.strip():
        for named in re.finditer(
            rf"(?<![\w]){re.escape(company.strip())}(?![\w])", text, re.IGNORECASE
        ):
            if named[0] != company.strip():
                found.append(
                    Finding(
                        question, f"spells the company {named[0]!r}, not {company!r}."
                    )
                )
                break
    for other in others:
        if re.search(rf"(?<![\w]){re.escape(other)}(?![\w])", text, re.IGNORECASE):
            found.append(Finding(question, f"names {other}, another company."))
    if re.search(r"[ \t]{2,}|\n{3,}| [,.;:!?]", text):
        found.append(Finding(question, "has stray spacing.", blocking=False))
    return found


def _letter(text: str, name: str, in_a_file: bool) -> list[Finding]:
    question = "Cover letter"
    found = []
    if not _GREETING.match(text):
        found.append(Finding(question, "does not open with a greeting."))
    closing = "\n".join(text.strip().splitlines()[-3:])
    if name.strip() and forms.plain(name) not in forms.plain(closing):
        found.append(Finding(question, f"does not sign off with your name, {name}."))
    if in_a_file:
        unwritable = sorted({c for c in text if c not in "\n\t" and not _encodable(c)})
        if unwritable:
            found.append(
                Finding(
                    question,
                    "has characters its PDF cannot show: " + " ".join(unwritable) + ".",
                )
            )
    return found


def _encodable(character: str) -> bool:
    try:
        character.encode(PDF_ENCODING)
    except UnicodeEncodeError:
        return False
    return True


def _accepts(accept: str, name: str) -> bool:
    """Whether a file input takes a file of this name's type."""
    if not accept.strip():
        return True
    suffix = PurePath(name).suffix.casefold()
    kind = mimetypes.guess_type(name)[0] or ""
    for taken in (part.strip().casefold() for part in accept.split(",")):
        if taken in (suffix, kind, "*/*", "*"):
            return True
        if taken.endswith("/*") and kind.startswith(taken[:-1]):
            return True
    return False


def check(
    answers: list[Answer],
    fields: tuple[Field, ...],
    *,
    company: str,
    known: dict[str, Any],
    resume_name: str,
    others: list[str],
    letter: str = "",
) -> list[Finding]:
    """What is wrong with an application's answers: every check on details and
    formatting, in the form's order. `fields` are the form's fields the answers are
    numbered by, `known` the profile, `others` the other companies applied to, and
    `letter` the cover letter when it goes into the form as a file."""
    by_number = {item.number: item for item in fields}
    findings: list[Finding] = []
    written: list[tuple[str, str]] = []
    name = str(known.get("name") or "")
    for answer in answers:
        item = by_number.get(answer.number)
        if item is None:
            continue
        question = answer.question
        if answer.needs:
            findings.append(Finding(question, f"needs your answer. {answer.needs}"))
            continue
        if answer.blank or (item.kind == "checkbox" and answer.value is False):
            if item.required:
                findings.append(Finding(question, "is required and has no answer."))
            continue
        if answer.asks in CONTACT and isinstance(answer.value, str):
            if (wrong := _contact(answer, known)) is not None:
                findings.append(wrong)
            continue
        if item.kind == "file":
            expected = resume_name if answer.asks == "resume" else ""
            if expected and answer.value != expected:
                findings.append(
                    Finding(
                        question,
                        f"holds {answer.value!r}, not your résumé, {expected}.",
                    )
                )
            elif not _accepts(item.accept, str(answer.value)):
                findings.append(
                    Finding(
                        question,
                        f"does not take a file like {answer.value}: it takes {item.accept}.",
                    )
                )
            continue
        findings += _format(answer, item)
        if isinstance(answer.value, str) and (
            item.kind == "textarea" or answer.source == forms.WRITTEN
        ):
            findings += _writing(answer, item, company, others)
            written.append((question, answer.value))
            if answer.asks == "cover_letter":
                findings += _letter(answer.value, name, in_a_file=False)
    if letter.strip():
        held = Field(0, "textarea", "Cover letter", False, "")
        answer = Answer(
            0, "Cover letter", "textarea", "cover_letter", letter, forms.WRITTEN
        )
        findings += _writing(answer, held, company, others)
        findings += _letter(letter, name, in_a_file=True)
        written.append(("Cover letter", letter))
    seen: dict[str, str] = {}
    for question, text in written:
        for sentence in _sentences(text):
            key = forms.plain(sentence)
            if key in seen:
                where = "again" if seen[key] == question else f"as {seen[key]!r} does"
                findings.append(Finding(question, f"says {sentence!r} {where}."))
            else:
                seen[key] = question
    return findings
