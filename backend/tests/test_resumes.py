"""Which of the user's résumés an application sends."""

import pytest

from jobs import resumes, store

COMMON = "Nidhish Kumar. SDE at 2Sigma School. Python, SQL and Git. B.Tech, NIT."
AI = store.Resume(
    "resume-ai-engineer.pdf",
    "/resumes/resume-ai-engineer.pdf",
    "from your computer",
    COMMON + " Built RAG pipelines with LangGraph and PyTorch; LLM agents.",
    "",
    1.0,
)
BACKEND = store.Resume(
    "resume-backend.pdf",
    "/resumes/resume-backend.pdf",
    "from your computer",
    COMMON + " Built REST APIs in Django; PostgreSQL, Kafka and Redis.",
    "",
    2.0,
)
ROLES = ["AI Engineer", "Backend Developer"]


def _chose(title: str, posting: str = "", kept=(AI, BACKEND)) -> resumes.Choice:
    chose = resumes.choose(list(kept), title, posting, "Nidhish Kumar", ROLES)
    assert chose is not None
    return chose


@pytest.mark.parametrize(
    ("name", "role"),
    [
        ("resume-backend.pdf", "backend"),
        ("resume-ai-engineer.pdf", "ai engineer"),
        ("NidhishKumarBackendCV_v2.pdf", "Backend"),
        # Nothing of the work: the user's name, "resume", a version or a year.
        ("Nidhish_Kumar_Resume.pdf", ""),
        ("CV (1).pdf", ""),
        ("resume-final-2026.pdf", ""),
    ],
)
def test_a_resumes_work_is_read_from_its_name(name, role):
    resume = store.Resume(name, f"/resumes/{name}", "from your computer", "", "", 0.0)

    assert resumes.role_of(resume, "Nidhish Kumar") == role


def test_what_the_user_said_a_resume_is_for_comes_first():
    said = store.Resume("cv.pdf", "/resumes/cv.pdf", "you", "", "Data Engineer", 0.0)

    assert resumes.role_of(said) == "Data Engineer"


@pytest.mark.parametrize(
    ("title", "sent"),
    [
        ("Senior Backend Engineer", BACKEND),
        ("GenAI Engineer", AI),
        ("Machine Learning Engineer", AI),
    ],
)
def test_the_resume_made_for_the_jobs_kind_of_work_is_sent(title, sent):
    chose = _chose(title)

    assert chose.resume == sent
    assert chose.why.startswith("it is your résumé for")


def test_else_the_one_whose_own_words_the_posting_uses_most():
    chose = _chose(
        "Software Engineer",
        "You will build microservices in Django on PostgreSQL and Kafka, in Python.",
    )

    assert chose.resume == BACKEND
    # Named as the posting writes them; Python is on both, so it tells them not apart.
    assert chose.why == (
        "the posting asks for Django, PostgreSQL, Kafka, which it names and your "
        "others do not"
    )


def test_a_job_both_are_made_for_goes_by_the_posting_too():
    chose = _chose("AI Backend Engineer", "Serve RAG behind APIs, with PyTorch models.")

    assert chose.resume == AI


def test_with_nothing_to_tell_them_apart_the_first_role_looked_for_wins():
    chose = _chose("Software Engineer", "Python, SQL and Git.")

    assert chose.resume == AI
    assert "the first you look for" in chose.why


def test_a_name_that_names_no_work_fits_no_job_in_particular():
    senior = store.Resume("resume-senior.pdf", "/r/s.pdf", "you", BACKEND.text, "", 1.0)
    plain = store.Resume("resume-2.pdf", "/r/2.pdf", "you", AI.text, "", 2.0)

    chose = _chose("Backend Engineer", "", kept=(senior, plain))

    # "senior" is no kind of work: nothing tells them apart, so the first kept is.
    assert chose.resume == senior
    assert chose.why == "nothing in the posting suits another better"


def test_one_resume_is_sent_without_a_word_why():
    assert _chose("Backend Engineer", kept=(AI,)) == resumes.Choice(AI, "")
    assert resumes.choose([], "Backend Engineer", "") is None
