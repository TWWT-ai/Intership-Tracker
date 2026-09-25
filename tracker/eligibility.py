"""What an opportunity really is, and whether the user can apply.

Titles are unreliable: a "Summer Internship" can be a one-week first-year taster
and a "Development Programme" can be a placement year. So an opportunity is
classified by what its posting says about itself - how long it runs, whether it
is a placement year, the span of its dates, whether it leads to a later
internship, which students it wants - and only then by its title.

Eligibility is read the same way and translated into the user's own academic
year from data/profile.json. Every fact keeps the employer's wording. What the
posting does not say stays blank, and a verdict that cannot be settled is
REQUIRES VERIFICATION, never a guess. Phrases live in config/programmes.yaml.
"""

from __future__ import annotations

import re
from functools import lru_cache

from tracker import config, graduation, programme
from tracker.models import Job

YES, NO, CHECK = "Yes", "No", "REQUIRES VERIFICATION"
SPRING, SUMMER, PLACEMENT = "Spring Week", "Summer Internship", "Industrial Placement"

#: Sentence and bullet boundaries, keeping "June 2027 - June 2028" whole.
_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\s\|\s|\n+|\s[*•·▪]\s")

_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15}
_DURATION = re.compile(
    r"\b(?:(\d{1,2}|" + "|".join(_NUMBERS) + r")(?:\s*(?:-|–|to)\s*(\d{1,2}))?[\s-]*(day|week|month)s?\b"
    r"|(week)[\s-]long\b)", re.IGNORECASE)

#: How close a length must sit to what it describes: "a duration of 12 months"
#: is 45 characters from "placement opportunities".
_DURATION_WINDOW = 60

_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december"
           "|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec")
_DATE_RANGE = re.compile(
    rf"\b(?:\d{{1,2}}(?:st|nd|rd|th)?\s+)?({_MONTHS})\.?\s+(20\d\d)\s*(?:-|–|to|until)\s*"
    rf"(?:\d{{1,2}}(?:st|nd|rd|th)?\s+)?({_MONTHS})\.?\s+(20\d\d)\b", re.IGNORECASE)
_DATE_SINGLE = re.compile(
    rf"\b(?:in|from|starting|during|place in|runs in)\s+(?:\d{{1,2}}(?:st|nd|rd|th)?\s+)?({_MONTHS})\.?\s+(20\d\d)\b",
    re.IGNORECASE)
_MONTH_WORD = re.compile(rf"\b({_MONTHS})\b", re.IGNORECASE)
_MONTH_NUMBER = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}

#: Facts filled from postings; `carry` keeps them between runs.
FACT_FIELDS = ("university_year", "degree_subject", "duration", "programme_dates", "right_to_work",
               "visa_sponsorship", "classification_basis")


@lru_cache(maxsize=1)
def _rules() -> dict:
    facts = config.load("programmes").get("facts") or {}
    rules = {key: programme._pattern(phrases) for key, phrases in facts.items() if isinstance(phrases, list)}
    rules["thresholds"] = facts.get("thresholds") or {}
    return rules


def _sentences(text: str) -> list[str]:
    return [" ".join(s.split()) for s in _SENTENCE.split(text or "") if s and s.strip()]


def _find(sentences: list[str], key: str, skip_negated: bool = True) -> str:
    """The first sentence holding one of `key`'s phrases, as evidence."""
    pattern = _rules().get(key)
    if not pattern:
        return ""
    negations = programme._rules().negations
    for sentence in sentences:
        low = sentence.casefold()
        if skip_negated and any(n in low for n in negations):
            continue
        match = pattern.search(low)
        if match:
            return programme._snippet(sentence, match)
    return ""


def _subject(sentences: list[str]) -> str:
    """The degree subject asked for. "A degree in 2029" is a graduation year
    and "a degree in which a year in industry..." a placement requirement, so
    neither counts as a subject."""
    pattern = _rules().get("subject_lead")
    if pattern:
        for sentence in sentences:
            low = sentence.casefold()
            if "graduat" in low:
                continue
            for match in pattern.finditer(low):
                if re.match(r"\s*(?:a |an |the )?(?:which|order|20\d\d)", low[match.end():]):
                    continue
                return programme._snippet(sentence, match)
    return _find(sentences, "any_subject")


def _duration(sentences: list[str]) -> tuple[str, float | None]:
    """(length as written, in weeks), only where the length describes the
    programme rather than holiday, office days or an assessment window."""
    rules = _rules()
    context, ignore = rules.get("duration_context"), rules.get("duration_ignore")
    for sentence in sentences:
        low = sentence.casefold()
        for match in _DURATION.finditer(low):
            window = low[max(0, match.start() - _DURATION_WINDOW): match.end() + _DURATION_WINDOW]
            if (ignore and ignore.search(window)) or not (context and context.search(window)):
                continue
            if match.group(4):
                return "1 week", 1.0
            low_n = _NUMBERS.get(match.group(1)) or int(match.group(1))
            high_n = int(match.group(2)) if match.group(2) else low_n
            unit = match.group(3)
            weeks = {"day": high_n / 5, "week": float(high_n), "month": high_n * 52 / 12}[unit]
            label = unit if high_n == 1 else unit + "s"
            return (f"{low_n}-{high_n} {label}" if low_n != high_n else f"{high_n} {label}"), weeks
    return "", None


def _dates(sentences: list[str]) -> tuple[str, int | None]:
    """(dates as written, span in months). Graduation and deadline sentences
    carry dates too, and are not the programme's own."""
    usable = [s for s in sentences if not re.search(r"graduat|deadline|clos|apply by|open", s, re.IGNORECASE)]
    for sentence in usable:
        match = _DATE_RANGE.search(sentence)
        if match:
            start = _MONTH_NUMBER[match.group(1).casefold()[:3]] + int(match.group(2)) * 12
            end = _MONTH_NUMBER[match.group(3).casefold()[:3]] + int(match.group(4)) * 12
            if end >= start:
                return sentence[match.start(): match.end()], end - start
    for sentence in usable:
        match = _DATE_SINGLE.search(sentence)
        if match:
            return sentence[match.start(): match.end()].split(None, 1)[1], None
    return "", None


def classify(title: str, text: str, employment: str = "") -> tuple[str, str]:
    """(type, what decided it) from what the posting says, the title last.

    A decision the title contradicts says so, and a title-only decision is
    marked for verification, so the basis column shows how firm each type is.
    """
    sentences = _sentences(text)
    limits = _rules()["thresholds"]
    titled = programme.type_from_title(title, employment)

    def decided(kind: str, basis: str) -> tuple[str, str]:
        return kind, basis + (f" (title says {titled})" if titled and titled != kind else "")

    wording, weeks = _duration(sentences)
    if weeks is not None:
        if weeks <= float(limits.get("spring_max_weeks", 4)):
            return decided(SPRING, f"duration: {wording}")
        if float(limits.get("summer_min_weeks", 5)) <= weeks <= float(limits.get("summer_max_weeks", 16)):
            return decided(SUMMER, f"duration: {wording}")
        if (weeks >= float(limits.get("placement_min_months", 9)) * 52 / 12
                and not re.search(r"off[\s-]?cycle", f"{title} {employment}", re.IGNORECASE)):
            return decided(PLACEMENT, f"duration: {wording}")

    placement = _find(sentences, "placement_year")
    if placement:
        return decided(PLACEMENT, f"placement year: {placement}")

    dates, span = _dates(sentences)
    if span is not None:
        if span >= int(limits.get("placement_min_months", 9)):
            return decided(PLACEMENT, f"dates: {dates}")
        # "14 June 2027 - 28th August 2027" starts with a day, not a month.
        first = _MONTH_WORD.search(dates)
        start_month = _MONTH_NUMBER[first.group(1).casefold()[:3]] if first else -1
        if span <= 4 and 4 <= start_month <= 8:
            return decided(SUMMER, f"dates: {dates}")

    lead = _find(sentences, "leads_to_internship")
    if lead:
        return decided(SPRING, f"leads to a later internship: {lead}")

    found = programme.classify(title, text, employment)
    if found.evidence:
        return decided(found.work_type, f"who may apply: {found.evidence}")

    years, _ = graduation.expected_years(title, text)
    intake = graduation._intake_year(title, text)
    if years and intake and not titled:
        gap = min(years) - intake
        if gap >= 2:
            return decided(SPRING, f"graduation {years[0]} for a {intake} programme")
        if gap == 1:
            return decided(SUMMER, f"graduation {years[0]} for a {intake} programme")

    if titled:
        return titled, "title only - REQUIRES VERIFICATION"
    return "", ""


def assess(job: Job, text: str, profile: dict) -> str:
    """Read one posting text into the job, classify it, and settle whether the
    user can apply. Returns the verdict.

    Facts only fill blanks, so an aggregator's copy and the employer's own page
    can both contribute; the verdict is recomputed from everything known.
    """
    title = job.raw_title or job.job_title
    sentences = _sentences(text)

    kind, basis = classify(title, text)
    firm = basis and not basis.startswith("title")
    # Firm evidence replaces earlier evidence (an employer page is read after the
    # feed copy, and a corrected rule must take effect); a title-only guess never
    # overrides anything firmer.
    if basis and (firm or not job.classification_basis or job.classification_basis.startswith("title")):
        job.classification_basis = basis
        if kind and (firm or not job.work_type):
            job.work_type = kind

    def fill(attr: str, value: str) -> None:
        if value and not getattr(job, attr):
            setattr(job, attr, value)

    fill("duration", _duration(sentences)[0])
    fill("programme_dates", _dates(sentences)[0])
    fill("university_year", next((found for found in (
        _find(sentences, key) for key in ("second_of_four", "completed_first_year", "first_year",
                                          "penultimate", "final_year", "placement_year")) if found), ""))
    fill("degree_subject", _subject(sentences))
    fill("right_to_work", _find(sentences, "right_to_work", skip_negated=False))
    fill("visa_sponsorship", "No" if _find(sentences, "visa_no", skip_negated=False)
         else "Yes" if _find(sentences, "visa_yes", skip_negated=False) else "")

    graduation.apply(job, text, profile)
    settle(job, profile)
    return job.eligible_for_you


def settle(job: Job, profile: dict) -> None:
    """Translate what the posting asks for into the user's academic year.

    The user's own programmes (a spring week or summer internship during the
    current year, or a placement year) start in graduation_year - 2: a student
    graduating in 2029 on a four-year placement degree is in Year 2 in 2026/27
    and on placement in 2027/28, as on a three-year degree Year 1 is 2026/27.
    """
    rules = _rules()
    words = (job.university_year or "").casefold()

    def says(key: str) -> bool:
        return bool(rules.get(key) and rules[key].search(words))

    years = {int(y) for y in re.findall(r"20\d\d", job.expected_graduation or "")}
    # A year derived from "first-year students" describes a three-year degree's
    # first-years, not this user; only a year the employer states is decisive.
    # Rows saved before the basis was recorded count as stated only when the
    # employer's own sentence holds the year.
    basis = job.graduation_basis or ("stated" if re.search(r"20\d\d", job.year_of_study or "")
                                     and not (job.year_of_study or "").endswith("intake]") else "derived")
    stated = years if basis == "stated" else set()
    title_years = [int(y) for y in re.findall(r"\b20[2-3]\d\b", job.raw_title or job.job_title or "")]
    date_years = [int(y) for y in re.findall(r"20[2-3]\d", job.programme_dates or "")]
    intake = max(title_years) if title_years else (min(date_years) if date_years else None)

    first, second_four, completed = says("first_year"), says("second_of_four"), says("completed_first_year")
    penultimate, final, placement_year = says("penultimate"), says("final_year"), says("placement_year")

    job.first_year_eligible = (YES if first or second_four or (intake and years & {intake + 2, intake + 3})
                               else NO if (penultimate or final) and not first or (intake and years) else "")
    job.second_year_eligible = (YES if second_four or penultimate or (intake and years & {intake + 1, intake + 2})
                                else NO if final and not penultimate or (intake and years) else "")
    job.placement_year_eligible = (YES if placement_year or (job.work_type == PLACEMENT and (penultimate or second_four))
                                   else "")

    target = (profile or {}).get("graduation_year")
    if not target:
        job.eligible_for_you, job.eligibility_reason = "", ""
        return
    target = int(target)
    own_year = target - 2
    degree = str((profile or {}).get("degree") or "").casefold()
    placement_degree = "placement" in degree or "year in industry" in degree or "sandwich" in degree

    def verdict(value: str, why: str) -> None:
        job.eligible_for_you, job.eligibility_reason = value, why

    if intake and intake < own_year:
        verdict(NO, f"{intake} intake - an earlier round than yours ({own_year})")
    elif job.work_type == PLACEMENT and not placement_degree:
        verdict(NO, "a placement year, and your degree does not include one")
    elif stated:
        if target in stated:
            verdict(YES, f"admits graduates of {target}")
        else:
            verdict(NO, f"for graduates of {', '.join(str(y) for y in sorted(stated))}; you graduate in {target}")
    elif second_four:
        verdict(YES, "open to second-years on a four-year degree")
    elif job.work_type == PLACEMENT and (placement_year or penultimate):
        if intake == own_year:
            verdict(YES, f"placement year starting {intake}, as yours does")
        else:
            verdict(CHECK, "placement year, but the posting does not say which year it starts")
    elif completed:
        verdict(YES, "open once the first year is complete")
    elif final and not (penultimate or first):
        verdict(NO, "final-year students only")
    elif first:
        verdict(CHECK, "stated for first-years; second-years on four-year degrees are often accepted - check")
    elif penultimate:
        verdict(CHECK, "stated for penultimate-year students; check how a four-year placement degree counts")
    else:
        verdict(CHECK, "the posting does not say which students may apply")

    if job.eligible_for_you == YES and job.degree_subject:
        subject = job.degree_subject.casefold()
        other = rules.get("non_cs_subject") and rules["non_cs_subject"].search(subject)
        if other and not ((rules.get("cs_subject") and rules["cs_subject"].search(subject))
                          or (rules.get("any_subject") and rules["any_subject"].search(subject))):
            verdict(CHECK, f"year matches, but check the degree subject: {job.degree_subject[:80]}")
    if intake and intake > own_year + 1 and job.eligible_for_you == YES:
        verdict(CHECK, f"{intake} intake - later than your current cycle")
    if job.visa_sponsorship == "No" and job.eligible_for_you:
        job.eligibility_reason += "; no visa sponsorship"


def reclassify(job: Job) -> None:
    """Classify again from the stored posting text after the rules change.

    Only a company-site row's stored text is the employer's own posting, so only
    there may a type the current rules no longer support be withdrawn; an
    aggregator row's firmer type may have come from a page that is not stored.
    """
    kind, basis = classify(job.raw_title or job.job_title, job.description)
    if basis and not basis.startswith("title"):
        job.work_type, job.classification_basis = kind, basis
    elif job.primary_source == "careers" and job.classification_basis and not job.classification_basis.startswith("title"):
        job.work_type, job.classification_basis = kind, basis


def carry(job: Job, old: Job) -> None:
    """Keep facts an earlier run read off a page this run did not revisit. A
    firm type comes back with its basis; a title-only one does not override."""
    for attr in FACT_FIELDS:
        if getattr(old, attr) and not getattr(job, attr):
            setattr(job, attr, getattr(old, attr))
    if old.classification_basis and not old.classification_basis.startswith("title") and old.work_type:
        if job.classification_basis == old.classification_basis:
            job.work_type = old.work_type
