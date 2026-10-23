"""Applying for jobs: reading real application forms, drafting answers, checking
their details and formatting, and the two yeses that fill a form in and submit it.

The forms in tests/forms are real Greenhouse, Lever and Ashby forms as Orbit's form
script read them in the browser, before anything was typed. The person applying is
made up.
"""

import copy
import io
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from livekit.agents.llm import ToolError
from pypdf import PdfReader
from test_jobs import FakeGoogle

import agent_tools.actions as action_tools
import agent_tools.applications as applications
import agent_tools.jobs as job_tools
from actions.gate import ActionGate
from agent_tools import applying, screen
from agent_tools.state import OrbitState
from integrations.google import gsheets
from jobs import (
    checks,
    forms,
    letter,
    profile,
    scripts,
    search,
    settings,
    sheet,
    store,
    writing,
)

FORMS = Path(__file__).parent / "forms"
TODAY = date(2026, 10, 18)
KNOWN = {
    "name": "Asha Rao",
    "email": "asha.rao@example.com",
    "phone": "+91 98765 43210",
    "city": "Bengaluru",
    "linkedin": "linkedin.com/in/asharao",
    "github": "github.com/asharao",
    "portfolio": "asharao.dev",
    "title": "Backend Engineer",
    "company": "Acme Labs",
    "years": "4",
    "skills": ["Python", "PostgreSQL", "Kubernetes"],
    "education": ["B.Tech, Indian Institute of Technology, Madras, 2021"],
    "authorization": "Indian citizen",
    "notice_period": "30 days",
    "roles": ["Backend Engineer"],
    "locations": ["Bengaluru", "remote"],
}
RESUME_TEXT = (
    "Asha Rao\nBackend Engineer, Acme Labs, 2021 to now\n"
    "Built a billing service in Python that handles 2 million invoices a month.\n"
    "Indian Institute of Technology, Madras. B.Tech, 2021."
)
LETTER = (
    "Dear Hiring Team,\n\nI would like to apply for the Director, Analytics "
    "Engineering role at GitLab.\n\nAt Acme Labs I built a billing service in Python "
    "that handles 2 million invoices a month.\n\nThank you for reading.\n\nAsha Rao"
)


def _form(name: str) -> dict:
    return json.loads((FORMS / f"{name}.json").read_text())


def _field(form: forms.Form, words: str) -> forms.Field:
    return next(item for item in form.fields if words in item.question)


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Job data is kept in a throwaway folder, never the real one."""
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ORBIT_DRY_RUN", raising=False)


# Reading a form.


def test_a_greenhouse_form_is_read_with_its_questions_and_options():
    form = forms.read(_form("greenhouse"))

    assert len(form.fields) == 19
    assert form.submit["said"] == "Submit application"
    assert not form.challenge and not form.password
    resume = _field(form, "Resume")
    assert (resume.kind, resume.question, resume.required) == (
        "file",
        "Resume/CV",
        True,
    )
    assert resume.accept == ".pdf,.doc,.docx,.txt,.rtf"
    based = _field(form, "based in the United States")
    assert based.kind == "combobox" and based.offers == ("Yes", "No")
    assert based.question == "Are you currently based in the United States?"
    assert len(_field(form, "country of residence").offers) == 197
    # The duplicate inputs a drop-down keeps for the browser are not fields.
    assert len({item.question for item in form.fields}) == 19


def test_a_lever_form_is_read_with_its_groups_and_long_lists():
    form = forms.read(_form("lever"))

    assert len(form.fields) == 21
    assert _field(form, "Resume/CV").required  # marked ✱ beside its button
    languages = _field(form, "Language Skill")
    assert languages.kind == "checkboxes" and len(languages.choices) == 33
    authorized = _field(form, "legally authorized")
    assert authorized.kind == "radio" and authorized.offers == ("Yes", "No")
    assert len(_field(form, "Which university").offers) == 3301
    # A question asked beside a field, not in a label, is still its question.
    assert _field(form, "Preferred Name").placeholder == "Type your response"


def test_an_ashby_form_is_read_without_its_autofill_upload():
    form = forms.read(_form("ashby"))

    assert len(form.fields) == 13
    assert [item.kind for item in form.fields].count("file") == 1
    located = _field(form, "currently located")
    # Marked required only by a star its stylesheet draws.
    assert located.required and located.typed
    assert _field(form, "Legal Name").hint.startswith("Please enter your legal name")
    assert _field(form, "Race").kind == "radio"


@pytest.mark.parametrize(
    "label, question",
    [
        ("Email *", "Email"),
        ("Full name ✱", "Full name"),
        ("Resume/CV ✱", "Resume/CV"),
        ("Phone (required)", "Phone"),
        ("Are you 18? *", "Are you 18?"),
    ],
)
def test_a_question_is_read_without_its_required_mark(label, question):
    assert forms.question_of(label) == question


# What each question asks, and the draft code makes.


def _drafted(name: str, known: dict | None = None, kept: dict | None = None):
    form = forms.read(_form(name))
    return form, forms.draft(
        form, KNOWN if known is None else known, kept or {}, "Asha Rao - Resume.pdf",
        "LinkedIn", TODAY,
    )  # fmt: skip


def _answer(drafted: forms.Drafted, words: str) -> forms.Answer:
    return next(a for a in drafted.answers if words in a.question)


def test_a_greenhouse_draft_fills_in_what_the_profile_says():
    _, drafted = _drafted("greenhouse")

    shown = {a.question: (a.shown(), a.source) for a in drafted.answers if not a.blank}
    assert shown["First Name"] == ("Asha", forms.FROM_PROFILE)
    assert shown["Last Name"] == ("Rao", forms.FROM_PROFILE)
    assert shown["Email"] == ("asha.rao@example.com", forms.FROM_PROFILE)
    assert shown["Phone"] == ("+91 98765 43210", forms.FROM_PROFILE)
    # The country-code list beside the phone number, from the number itself.
    assert shown["Country"] == ("India +91", forms.FROM_PROFILE)
    assert shown["LinkedIn Profile"][0] == "https://linkedin.com/in/asharao"
    assert shown["Resume/CV"] == ("Asha Rao - Resume.pdf", forms.FROM_RESUME)
    assert drafted.letter is not None and drafted.letter.question == "Cover Letter"


def test_personal_questions_and_consents_are_never_answered_from_a_guess():
    _, drafted = _drafted("greenhouse")

    for words in ("Gender", "Hispanic", "Veteran", "accessible and inclusive"):
        answer = _answer(drafted, words)
        # Not required: left blank for the user, never guessed.
        assert answer.blank and not answer.needs, words
    _, ashby = _drafted("ashby")
    assert _answer(ashby, "Arbitration Agreement").blank
    _, lever = _drafted("lever")
    consent = _answer(lever, "AI notetakers")
    assert consent.needs == forms.CONSENT


def test_a_kept_answer_answers_its_topic_on_any_form():
    _, greenhouse = _drafted("greenhouse", kept={"gender": "Decline to self-identify"})
    _, ashby = _drafted("ashby", kept={"gender": "Decline to self-identify"})

    assert _answer(greenhouse, "Gender").shown() == "Decline To Self Identify"
    assert _answer(ashby, "Gender").shown() == "Decline to self-identify"
    assert _answer(ashby, "Gender").source == forms.KEPT


def test_how_the_user_heard_of_a_job_is_where_orbit_found_it():
    _, lever = _drafted("lever")

    heard = _answer(lever, "how you heard")
    assert (heard.shown(), heard.source) == ("LinkedIn", forms.FROM_JOB)


def test_a_start_date_comes_from_the_notice_period():
    _, ashby = _drafted("ashby")

    assert _answer(ashby, "When can you start").shown() == "2026-11-17"
    assert _answer(ashby, "currently located").shown() == "Bengaluru"


def test_what_the_profile_lacks_needs_the_user_only_when_required():
    _, drafted = _drafted("lever", known={"name": "Asha Rao", "email": "a@example.com"})

    assert _answer(drafted, "Current location").needs == forms.NOT_HELD.format(
        what="city"
    )
    phone = _answer(drafted, "Phone")
    assert phone.blank and not phone.needs


def test_the_rest_are_left_for_the_drafting_model_with_their_options():
    form, drafted = _drafted("lever")

    asked = forms.questions_for_model(drafted, form, forms.plain(json.dumps(KNOWN)))
    by_words = {q["question"][:30]: q for q in asked}
    university = next(q for q in asked if "university" in q["question"])
    # A long list is cut down to the options that may fit, and those for none.
    assert "Indian Institute of Technology - Madras" in university["options"]
    assert "Other - School Not Listed" in university["options"]
    assert len(university["options"]) <= forms.OPTIONS_SHOWN
    assert any(q["kind"] == "writing" for q in asked)
    assert next(q for q in asked if "Language" in q["question"])["kind"] == (
        "several choices"
    )
    assert not any("Full name" in words for words in by_words)


@pytest.mark.parametrize(
    "options, wanted, meant",
    [
        (("Yes", "No"), "no", "No"),
        (("Yes", "No"), "No, I do not need one", "No"),
        (("Yes, I consent", "No, I do not consent"), "Yes", "Yes, I consent"),
        (("No", "Yes, EU Blue Card", "Yes, H-1B"), "Yes", None),
        (("India", "British Indian Ocean Territory"), "India", "India"),
        (("India +91", "Indonesia +62"), "India", "India +91"),
        (("Yes", "No"), "Maybe", None),
    ],
)
def test_an_answer_is_matched_to_the_option_it_means(options, wanted, meant):
    assert forms.match_option(options, wanted) == meant


@pytest.mark.parametrize(
    "notice, days",
    [
        ("30 days", 30),
        ("two months", 60),
        ("Immediately", 0),
        ("2 weeks", 14),
        ("30-60 days", 60),
        ("negotiable", None),
    ],
)
def test_the_day_the_user_could_start_is_worked_out(notice, days):
    day = forms.start_day(notice, TODAY)
    assert (day - TODAY).days == days if days is not None else day is None


def test_a_day_is_written_as_its_field_takes_it():
    day = date(2026, 11, 17)
    field = forms.Field(1, "text", "Start date", False, "#d", placeholder="DD/MM/YYYY")

    assert forms.date_text(day, field) == "17/11/2026"
    assert forms.date_text(day, forms.Field(1, "date", "Start", False, "#d")) == (
        "2026-11-17"
    )


def test_the_drafting_models_answers_are_checked():
    form, drafted = _drafted("greenhouse")
    sources = "\n".join(f"{k}: {v}" for k, v in KNOWN.items())
    based = _field(form, "based in the United States").number
    sponsor = _field(form, "sponsorship").number
    worked = _field(form, "worked at or consulted").number
    agreements = _field(form, "employment agreements").number
    country = _field(form, "country of residence").number

    answers = forms.with_written(
        drafted,
        form,
        {
            based: {"answer": "No", "from": "profile", "basis": "city: Bengaluru"},
            sponsor: {"answer": "No", "from": "profile", "basis": "Indian citizen"},
            worked: {"answer": "No", "from": "résumé", "basis": "never at GitLab"},
            agreements: {"answer": "", "from": "unknown", "basis": ""},
            country: {"answer": "Narnia", "from": "profile", "basis": "Bengaluru"},
        },
        sources,
        LETTER,
        letter.file_name("Asha Rao"),
    )
    by_number = {a.number: a for a in answers}

    assert by_number[based].shown() == "No" and not by_number[based].confirm
    # A statement with legal weight is put to the user to confirm.
    assert by_number[sponsor].confirm == forms.LEGAL
    # Words the profile and résumé do not have are no basis.
    assert by_number[worked].confirm == forms.UNSUPPORTED
    assert by_number[agreements].needs == forms.NOT_KNOWN
    assert "Narnia" in by_number[country].needs
    cover = _answer(drafted, "Cover Letter").number
    assert by_number[cover].shown() == "Asha Rao - Cover letter.pdf"


def test_an_answer_the_user_gives_must_be_one_the_form_takes():
    form = forms.read(_form("greenhouse"))
    sponsor = _field(form, "sponsorship").number

    assert forms.changed(form, sponsor, "no").shown() == "No"
    assert forms.changed(form, sponsor, "no").source == forms.FROM_YOU
    with pytest.raises(ValueError, match="has no option 'Maybe'"):
        forms.changed(form, sponsor, "Maybe")
    assert forms.number_of(form, "Gender") == _field(form, "Gender").number
    assert forms.number_of(form, str(sponsor)) == sponsor


# Filling in, and reading back.


def test_the_steps_fill_together_what_they_can_then_each_list_then_files():
    form = forms.read(_form("lever"))
    _, drafted = _drafted("lever")
    answers = list(drafted.answers)
    authorized = _field(form, "legally authorized")
    university = _field(form, "Which university")
    for number, value in (
        (authorized.number, "Yes"),
        (university.number, "Indian Institute of Technology - Madras"),
    ):
        answers = [
            forms.Answer(a.number, a.question, a.kind, a.asks, value, forms.FROM_YOU)
            if a.number == number
            else a
            for a in answers
        ]
    resume = Path("/tmp/Asha Rao - Resume.pdf")

    steps = forms.steps(answers, form, {resume.name: resume})

    assert [step.how for step in steps] == ["fill", "file"]
    filled = {item["name"]: item for item in steps[0].fields}
    assert filled["Full name"]["value"] == "Asha Rao"
    assert filled["Full name"]["type"] == "textbox"
    radio = filled[f"{authorized.question[:80]}: Yes"]
    assert (radio["type"], radio["value"]) == ("radio", "true")
    # A radio button is unticked by ticking another, never by itself.
    assert not any(name.endswith(": No") for name in filled)
    assert filled[university.question[:80]]["type"] == "combobox"
    assert steps[1].value == str(resume)
    # Nothing is done to a field left blank or one that needs the user.
    assert not any("notetakers" in name for name in filled)


def test_each_drop_down_is_picked_one_at_a_time():
    form = forms.read(_form("greenhouse"))
    _, drafted = _drafted("greenhouse")

    picks = [
        step for step in forms.steps(drafted.answers, form, {}) if step.how == "pick"
    ]

    assert [(step.question, step.value) for step in picks] == [("Country", "India +91")]


def _on_page(found: dict, **values) -> forms.Form:
    page = copy.deepcopy(found)
    for item in page["fields"]:
        for words, value in values.items():
            if words.replace("_", " ") in item["label"]:
                item["value"] = value
    return forms.read(page)


def test_the_read_back_finds_what_the_page_holds_differently():
    found = _form("lever")
    _, drafted = _drafted("lever")
    answers = list(drafted.answers)
    as_drafted = {
        "Full name": "Asha Rao",
        "Email": "asha.rao@example.com",
        "Phone": "+91-98765-43210",
        "Current location": "Bengaluru",
        "Current company": "Acme Labs",
        "LinkedIn URL": "https://linkedin.com/in/asharao",
        "GitHub URL": "https://github.com/asharao",
        "Portfolio URL": "https://asharao.dev",
        "Resume/CV": "Asha Rao - Resume.pdf",
    }
    page = _on_page(found, **as_drafted)
    for item in page.fields:
        if "how you heard" in item.question:
            object.__setattr__(item, "value", "LinkedIn")

    assert forms.compare(answers, page) == []

    changed = _on_page(found, **{**as_drafted, "Current company": "Acme"})
    for item in changed.fields:
        if "how you heard" in item.question:
            object.__setattr__(item, "value", "LinkedIn")
    differences = forms.compare(answers, changed)
    assert differences == [forms.Difference("Current company", "Acme Labs", "Acme")]
    held = forms.page_answers(answers, changed)
    company = next(a for a in held if a.question == "Current company")
    assert (company.value, company.source) == ("Acme", forms.ON_THE_PAGE)


def test_a_field_the_page_no_longer_asks_is_a_difference():
    found = _form("ashby")
    _, drafted = _drafted("ashby")
    page = copy.deepcopy(found)
    page["fields"] = [f for f in page["fields"] if f["label"] != "Email"]

    assert forms.missing_fields(drafted.answers, forms.read(page)) == ["Email"]


# Checking details and formatting.


def _answered(
    question: str, value, kind: str = "text", asks: str = "question", **field
) -> tuple[forms.Answer, forms.Field]:
    number = abs(hash(question)) % 1000
    answer = forms.Answer(
        number, question, kind, asks, value, field.pop("source", forms.WRITTEN)
    )
    item = forms.Field(
        number, kind, question, field.pop("required", False), "#f", **field
    )
    return answer, item


def _problems(*pairs, others=(), letter_text="") -> list[str]:
    answers = [answer for answer, _ in pairs]
    fields = tuple(item for _, item in pairs)
    return [
        finding.said()
        for finding in checks.check(
            answers,
            fields,
            company="GitLab",
            known=KNOWN,
            resume_name="Asha Rao - Resume.pdf",
            others=list(others),
            letter=letter_text,
        )
    ]


def test_a_good_draft_passes_every_check():
    assert (
        _problems(
            _answered(
                "Email",
                "asha.rao@example.com",
                "email",
                "email",
                source=forms.FROM_PROFILE,
            ),
            _answered(
                "Phone", "+91 98765 43210", "tel", "phone", source=forms.FROM_PROFILE
            ),
            _answered("LinkedIn", "https://linkedin.com/in/asharao", "url", "linkedin"),
            _answered(
                "Why GitLab?",
                "I have run GitLab CI for four years.",
                "textarea",
                max_length=500,
            ),
            _answered("Years", "4", "number", "years"),
            _answered("Sponsorship?", "No", "select", options=("Yes", "No")),
            _answered(
                "Resume/CV",
                "Asha Rao - Resume.pdf",
                "file",
                "resume",
                accept=".pdf,.docx",
            ),
            letter_text=LETTER.replace("Director, Analytics Engineering", "Backend"),
        )
        == []
    )


@pytest.mark.parametrize(
    "pair, problem",
    [
        (
            _answered("Visa", "", "select", required=True, options=("Yes", "No")),
            "Visa: is required and has no answer.",
        ),
        (
            _answered("Email", "asha@example", "email", "email", source=forms.FROM_YOU),
            "Email: 'asha@example' is not an email address.",
        ),
        (
            _answered(
                "Email", "asha@example.com", "email", "email", source=forms.FROM_PROFILE
            ),
            "Email: 'asha@example.com' is not as in your profile: 'asha.rao@example.com'.",
        ),
        (
            _answered("Phone", "call me", "tel", "phone", source=forms.FROM_YOU),
            "Phone: 'call me' is not a phone number.",
        ),
        (
            _answered("GitHub", "https://gitlab.com/asharao", "url", "github"),
            "GitHub: 'https://gitlab.com/asharao' is not a link to your GitHub.",
        ),
        (
            _answered("GitHub", "https://github.com/asha", "url", "github"),
            "GitHub: 'https://github.com/asha' is not as in your profile: "
            "'github.com/asharao'.",
        ),
        (
            _answered("Why us?", "I love Gitlab.", "textarea"),
            "Why us?: spells the company 'Gitlab', not 'GitLab'.",
        ),
        (
            _answered("Why us?", "I want to join [Company].", "textarea"),
            "Why us?: has a placeholder: '[Company]'.",
        ),
        (
            _answered("Why us?", "I **love** it.", "textarea"),
            "Why us?: has a Markdown or HTML mark: '**love**'.",
        ),
        (
            _answered("Why us?", "word " * 60, "textarea", max_length=200),
            "Why us?: is 300 characters, over its limit of 200.",
        ),
        (
            _answered("Why us? (in 50 words or fewer)", "word " * 60, "textarea"),
            "Why us? (in 50 words or fewer): is 60 words, over its limit of 50.",
        ),
        (
            _answered("Years", "four", "number", "years"),
            "Years: 'four' is not a number, and the field takes one.",
        ),
        (
            _answered("Start", "next week", "date"),
            "Start: 'next week' is not a date the field takes.",
        ),
        (
            _answered("Visa", "Maybe", "select", options=("Yes", "No")),
            "Visa: 'Maybe' is not one of its options.",
        ),
        (
            _answered("Resume/CV", "Other.pdf", "file", "resume"),
            "Resume/CV: holds 'Other.pdf', not your résumé, Asha Rao - Resume.pdf.",
        ),
        (
            _answered(
                "Resume/CV", "Asha Rao - Resume.pdf", "file", "resume", accept=".docx"
            ),
            "Resume/CV: does not take a file like Asha Rao - Resume.pdf: it takes .docx.",
        ),
    ],
)
def test_each_check_catches_what_it_is_for(pair, problem):
    assert _problems(pair) == [problem]


def test_a_draft_that_needs_the_user_cannot_be_filled_in():
    answer = forms.Answer(1, "Salary", "text", "yours", needs=forms.YOURS)
    item = forms.Field(1, "text", "Salary", True, "#s")

    [finding] = checks.check(
        [answer], (item,), company="GitLab", known=KNOWN, resume_name="", others=[]
    )
    assert finding.blocking and finding.said() == (
        "Salary: needs your answer. This is yours to answer."
    )


def test_another_company_and_a_repeated_sentence_are_found():
    said = "I have built billing systems in Python for four years now."
    found = _problems(
        _answered("Why us?", f"{said} Initech taught me a lot.", "textarea"),
        _answered("Your best project?", said, "textarea"),
        others=["Initech"],
    )

    assert "Why us?: names Initech, another company." in found
    assert f"Your best project?: says {said!r} as 'Why us?' does." in found


def test_a_cover_letter_must_greet_and_sign_off():
    found = _problems(letter_text="I would like to apply to GitLab.\n\nThanks.")

    assert "Cover letter: does not open with a greeting." in found
    assert "Cover letter: does not sign off with your name, Asha Rao." in found
    pdf_found = _problems(letter_text="Dear team,\n\nI earn ₹ in GitLab.\n\nAsha Rao")
    assert pdf_found == ["Cover letter: has characters its PDF cannot show: ₹."]


def test_writing_is_tidied_before_it_is_checked():
    text = "Dear  team ,\n\n\n\n**I** love gitlab's [CI](https://x.dev) .\n# Done"

    assert checks.tidy(text, "GitLab", "Backend Engineer") == (
        "Dear team,\n\nI love GitLab's CI (https://x.dev).\nDone"
    )


@pytest.mark.parametrize(
    "question, hint, limits",
    [
        ("Why us? (max 300 characters)", "", (300, None)),
        ("Tell us about you", "Please answer in 150 words.", (None, 150)),
        ("Anything else?", "Up to 1,000 characters", (1000, None)),
        ("Why us?", "", (None, None)),
    ],
)
def test_a_limit_the_question_states_is_read(question, hint, limits):
    item = forms.Field(1, "textarea", question, False, "#q", hint=hint)
    assert checks.limits(item) == limits


# The cover letter as a file.


def test_a_cover_letter_is_written_to_a_pdf_that_reads_back():
    path = letter.kept(LETTER + " — café", "Asha Rao", 7)

    reader = PdfReader(io.BytesIO(path.read_bytes()))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert path.name == "Asha Rao - Cover letter.pdf"
    assert reader.metadata.title == "Asha Rao - Cover letter"
    assert " ".join(text.split()) == " ".join((LETTER + " — café").split())


def _with_attachment(found: dict) -> dict:
    """A form with a file field for anything more, as some sites have."""
    more = copy.deepcopy(next(f for f in found["fields"] if f["kind"] == "file"))
    more.update(
        label="Add attachment",
        name="attachment",
        id="attachment",
        required=False,
        selector="#attachment",
    )
    return {**found, "fields": [*found["fields"], more]}


def test_a_form_with_no_place_for_a_letter_takes_it_as_an_attachment():
    form = forms.read(_with_attachment(_form("ashby")))

    drafted = forms.draft(form, KNOWN, {}, "Asha Rao - Resume.pdf", "LinkedIn", TODAY)

    assert drafted.letter is not None and drafted.letter.question == "Add attachment"
    held = next(a for a in drafted.answers if a.question == "Add attachment")
    assert held.asks == "cover_letter"


def test_a_form_with_a_place_for_the_letter_leaves_its_attachment_free():
    form = forms.read(_with_attachment(_form("greenhouse")))

    drafted = forms.draft(form, KNOWN, {}, "Asha Rao - Resume.pdf", "LinkedIn", TODAY)

    assert drafted.letter is not None and drafted.letter.question == "Cover Letter"
    other = next(a for a in drafted.answers if a.question == "Add attachment")
    assert other.asks == "attachment" and other.blank


@pytest.mark.parametrize(
    ("label", "asks"),
    [
        ("Additional documents", "attachment"),
        ("Supporting files (optional)", "attachment"),
        ("Portfolio attachment", "file"),
        ("Upload a photo", "file"),
        ("", "file"),
    ],
)
def test_only_a_field_for_any_file_is_an_attachment(label, asks):
    item = forms.Field(1, "file", label, False, "")

    assert forms.asks(item) == asks


# The drafting model's answers.


async def test_the_drafting_models_answers_are_kept_only_for_questions_asked(
    monkeypatch,
):
    asked = []

    async def ask(instructions, data, think="high"):
        asked.append(json.loads(data))
        return json.dumps(
            {
                "answers": [
                    {"id": 3, "answer": "No", "from": "profile", "basis": "city"},
                    {"id": 99, "answer": "Yes", "from": "posting"},
                ],
                "letter": "Dear Hiring Team, ...",
            }
        )

    monkeypatch.setattr(writing, "ask", ask)
    answers, letter_text = await writing.answer_form(
        KNOWN, RESUME_TEXT, {"company": "GitLab"}, "Posting", [{"id": 3}], None
    )

    assert answers == {3: {"answer": "No", "from": "profile", "basis": "city"}}
    # No letter was asked for, so none is taken.
    assert letter_text == ""
    assert asked[0]["letter"] == {"wanted": False}


async def test_a_proofread_keeps_only_problems_in_the_words_given(monkeypatch):
    async def ask(instructions, data, think="high"):
        return json.dumps(
            {
                "problems": [
                    {
                        "id": 1,
                        "words": "recieved",
                        "problem": "Spelling.",
                        "kind": "spelling",
                    },
                    {
                        "id": 1,
                        "words": "not there",
                        "problem": "Made up.",
                        "kind": "claim",
                    },
                    {
                        "id": 2,
                        "words": "ten years",
                        "problem": "The résumé says four.",
                        "kind": "claim",
                    },
                ]
            }
        )

    monkeypatch.setattr(writing, "ask", ask)
    problems = await writing.proofread(
        KNOWN,
        RESUME_TEXT,
        {"company": "GitLab"},
        [
            {"id": 1, "question": "Why us?", "answer": "I recieved an award."},
            {
                "id": 2,
                "question": "Experience",
                "answer": "I have ten years of Python.",
            },
        ],
    )

    assert [p["words"] for p in problems] == ["recieved", "ten years"]


async def test_a_rewrite_gives_back_only_what_changed(monkeypatch):
    async def ask(instructions, data, think="high"):
        return json.dumps(
            {"written": [{"id": 1, "answer": "Shorter."}, {"id": 2, "answer": "Same."}]}
        )

    monkeypatch.setattr(writing, "ask", ask)
    changed = await writing.rewrite(
        KNOWN,
        RESUME_TEXT,
        {"company": "GitLab"},
        "Posting",
        "make it shorter",
        [{"id": 1, "answer": "A long answer."}, {"id": 2, "answer": "Same."}],
    )

    assert changed == {1: "Shorter."}


# Finding the form on a page.

NOTHING = {"url": "https://example.com/apply", "fields": [], "submit": None}
ONE_FORM = {
    "url": "https://example.com/apply",
    "fields": [
        {"kind": "text", "label": "Name", "selector": "#name", "required": True},
        {"kind": "email", "label": "Email", "selector": "#email", "required": True},
    ],
    "submit": {"selector": "#submit", "said": "Submit application"},
}


class PageBrowser:
    """The browser in use, as Orbit looks for a form in it: its tabs, what the form
    script reads in each, one read after another (the last one stays), and the
    buttons that bring a form up."""

    def __init__(self, *tabs: list[dict]) -> None:
        self.reads = [list(reads) for reads in tabs]
        self.front = 0
        self.done: list[tuple] = []
        self.buttons: dict[str, list[dict]] = {}

    async def read_page(self, context):
        return "- Page URL: https://example.com/apply\n### Text\nA page"

    async def open_page(self, context, url):
        self.done.append(("open", url))
        return await self.read_page(context)

    async def run_script(self, context, function):
        if function == scripts.FORM:
            reads = self.reads[self.front]
            return copy.deepcopy(reads.pop(0) if len(reads) > 1 else reads[0])
        if function == applying.WAYS_TO_APPLY:
            return []
        if function.startswith("() => ((words)"):
            words = json.loads(function[function.rindex("})(") + 3 : -1])
            return '[data-orbit-press="1"]' if words in self.buttons else ""
        raise AssertionError(f"an unknown script: {function[:60]}")

    async def click(self, context, target, what):
        self.done.append(("click", what))
        self.reads[self.front] = list(self.buttons[what])
        return ""

    async def tabs(self, context):
        return [
            (n, n == self.front, f"https://example.com/{n}")
            for n in range(len(self.reads))
        ]

    async def to_tab(self, context, index):
        self.done.append(("tab", index))
        self.front = index

    async def screenshot(self, context):
        self.done.append(("screenshot",))
        return b"a page"


@pytest.fixture
def looks(monkeypatch) -> list:
    """What the vision model says of each screenshot, in turn."""
    said: list = []

    async def answered(screens, prompt, schema):
        assert screens == [b"a page"] and schema is applying.LOOKED
        answer = said.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(screen, "answered", answered)
    return said


def _seen(
    form=False, loading=False, button="", asks="", shows="a job's description."
) -> dict:
    return {
        "form": form,
        "loading": loading,
        "button": button,
        "asks": asks,
        "shows": shows,
    }


async def _opened(browser: PageBrowser) -> applying.Opened:
    return await applying.Applier(browser).open_application(
        None, "https://example.com/apply", None
    )


async def test_a_form_that_shows_a_moment_after_its_page_is_waited_for(monkeypatch):
    monkeypatch.setattr(applying, "FORM_SECONDS", 1.0)
    monkeypatch.setattr(applying, "FORM_EVERY_SECONDS", 0.01)
    browser = PageBrowser([NOTHING, NOTHING, ONE_FORM])

    opened = await _opened(browser)

    assert opened.form is not None and len(opened.form["fields"]) == 2
    # It came in time: there was no need to look.
    assert ("screenshot",) not in browser.done


class CutshortPage(PageBrowser):
    """A Cutshort posting: its own Apply button brings its form up, and its others
    are for jobs like it."""

    async def run_script(self, context, function):
        if function == applying.WAYS_TO_APPLY:
            return [
                {"said": "Apply now", "href": "", "selector": "#like-it-1"},
                {"said": "Apply to this job", "href": "", "selector": "#this-job"},
                {"said": "Apply now", "href": "", "selector": "#like-it-2"},
            ]
        return await super().run_script(context, function)

    async def tabs(self, context):
        return [(0, True, "https://cutshort.io/job/AI-Engineer-Acme-AbCd1234")]


@pytest.mark.parametrize(
    ("found_on", "on_board"),
    [
        # Found on Cutshort, where the user said to apply.
        ("cutshort", True),
        # Found on LinkedIn, whose Apply led to Cutshort.
        ("linkedin", False),
    ],
)
async def test_on_cutshort_only_the_postings_own_apply_button_is_pressed(
    monkeypatch, found_on, on_board
):
    monkeypatch.setattr(applying, "FORM_SECONDS", 0)
    monkeypatch.setattr(applying, "SETTLING_SECONDS", 0)
    browser = CutshortPage([NOTHING])
    browser.buttons["Apply to this job"] = [ONE_FORM]

    opened = await applying.Applier(browser).open_application(
        None,
        "https://cutshort.io/job/AI-Engineer-Acme-AbCd1234",
        search.SITES[found_on],
        on_board,
    )

    assert opened.form is not None
    assert ("click", "Apply to this job") in browser.done
    assert ("click", "Apply now") not in browser.done


async def test_a_cutshort_job_is_applied_to_there_only_when_the_user_said_so(
    monkeypatch,
):
    monkeypatch.setattr(applying, "FORM_SECONDS", 0)
    browser = CutshortPage([NOTHING])

    opened = await applying.Applier(browser).open_application(
        None,
        "https://cutshort.io/job/AI-Engineer-Acme-AbCd1234",
        search.SITES["cutshort"],
    )

    assert opened.board_only == "Cutshort"
    assert not any(step[0] == "click" for step in browser.done)


class NaukriPage(PageBrowser):
    """A Naukri posting, whose own Apply sends the application at once."""

    async def run_script(self, context, function):
        if function == applying.WAYS_TO_APPLY:
            return [{"said": "Apply", "href": "", "selector": "#apply"}]
        return await super().run_script(context, function)

    async def tabs(self, context):
        return [(0, True, NAUKRI_POSTING)]


@pytest.mark.parametrize("on_board", [True, False])
async def test_naukris_apply_is_never_pressed_to_open_an_application(
    monkeypatch, on_board
):
    monkeypatch.setattr(applying, "FORM_SECONDS", 0)
    browser = NaukriPage([NOTHING])

    opened = await applying.Applier(browser).open_application(
        None, NAUKRI_POSTING, search.SITES["naukri"], on_board
    )

    assert not any(step[0] == "click" for step in browser.done)
    if on_board:
        assert opened.press == {"said": "Apply", "href": "", "selector": "#apply"}
    else:
        assert opened.board_only == "Naukri"


async def test_a_look_at_the_page_never_presses_naukris_apply(looks):
    browser = NaukriPage([NOTHING])
    # Pressed, it would send the application.
    browser.buttons = {"Apply": [NOTHING]}
    looks += [_seen(button="Apply"), _seen(button="Apply")]

    opened = await applying.Applier(browser).open_here(None)

    assert not any(step[0] == "click" for step in browser.done)
    assert opened.form is None and opened.needs_you


async def test_an_application_open_in_another_tab_is_read_there():
    browser = PageBrowser([NOTHING], [ONE_FORM])

    opened = await applying.Applier(browser).open_here(None)

    assert opened.form is not None
    assert ("tab", 1) in browser.done
    # Nothing was opened again.
    assert not any(step[0] == "open" for step in browser.done)


async def test_a_page_seen_loading_is_waited_for(looks):
    looks.append(_seen(loading=True, shows="a page still loading."))
    browser = PageBrowser([NOTHING, ONE_FORM])

    opened = await _opened(browser)

    assert opened.form is not None and ("screenshot",) in browser.done


async def test_a_button_the_page_shows_for_applying_is_pressed(looks):
    looks.append(_seen(button="Start your application"))
    browser = PageBrowser([NOTHING])
    browser.buttons["Start your application"] = [ONE_FORM]

    opened = await _opened(browser)

    assert opened.form is not None
    assert ("click", "Start your application") in browser.done


async def test_a_button_that_sends_an_application_is_never_pressed(looks):
    looks.append(_seen(button="Submit application", shows="a form left empty."))
    browser = PageBrowser([NOTHING])
    browser.buttons["Submit application"] = [ONE_FORM]

    opened = await _opened(browser)

    assert not any(step[0] == "click" for step in browser.done)
    assert opened.needs_you == applying.SEEN.format(shows="a form left empty.")


@pytest.mark.parametrize(
    ("asks", "check"), [("sign in", search.SIGNING_IN), ("person", search.A_PERSON)]
)
async def test_a_sign_in_or_a_check_seen_on_the_page_is_handed_over(looks, asks, check):
    looks.append(_seen(asks=asks))

    opened = await _opened(PageBrowser([NOTHING]))

    assert opened.check == check and opened.needs_you


async def test_what_the_page_shows_is_told_when_it_holds_no_form(looks):
    looks.append(_seen(shows="a job's description, with no way to apply."))

    opened = await _opened(PageBrowser([NOTHING]))

    assert opened.form is None
    assert opened.needs_you == (
        "No application form Orbit can fill in was found. The page shows a job's "
        "description, with no way to apply."
    )


async def test_a_form_seen_but_not_read_is_said_so(looks):
    looks.append(_seen(form=True, shows="a form in a box Orbit cannot reach."))

    opened = await _opened(PageBrowser([NOTHING]))

    assert opened.needs_you == applying.UNREAD.format(
        shows="a form in a box Orbit cannot reach."
    )


async def test_a_page_that_cannot_be_looked_at_has_no_form_said(looks):
    looks.append(RuntimeError("the vision model is down"))

    opened = await _opened(PageBrowser([NOTHING]))

    assert opened.needs_you == applying.NO_FORM


def test_a_frame_that_holds_no_application_is_passed_over():
    frames = [
        "https://www.youtube.com/embed/abc",
        "https://www.google.com/recaptcha/api2/anchor?k=1",
        "https://boards.greenhouse.io/embed/job_app?for=acme",
    ]

    assert applying.application_frame(frames) == frames[2]
    assert applying.application_frame(frames[:2]) is None


# Applying, end to end.


POSTING = "https://www.linkedin.com/jobs/view/4100000001/"


class FormBrowser:
    """The browser in use while applying: a posting with a way to apply, and a real
    application form as the form script reads it, which filling in changes as a page
    would. A form over several steps goes on with its Next button. Submitting shows
    a thank-you. On a job board, its own Apply button opens the form in a dialog."""

    def __init__(self, found: dict, *more: dict, board: bool = False) -> None:
        self.pages = [copy.deepcopy(page) for page in (found, *more)]
        self.at = 0
        self.board = board
        self.opened_on_board = False
        # A drop-down shows its options only once it is opened.
        self.lists = {
            f["selector"]: list(f.get("options") or [])
            for page in self.pages
            for f in page["fields"]
            if f["kind"] == "combobox"
        }
        for page in self.pages:
            for item in page["fields"]:
                if item["kind"] == "combobox":
                    item["options"] = []
        self.url = ""
        self.in_use = True
        self.shown = True
        self.held: list[str | None] = []
        self.done: list[tuple] = []
        self.open_list = ""
        self.picking = ""
        self.sent = False
        # A check that the user is a person on the form, not yet done; one that comes
        # up when Submit is pressed; and how many more reads of a page show a check
        # page instead.
        self.challenge = False
        self.check_on_submit = False
        self.checking = 0
        self.holding: tuple[str | None, bool] = (None, False)

    def hold(self, what, showing=False):
        self.held.append(what)
        self.holding = (what, showing)

    def release(self):
        self.hold(None)

    @property
    def form(self) -> dict:
        return self.pages[self.at]

    def _field(self, selector: str) -> dict | None:
        return next((f for f in self.form["fields"] if f["selector"] == selector), None)

    async def open_page(self, context, url):
        self.done.append(("open", url))
        self.url = url
        return await self.read_page(context)

    async def read_page(self, context):
        if self.checking:
            self.checking -= 1
            return f"- Page URL: {self.url}\n### Text\n# Verify you are human"
        if self.sent:
            return (
                f"- Page URL: {self.url}\n### Text\nThank you for applying to GitLab."
            )
        return f"- Page URL: {self.url}\n### Text\nA page"

    async def run_script(self, context, function):
        if function == scripts.FORM:
            if self.checking:
                return {"url": self.url, "fields": [], "challenge": True}
            if (self.url == POSTING and not self.opened_on_board) or self.sent:
                return {"url": self.url, "fields": [], "submit": None, "errors": []}
            return {
                **copy.deepcopy(self.form),
                "url": self.url,
                "challenge": self.challenge,
            }
        if function == applying.WAYS_TO_APPLY:
            if self.board:
                return [
                    {"said": "Easy Apply", "href": "", "selector": "[data-orbit-apply]"}
                ]
            return [
                {
                    "said": "Apply",
                    "href": self.form["url"],
                    "selector": "[data-orbit-apply]",
                }
            ]
        if function == scripts.OPTIONS:
            return list(self.lists.get(self.open_list, []))
        if function.startswith("() => ((wanted)"):
            wanted = json.loads(function[function.rindex("})(") + 3 : -1])
            self.picking = (
                wanted if wanted in self.lists.get(self.open_list, []) else ""
            )
            return bool(self.picking)
        raise AssertionError(f"an unknown script: {function[:60]}")

    async def tabs(self, context):
        return [(0, True, self.url)]

    async def to_tab(self, context, index):
        raise AssertionError("no tab opens")

    async def click(self, context, target, what):
        self.done.append(("click", target))
        if target == scripts.PICKED:
            self._field(self.open_list)["value"] = self.picking
            self.open_list = ""
        elif (item := self._field(target)) is not None and item["kind"] == "combobox":
            self.open_list = target
        elif target == "[data-orbit-apply]" and self.board:
            self.opened_on_board = True
        elif self.form.get("next") and target == self.form["next"]["selector"]:
            self.at += 1
        elif self.form.get("submit") and target == self.form["submit"]["selector"]:
            if self.check_on_submit:
                self.challenge = True
            else:
                self.sent = True
        return ""

    async def press(self, context, key):
        if key == "Escape":
            self.open_list = ""

    async def type_into(self, context, target, text, what):
        self.done.append(("type", target, text))

    async def fill_in(self, context, fields):
        self.done.append(("fill", tuple(f["name"] for f in fields)))
        for given in fields:
            item = self._field(given["target"])
            if given["type"] == "checkbox":
                item["value"] = given["value"] == "true"
            else:
                item["value"] = given["value"]

    async def put_file(self, context, chooser, path, what):
        self.done.append(("file", Path(path).name))
        item = next(f for f in self.form["fields"] if f.get("chooser") == chooser)
        item["value"] = Path(path).name

    def did(self, kind: str) -> list[tuple]:
        return [step for step in self.done if step[0] == kind]


# What the drafting model answers on the Greenhouse form, by the start of a question:
# the answer, where it comes from, and the words that say it.
MODEL_ANSWERS = {
    "Please choose the country": ("India", "profile", "Bengaluru"),
    "Are you currently based": ("No", "profile", "Bengaluru"),
    "Will you now or in the future": ("No", "profile", "Indian citizen"),
    "Have you previously worked": ("No", "résumé", "Backend Engineer, Acme Labs"),
    "What is your current country": ("India", "profile", "Bengaluru"),
    "Why do you want": (
        "I have run data pipelines on GitLab CI for years.",
        "written",
        "",
    ),
}
SHORTER = (
    "Dear Hiring Team,\n\nI build data systems in Python at Acme Labs.\n\nAsha Rao"
)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """A fake page needs no time to settle, nor to build its form."""
    monkeypatch.setattr(applying, "OPENING_SECONDS", 0)
    monkeypatch.setattr(applying, "SETTLING_SECONDS", 0)
    monkeypatch.setattr(applying, "FORM_SECONDS", 0)


@pytest.fixture
def drafting(monkeypatch) -> dict[str, list]:
    """The drafting model, standing in: it answers what MODEL_ANSWERS says and does
    not know the rest, writes the letter, finds its sign-off a little informal, and
    shortens it when asked."""
    asked: dict[str, list] = {"answers": [], "proofread": [], "rewrite": []}

    async def ask(instructions, data, think="high"):
        given = json.loads(data)
        if instructions == writing._ANSWER:
            asked["answers"].append(given)
            answers = []
            for question in given["questions"]:
                known = next(
                    (
                        v
                        for k, v in MODEL_ANSWERS.items()
                        if question["question"].startswith(k)
                    ),
                    ("", "unknown", ""),
                )
                answer, source, basis = known
                answers.append(
                    {
                        "id": question["id"],
                        "answer": answer,
                        "from": source,
                        "basis": basis,
                    }
                )
            letter_text = LETTER if given["letter"].get("wanted") else ""
            return json.dumps({"answers": answers, "letter": letter_text})
        if instructions == writing._PROOFREAD:
            asked["proofread"].append(given)
            problems = [
                {
                    "id": item["id"],
                    "words": "Thank you for reading.",
                    "problem": "A little informal.",
                    "kind": "tone",
                }
                for item in given["written"]
                if "Thank you for reading." in item["answer"]
            ]
            return json.dumps({"problems": problems})
        asked["rewrite"].append(given)
        return json.dumps({"written": [{"id": 0, "answer": SHORTER}]})

    monkeypatch.setattr(writing, "ask", ask)
    return asked


@pytest.fixture
def google(monkeypatch) -> FakeGoogle:
    fake = FakeGoogle()
    for name in ("create", "spreadsheet", "table", "update", "write"):
        monkeypatch.setattr(gsheets, name, getattr(fake, name))
    monkeypatch.setattr(job_tools, "_http", lambda: None)
    monkeypatch.setattr(applications, "_http", lambda: None)
    monkeypatch.setattr(action_tools, "_require_write", lambda service: None)
    return fake


def _talking(*said: str) -> SimpleNamespace:
    moment = [1000.0]
    messages = [
        SimpleNamespace(type="message", role="user", text_content=t) for t in said
    ]
    told: list[str] = []

    async def generate_reply(instructions=""):
        told.append(instructions)

    return SimpleNamespace(
        session=SimpleNamespace(
            history=SimpleNamespace(items=messages), generate_reply=generate_reply
        ),
        userdata=OrbitState(gate=ActionGate(clock=lambda: moment[0])),
        disallow_interruptions=lambda: None,
        told=told,
        moment=moment,
    )


def _yes(talk: SimpleNamespace) -> str:
    """The user heard the draft waiting, then said yes. Returns its action_id."""
    gate = talk.userdata.gate
    action = gate.awaiting()
    talk.moment[0] += 1
    gate.mark_reviewed(action.id)
    talk.moment[0] += 1
    gate.note_user_input()
    return action.id


async def _applying(google) -> tuple[job_tools.JobsToolset, FormBrowser, store.Job]:
    """A ticked GitLab job in Orbit jobs, the user's profile and résumé, and the
    browser on their Chrome profile."""
    store.keep([store.Known(key, value, "you", []) for key, value in KNOWN.items()])
    profile.keep_resume(
        "Asha Rao - Resume.pdf", letter.pdf(RESUME_TEXT, "Résumé"), "from your computer"
    )
    await sheet.make_book(None)
    store.add_job(
        key="linkedin:4100000002", site="LinkedIn", url=POSTING + "2", company="Initech",
        title="Backend Engineer", location="Remote", fit="high", why="Python", search=1,
        apply=True,
    )  # fmt: skip
    job = store.add_job(
        key="linkedin:4100000001", site="LinkedIn", url=POSTING, company="GitLab",
        title="Director, Analytics Engineering", location="Remote", fit="high",
        why="Analytics", search=1, apply=True, posting="GitLab is hiring a director.",
    )  # fmt: skip
    await sheet.sync(None)
    browser = FormBrowser(_form("greenhouse"))
    return job_tools.JobsToolset(browser), browser, job


async def _drafted_for(tools, talk, job: int = 0, on_the_board: bool = False) -> dict:
    told = await tools._application_draft(talk, job=job, on_the_board=on_the_board)
    await tools._applications._working
    return told


async def test_an_application_is_drafted_from_the_job_and_put_on_screen(
    google, drafting
):
    tools, browser, job = await _applying(google)
    talk = _talking("Looks good, start applying with the GitLab one")

    told = await _drafted_for(tools, talk, job.number)

    assert "drafting it" in told["say"]
    # The posting's way to apply was followed to the company's own form.
    assert ("open", POSTING) in browser.done
    assert ("open", _form("greenhouse")["url"]) in browser.done
    # Each drop-down was opened to read its options, and the model was given them.
    questions = drafting["answers"][0]["questions"]
    based = next(q for q in questions if q["question"].startswith("Are you currently"))
    assert based["options"] == ["Yes", "No"]
    action = talk.userdata.gate.awaiting()
    assert (
        action.kind == applications.FILL
        and action.expiry == applications.EXPIRY_SECONDS
    )
    assert f"action_id {action.id}" in talk.told[-1]
    assert "Are you subject to any employment agreements" in talk.told[-1]
    preview = action.preview
    assert preview["résumé"] == "Asha Rao - Resume.pdf"
    assert preview["cover letter"] == LETTER
    assert "employment agreements" in preview["needs you"]
    assert "Will you now or in the future require sponsorship" in preview["to confirm"]
    assert "Cover letter: A little informal." in preview["checks"]
    rows = {q["question"]: [q["answer"], q["from"]] for q in preview["questions"]}
    assert rows["First Name"] == ["Asha", forms.FROM_PROFILE]
    assert rows["Country"] == ["India +91", forms.FROM_PROFILE]
    assert rows["Gender"] == ["Left blank", ""]
    assert store.job(job.number).status == store.DRAFTING
    assert store.job(job.number).apply_url == _form("greenhouse")["url"]
    # Nothing was typed into the form while drafting it.
    assert not browser.did("fill") and not browser.did("file")
    assert browser.held[0].startswith("drafting") and browser.held[-1] is None


async def test_applying_waits_for_a_browser_a_resume_and_orbit_jobs(google, drafting):
    tools, browser, _ = await _applying(google)
    talk = _talking("Apply to the next one")
    browser.in_use = False
    with pytest.raises(ToolError, match="Chrome profiles"):
        await tools._application_draft(talk)
    browser.in_use = True
    store.lose_book()
    with pytest.raises(ToolError, match="Orbit jobs has not been made"):
        await tools._application_draft(talk)


ANALYTICS = "Asha Rao - Analytics.pdf"


def _keep_analytics_resume() -> None:
    """A second résumé of Asha's, for analytics engineering roles."""
    profile.keep_resume(
        ANALYTICS,
        letter.pdf(RESUME_TEXT + "\nLooker and dbt dashboards for finance.", "Résumé"),
        "from your computer",
        role="Analytics Engineering",
    )


async def test_each_application_sends_the_resume_that_suits_its_job(google, drafting):
    tools, browser, job = await _applying(google)
    _keep_analytics_resume()
    talk = _talking("Apply to the GitLab one", "Yes, fill it in")

    await _drafted_for(tools, talk, job.number)
    draft = talk.userdata.gate.awaiting()
    await tools._application_change(talk, answers=["employment agreements: No"])
    await action_tools.confirm_action(talk, _yes(talk))

    assert draft.preview["résumé"] == (
        f"{ANALYTICS}: it is your résumé for Analytics Engineering roles"
    )
    assert store.applications()[0].resume == ANALYTICS
    # Its words are what the answers were drafted from, and it is the file sent.
    assert "Looker and dbt" in drafting["answers"][0]["resume"]
    page = {forms.question_of(f["label"]): f["value"] for f in browser.form["fields"]}
    assert page["Resume/CV"] == ANALYTICS


async def test_another_resume_is_sent_when_the_user_says_so(google, drafting):
    tools, _, job = await _applying(google)
    _keep_analytics_resume()
    talk = _talking("Apply to the GitLab one", "Send my usual résumé instead")
    await _drafted_for(tools, talk, job.number)

    told = await tools._application_change(talk, resume="Asha Rao - Resume.pdf")
    await tools._applications._working

    assert told["resume"] == "Asha Rao - Resume.pdf"
    application = store.applications()[0]
    assert application.resume == "Asha Rao - Resume.pdf"
    sent = next(a for a in application.answers if a["asks"] == "resume")
    assert sent["value"] == "Asha Rao - Resume.pdf"
    # What was written is drafted again from that résumé.
    (rewrite,) = drafting["rewrite"]
    assert rewrite["asked"].startswith("The résumé sent is now Asha Rao - Resume.pdf")
    assert "Looker" not in rewrite["resume"]
    preview = talk.userdata.gate.awaiting().preview
    assert preview["résumé"] == "Asha Rao - Resume.pdf, as you chose"


async def test_the_letter_goes_as_a_pdf_where_a_form_takes_any_file(google, drafting):
    tools, browser, job = await _applying(google)
    browser.pages[0] = _with_attachment(_form("ashby"))
    talk = _talking("Apply to the GitLab one")

    await _drafted_for(tools, talk, job.number)

    application = store.applications()[0]
    held = next(a for a in application.answers if a["question"] == "Add attachment")
    assert held["value"] == "Asha Rao - Cover letter.pdf"
    files = tools._applications._files(
        application, job, forms.read(application.form), application.letter
    )
    assert files["Asha Rao - Cover letter.pdf"].read_bytes().startswith(b"%PDF")
    assert talk.userdata.gate.awaiting().preview["cover letter"] == application.letter


async def test_an_application_already_open_is_drafted_where_it_is(google, drafting):
    tools, browser, job = await _applying(google)
    talk = _talking("The form is already open in the browser")

    await tools._application_draft(talk, job=job.number, already_open=True)
    await tools._applications._working

    # The job was not opened again: its form was read as the browser had it.
    assert not browser.did("open")
    assert talk.userdata.gate.awaiting().kind == applications.FILL


async def test_the_next_job_is_the_first_ticked_in_the_sheets_order(google, drafting):
    tools, _, job = await _applying(google)
    google.set("Jobs", 1, "Apply", False)  # Initech, unticked in the Sheet

    told = await _drafted_for(tools, _talking("Start applying"))

    assert told["job"] == job.number
    assert store.job(1).apply is False and store.job(1).status == store.FOUND


async def test_a_job_only_on_the_board_is_applied_to_there_unasked(google, drafting):
    tools, browser, job = await _applying(google)
    store.change_job(job.number, applies_on="LinkedIn")
    browser.board = True
    talk = _talking("Apply to job 2")

    await _drafted_for(tools, talk, job.number)

    # The user allows jobs that apply only on the board: its Easy Apply was taken,
    # and its form drafted for their yes.
    assert ("click", "[data-orbit-apply]") in browser.done
    assert talk.userdata.gate.awaiting().kind == applications.FILL


async def test_a_job_only_on_the_board_joins_the_ticked_jobs_applied_to(
    google, drafting
):
    tools, browser, job = await _applying(google)
    for number in (1, job.number):
        store.change_job(number, applies_on="LinkedIn")
    browser.board = True

    told = await _drafted_for(tools, _talking("Start applying"))

    assert told["job"] == 1


async def test_a_job_only_on_the_board_is_asked_about_when_such_jobs_are_left_out(
    google, drafting
):
    settings.change("board only: leave out")
    tools, browser, job = await _applying(google)
    store.change_job(job.number, applies_on="LinkedIn")
    browser.board = True
    talk = _talking("Apply to job 2", "Yes, apply on LinkedIn")

    with pytest.raises(ToolError, match="only on LinkedIn itself"):
        await tools._application_draft(talk, job=job.number)
    await _drafted_for(tools, talk, job.number, on_the_board=True)

    # The board's own way to apply was taken, and its form drafted.
    assert ("click", "[data-orbit-apply]") in browser.done
    assert talk.userdata.gate.awaiting().kind == applications.FILL


async def test_a_job_found_to_apply_only_on_the_board_goes_on_there(google, drafting):
    tools, browser, job = await _applying(google)
    browser.board = True
    talk = _talking("Apply to the GitLab one")

    await _drafted_for(tools, talk, job.number)

    # Its company's own page has no such job: it is applied to on LinkedIn, still
    # ticked.
    saved = store.job(job.number)
    assert (saved.applies_on, saved.apply) == ("LinkedIn", True)
    assert ("click", "[data-orbit-apply]") in browser.done
    assert talk.userdata.gate.awaiting().kind == applications.FILL


async def test_a_job_found_to_apply_only_on_the_board_is_unticked_when_left_out(
    google, drafting
):
    settings.change("board only: leave out")
    tools, browser, job = await _applying(google)
    browser.board = True
    talk = _talking("Apply to the GitLab one")

    await _drafted_for(tools, talk, job.number)

    assert "only on LinkedIn" in talk.told[-1]
    saved = store.job(job.number)
    assert (saved.applies_on, saved.apply) == ("LinkedIn", False)
    assert google.cell("Jobs", job.number, "Apply") is False
    assert talk.userdata.gate.awaiting() is None


NAUKRI_POSTING = (
    "https://www.naukri.com/job-listings-backend-engineer-initech-bengaluru-"
    "2-to-5-years-221026000001"
)


class NaukriBrowser(FormBrowser):
    """A Naukri posting whose own Apply sends the user's Naukri profile at once:
    there is no form, and once it is pressed the page says it was sent, or asks the
    recruiter's questions."""

    def __init__(self, asks_questions: bool = False) -> None:
        super().__init__(_form("greenhouse"))
        self.asks_questions = asks_questions
        self.applied = False

    async def read_page(self, context):
        if not self.applied:
            said = "Apply\nSave"
        elif self.asks_questions:
            said = "Kindly answer all the recruiter's questions to successfully apply."
        else:
            said = "Applied\nYou have successfully applied to Backend Engineer"
        return f"- Page URL: {self.url}\n### Text\n# Backend Engineer\n{said}"

    async def run_script(self, context, function):
        if function == scripts.FORM:
            return {"url": self.url, "fields": [], "submit": None, "errors": []}
        if function == applying.WAYS_TO_APPLY:
            if self.applied:
                return []
            return [{"said": "Apply", "href": "", "selector": "[data-orbit-apply]"}]
        return await super().run_script(context, function)

    async def click(self, context, target, what):
        self.done.append(("click", target))
        if target == "[data-orbit-apply]":
            self.applied = True
        return ""


async def _on_naukri(google, browser: NaukriBrowser) -> tuple:
    """A ticked job that applies only on Naukri, and the user's profile and résumé."""
    await _applying(google)
    job = store.add_job(
        key="naukri:221026000001", site="Naukri", url=NAUKRI_POSTING,
        applies_on="Naukri", company="Initech", title="Backend Engineer",
        location="Bengaluru", fit="high", why="Python", search=1, apply=True,
    )  # fmt: skip
    await sheet.sync(None)
    return job_tools.JobsToolset(browser), job


async def test_naukris_own_apply_is_pressed_only_on_the_yes_to_send(google, drafting):
    browser = NaukriBrowser()
    tools, job = await _on_naukri(google, browser)
    talk = _talking("Apply to the Naukri one", "Yes, apply")

    await _drafted_for(tools, talk, job.number)

    # Nothing to fill in: one yes, to send it, and nothing pressed before it.
    awaiting = talk.userdata.gate.awaiting()
    assert awaiting.kind == applications.SUBMIT
    assert awaiting.preview["what"] == "Apply on Naukri"
    assert "ready on Naukri" in talk.told[-1]
    assert not browser.applied

    done = await action_tools.confirm_action(talk, _yes(talk))

    assert browser.applied
    assert "You have successfully applied" in done["submitted"]
    saved = store.job(job.number)
    assert saved.status == store.SUBMITTED and saved.applied_at
    assert google.cell("Jobs", job.number, "Status") == store.SUBMITTED


async def test_a_dry_run_never_presses_naukris_apply(google, drafting, monkeypatch):
    browser = NaukriBrowser()
    tools, job = await _on_naukri(google, browser)
    talk = _talking("Apply to the Naukri one", "Yes, apply")
    await _drafted_for(tools, talk, job.number)
    monkeypatch.setenv("ORBIT_DRY_RUN", "1")

    done = await action_tools.confirm_action(talk, _yes(talk))

    assert done["dry_run"] == applications.DRY_RUN
    assert not browser.applied
    assert store.job(job.number).status != store.SUBMITTED


async def test_naukris_questions_after_apply_are_the_users_to_answer(google, drafting):
    browser = NaukriBrowser(asks_questions=True)
    tools, job = await _on_naukri(google, browser)
    talk = _talking("Apply to the Naukri one", "Yes, apply")
    await _drafted_for(tools, talk, job.number)

    done = await action_tools.confirm_action(talk, _yes(talk))

    assert browser.applied
    assert "recruiter's questions" in done["next"]
    assert store.job(job.number).status == store.NEEDS_YOU


async def test_naukri_answered_by_the_user_is_recorded_once_it_says_applied(
    google, drafting
):
    browser = NaukriBrowser(asks_questions=True)
    tools, job = await _on_naukri(google, browser)
    talk = _talking("Apply to the Naukri one", "Yes, apply", "I answered them")
    await _drafted_for(tools, talk, job.number)
    await action_tools.confirm_action(talk, _yes(talk))
    # The user answers the recruiter's questions, and Naukri shows it applied.
    browser.asks_questions = False

    await tools._application_check(talk)

    saved = store.job(job.number)
    assert saved.status == store.SUBMITTED and saved.applied_at


async def test_a_draft_that_needs_the_user_is_not_filled_in(google, drafting):
    tools, browser, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "Yes, fill it in")
    await _drafted_for(tools, talk, job.number)

    with pytest.raises(ToolError, match="still has problems: Are you subject"):
        await action_tools.confirm_action(talk, _yes(talk))
    assert not browser.did("fill")


async def test_the_users_answers_change_the_draft_and_can_be_kept(google, drafting):
    tools, _, job = await _applying(google)
    talk = _talking("Apply to the GitLab one")
    await _drafted_for(tools, talk, job.number)

    told = await tools._application_change(
        talk,
        answers=["employment agreements: no", "Gender: decline to self-identify"],
        keep=["Gender"],
    )

    action = talk.userdata.gate.awaiting()
    assert told["action_id"] == action.id and "needs you" not in action.preview
    rows = {
        q["question"]: [q["answer"], q["from"]] for q in action.preview["questions"]
    }
    assert rows["Gender"] == ["Decline To Self Identify", forms.FROM_YOU]
    assert told["kept"] == "Kept for next time: Gender."
    assert store.answers() == {"gender": "Decline To Self Identify"}
    with pytest.raises(ToolError, match="has no option 'Perhaps'"):
        await tools._application_change(
            talk, answers=["employment agreements: Perhaps"]
        )
    with pytest.raises(ToolError, match="not a question on the form"):
        await tools._application_change(talk, answers=["Favourite colour: blue"])


async def test_a_rewrite_runs_in_the_background_and_redrafts(google, drafting):
    tools, _, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "Make the cover letter shorter")
    await _drafted_for(tools, talk, job.number)

    told = await tools._application_change(
        talk, rewrite="Make the cover letter shorter"
    )
    await tools._applications._working

    assert "making that change" in told["say"]
    assert drafting["rewrite"][0]["asked"] == "Make the cover letter shorter"
    action = talk.userdata.gate.awaiting()
    assert action.preview["cover letter"] == SHORTER
    assert f"action_id {action.id}" in talk.told[-1]
    # What the proofread found in the letter it replaced is gone with it.
    assert "informal" not in action.preview.get("checks", "")


async def _filled(tools, browser, job, talk) -> dict:
    await _drafted_for(tools, talk, job.number)
    await tools._application_change(talk, answers=["employment agreements: No"])
    return await action_tools.confirm_action(talk, _yes(talk))


async def test_the_first_yes_fills_the_form_in_and_submits_nothing(google, drafting):
    tools, browser, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "Yes, fill it in")

    done = await _filled(tools, browser, job, talk)

    assert done["filled"] == applications.FILLED
    assert done["read_back"] == applications.READ_BACK_SAME
    page = {forms.question_of(f["label"]): f["value"] for f in browser.form["fields"]}
    assert page["First Name"] == "Asha" and page["Last Name"] == "Rao"
    assert page["Country"] == "India +91"
    assert page["Are you currently based in the United States?"] == "No"
    assert page["Resume/CV"] == "Asha Rao - Resume.pdf"
    assert page["Cover Letter"] == "Asha Rao - Cover letter.pdf"
    # Fields left for the user are left alone.
    assert page["Gender"] == ""
    assert not browser.sent
    submit = talk.userdata.gate.awaiting()
    assert submit.kind == applications.SUBMIT and submit.id == done["submit_action_id"]
    assert submit.preview["what"] == "Submit application"
    assert store.job(job.number).status == store.FILLED
    assert google.cell("Jobs", job.number, "Status") == store.FILLED


async def test_the_second_yes_submits_and_records_it(google, drafting):
    tools, browser, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "Yes, fill it in", "Submit it")
    await _filled(tools, browser, job, talk)

    done = await action_tools.confirm_action(talk, _yes(talk))

    assert browser.sent
    assert "Thank you for applying to GitLab" in done["submitted"]
    saved = store.job(job.number)
    assert saved.status == store.SUBMITTED and saved.applied_at
    assert saved.confirmation == "Thank you for applying to GitLab"
    assert google.cell("Jobs", job.number, "Status") == store.SUBMITTED
    [header, row] = google.tab("Applications")["rows"][:2]
    sent = dict(zip(header, row, strict=False))
    assert (sent["Job"], sent["Company"], sent["Résumé"]) == (
        job.number,
        "GitLab",
        "Asha Rao - Resume.pdf",
    )
    assert sent["Cover letter"] == LETTER
    assert "Are you currently based in the United States?: No" in sent["Answers"]
    assert "1 of 10 applications this sitting" in done["next"]


async def test_an_application_is_never_sent_twice(google, drafting):
    tools, browser, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "Yes, fill it in", "Submit it")
    await _filled(tools, browser, job, talk)
    submitted = _yes(talk)
    await action_tools.confirm_action(talk, submitted)

    with pytest.raises(ToolError, match="already done"):
        await action_tools.confirm_action(talk, submitted)
    with pytest.raises(ToolError, match="applied to already"):
        await tools._application_draft(talk, job=job.number)


async def test_a_field_changed_by_hand_stops_the_submit_until_the_user_answers(
    google, drafting
):
    tools, browser, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "Yes, fill it in", "Submit it")
    await _filled(tools, browser, job, talk)
    linkedin = next(
        f for f in browser.form["fields"] if f["label"] == "LinkedIn Profile"
    )
    linkedin["value"] = "https://linkedin.com/in/asha-rao"

    with pytest.raises(ToolError, match="not as the user approved it"):
        await action_tools.confirm_action(talk, _yes(talk))

    assert not browser.sent
    again = talk.userdata.gate.awaiting()
    rows = {q["question"]: [q["answer"], q["from"]] for q in again.preview["questions"]}
    assert rows["LinkedIn Profile"] == [
        "https://linkedin.com/in/asha-rao",
        forms.ON_THE_PAGE,
    ]
    # The user agrees to the page as it is: it is submitted.
    await action_tools.confirm_action(talk, _yes(talk))
    assert browser.sent


async def test_a_dry_run_does_everything_but_submit(google, drafting, monkeypatch):
    tools, browser, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "Yes, fill it in", "Submit it")
    await _filled(tools, browser, job, talk)
    monkeypatch.setenv("ORBIT_DRY_RUN", "1")

    done = await action_tools.confirm_action(talk, _yes(talk))

    assert done["dry_run"] == applications.DRY_RUN
    assert not browser.sent
    assert store.job(job.number).status == store.FILLED
    assert len(google.tab("Applications")["rows"]) == 1


async def test_a_check_that_the_user_is_a_person_is_theirs_to_do(google, drafting):
    tools, browser, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "Yes, fill it in", "Submit it")
    await _filled(tools, browser, job, talk)
    browser.challenge = True

    with pytest.raises(ToolError, match="check that the user is a person"):
        await action_tools.confirm_action(talk, _yes(talk))
    assert not browser.sent


async def test_the_model_is_told_the_gist_and_the_card_shows_every_answer(
    google, drafting
):
    tools, _, job = await _applying(google)
    talk = _talking("Apply to the GitLab one")
    await _drafted_for(tools, talk, job.number)

    action = talk.userdata.gate.awaiting()
    told = action_tools._told(action.preview)

    assert "questions" not in told and len(action.preview["questions"]) == 19
    assert told["answers"] == (
        "13 of 19 questions answered, a cover letter and 1 needs you"
    )


async def test_after_a_change_by_hand_the_form_is_read_again_for_the_second_yes(
    google, drafting
):
    tools, browser, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "Yes, fill it in", "I changed my phone")
    await _filled(tools, browser, job, talk)
    phone = next(f for f in browser.form["fields"] if f["label"] == "Phone")
    phone["value"] = "+91 91234 56789"

    told = await tools._application_check(talk)

    action = talk.userdata.gate.awaiting()
    assert action.kind == applications.SUBMIT and action.id in told["say"]
    assert "'Phone' was '+91 98765 43210'" in told["differences"]
    rows = {q["question"]: q["answer"] for q in action.preview["questions"]}
    assert rows["Phone"] == "+91 91234 56789"


async def test_an_application_finished_in_the_browser_is_recorded(google, drafting):
    tools, browser, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "Yes, fill it in", "I submitted it")
    await _filled(tools, browser, job, talk)
    browser.sent = True  # The user did a check and submitted it themselves.

    told = await tools._application_check(talk)

    assert "submitted" in told["submitted"]
    assert store.job(job.number).status == store.SUBMITTED


async def test_a_job_the_user_does_not_want_is_skipped(google, drafting):
    tools, _, job = await _applying(google)
    talk = _talking("Skip the GitLab one, it is too senior")
    await _drafted_for(tools, talk, job.number)

    told = await tools._application_skip(talk, job.number, "too senior")

    assert told["skipped"] == applications.SKIPPED.format(number=job.number)
    saved = store.job(job.number)
    assert (saved.status, saved.apply, saved.notes) == (
        store.SKIPPED,
        False,
        "too senior",
    )
    assert google.cell("Jobs", job.number, "Status") == store.SKIPPED
    with pytest.raises(ToolError, match="no application being drafted"):
        await tools._application_change(talk, answers=["Gender: Male"])


async def test_a_fact_about_the_user_is_kept_in_their_profile(google, drafting):
    tools, _, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "My LinkedIn is asha-rao now, keep it")
    await _drafted_for(tools, talk, job.number)

    await tools._application_change(
        talk,
        answers=["LinkedIn Profile: https://linkedin.com/in/asha-rao"],
        keep=["LinkedIn Profile"],
    )

    kept = store.profile()["linkedin"]
    assert (kept.value, kept.source) == (
        "https://linkedin.com/in/asha-rao",
        profile.YOU,
    )


STEP_ONE = {
    "url": "https://jobs.example.com/apply/1",
    "title": "Apply",
    "fields": [
        {
            "kind": "text",
            "label": "First name *",
            "required": True,
            "selector": "#first",
            "value": "",
        },
        {
            "kind": "email",
            "label": "Email *",
            "required": True,
            "selector": "#email",
            "value": "",
        },
    ],
    "next": {"selector": "#next", "said": "Next"},
    "submit": None,
    "errors": [],
}
STEP_TWO = {
    "url": "https://jobs.example.com/apply/1",
    "title": "Apply",
    "fields": [
        {
            "kind": "textarea",
            "label": "Why do you want to work at GitLab?",
            "required": True,
            "selector": "#why",
            "value": "",
        },
        {
            "kind": "file",
            "label": "Resume *",
            "required": True,
            "selector": "#cv",
            "chooser": "#cv-button",
            "accept": ".pdf",
            "value": "",
        },
    ],
    "next": None,
    "submit": {"selector": "#send", "said": "Submit application"},
    "errors": [],
}


async def test_a_form_over_several_steps_is_drafted_and_filled_a_step_at_a_time(
    google, drafting
):
    tools, _, job = await _applying(google)
    browser = FormBrowser(STEP_ONE, STEP_TWO)
    tools._applications._browser = browser
    tools._applications._applier = applying.Applier(browser)
    talk = _talking("Apply to the GitLab one", "Fill it in", "Fill it in", "Submit")
    await _drafted_for(tools, talk, job.number)

    # The first yes fills step one in and goes on, which sends nothing.
    done = await action_tools.confirm_action(talk, _yes(talk))
    await tools._applications._working

    assert done["next"] == applications.ON_TO_NEXT and not browser.sent
    assert browser.at == 1
    assert "its step 2" in talk.told[-1]
    step_two = talk.userdata.gate.awaiting()
    assert step_two.kind == applications.FILL
    assert [q["question"] for q in step_two.preview["questions"]] == [
        "Why do you want to work at GitLab?",
        "Resume",
    ]
    await action_tools.confirm_action(talk, _yes(talk))
    assert talk.userdata.gate.awaiting().kind == applications.SUBMIT
    await action_tools.confirm_action(talk, _yes(talk))

    assert browser.sent
    [header, row] = google.tab("Applications")["rows"][:2]
    sent = dict(zip(header, row, strict=False))
    assert "First name: Asha" in sent["Answers"]
    assert "Why do you want to work at GitLab?: I have run data" in sent["Answers"]
    assert sent["Résumé"] == "Asha Rao - Resume.pdf"
    assert len(google.tab("Applications")["rows"]) == 2


async def test_a_new_conversation_is_told_where_applying_stands(google, drafting):
    tools, browser, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "Yes, fill it in", "Submit it")
    assert job_tools.jobs_note().startswith(
        "Applying for jobs, as it stands: 0 of the 2 jobs ticked"
    )
    await _filled(tools, browser, job, talk)
    assert (
        "Left part way: job 2, Director, Analytics Engineering at GitLab, filled."
        in (job_tools.jobs_note())
    )
    done = await action_tools.confirm_action(talk, _yes(talk))

    note = job_tools.jobs_note()
    assert (
        "1 of the 2 jobs ticked to apply to in Orbit jobs have been applied to" in note
    )
    assert "The next is job 1, Backend Engineer at Initech." in note
    # A reminder to follow up is offered once, after the first sent.
    assert "reminder_add" in done["follow_up"]


def test_nothing_is_said_of_applying_before_a_job_is_ticked():
    assert job_tools.jobs_note() == ""


async def test_a_check_on_the_way_to_the_application_is_the_users_then_drafting_goes_on(
    google, drafting, monkeypatch
):
    monkeypatch.setattr(applying, "LOOK_EVERY_SECONDS", 0.01)
    tools, browser, job = await _applying(google)
    browser.checking = 3
    talk = _talking("Apply to the GitLab one")

    await _drafted_for(tools, talk, job.number)

    assert talk.told[0].startswith(
        "The application for Director, Analytics Engineering at GitLab asks the user "
        "for a check that they are a person"
    )
    assert talk.userdata.gate.awaiting().kind == applications.FILL
    assert "action_id" in talk.told[-1]


async def test_a_hidden_browser_is_offered_to_be_shown_for_the_check(
    google, drafting, monkeypatch
):
    monkeypatch.setattr(applying, "LOOK_EVERY_SECONDS", 0.01)
    tools, browser, job = await _applying(google)
    browser.shown = False
    browser.checking = 2
    talk = _talking("Apply to the GitLab one")

    await _drafted_for(tools, talk, job.number)

    assert (
        "The browser is hidden, so they cannot see it: ask whether to show it"
        in (talk.told[0])
    )


async def test_a_check_on_the_form_is_the_users_to_do_before_it_is_submitted(
    google, drafting
):
    tools, browser, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "Fill it in", "Submit", "Done, submit")
    # The form has a tick box to show the user is a person, not yet ticked.
    browser.challenge = True

    done = await _filled(tools, browser, job, talk)

    assert done["check"] == applications.CHECK_ON_THE_FORM
    with pytest.raises(ToolError, match="on the form is not done yet"):
        await action_tools.confirm_action(talk, _yes(talk))
    assert not browser.sent
    # The user ticks it and says to submit: the new draft is submitted.
    browser.challenge = False
    await action_tools.confirm_action(talk, _yes(talk))
    assert browser.sent


async def test_a_check_after_submit_is_watched_until_the_application_goes_through(
    google, drafting, monkeypatch
):
    monkeypatch.setattr(applying, "LOOK_EVERY_SECONDS", 0.01)
    tools, browser, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "Fill it in", "Submit it")
    await _filled(tools, browser, job, talk)
    browser.check_on_submit = True

    done = await action_tools.confirm_action(talk, _yes(talk))
    assert "Orbit watches the page" in done["next"]
    assert not browser.sent and store.job(job.number).status == store.FILLED
    # The user does the check, and the page sends the form.
    browser.challenge = False
    browser.sent = True
    await tools._applications._watching

    assert store.job(job.number).status == store.SUBMITTED
    assert "Thank you for applying to GitLab" in talk.told[-1]


async def test_a_check_done_without_the_form_going_is_put_to_the_user_again(
    google, drafting, monkeypatch
):
    monkeypatch.setattr(applying, "LOOK_EVERY_SECONDS", 0.01)
    tools, browser, job = await _applying(google)
    talk = _talking("Apply to the GitLab one", "Fill it in", "Submit it")
    await _filled(tools, browser, job, talk)
    browser.check_on_submit = True
    await action_tools.confirm_action(talk, _yes(talk))

    browser.challenge = False
    await tools._applications._watching

    again = talk.userdata.gate.awaiting()
    assert again.kind == applications.SUBMIT
    assert f"action_id {again.id}" in talk.told[-1]
    assert not browser.sent


async def test_a_companys_board_is_learnt_from_where_its_application_is(
    google, drafting
):
    tools, _, job = await _applying(google)

    await _drafted_for(tools, _talking("Apply to the GitLab one"), job.number)

    board = store.board("GitLab")
    assert (board.system, board.slug, board.source) == ("greenhouse", "gitlab", "found")
