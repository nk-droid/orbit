"""Application forms: what a form asks, an answer drafted for each question from what
is known about the user, the steps that fill them in, and what the page holds
afterwards compared with what the user approved.

A form is read on its page by a script (scripts.FORM). Answers come from the user's
profile and résumé, the job, and answers the user gave and kept; the drafting model
writes the rest (writing.answer_form), and they are checked here: a choice must be one
the form offers, and a fact about the user must be in their profile or résumé in the
words given. Some answers are always the user's to give: consents, salary, and
personal questions such as gender, which are never filled in from a guess.
"""

import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# Where an answer came from, as the draft shows it.
FROM_PROFILE = "profile"
FROM_RESUME = "résumé"
FROM_JOB = "the job"
FROM_POSTING = "posting"
WRITTEN = "written"
FROM_YOU = "you said"
KEPT = "kept answer"
# An answer the page holds differently from the draft, such as one the user changed
# there by hand.
ON_THE_PAGE = "as on the page"

TEXT_KINDS = ("text", "email", "tel", "url", "number", "date", "textarea")

# The drafting model is given a choice's options when there are no more than this.
# A longer list is cut down to the options that share words with the user's profile,
# and its answer is still looked up in the whole list.
OPTIONS_SHOWN = 60

NOT_HELD = "Orbit has no {what} for you."
NO_RESUME = "Orbit has no résumé of yours: hand one over, or say where it is."
YOURS = "This is yours to answer."
CONSENT = "This asks for your consent."
NO_FILE = "This asks for a file Orbit has not got."
NO_OPTION = "None of its options fits what is known: {wanted!r}."
NOT_KNOWN = "Nothing Orbit knows answers this."
UNSUPPORTED = "Check this: your profile and résumé do not say it in those words."
LEGAL = "Check this: it is a legal statement about you."


@dataclass(frozen=True)
class Choice:
    """One of a group of radio buttons or tick boxes."""

    label: str
    selector: str
    checked: bool = False


@dataclass(frozen=True)
class Field:
    """A field of a form as its page holds it."""

    number: int
    kind: str
    label: str
    required: bool
    selector: str
    options: tuple[str, ...] = ()
    choices: tuple[Choice, ...] = ()
    max_length: int | None = None
    placeholder: str = ""
    hint: str = ""
    value: Any = ""
    accept: str = ""
    chooser: str = ""

    @property
    def question(self) -> str:
        return question_of(self.label)

    @property
    def offers(self) -> tuple[str, ...]:
        """The answers it takes, when it takes only some."""
        return tuple(choice.label for choice in self.choices) or self.options

    @property
    def typed(self) -> bool:
        """A drop-down that lists its options only as something is typed in it."""
        return self.kind == "combobox" and not self.options


@dataclass(frozen=True)
class Form:
    """An application form as its page holds it."""

    url: str
    title: str
    fields: tuple[Field, ...]
    # The button that submits it, and one that goes on to a next step: each a
    # selector and what it says.
    submit: dict[str, str] | None = None
    next: dict[str, str] | None = None
    # Frames on the page holding an application from elsewhere.
    frames: tuple[str, ...] = ()
    # Whether the page asks for a password, or shows a check that the user is a
    # person: both are the user's to do.
    password: bool = False
    challenge: bool = False
    errors: tuple[str, ...] = ()


def read(found: dict[str, Any]) -> Form:
    """A form from what the form script found on its page."""
    fields = []
    for number, item in enumerate(found.get("fields") or [], start=1):
        choices = ()
        options: tuple[str, ...] = ()
        if item.get("kind") in ("radio", "checkboxes"):
            choices = tuple(
                Choice(
                    " ".join(str(option.get("label", "")).split()),
                    str(option.get("selector", "")),
                    bool(option.get("checked")),
                )
                for option in item.get("options") or []
            )
        else:
            options = tuple(
                " ".join(str(option).split()) for option in item.get("options") or []
            )
        fields.append(
            Field(
                number=number,
                kind=str(item.get("kind", "text")),
                label=" ".join(str(item.get("label", "")).split()),
                required=bool(item.get("required")),
                selector=str(item.get("selector", "")),
                options=options,
                choices=choices,
                max_length=item.get("max_length") or None,
                placeholder=str(item.get("placeholder", "")),
                hint=str(item.get("hint", "")),
                value=item.get("value", ""),
                accept=str(item.get("accept", "")),
                chooser=str(item.get("chooser", "")),
            )
        )
    return Form(
        url=str(found.get("url", "")),
        title=str(found.get("title", "")),
        fields=tuple(fields),
        submit=found.get("submit") or None,
        next=found.get("next") or None,
        frames=tuple(found.get("frames") or ()),
        password=bool(found.get("password")),
        challenge=bool(found.get("challenge")),
        errors=tuple(found.get("errors") or ()),
    )


_MARKS = re.compile(r"\s*(?:\*|\u2731|\(required\))\s*$", re.IGNORECASE)


def question_of(label: str) -> str:
    """A field's question without the marks that say it is required."""
    text = " ".join(label.replace("\u2731", " ").split())
    while (bare := _MARKS.sub("", text)) != text:
        text = bare
    return text.strip()


def plain(text: str) -> str:
    """Text as answers are compared: one case, single spaces, plain quotes."""
    text = text.translate({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"'})
    return " ".join(text.casefold().split())


# What a question asks, by its words, for the questions code answers itself. The
# first that fits is taken.
_FROM_PROFILE = (
    ("first_name", r"\b(?:first|given)[ -]?name\b"),
    ("last_name", r"\b(?:last|family)[ -]?name\b|\bsurname\b"),
    ("preferred_name", r"\bprefer(?:red)?\b.*\bname\b|\bname\b.*\bprefer|\bcall you\b"),
    ("pronunciation", r"\bpronounc"),
    (
        "name",
        r"^(?:your |full |legal |candidate'?s? )*name\b|\bfull name\b|\blegal name\b",
    ),
    ("email", r"\be-?mail\b"),
    ("phone", r"\b(?:phone|mobile|cell|telephone|contact number|whatsapp)\b"),
    ("linkedin", r"linked\s?in"),
    ("github", r"git\s?hub"),
    (
        "portfolio",
        r"\b(?:portfolio|website|web site|personal (?:site|page|url)|blog)\b",
    ),
    (
        "city",
        r"\b(?:city|location|address)\b|\bwhere (?:are you|do you) (?:currently )?"
        r"(?:live|located|based)\b|\bcurrently (?:located|based)\b",
    ),
    (
        "company",
        r"\b(?:current|present|most recent|latest) (?:company|employer|organi[sz]ation)"
        r"\b|^(?:company|employer)$",
    ),
    (
        "title",
        r"\b(?:current|present|most recent|latest) (?:job )?(?:title|role|position|"
        r"designation)\b",
    ),
    ("years", r"\byears\b.*\bexperience\b|\bexperience\b.*\byears\b"),
    (
        "notice",
        r"\bnotice period\b|\bwhen can you (?:start|join)\b|\bstart(?:ing)? date\b|"
        r"\bearliest\b.*\b(?:start|join)|\bavailab\w* to (?:start|join)\b|"
        r"\bjoin(?:ing)? date\b",
    ),
)
# The questions that are always the user's to answer, each kept under its topic when
# they say to keep their answer.
TOPICS = (
    ("salary", r"\b(?:salary|compensation|ctc|pay|remuneration|stipend|hourly rate)\b"),
    ("gender", r"\bgender\b|\bsex\b"),
    ("hispanic", r"hispanic|latin[oax]"),
    ("race", r"\brace\b|\bracial\b|\bethnic"),
    ("veteran", r"\bveteran"),
    ("disability", r"\bdisabilit"),
    ("sexual orientation", r"\bsexual orientation\b|\blgbt"),
    ("pronouns", r"\bpronouns?\b"),
    ("transgender", r"\btransgender\b"),
    ("age", r"\bage\b|\bdate of birth\b|\bbirth ?date\b"),
    ("religion", r"\breligio|\bcaste\b"),
    ("accommodations", r"\baccommodat|\badjustments?\b"),
)
# Questions whose answer is a statement about the user with legal weight: answered
# from what is known, and the user is asked to confirm.
_LEGAL = re.compile(
    r"\bsponsor|\bauthori[sz]ed\b|\beligib|\blegally\b|\bright to work\b|\bvisa\b|"
    r"\bwork permit\b|\bcitizen|\bpreviously (?:worked|employed|been)\b|"
    r"\b(?:ever|before) (?:worked|been employed)\b|\bformer employee\b|\bnon-?compete\b|"
    r"\b(?:employment|restrictive) (?:agreements?|covenants?)\b|"
    r"\bpost-employment\b|\bconvicted\b|\bcriminal\b|\bbackground check\b",
    re.IGNORECASE,
)
_CONSENT = re.compile(
    r"\b(?:consent|agree|acknowledge|certify|confirm|accept|privacy|terms|polic(?:y|ies)|"
    r"declare|understood|read the)\b",
    re.IGNORECASE,
)
_HEARD = re.compile(
    r"\bhow (?:did )?you (?:hear|heard|find|found|learn|learned|learnt|come across|"
    r"came across)\b|\bwhere did you (?:hear|find|see|learn)\b|"
    r"\bhow (?:did|do) you know about\b|^(?:application |referral |job )?source$",
    re.IGNORECASE,
)
_CATCH_ALL = re.compile(
    r"\b(?:additional information|anything else|other information|comments?|"
    r"anything you(?:'d| would) like)\b",
    re.IGNORECASE,
)
_LETTER = re.compile(r"\bcover\s*letter\b", re.IGNORECASE)
_RESUME = re.compile(r"r[ée]sum[ée]|\bcv\b|curriculum vitae", re.IGNORECASE)
# A file field for anything more the applicant wants to add, such as "Add
# attachment" or "Additional documents": where the cover letter goes, as a PDF,
# when the form has no place of its own for one. Not one that names another thing.
_ATTACHMENT = re.compile(
    r"\battach(?:ment)?s?\b|\b(?:additional|other|supporting|more)\s+"
    r"(?:documents?|files?|attachments?)\b",
    re.IGNORECASE,
)
_SOMETHING_ELSE = re.compile(
    r"portfolio|transcript|certificat|photo|picture|passport|identity|\bid\b|"
    r"sample|degree|mark\s*sheet|reference|visa|permit",
    re.IGNORECASE,
)
_PHONE_CODE = re.compile(
    r"^(?:country(?: code)?|code|dial(?:ling)? code)$", re.IGNORECASE
)
_CODE = re.compile(r"\+\s?(\d{1,4})\b")


def topic_of(question: str) -> str | None:
    """The topic of a question that is always the user's to answer, if it is one."""
    return next(
        (topic for topic, pattern in TOPICS if re.search(pattern, question, re.I)),
        None,
    )


def asks(item: Field) -> str:
    """What a field asks, for the fields code answers itself; "question" for the rest,
    which the drafting model answers."""
    question = item.question
    if item.kind == "file":
        if _LETTER.search(question):
            return "cover_letter"
        if _RESUME.search(question):
            return "resume"
        if _ATTACHMENT.search(question) and not _SOMETHING_ELSE.search(question):
            return "attachment"
        return "file"
    if _LETTER.search(f"{question} {item.placeholder}") and item.kind == "textarea":
        return "cover_letter"
    if item.kind == "checkbox" or (
        item.kind in ("radio", "checkboxes") and _CONSENT.search(question)
    ):
        return "consent"
    if topic_of(question):
        return "yours"
    if _HEARD.search(question):
        return "heard"
    if _PHONE_CODE.match(question) and any(_CODE.search(o) for o in item.offers):
        return "phone_code"
    for what, pattern in _FROM_PROFILE:
        if re.search(pattern, question, re.IGNORECASE):
            return what
    if item.kind == "textarea" and _CATCH_ALL.search(question):
        return "anything_else"
    return "question"


@dataclass(frozen=True)
class Answer:
    """The answer drafted for one field."""

    number: int
    question: str
    kind: str
    asks: str
    # Text; True or False for a tick box; the options ticked for a group of them.
    value: str | bool | tuple[str, ...] = ""
    source: str = ""
    # Why the user must answer it before the form is filled in, when they must.
    needs: str = ""
    # Why the user is asked to confirm it, when they are.
    confirm: str = ""
    # The words in the profile or résumé that say it.
    basis: str = ""

    @property
    def blank(self) -> bool:
        """No answer. A tick box left unticked on purpose is an answer."""
        return self.value in ("", ())

    def shown(self) -> str:
        """The answer as the draft shows it."""
        if isinstance(self.value, bool):
            return "Ticked" if self.value else "Not ticked"
        if isinstance(self.value, tuple):
            return ", ".join(self.value)
        return self.value


def as_saved(answers: list[Answer]) -> list[dict[str, Any]]:
    return [asdict(answer) for answer in answers]


def from_saved(saved: list[dict[str, Any]]) -> list[Answer]:
    return [
        Answer(**{**item, "value": tuple(item["value"])})
        if isinstance(item.get("value"), list)
        else Answer(**item)
        for item in saved
    ]


@dataclass
class Drafted:
    """What code drafted of a form: an answer for each field, those left for the
    drafting model to write, and the field the cover letter goes in, if any."""

    answers: list[Answer]
    to_write: list[Field] = field(default_factory=list)
    letter: Field | None = None


def _first_last(name: str) -> tuple[str, str]:
    words = name.split()
    return (words[0], " ".join(words[1:])) if len(words) > 1 else (name, "")


def _link(value: str) -> str:
    value = value.strip()
    return value if re.match(r"https?://", value, re.I) else f"https://{value}"


_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "eight": 8, "ten": 10, "twelve": 12, "fifteen": 15, "thirty": 30, "sixty": 60,
    "ninety": 90,
}  # fmt: skip
_SPANS = {"day": 1, "week": 7, "month": 30}


def start_day(notice: str, today: date) -> date | None:
    """The day the user could start, from their notice period as they gave it, such
    as "30 days", "two months" or "immediately". The longest it says is taken."""
    said = notice.casefold()
    if re.search(r"\b(?:immediate(?:ly)?|right away|now|no notice)\b", said):
        return today
    longest = None
    for number, unit in re.findall(
        r"\b(\d+|" + "|".join(_NUMBERS) + r")\s*(day|week|month)s?\b", said
    ):
        count = int(number) if number.isdigit() else _NUMBERS[number]
        days = count * _SPANS[unit]
        longest = days if longest is None else max(longest, days)
    return today + timedelta(days=longest) if longest is not None else None


_FORMATS = (
    (re.compile(r"\bdd[/.-]mm[/.-]yyyy\b", re.I), "%d/%m/%Y"),
    (re.compile(r"\bmm[/.-]dd[/.-]yyyy\b", re.I), "%m/%d/%Y"),
    (re.compile(r"\byyyy[/.-]mm[/.-]dd\b", re.I), "%Y-%m-%d"),
)


def date_text(day: date, item: Field) -> str:
    """A day written as the field takes it: as a date field does, or in the format
    its placeholder or hint shows, or else year first."""
    if item.kind != "date":
        shown = f"{item.placeholder} {item.hint}"
        for pattern, written in _FORMATS:
            if found := pattern.search(shown):
                separator = re.search(r"[/.-]", found[0])
                mark = separator[0] if separator else "/"
                return day.strftime(re.sub(r"[/-]", mark, written))
    return day.isoformat()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9+#.]*", plain(text))


def words_of(text: str) -> str:
    """Text as options are matched: its words in one case, without punctuation, so
    that "self-identify" is "self identify". A plus and a hash stay, as in +91 and C#."""
    return " ".join(re.sub(r"[^\w+#]+", " ", plain(text)).split())


def match_option(options: tuple[str, ...], wanted: str) -> str | None:
    """The option an answer means: the same words, or the only option that starts
    with them, holds them, or is held in them. None when no one option is meant."""
    asked = words_of(wanted)
    if not asked:
        return None
    bare = {option: words_of(option) for option in options}
    same = [option for option, text in bare.items() if text == asked]
    if same:
        return same[0]
    for test in (
        lambda text: text.startswith(asked + " "),
        lambda text: re.search(rf"(?<![\w+#]){re.escape(asked)}(?![\w+#])", text),
        lambda text: re.search(rf"(?<![\w+#]){re.escape(text)}(?![\w+#])", asked),
    ):
        found = [option for option, text in bare.items() if text and test(text)]
        if len(found) == 1:
            return found[0]
    return None


# Words most institutions' names share, which say nothing about which one is meant.
_COMMON = frozenset(
    {
        "the", "of", "and", "for", "in", "at", "a", "an", "to", "on", "with",
        "university", "college", "institute", "school", "technology", "sciences",
        "science", "engineering", "management", "studies",
    }
)  # fmt: skip


def shortlist(
    options: tuple[str, ...], about: str, size: int = OPTIONS_SHOWN
) -> list[str]:
    """The options of a long list that share words with what is known about the user,
    the most first, and those for an answer not listed, such as "Other"."""
    known = set(_words(about)) - _COMMON
    scored = []
    for index, option in enumerate(options):
        shared = (set(_words(option)) - _COMMON) & known
        if shared:
            scored.append((-len(shared), index, option))
    picked = [option for _, _, option in sorted(scored)[: size - 4]]
    others = [
        option
        for option in options
        if re.search(r"\bother\b|not listed|did not|none of", option, re.I)
    ]
    return list(dict.fromkeys([*picked, *others[:4]]))


def _for(item: Field, value: str, source: str, basis: str = "") -> Answer:
    return Answer(
        item.number, item.question, item.kind, asks(item), value, source, basis=basis
    )


def _missing(item: Field, why: str) -> Answer:
    """No answer: one the user must give when the field is required."""
    return Answer(
        item.number,
        item.question,
        item.kind,
        asks(item),
        needs=why if item.required else "",
    )


def _phone_code(item: Field, phone: str) -> str | None:
    """The option of a country-code list that a phone number written with its code,
    such as +91 98765 43210, starts with: the longest code that fits."""
    digits = re.sub(r"\D", "", phone)
    if not phone.strip().startswith("+") or not digits:
        return None
    best = None
    for option in item.offers:
        code = _CODE.search(option)
        if (
            code
            and digits.startswith(code[1])
            and (best is None or len(code[1]) > len(_CODE.search(best)[1]))
        ):
            best = option
    return best


def _profile_value(what: str, item: Field, known: dict[str, Any], today: date) -> str:
    """What the profile says for a question code answers itself, written as the field
    takes it, or "" when it does not say."""

    def said(key: str) -> str:
        value = known.get(key)
        if isinstance(value, list):
            return ", ".join(value)
        return str(value or "").strip()

    name = said("name")
    if what == "name":
        return name
    if what in ("first_name", "last_name"):
        first, last = _first_last(name)
        return first if what == "first_name" else last
    if what in ("linkedin", "github", "portfolio"):
        return _link(said(what)) if said(what) else ""
    if what == "years":
        years = said("years")
        number = re.search(r"\d+(?:\.\d+)?", years)
        if item.kind == "number":
            return number[0] if number else ""
        return years
    if what == "notice":
        notice = said("notice_period")
        if item.kind == "date" or re.search(r"\bdate\b|\bwhen\b", item.question, re.I):
            day = start_day(notice, today) if notice else None
            return date_text(day, item) if day else ""
        return notice
    return said(what)


def letter_field(form: Form) -> Field | None:
    """Where the cover letter goes: the field that asks for one, or else, as a PDF,
    a file field for anything more the applicant wants to add."""
    for wanted in ("cover_letter", "attachment"):
        if (
            found := next((f for f in form.fields if asks(f) == wanted), None)
        ) is not None:
            return found
    return None


def draft(
    form: Form,
    known: dict[str, Any],
    kept: dict[str, str],
    resume_name: str,
    site: str,
    today: date,
) -> Drafted:
    """Answers what code can answer itself, from the profile (`known`), the user's
    kept answers by topic, the résumé's name, and the site the job was found on.
    The rest are left for the drafting model."""
    drafted = Drafted(answers=[])
    letter = letter_field(form)
    for item in form.fields:
        what = asks(item)
        answer: Answer | None = None
        if what == "resume":
            answer = (
                _for(item, resume_name, FROM_RESUME)
                if resume_name
                else _missing(item, NO_RESUME)
            )
        elif item is letter:
            drafted.letter = item
            # A field for any document holds the letter as one.
            answer = replace(_for(item, "", WRITTEN), asks="cover_letter")
        elif what == "cover_letter":
            answer = _missing(item, NOT_KNOWN)
        elif what in ("file", "attachment"):
            answer = _missing(item, NO_FILE)
        elif what == "consent":
            answer = _missing(item, CONSENT)
        elif what == "yours":
            topic = topic_of(item.question) or ""
            answer = _kept(item, kept.get(topic, ""))
        elif what == "heard":
            answer = _heard(item, site)
        elif what == "phone_code":
            option = _phone_code(item, str(known.get("phone") or ""))
            answer = _for(item, option, FROM_PROFILE) if option else None
        elif what in ("preferred_name", "pronunciation", "anything_else"):
            # The user's to say, if they want to: the first name when one is required.
            if what == "preferred_name" and item.required and known.get("name"):
                answer = _for(item, _first_last(str(known["name"]))[0], FROM_PROFILE)
            else:
                answer = _missing(item, YOURS)
        elif what != "question" and (item.kind in TEXT_KINDS or item.typed):
            value = _profile_value(what, item, known, today)
            label = dict(_FROM_PROFILE_LABELS).get(what, what)
            answer = (
                _for(item, value, FROM_PROFILE)
                if value
                else _missing(item, NOT_HELD.format(what=label))
            )
        if answer is None:
            drafted.to_write.append(item)
            answer = _for(item, "", "")
        drafted.answers.append(answer)
    return drafted


_FROM_PROFILE_LABELS = (
    ("name", "name"),
    ("first_name", "name"),
    ("last_name", "name"),
    ("email", "email address"),
    ("phone", "phone number"),
    ("linkedin", "LinkedIn link"),
    ("github", "GitHub link"),
    ("portfolio", "portfolio link"),
    ("city", "city"),
    ("company", "current company"),
    ("title", "current role"),
    ("years", "years of experience"),
    ("notice", "notice period"),
)


def _kept(item: Field, said: str) -> Answer:
    """A question that is always the user's: their kept answer, as the field takes
    it, or theirs to give."""
    if not said:
        return _missing(item, YOURS)
    if item.offers:
        option = match_option(item.offers, said)
        if option is None:
            return _missing(item, NO_OPTION.format(wanted=said))
        value: str | tuple[str, ...] = (
            (option,) if item.kind == "checkboxes" else option
        )
        return Answer(item.number, item.question, item.kind, asks(item), value, KEPT)
    return Answer(item.number, item.question, item.kind, asks(item), said, KEPT)


def _heard(item: Field, site: str) -> Answer | None:
    """How the user heard of the job: the site Orbit found it on."""
    if not site:
        return None
    if not item.offers:
        return _for(item, site, FROM_JOB)
    option = match_option(item.offers, site)
    if option is None:
        return None
    value: str | tuple[str, ...] = (option,) if item.kind == "checkboxes" else option
    return Answer(item.number, item.question, item.kind, asks(item), value, FROM_JOB)


def questions_for_model(
    drafted: Drafted, form: Form, about: str
) -> list[dict[str, Any]]:
    """The questions the drafting model answers, as it is given them: each with its
    id, words, kind, whether required, its options or a shortlist of them, and its
    limit in characters."""
    asked = []
    for item in drafted.to_write:
        if item.kind == "checkboxes":
            kind = "several choices"
        elif item.offers:
            kind = "choice"
        else:
            kind = "writing" if item.kind == "textarea" else "short answer"
        question: dict[str, Any] = {
            "id": item.number,
            "question": item.question,
            "kind": kind,
            "required": item.required,
        }
        if item.hint:
            question["hint"] = item.hint[:300]
        if item.offers:
            if len(item.offers) <= OPTIONS_SHOWN:
                question["options"] = list(item.offers)
            else:
                question["options"] = shortlist(item.offers, about)
                question["options_note"] = (
                    f"The form lists {len(item.offers)} options; these are those that "
                    "may fit. Answer with the exact words of the option meant."
                )
        if item.max_length:
            question["max_characters"] = item.max_length
        asked.append(question)
    return asked


def with_written(
    drafted: Drafted,
    form: Form,
    written: dict[int, dict[str, str]],
    sources: str,
    letter: str = "",
    letter_file: str = "",
) -> list[Answer]:
    """The draft with the drafting model's answers in: each choice one the field
    offers, and each fact about the user said in their profile or résumé (`sources`),
    or else marked for the user to confirm or answer. The cover letter goes into its
    field as it is written, or for a file, as the name of the file it is put in."""
    by_number = {item.number: item for item in form.fields}
    answers = []
    for answer in drafted.answers:
        item = by_number[answer.number]
        if drafted.letter is not None and answer.number == drafted.letter.number:
            held = letter.strip() if item.kind != "file" else letter_file
            answers.append(
                Answer(
                    answer.number,
                    answer.question,
                    answer.kind,
                    answer.asks,
                    held if letter.strip() else "",
                    WRITTEN if letter.strip() else "",
                    needs="" if letter.strip() or not item.required else NOT_KNOWN,
                )
            )
            continue
        if item not in drafted.to_write:
            answers.append(answer)
            continue
        answers.append(_checked(item, written.get(item.number) or {}, sources))
    return answers


def _checked(item: Field, given: dict[str, str], sources: str) -> Answer:
    said = str(given.get("answer") or "").strip()
    source = str(given.get("from") or "").strip().casefold()
    basis = str(given.get("basis") or "").strip()
    if not said or source in ("", "unknown"):
        return _missing(item, NOT_KNOWN)
    value: str | tuple[str, ...] = said
    if item.offers:
        picked = []
        for part in said.split(" | ") if item.kind == "checkboxes" else [said]:
            option = match_option(item.offers, part)
            if option is None:
                return _missing(item, NO_OPTION.format(wanted=part))
            picked.append(option)
        value = tuple(picked) if item.kind == "checkboxes" else picked[0]
    named = {
        "profile": FROM_PROFILE,
        "résumé": FROM_RESUME,
        "resume": FROM_RESUME,
        "posting": FROM_POSTING,
    }.get(source, WRITTEN)
    confirm = ""
    if named in (FROM_PROFILE, FROM_RESUME) and (
        not basis or plain(basis) not in plain(sources)
    ):
        confirm = UNSUPPORTED
    if not confirm and _LEGAL.search(item.question):
        confirm = LEGAL
    return Answer(
        item.number,
        item.question,
        item.kind,
        asks(item),
        value,
        named,
        confirm=confirm,
        basis=basis,
    )


def changed(form: Form, number: int, said: str) -> Answer:
    """An answer as the user gave it, for the field with that number: for a choice,
    one of its options. Raises ValueError when the field takes no such answer."""
    item = next((f for f in form.fields if f.number == number), None)
    if item is None:
        raise ValueError(f"The form has no question {number}.")
    said = said.strip()
    if item.kind == "checkbox":
        value: str | bool | tuple[str, ...] = bool(
            re.match(
                r"(?:yes|y|true|tick|ticked|agree|i agree|check|checked)\b", said, re.I
            )
        )
    elif item.offers:
        picked = []
        for part in (
            re.split(r"\s*(?:\||;)\s*", said) if item.kind == "checkboxes" else [said]
        ):
            option = match_option(item.offers, part)
            if option is None:
                offered = "; ".join(item.offers[:20])
                raise ValueError(
                    f"{item.question!r} has no option {part!r}. It offers: {offered}."
                )
            picked.append(option)
        value = tuple(picked) if item.kind == "checkboxes" else picked[0]
    else:
        value = said
    return Answer(item.number, item.question, item.kind, asks(item), value, FROM_YOU)


def number_of(form: Form, named: str) -> int | None:
    """The number of the field a question names: its number, or its words."""
    named = named.strip()
    if named.isdigit():
        return int(named)
    wanted = plain(question_of(named))
    exact = [f.number for f in form.fields if plain(f.question) == wanted]
    if exact:
        return exact[0]
    held = [f.number for f in form.fields if wanted and wanted in plain(f.question)]
    return held[0] if len(held) == 1 else None


# How the browser fills a field in.
@dataclass(frozen=True)
class Step:
    # "fill" for the fields filled in together, "pick" for a drop-down whose option
    # is picked, and "file" for a file put into the form.
    how: str
    question: str
    target: str = ""
    value: str = ""
    # For "fill": each field as the browser's fill_form takes it.
    fields: tuple[dict[str, str], ...] = ()


def matched(answers: list[Answer], form: Form) -> list[tuple[Answer, Field]]:
    """Each answer with the field it is for on the page now: the same question of the
    same kind, the first not yet taken."""
    taken: set[int] = set()
    pairs = []
    for answer in answers:
        found = next(
            (
                item
                for item in form.fields
                if item.number not in taken
                and item.kind == answer.kind
                and plain(item.question) == plain(answer.question)
            ),
            None,
        )
        if found is not None:
            taken.add(found.number)
            pairs.append((answer, found))
    return pairs


def missing_fields(answers: list[Answer], form: Form) -> list[str]:
    """The questions answered in the draft that the page no longer asks."""
    found = {answer.number for answer, _ in matched(answers, form)}
    return [a.question for a in answers if a.number not in found and not a.blank]


def steps(answers: list[Answer], form: Form, files: dict[str, Path]) -> list[Step]:
    """How the browser fills in the answers: those it can fill together first, then
    each drop-down, then each file. A blank answer leaves its field as it is.
    `files` are the files to put in, by the name an answer gives."""
    together: list[dict[str, str]] = []
    one_by_one: list[Step] = []
    put: list[Step] = []
    for answer, item in matched(answers, form):
        if answer.needs or answer.blank:
            continue
        name = item.question[:80] or "field"
        if item.kind == "file":
            path = files.get(str(answer.value))
            if path is not None and plain(str(item.value)) != plain(path.name):
                put.append(
                    Step(
                        "file", item.question, item.chooser or item.selector, str(path)
                    )
                )
        elif item.kind == "checkbox":
            if bool(answer.value) != bool(item.value):
                value = "true" if answer.value else "false"
                together.append(
                    {
                        "target": item.selector,
                        "name": name,
                        "type": "checkbox",
                        "value": value,
                    }
                )
        elif item.kind in ("radio", "checkboxes"):
            wanted = (
                set(answer.value)
                if isinstance(answer.value, tuple)
                else {str(answer.value)}
            )
            for choice in item.choices:
                # A radio button is unticked by ticking another.
                if item.kind == "radio" and choice.label not in wanted:
                    continue
                if (choice.label in wanted) != choice.checked:
                    together.append(
                        {
                            "target": choice.selector,
                            "name": f"{name}: {choice.label}"[:100],
                            "type": "radio" if item.kind == "radio" else "checkbox",
                            "value": "true" if choice.label in wanted else "false",
                        }
                    )
        elif item.kind == "select":
            together.append(
                {
                    "target": item.selector,
                    "name": name,
                    "type": "combobox",
                    "value": str(answer.value),
                }
            )
        elif item.kind == "combobox":
            if plain(str(item.value)) != plain(str(answer.value)):
                one_by_one.append(
                    Step("pick", item.question, item.selector, str(answer.value))
                )
        else:
            together.append(
                {
                    "target": item.selector,
                    "name": name,
                    "type": "textbox",
                    "value": str(answer.value),
                }
            )
    filled = [Step("fill", "the form", fields=tuple(together))] if together else []
    return [*filled, *one_by_one, *put]


@dataclass(frozen=True)
class Difference:
    """A field whose value on the page is not the one approved."""

    question: str
    approved: str
    on_page: str
    # A field the page asks that the draft did not have.
    new: bool = False


def _on_page(item: Field) -> str:
    if item.kind in ("radio", "checkboxes"):
        return ", ".join(choice.label for choice in item.choices if choice.checked)
    if item.kind == "checkbox":
        return "Ticked" if item.value else "Not ticked"
    return " ".join(str(item.value or "").split())


def _same(answer: Answer, item: Field) -> bool:
    page = _on_page(item)
    if item.kind == "checkbox":
        return bool(item.value) == bool(answer.value)
    if item.kind in ("radio", "checkboxes", "select", "combobox"):
        wanted = (
            answer.value if isinstance(answer.value, tuple) else (str(answer.value),)
        )
        held = tuple(p for p in (c.label for c in item.choices if c.checked)) or (
            (page,) if page else ()
        )
        return {plain(w) for w in wanted if w} == {plain(h) for h in held if h}
    if item.kind == "tel" or answer.asks == "phone":
        return re.sub(r"\D", "", page) == re.sub(r"\D", "", str(answer.value))
    if item.kind == "file":
        return (
            plain(Path(page).name) == plain(str(answer.value))
            if page
            else not answer.value
        )
    return " ".join(str(answer.value).split()) == page


def compare(answers: list[Answer], form: Form) -> list[Difference]:
    """What the page holds that is not what was approved, field by field: character
    for character apart from spacing, with a phone number's digits, each choice and
    tick box, and the name of a file put in. Fields the page asks that the draft did
    not have are differences too."""
    differences = []
    pairs = matched(answers, form)
    for answer, item in pairs:
        if (answer.needs or answer.blank) and not _on_page(item):
            continue
        if not _same(answer, item):
            differences.append(
                Difference(item.question, answer.shown(), _on_page(item))
            )
    taken = {item.number for _, item in pairs}
    for item in form.fields:
        if item.number not in taken:
            differences.append(Difference(item.question, "", _on_page(item), new=True))
    for question in missing_fields(answers, form):
        differences.append(Difference(question, "", "(no longer on the page)"))
    return differences


def page_answers(answers: list[Answer], form: Form) -> list[Answer]:
    """The draft's answers as the page holds them now: what the user approved, with
    each field the user changed by hand taking its value from the page."""
    by_answer = {answer.number: item for answer, item in matched(answers, form)}
    held = []
    for answer in answers:
        item = by_answer.get(answer.number)
        if item is None or _same(answer, item) or (answer.blank and not _on_page(item)):
            held.append(answer)
            continue
        if item.kind in ("radio", "checkboxes"):
            labels = tuple(c.label for c in item.choices if c.checked)
            value: str | bool | tuple[str, ...] = (
                labels if item.kind == "checkboxes" else (labels[0] if labels else "")
            )
        elif item.kind == "checkbox":
            value = bool(item.value)
        else:
            value = _on_page(item)
        held.append(
            Answer(
                answer.number,
                answer.question,
                answer.kind,
                answer.asks,
                value,
                ON_THE_PAGE,
            )
        )
    return held
