"""Cross-platform deduplication and stable ID assignment.

The same internship appears on Trackr, Gradcracker and Bright Network with
different titles, different company spellings and often different URLs. This
module collapses those into one `Job` carrying every source reference, so the
workbook shows one row per real job.

It is deliberately conservative. Two genuinely different internships at the
same company ("Software Engineering Intern" and "Machine Learning Intern")
must not merge — a wrongly merged pair silently hides a job the user could
have applied to, which is worse than a duplicate row they can see.
"""

from __future__ import annotations

import json
import re
from datetime import date

from rapidfuzz import fuzz

from tracker import config, normalize
from tracker.models import Job, SourceRef

#: Title similarity (0-100) required before two same-company listings merge.
#: 88 keeps "software engineer" / "software engineer backend" apart while
#: still merging the verb/noun variants normalization did not catch.
TITLE_THRESHOLD = 88

#: Deadlines this far apart mean the platforms disagree about which round a
#: role belongs to, so the listings are probably different intakes.
DEADLINE_TOLERANCE_DAYS = 21

#: Locations that name a country and no city. One listing saying "United
#: Kingdom" and another saying "London" is no evidence of two postings.
_COUNTRY_ONLY = frozenset({"united kingdom", "uk", "england", "scotland", "wales",
                           "northern ireland", "great britain"})

#: A year in a title: "2027", or a span such as "2026/27" or "2026-2027".
_TITLE_YEAR = re.compile(r"\b(20\d\d)(?:\s*[/-]\s*(\d{4}|\d{2}))?\b")


def merge_key(job: Job) -> str:
    """Blocking key. Only jobs sharing this are compared pairwise, which keeps
    deduplication linear rather than quadratic across the whole corpus."""
    return job.normalized_company or job.company.casefold()


def title_years(job: Job) -> frozenset[int]:
    """The intake years a title names.

    Normalization strips years so "SWE Intern - Summer 2027" can meet an
    undated "Software Engineering Intern" on another platform. That same
    stripping makes "Programme 2026" and "Programme 2027" identical, so the
    years are read back from the title the platform actually showed.
    """
    years: set[int] = set()
    for match in _TITLE_YEAR.finditer(job.raw_title or job.job_title):
        start = int(match.group(1))
        years.add(start)
        # "2026/27" is one academic-year intake covering both years. Any other
        # trailing number ("2026 - 12 months") is not a year.
        if match.group(2):
            end = int(match.group(2))
            if end in (start + 1, (start + 1) % 100):
                years.add(start + 1)
    return frozenset(years)


def _platform_ids(job: Job) -> dict[str, set[str]]:
    """{source: {external_id, ...}} for every listing folded into this job."""
    ids: dict[str, set[str]] = {}
    for ref in job.sources:
        if ref.external_id:
            ids.setdefault(ref.source, set()).add(ref.external_id)
    return ids


def _distinct_on_a_shared_platform(a: Job, b: Job) -> bool:
    """Whether a platform both jobs came from lists them under different ids.

    A platform does not list one job twice under two ids, so that is proof of
    two postings - typically one programme's 2026 and 2027 rounds, which read
    identically once years are normalized away.
    """
    ids_a, ids_b = _platform_ids(a), _platform_ids(b)
    return any(ids_a[s].isdisjoint(ids_b[s]) for s in ids_a.keys() & ids_b.keys())


def _posting_keys(job: Job) -> set[str]:
    urls = [job.application_url] + [ref.application_url for ref in job.sources]
    return {key for key in (normalize.posting_key(u) for u in urls) if key}


def _shared_posting(a: Job, b: Job) -> bool:
    return bool(_posting_keys(a) & _posting_keys(b))


def is_same_listing(a: Job, b: Job) -> bool:
    """Whether two records, e.g. today's job and last run's, are one listing.

    Stricter than `is_same_job`: this decides whether verification state and
    ids may carry from one to the other, and a 2026 round's verified status
    says nothing about the 2027 round.
    """
    ids_a, ids_b = _platform_ids(a), _platform_ids(b)
    shared = ids_a.keys() & ids_b.keys()
    if shared:
        return all(ids_a[s] & ids_b[s] for s in shared)
    return a.fingerprint() == b.fingerprint()


def is_same_job(a: Job, b: Job) -> bool:
    """Whether two listings describe the same real internship."""
    if merge_key(a) != merge_key(b):
        return False

    if _distinct_on_a_shared_platform(a, b):
        return False

    # Both link to one employer posting: the same job, however each platform
    # titles or locates it ("United Kingdom" on Trackr, "London" on the ATS).
    if _shared_posting(a, b):
        return True

    title_score = fuzz.token_set_ratio(a.normalized_title, b.normalized_title)
    if title_score < TITLE_THRESHOLD:
        return False

    # Same company and role but different cities are different postings.
    if (a.location and b.location and a.location.casefold() != b.location.casefold()
            and not {a.location.casefold(), b.location.casefold()} & _COUNTRY_ONLY):
        return False

    # Same company and role, but deadlines months apart: different intakes
    # (e.g. a 2026 and a 2027 round both listed at once).
    if a.deadline and b.deadline:
        if abs((a.deadline - b.deadline).days) > DEADLINE_TOLERANCE_DAYS:
            return False

    # Titles naming different years are different intakes, even when neither
    # listing carries a deadline to tell them apart.
    years_a, years_b = title_years(a), title_years(b)
    if years_a and years_b and years_a.isdisjoint(years_b):
        return False

    # A work type mismatch separates a summer internship from a 12-month
    # industrial placement at the same company.
    if a.work_type and b.work_type and a.work_type != b.work_type:
        return False

    return True


def merge(primary: Job, other: Job, preference: list[str]) -> Job:
    """Fold `other` into `primary`, keeping the better value for each field.

    Every source reference from both survives, which is what lets the workbook
    show all three platforms on one row and keep all their URLs.
    """
    for ref in other.sources:
        if not any(_same_ref(s, ref) for s in primary.sources):
            primary.sources.append(ref)

    # The description travels with the listing it came from.
    if not primary.description and other.description:
        primary.description_source = other.description_source

    # Prefer a real value over an empty one, field by field. Neither listing is
    # authoritative, so the rule is simply "something beats nothing".
    for attr in (
        "salary", "duration", "degree_requirements", "year_of_study",
        "experience_requirements", "description", "location",
        "work_arrangement", "work_type", "start_date",
        "cv", "written_answers", "cover_letter", "expected_graduation",
        "university_year", "degree_subject", "programme_dates", "right_to_work", "visa_sponsorship",
        "classification_basis", "graduation_basis",
    ):
        if not getattr(primary, attr) and getattr(other, attr):
            setattr(primary, attr, getattr(other, attr))

    # Skills are additive: each platform lists a partial set.
    for attr in ("skills", "programming_languages", "technologies"):
        combined = list(dict.fromkeys(getattr(primary, attr) + getattr(other, attr)))
        setattr(primary, attr, combined)

    # The earliest deadline is the safe one to show: if platforms disagree,
    # acting on the later date risks missing the real cutoff.
    deadlines = [d for d in (primary.deadline, other.deadline) if d]
    if deadlines:
        primary.deadline = min(deadlines)

    # Platforms repost; the earliest date any of them states is when it opened.
    posted = [d for d in (primary.date_posted, other.date_posted) if d]
    if posted:
        primary.date_posted = min(posted)

    if other.date_found and (not primary.date_found or other.date_found < primary.date_found):
        primary.date_found = other.date_found

    _set_primary_source(primary, preference)
    return primary


def _same_ref(a: SourceRef, b: SourceRef) -> bool:
    """One platform listing seen twice, so only one SourceRef is kept."""
    if a.source != b.source:
        return False
    if a.external_id or b.external_id:
        return a.external_id == b.external_id
    return a.source_url == b.source_url


def _set_primary_source(job: Job, preference: list[str]) -> None:
    """Pick which platform the row is attributed to.

    Completeness wins first — a source giving both a deadline and an
    application URL is more useful than one giving neither — with the
    configured preference order breaking ties.
    """
    if not job.sources:
        return

    def rank(ref: SourceRef) -> tuple[int, int]:
        order = preference.index(ref.source) if ref.source in preference else len(preference)
        return (-ref.completeness(), order)

    best = sorted(job.sources, key=rank)[0]
    job.primary_source = best.source
    job.source_url = best.source_url
    job.application_url = best.application_url or best.source_url
    if best.listed_deadline:
        job.listed_deadline = best.listed_deadline


def deduplicate(jobs: list[Job]) -> tuple[list[Job], int]:
    """Collapse duplicates. Returns (merged_jobs, duplicates_merged)."""
    preference = config.load("sources").get("primary_source_preference", [])

    buckets: dict[str, list[Job]] = {}
    for job in jobs:
        buckets.setdefault(merge_key(job), []).append(job)

    merged: list[Job] = []
    duplicates = 0

    for bucket in buckets.values():
        kept: list[Job] = []
        for job in bucket:
            for existing in kept:
                if is_same_job(existing, job):
                    merge(existing, job, preference)
                    duplicates += 1
                    break
            else:
                _set_primary_source(job, preference)
                kept.append(job)
        merged.extend(kept)

    return merged, duplicates


# ---------------------------------------------------------------------------
# Stable IDs
# ---------------------------------------------------------------------------


#: Registry keys for platform listings ("ref:trackr:fhfnv05uki") live beside the
#: 16-hex content fingerprints; the prefix keeps the two kinds apart.
REF_PREFIX = "ref:"


def ref_keys(job: Job) -> list[str]:
    """Registry keys for every platform listing folded into this job."""
    return [f"{REF_PREFIX}{ref.source}:{ref.external_id}" for ref in job.sources if ref.external_id]


def _latest_intake(claimants: list[Job]) -> Job:
    """Which of several listings contesting one fingerprint's id gets it.

    Only reached for ids minted before listings were keyed by platform id, when
    one row could hide several rounds of a programme. A status on such a row
    was set against the round that was open: the latest. A year in the title
    decides that; failing one, the later listing in collection order, since
    Trackr is swept season by season in config order, oldest first. Deadlines
    do not decide it: Trackr's past rounds usually carry one and its new
    rounds usually do not.
    """
    ranked = enumerate(claimants)
    return max(ranked, key=lambda pair: (max(title_years(pair[1]), default=0), pair[0]))[1]


def assign_ids(jobs: list[Job]) -> None:
    """Give every job its permanent INT-nnnnnn id, in place.

    That id is what links the workbook row to the user's status and notes, so
    two rules hold:

    * One id per job per run. Two rows sharing an id would let a status set on
      one appear on the other.
    * An id follows the listing. A platform's own listing id is looked up
      first. The content fingerprint is the fallback, carrying an id across a
      retitle or onto a new platform, but it never hands over an id that
      already belongs to a different listing on the same platform: that is a
      new round of the programme, not the one the user acted on.
    """
    registry = _load_registry()
    before = dict(registry)
    next_number = registry.get("_next", 1)

    platforms_by_id: dict[str, set[str]] = {}
    for key, value in registry.items():
        if key.startswith(REF_PREFIX):
            platforms_by_id.setdefault(value, set()).add(key[len(REF_PREFIX):].split(":", 1)[0])

    taken: set[str] = set()
    unresolved: list[Job] = []
    for job in jobs:
        if job.id and job.id not in taken:
            taken.add(job.id)
        else:
            job.id = ""
            unresolved.append(job)

    # 1. A listing seen before keeps its id.
    still: list[Job] = []
    for job in unresolved:
        known = sorted({registry[k] for k in ref_keys(job) if k in registry} - taken)
        if known:
            job.id = known[0]
            taken.add(job.id)
        else:
            still.append(job)

    # 2. Otherwise the fingerprint's id, if free and not another listing's.
    claims: dict[str, list[Job]] = {}
    for job in still:
        candidate = registry.get(job.fingerprint())
        if (
            candidate
            and candidate not in taken
            and not platforms_by_id.get(candidate, set()) & set(job.source_names())
        ):
            claims.setdefault(candidate, []).append(job)
    for candidate, claimants in claims.items():
        _latest_intake(claimants).id = candidate
        taken.add(candidate)

    # 3. Anything left is new.
    for job in still:
        if not job.id:
            job.id = f"INT-{next_number:06d}"
            next_number += 1

    for job in jobs:
        registry.setdefault(job.fingerprint(), job.id)
        for key in ref_keys(job):
            registry.setdefault(key, job.id)
    registry["_next"] = next_number

    if registry != before:
        _save_registry(registry)


def _load_registry() -> dict:
    if not config.ID_REGISTRY.exists():
        return {"_next": 1}
    try:
        with config.ID_REGISTRY.open(encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError:
        # A corrupt registry must not mint duplicate ids for existing jobs, so
        # refuse rather than silently restarting the numbering.
        raise RuntimeError(
            f"{config.ID_REGISTRY} is corrupt. Restore it from backup or delete it "
            "and accept that existing rows will be re-numbered."
        )


def _save_registry(registry: dict) -> None:
    config.ensure_dirs()
    tmp = config.ID_REGISTRY.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2, sort_keys=True)
    tmp.replace(config.ID_REGISTRY)
