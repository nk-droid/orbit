"""Which of the user's résumés an application sends, when they have one for each kind
of role they apply to.

Code chooses, so a job always gets the same résumé and the user is told why:

1. The one made for the job's kind of work, as the user said it is, or as its
   file's name says, such as "backend" for resume-backend.pdf.
2. Else the one whose own names for things, those its other résumés do not give,
   the posting uses most: a backend résumé's Kafka and PostgreSQL, an AI one's RAG
   and PyTorch.
3. Else the one for the first of the roles in the user's profile, and else the one
   they gave first.
"""

import re
from dataclasses import dataclass
from pathlib import PurePath

from . import relevance, store

# What a résumé's file name may say besides the work it is for.
_NOT_THE_WORK = re.compile(
    r"\b(?:r[ée]sum[ée]s?|cv|curriculum|vitae|final|latest|new|updated|copy|draft|"
    r"v?\d+)\b",
    re.IGNORECASE,
)
# Web and email addresses, which tell nothing of the work.
_ADDRESSES = re.compile(r"https?://\S+|www\.\S+|\S+@\S+")
# A word as a résumé writes it.
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.]*[A-Za-z0-9+#]|[A-Za-z]")
# What ends a sentence, or opens a line, before a word.
_STARTED = re.compile(r"(?:^|[.!?:;]|[\u2022\u00b7*\u2013-])\s*$")
# How many of the words a résumé shares with a posting the user is told of.
TOLD = 4


@dataclass(frozen=True)
class Choice:
    """The résumé an application sends, and why, as the user is told it: "" when
    it is the only one."""

    resume: store.Resume
    why: str


def role_of(resume: store.Resume, owner: str = "") -> str:
    """The work a résumé is for: as the user said, or else as its file's name says,
    without the user's own name (`owner`) or words such as "resume". "" when
    neither says."""
    if resume.role.strip():
        return resume.role.strip()
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", PurePath(resume.name).stem)
    said = _NOT_THE_WORK.sub(" ", re.sub(r"[\W_]+", " ", stem))
    for name in owner.split():
        said = re.sub(rf"\b{re.escape(name)}\b", " ", said, flags=re.IGNORECASE)
    return " ".join(said.split())


def _names_work(role: str) -> bool:
    """Whether a role names a kind of work or a field, as "Backend Developer" does
    and "senior" does not: one that names neither would fit every job."""
    named = relevance.role(role)
    return named.core is not None or bool(named.focus)


def _as_written(word: str, text: str) -> str:
    """A word as a text first writes it, such as "PostgreSQL" for "postgresql"."""
    found = re.search(rf"(?<![\w+#]){re.escape(word)}(?![\w+#])", text, re.IGNORECASE)
    return found[0] if found else word


def _names(text: str) -> set[str]:
    """The names a résumé gives the things it knows, as it writes them: with capitals
    inside a sentence (Kafka, PyTorch, PostgreSQL) or all in capitals (AWS, RAG).
    Not plain words, nor a capital that only opens a sentence or a line."""
    found: set[str] = set()
    for line in _ADDRESSES.sub(" ", text).splitlines():
        for match in _WORD.finditer(line):
            word = match[0]
            inside = any(c.isupper() for c in word[1:])
            if inside or (
                word[0].isupper() and not _STARTED.search(line[: match.start()])
            ):
                found |= relevance.words(word)
    return found


def _own_words(kept: list[store.Resume]) -> dict[str, set[str]]:
    """The names each résumé gives that not every other one does."""
    words = {resume.name: _names(resume.text) for resume in kept}
    everywhere = set.intersection(*words.values()) if words else set()
    return {name: found - everywhere for name, found in words.items()}


def choose(
    kept: list[store.Resume],
    title: str,
    posting: str,
    owner: str = "",
    roles: list[str] | None = None,
) -> Choice | None:
    """The résumé to send for a job, from its title and posting, and why. None when
    the user has no résumé."""
    if not kept:
        return None
    if len(kept) == 1:
        return Choice(kept[0], "")
    work = {
        resume.name: role if _names_work(role := role_of(resume, owner)) else ""
        for resume in kept
    }
    made_for = [
        r for r in kept if work[r.name] and relevance.is_role(title, [work[r.name]])
    ]
    if len(made_for) == 1:
        return Choice(
            made_for[0], f"it is your résumé for {work[made_for[0].name]} roles"
        )
    pool = made_for or kept
    asked = relevance.words(_ADDRESSES.sub(" ", posting))
    own = _own_words(pool)
    shared = {name: own[name] & asked for name in own}
    ranked = sorted(pool, key=lambda resume: len(shared[resume.name]), reverse=True)
    best, second = ranked[0], ranked[1]
    if len(shared[best.name]) > len(shared[second.name]):
        said = posting.casefold()
        named = sorted(
            shared[best.name], key=lambda word: (word not in said, said.find(word))
        )
        told = ", ".join(_as_written(word, posting) for word in named[:TOLD])
        return Choice(
            best,
            f"the posting asks for {told}, which it names and your others do not",
        )
    for role in roles or []:
        for resume in pool:
            if work[resume.name] and relevance.is_role(role, [work[resume.name]]):
                return Choice(
                    resume,
                    f"nothing in the posting suits another better, and it is for "
                    f"{work[resume.name]} roles, the first you look for",
                )
    return Choice(pool[0], "nothing in the posting suits another better")
