"""Which student programme a posting is: spring week, summer internship or
industrial placement.

Employers name one programme many ways - "Spring Week", "Spring Insight",
"Discovery Programme", "Technology Development Programme" - but who may apply
does not vary: spring weeks take first-years, summer internships take
penultimate-years, industrial placements take students doing a placement year.
So the requirements decide the type, whatever the programme is called: a
"Summer Internship" open only to first-year students is a first-year programme.

Nothing is guessed. Exactly one eligible year in the requirements decides it,
and that sentence is kept as the evidence. Failing that, a title naming one
type decides it. Otherwise the type stays as the platform gave it, or blank.
The phrases live in config/programmes.yaml.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from tracker import config
from tracker.models import Job

#: Longest evidence kept, so the Year of Study cell stays one readable line.
_EVIDENCE_CHARS = 200

#: Sentence and bullet boundaries in a cleaned description.
_SENTENCE = re.compile(r"(?<=[.!?;:])\s+|\s[|*\u2022\u00b7\u25aa-]\s|\n+")


@dataclass(frozen=True)
class Programme:
    work_type: str = ""
    evidence: str = ""      # the requirement sentence behind the type, if any


@dataclass(frozen=True)
class _Rules:
    titles: tuple[tuple[str, re.Pattern], ...]
    eligibility: tuple[tuple[str, re.Pattern], ...]
    context: re.Pattern | None
    negations: tuple[str, ...]
    programme_titles: re.Pattern | None
    compatible: dict[str, frozenset[str]]


def _pattern(phrases) -> re.Pattern | None:
    parts = []
    for phrase in phrases or []:
        words = [re.escape(w) for w in re.split(r"[\s-]+", str(phrase).strip().casefold()) if w]
        if words:
            parts.append(r"[\s-]+".join(words))
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b") if parts else None


@lru_cache(maxsize=1)
def _rules() -> _Rules:
    settings = config.load("programmes")

    def typed(section: str) -> tuple[tuple[str, re.Pattern], ...]:
        pairs = []
        for work_type, phrases in (settings.get(section) or {}).items():
            pattern = _pattern(phrases)
            if pattern:
                pairs.append((work_type, pattern))
        return tuple(pairs)

    return _Rules(
        titles=typed("title_types"),
        eligibility=typed("eligibility"),
        context=_pattern(settings.get("student_context")),
        negations=tuple(str(n).casefold() for n in settings.get("negations") or []),
        programme_titles=_pattern(settings.get("programme_titles")),
        compatible={t: frozenset(v or []) for t, v in (settings.get("compatible") or {}).items()},
    )


def type_from_title(title: str, employment: str = "") -> str:
    """The type a title names outright, or "".

    A bare "Software Engineering Intern" could be summer or off-cycle, so it
    stays "" rather than guessed - and an empty type still merges with an
    aggregator's copy that knows.
    """
    text = f"{title} {employment}".casefold()
    for work_type, pattern in _rules().titles:
        if pattern.search(text):
            return work_type
    return ""


def is_programme_title(title: str) -> bool:
    """Whether a title names a student programme without saying which, e.g.
    "Discovery Programme". Worth opening, not yet proof of a student role."""
    pattern = _rules().programme_titles
    return bool(pattern and pattern.search((title or "").casefold()))


def eligible_years(text: str) -> dict[str, str]:
    """{type: evidence} for every type the requirement sentences admit.

    A phrase only counts in a sentence about studying with no negation or
    redirect, so "not open to first-year students" is never read as the
    opposite, nor "first-years should apply to our Spring Week instead" as
    describing this posting.
    """
    rules = _rules()
    found: dict[str, str] = {}
    for sentence in _SENTENCE.split(text or ""):
        sentence = " ".join(sentence.split())
        low = sentence.casefold()
        if not (rules.context and rules.context.search(low)):
            continue
        if any(negation in low for negation in rules.negations):
            continue
        for work_type, pattern in rules.eligibility:
            match = pattern.search(low)
            if match and work_type not in found:
                found[work_type] = _snippet(sentence, match)
    return found


def classify(title: str, text: str = "", employment: str = "") -> Programme:
    """The programme a posting is, with the evidence behind it.

    Who may apply wins: employers title programmes loosely, but a posting open
    only to first-years is a first-year programme. The exception is wording
    that fits the title's type as well - penultimate-year students apply for
    placements too - so it never turns "Industrial Placement" into a summer
    internship. When the requirements name two years, or none, the title's
    own type is used.
    """
    found = eligible_years(text)
    titled = type_from_title(title, employment)
    if len(found) == 1:
        work_type, evidence = next(iter(found.items()))
        if titled in _rules().compatible.get(work_type, ()):
            return Programme(titled, evidence)
        return Programme(work_type, evidence)
    return Programme(titled, found.get(titled, "")) if titled else Programme()


def refine(job: Job, text: str) -> bool:
    """Settle a job's type from page text that states who may apply.

    Used on the employer's own posting, which is where aggregator rows learn
    that their "Technology Programme" is really a spring week. Without evidence
    the job is left exactly as it was. Returns whether it was settled.
    """
    found = classify(job.raw_title or job.job_title, text)
    if not found.evidence:
        return False
    job.work_type = found.work_type
    if not job.year_of_study:
        job.year_of_study = found.evidence
    return True


def carry(job: Job, old: Job) -> None:
    """Keep a type an earlier run read off the employer's page.

    Verification revisits a page only every few days, and between visits the
    aggregator's own type would otherwise come back. The stored sentence is
    re-read rather than trusting the old type, so a changed title still wins.
    """
    if job.year_of_study or not old.year_of_study:
        return
    found = classify(job.raw_title or job.job_title, old.year_of_study)
    if found.evidence:
        job.work_type = found.work_type
        job.year_of_study = old.year_of_study


def _snippet(sentence: str, match: re.Match) -> str:
    if len(sentence) <= _EVIDENCE_CHARS:
        return sentence
    half = (_EVIDENCE_CHARS - len(match.group(0))) // 2
    start, end = max(0, match.start() - half), min(len(sentence), match.end() + half)
    return ("..." if start else "") + sentence[start:end].strip() + ("..." if end < len(sentence) else "")
