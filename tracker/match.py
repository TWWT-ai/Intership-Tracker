"""Eligibility, match scoring and priority ranking.

Three related steps kept together because they all answer "how much should the
user care about this row", and all read the same profile and weights.

The governing rule: never claim a strength the profile does not evidence.
While `data/profile.json` is unconfigured the matcher runs a relevance-only
pass, caps the score, and returns empty strengths rather than plausible ones.
A fabricated 91% is worse than an honest "unscored".
"""

from __future__ import annotations

import re
from datetime import date

from tracker import config
from tracker.models import Job, OpenStatus, Priority, USER_OWNED_STATUSES

#: Profile keys whose entries count as real evidence of a skill.
_SKILL_KEYS = ("languages", "frameworks", "libraries", "systems", "hardware", "machine_learning")

#: Evidence strengths that count fully. "preferred" and "uncertain" do not —
#: they record interest, not proof.
_STRONG_EVIDENCE = frozenset({"demonstrated", "professional", "project"})


# ---------------------------------------------------------------------------
# Profile access
# ---------------------------------------------------------------------------


def profile_skills(profile: dict) -> dict[str, str]:
    """Flatten the profile's skill lists to {canonical_skill: evidence}.

    Accepts both the plain string form and the {"name", "evidence"} form so a
    hand-edited profile works without ceremony.
    """
    from tracker import normalize

    out: dict[str, str] = {}
    for key in _SKILL_KEYS:
        for entry in profile.get(key) or []:
            if isinstance(entry, dict):
                name, evidence = entry.get("name", ""), entry.get("evidence", "demonstrated")
            else:
                name, evidence = str(entry), "demonstrated"
            canonical = normalize.skill(name)
            if canonical:
                # Keep the strongest evidence if a skill appears twice.
                if canonical not in out or evidence in _STRONG_EVIDENCE:
                    out[canonical] = evidence
    return out


# ---------------------------------------------------------------------------
# Requirement classification
# ---------------------------------------------------------------------------


def classify_requirements(job: Job) -> dict[str, str]:
    """Map each skill the posting mentions to required/preferred/nice_to_have.

    Uses the sentence the skill appears in, so "experience with Python is
    essential" and "exposure to Rust is a bonus" are weighted differently.
    This is what keeps the score from being keyword frequency.
    """
    weights = config.load("matching_weights")
    markers = weights.get("requirement_markers", {})
    text = job.description or ""
    sentences = re.split(r"[.;\n•·]+", text)

    out: dict[str, str] = {}
    for skill_name in job.skills:
        bucket = "preferred"  # a mentioned skill with no marker sits in the middle
        needle = skill_name.casefold()
        for sentence in sentences:
            low = sentence.casefold()
            if needle not in low:
                continue
            for level in ("required", "nice_to_have", "preferred"):
                if any(m in low for m in markers.get(level, [])):
                    bucket = level
                    break
            else:
                continue
            break
        out[skill_name] = bucket
    return out


# ---------------------------------------------------------------------------
# Eligibility (hard filters)
# ---------------------------------------------------------------------------


def check_eligibility(job: Job, profile: dict, filters: dict) -> tuple[bool, list[str]]:
    """Hard pass/fail against configured filters and the profile.

    Returns (eligible, reasons_if_not). Only rejects on evidence: a missing
    field never disqualifies a listing, because absence of data is not the same
    as failing the requirement.
    """
    reasons: list[str] = []

    title_low = (job.job_title or "").casefold()
    for word in filters.get("excluded_keywords", []):
        if word.casefold() in title_low:
            reasons.append(f"Title contains excluded term '{word}'")
            break

    # Location: only reject when a location is stated and matches nothing.
    allowed = [loc.casefold() for loc in filters.get("locations", [])]
    if job.location and allowed:
        loc_low = job.location.casefold()
        if not any(a in loc_low or loc_low in a for a in allowed):
            if job.work_arrangement != "Remote":
                reasons.append(f"Location '{job.location}' is outside the configured regions")

    # Work type, when the platform stated one.
    allowed_types = [w.casefold() for w in filters.get("work_types", [])]
    if job.work_type and allowed_types:
        wt = job.work_type.casefold()
        if not any(a in wt or wt in a for a in allowed_types):
            reasons.append(f"Work type '{job.work_type}' is not a targeted type")

    # Graduation-year gates, only when both sides are known.
    grad_year = profile.get("graduation_year")
    stated = job.expected_graduation or job.year_of_study
    if grad_year and stated:
        years = [int(y) for y in re.findall(r"\b(20\d{2})\b", stated)]
        if years and grad_year not in years:
            reasons.append(
                f"Posting targets graduation year {'/'.join(map(str, years))}, profile says {grad_year}"
            )

    return (not reasons), reasons


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def role_relevance(job: Job, filters: dict) -> tuple[float, list[str]]:
    """How well the role itself matches what the user is looking for, 0.0-1.0.

    Independent of the CV, so it works with a blank profile.
    """
    haystack = f"{job.normalized_title} {job.job_title} {job.description[:1500]}".casefold()
    matched: list[str] = []

    for family, tokens in (filters.get("role_families") or {}).items():
        if any(t.casefold() in haystack for t in tokens):
            matched.append(family.replace("_", " "))

    keywords = [k for k in filters.get("keywords", []) if k.casefold() in haystack]

    if not matched and not keywords:
        return 0.0, []

    # Title hits count double: a keyword in the title is a far stronger signal
    # than the same word buried in a description.
    title_low = (job.job_title or "").casefold()
    title_hits = sum(1 for k in keywords if k.casefold() in title_low)
    raw = len(matched) * 0.34 + title_hits * 0.30 + (len(keywords) - title_hits) * 0.08
    return min(1.0, raw), matched


def score(job: Job, profile: dict, filters: dict, weights: dict) -> None:
    """Populate match_score, match_category, strengths, missing_skills, concerns."""
    if config.profile_is_configured(profile):
        _score_full(job, profile, filters, weights)
    else:
        _score_relevance_only(job, filters, weights)

    if not config.profile_is_configured(profile):
        job.match_category = weights.get("blank_profile", {}).get(
            "category_label", "UNSCORED - profile not configured"
        )
    else:
        job.match_category = _category(job.match_score, weights)
    _add_verification_concerns(job)


def _score_relevance_only(job: Job, filters: dict, weights: dict) -> None:
    """Blank-profile mode: score what is knowable without a CV, and say so."""
    blank = weights.get("blank_profile", {})
    relevance, _ = role_relevance(job, filters)

    total = relevance * blank.get("role_relevance_weight", 60)

    active = set(blank.get("active_components", []))
    component_weights = weights.get("weights", {})
    if "eligibility" in active:
        total += component_weights.get("eligibility", 0) * (1.0 if job.eligible else 0.0)
    if "location" in active:
        total += component_weights.get("location", 0) * _location_fit(job, filters)

    cap = blank.get("score_cap", 60)
    job.match_score = int(min(round(total), cap))

    # Strengths and missing skills stay EMPTY. With no profile there is no
    # evidence for either, and inventing them is the failure this guards against.
    job.strengths = []
    job.missing_skills = []
    job.concerns = ["Profile not configured - score reflects role relevance only, not your CV"]


def _score_full(job: Job, profile: dict, filters: dict, weights: dict) -> None:
    """Full weighted scoring against a configured profile."""
    w = weights.get("weights", {})
    multipliers = weights.get("requirement_multipliers", {})
    have = profile_skills(profile)
    requirements = classify_requirements(job)

    strengths: list[str] = []
    missing: list[str] = []
    earned = possible = 0.0

    for skill_name, level in requirements.items():
        weight = multipliers.get(level, 0.5)
        possible += weight
        evidence = have.get(skill_name)
        if evidence in _STRONG_EVIDENCE:
            earned += weight
            strengths.append(skill_name)
        elif evidence:
            earned += weight * 0.5          # listed, but only as coursework/interest
            strengths.append(f"{skill_name} ({evidence})")
        elif level == "required":
            missing.append(skill_name)
        elif level == "preferred":
            missing.append(f"{skill_name} (preferred)")

    skill_fit = (earned / possible) if possible else 0.0

    langs_required = [s for s in job.programming_languages]
    lang_fit = (
        sum(1 for l in langs_required if l in have) / len(langs_required)
        if langs_required else 0.0
    )

    total = (
        w.get("technical_skills", 0) * skill_fit
        + w.get("programming_languages", 0) * lang_fit
        + w.get("projects", 0) * _projects_fit(job, profile)
        + w.get("education", 0) * _education_fit(job, profile)
        + w.get("eligibility", 0) * (1.0 if job.eligible else 0.0)
        + w.get("location", 0) * _location_fit(job, filters)
        + w.get("experience", 0) * _experience_fit(job, profile)
    )

    job.match_score = int(max(0, min(100, round(total))))
    job.strengths = strengths[:12]
    job.missing_skills = missing[:12]
    job.concerns = [] if job.eligible else ["Does not meet stated eligibility"]


def _projects_fit(job: Job, profile: dict) -> float:
    """Overlap between the posting's skills and the tech used in real projects."""
    from tracker import normalize

    project_tech: set[str] = set()
    for project in profile.get("projects") or []:
        tech = project.get("technologies", []) if isinstance(project, dict) else []
        project_tech |= {normalize.skill(t) for t in tech}
    project_tech.discard("")
    if not project_tech or not job.skills:
        return 0.0
    return len(project_tech & set(job.skills)) / len(set(job.skills))


def _education_fit(job: Job, profile: dict) -> float:
    """1.0 when the degree requirement is met or unstated, 0.0 when clearly not."""
    requirement = (job.degree_requirements or "").casefold()
    if not requirement:
        return 1.0
    degree = (profile.get("degree") or "").casefold()
    if not degree:
        return 0.5
    tokens = [t for t in re.split(r"\W+", degree) if len(t) > 3]
    return 1.0 if any(t in requirement for t in tokens) else 0.4


def _location_fit(job: Job, filters: dict) -> float:
    if not job.location:
        return 0.5                              # unstated is not a penalty
    allowed = [loc.casefold() for loc in filters.get("locations", [])]
    loc = job.location.casefold()
    if any(a in loc or loc in a for a in allowed):
        return 1.0
    return 1.0 if job.work_arrangement == "Remote" else 0.0


def _experience_fit(job: Job, profile: dict) -> float:
    """Internships rarely demand prior experience; penalise only explicit years."""
    text = (job.experience_requirements or "") + " " + (job.description or "")[:800]
    years = re.findall(r"(\d+)\+?\s*years?\s+(?:of\s+)?experience", text.casefold())
    if not years:
        return 1.0
    required = max(int(y) for y in years)
    if required <= 1:
        return 1.0
    have = len(profile.get("experience") or [])
    return max(0.0, min(1.0, have / required))


def _category(score_value: int | None, weights: dict) -> str:
    if score_value is None:
        return ""
    for band in weights.get("categories", []):
        if band["min"] <= score_value <= band["max"]:
            return band["label"]
    return ""


def _add_verification_concerns(job: Job) -> None:
    """Surface anything the verifier found that should temper the score."""
    if job.open_status is OpenStatus.CLOSED:
        job.concerns.insert(0, "Applications appear to be CLOSED")
    elif job.open_status is OpenStatus.UNKNOWN:
        job.concerns.append("Could not confirm the role is still open")
    if job.deadline_disagreement:
        job.concerns.append(
            f"Platform said {job.listed_deadline}, job page said {job.deadline} - check before applying"
        )
    if not job.deadline:
        job.concerns.append("No deadline found - could close at any time")


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


def assign_priority(job: Job, weights: dict, today: date | None = None) -> None:
    """Rank by urgency, not by score alone.

    A 96% match closing tomorrow must outrank a 98% match closing in four
    months, because only one of them is still actionable next week.
    """
    today = today or date.today()
    rules = weights.get("priority", {})
    thresholds = rules.get("score_thresholds", {})
    value = job.match_score or 0

    if value >= thresholds.get("high", 75):
        priority = Priority.HIGH
    elif value >= thresholds.get("medium", 55):
        priority = Priority.MEDIUM
    else:
        priority = Priority.LOW

    # Deadline proximity can promote, never demote.
    if job.deadline:
        days_left = (job.deadline - today).days
        if days_left >= 0:
            for rule in rules.get("deadline_promotions", []):
                if days_left <= rule["within_days"] and value >= rule.get("min_score", 0):
                    promoted = Priority(rule["promote_to"])
                    if _rank(promoted) > _rank(priority):
                        priority = promoted
                    break

    # A deadline in the past is not actionable, whatever it scored. Without
    # this an expired role keeps its band AND sorts to the top, because its
    # deadline is the earliest one there is.
    if job.deadline and job.deadline < today:
        priority = Priority.LOW

    demote = rules.get("demote_states", {})
    if str(job.open_status) in demote.get("open_status", []):
        priority = Priority.LOW
    if str(job.status) in demote.get("application_status", []):
        priority = Priority.LOW
    if job.status in USER_OWNED_STATUSES:
        priority = Priority.LOW
    if not job.eligible:
        priority = Priority.LOW

    # An unverifiable listing must not displace a confirmed-open one at the top.
    cap = rules.get("unknown_open_status_cap")
    if cap and job.open_status is OpenStatus.UNKNOWN and _rank(priority) > _rank(Priority(cap)):
        priority = Priority(cap)

    job.priority = priority


def _rank(priority: Priority) -> int:
    return {Priority.LOW: 0, Priority.MEDIUM: 1, Priority.HIGH: 2}[priority]
