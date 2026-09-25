"""Which graduation years a posting admits, and whether the user's is one.

Employers rarely write "first year". They write "graduating in 2029", "class
of 2029" or "expected graduation between December 2028 and June 2029" - or
they name a year of study for an intake: first-years on a spring week,
penultimate-years on a summer internship, students taking a placement year on
a placement. Given the programme's own year, each of those fixes a graduation
year.

A stated year always wins over a derived one, and a year is only derived when
the programme's year is written in its title or posting: never assumed from
today's date. Phrases live in config/programmes.yaml.
"""

from __future__ import annotations

import re
from functools import lru_cache

from tracker import config, programme
from tracker.models import Job

#: Longest evidence kept, so the Year of Study cell stays one readable line.
_EVIDENCE_CHARS = 200

#: How far past the graduation wording a year may sit. "graduate with an
#: undergraduate or master's degree in 2029" puts it 50 characters on.
_WINDOW = 120

_YEAR = re.compile(r"\b(20[2-3]\d)\b")

#: Sentence boundaries only. Unlike programme's splitter, " - " is kept inside a
#: sentence, because "December 2027 - June 2028" is one range.
_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\s\|\s|\n+")

#: The programme's own year, where the posting rather than the title names it.
_INTAKE = re.compile(
    r"\b(?:summer|spring|autumn|september|intake|starting|start|commencing|placement|internship|"
    r"programme|program)\s+(?:in\s+)?(20[2-3]\d)\b", re.I)


@lru_cache(maxsize=1)
def _settings() -> tuple[re.Pattern | None, dict[str, tuple[int, ...]]]:
    settings = config.load("programmes")
    derived = {t: tuple(int(k) for k in offsets or [])
               for t, offsets in (settings.get("derived_graduation") or {}).items()}
    return programme._pattern(settings.get("graduation_words")), derived


def expected_years(title: str, text: str) -> tuple[list[int], str]:
    """(years, evidence) for the graduation years a posting admits.

    Returns ([], "") when the posting neither states a year nor names a year of
    study for a dated intake, rather than guessing one.
    """
    words, derived = _settings()
    negations = programme._rules().negations

    if words:
        for sentence in _SENTENCE.split(text or ""):
            sentence = " ".join(sentence.split())
            low = sentence.casefold()
            if any(negation in low for negation in negations):
                continue
            match = words.search(low)
            if not match:
                continue
            years = sorted({int(y) for y in _YEAR.findall(low[match.start(): match.end() + _WINDOW])})
            if years:
                # Centred on the wording, so a long sentence still shows the year.
                return years, programme._snippet(sentence, match)

    found = programme.classify(title, text)
    intake = _intake_year(title, text)
    offsets = derived.get(found.work_type, ())
    if found.evidence and intake and offsets:
        years = sorted({intake + k for k in offsets})
        return years, _clip(f"{found.evidence} [{found.work_type}, {intake} intake]")
    return [], ""


def apply(job: Job, text: str, profile: dict) -> bool:
    """Record the years a posting admits, then flag the row if the user's
    graduation year is among them. Returns whether it was flagged."""
    if not job.expected_graduation:
        years, evidence = expected_years(job.raw_title or job.job_title, text)
        if years:
            job.expected_graduation = ", ".join(str(y) for y in years)
            job.graduation_basis = "derived" if evidence.endswith("intake]") else "stated"
            if not job.year_of_study:
                job.year_of_study = evidence
    return flag(job, profile)


def flag(job: Job, profile: dict) -> bool:
    """Whether the user's graduation year is among the years a posting admits.

    The row's type is not touched: it comes from what the posting says about
    itself (tracker/eligibility.py).
    """
    target = (profile or {}).get("graduation_year")
    if not target or not job.expected_graduation:
        return False
    if int(target) not in {int(y) for y in _YEAR.findall(job.expected_graduation)}:
        return False
    return True


def carry(job: Job, old: Job) -> None:
    """Keep years an earlier run read off an employer page this run did not
    revisit; the aggregator's own listing never states them."""
    if old.expected_graduation and not job.expected_graduation:
        job.expected_graduation = old.expected_graduation
        job.graduation_basis = old.graduation_basis
        if not job.year_of_study:
            job.year_of_study = old.year_of_study


def _intake_year(title: str, text: str) -> int | None:
    years = [int(y) for y in _YEAR.findall(title or "")]
    if years:
        return max(years)
    match = _INTAKE.search(text or "")
    return int(match.group(1)) if match else None


def _clip(text: str) -> str:
    return text if len(text) <= _EVIDENCE_CHARS else text[: _EVIDENCE_CHARS - 3].rstrip() + "..."
