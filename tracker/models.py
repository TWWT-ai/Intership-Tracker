"""The standard internal job schema.

Every source adapter converts its platform's payload into a `Job`. Nothing
downstream of `sources/` knows which platform a listing came from.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from enum import StrEnum
from typing import Any


class OpenStatus(StrEnum):
    """Whether the role is still accepting applications.

    UNKNOWN is a real answer, not a placeholder: verification ran and could not
    establish the state. It is displayed as UNKNOWN rather than being quietly
    rendered as OPEN.
    """

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


class Priority(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ApplicationStatus(StrEnum):
    """User-owned. The pipeline may write FOUND on a brand-new row and must
    never change the value again after that - with one exception: the email
    status sync (tracker/status_sync.py) may move a row forward on explicit
    recruitment-email evidence, never backwards, never past Offer, Rejected or
    Withdrawn, and never on a row the user edited after it.

    The values are what the user sees in the Application Status dropdown.
    FOUND means "not applied" and is shown as a blank cell.
    """

    FOUND = "FOUND"
    APPLIED = "Application Submitted"
    OA = "Online Assessment"
    VIDEO_INTERVIEW = "Video Interview"
    SECOND_STAGE = "Second Stage"
    ASSESSMENT_CENTRE = "Assessment Centre"
    OFFER = "Offer"
    REJECTED = "Rejected"
    WITHDRAWN = "Withdrawn"

    @classmethod
    def parse(cls, raw: Any) -> "ApplicationStatus | None":
        """Read a status cell as the user left it.

        Accepts the dropdown labels in any case, the codes older workbooks used
        (APPLIED, OA, REJECTED...) and Trackr's own wording, so upgrading the
        vocabulary never drops a status already set. Returns None for text it
        does not recognise rather than guessing a stage.
        """
        text = str(raw or "").strip().casefold()
        if not text:
            return cls.FOUND
        for member in cls:
            if text in (member.value.casefold(), member.name.casefold()):
                return member
        return _STATUS_ALIASES.get(text)


#: Wording from older workbooks and from Trackr that maps to exactly one stage.
_STATUS_ALIASES = {
    "not applied": ApplicationStatus.FOUND,
    "submitted": ApplicationStatus.APPLIED,
    "interview": ApplicationStatus.SECOND_STAGE,
}

#: Once a row reaches any of these the user has acted on it, so the pipeline
#: treats its status as untouchable. See tracker/excel.py.
USER_OWNED_STATUSES = frozenset(set(ApplicationStatus) - {ApplicationStatus.FOUND})


class ChangeType(StrEnum):
    NEW = "NEW"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"
    EXPIRED = "EXPIRED"
    REMOVED = "REMOVED"


@dataclass
class SourceRef:
    """One platform's view of a job.

    A job found on all three platforms keeps three of these on a single `Job`,
    which is what lets the workbook show one row while preserving every URL.
    """

    source: str = ""                      # trackr | gradcracker | brightnetwork | careers
    source_url: str = ""                  # the listing page on that platform
    application_url: str = ""             # where you actually apply
    external_id: str = ""
    listed_deadline: date | None = None   # the deadline THAT PLATFORM claimed
    first_seen: date | None = None
    last_seen: date | None = None
    via: str = ""                         # sub-platform, e.g. "Greenhouse" for careers

    def completeness(self) -> int:
        """How much this source contributes; picks the primary source.

        A source supplying both a real deadline and an application URL beats one
        supplying neither, whatever the configured preference order says.
        """
        return (
            (2 if self.listed_deadline else 0)
            + (2 if self.application_url else 0)
            + (1 if self.source_url else 0)
        )


@dataclass
class Job:
    """A single real internship, possibly seen on several platforms."""

    # identity
    id: str = ""                      # INT-000001, stable for the job's lifetime
    company: str = ""
    job_title: str = ""

    # normalized forms: used for dedupe and matching, never displayed
    normalized_company: str = ""
    normalized_title: str = ""
    raw_title: str = ""

    # role detail
    location: str = ""
    work_arrangement: str = ""        # Remote | Hybrid | On-site | ""
    work_type: str = ""
    salary: str = ""
    duration: str = ""
    start_date: date | None = None
    # When applications opened, as the posting or platform states it. Never the
    # day this tool first saw the row - that is date_found.
    date_posted: date | None = None

    # deadline, and the evidence behind it
    deadline: date | None = None          # best available, verified where possible
    listed_deadline: date | None = None   # what the aggregator claimed
    deadline_source: str = "none"         # job-page | employer-ats | <platform>-listing | none
    deadline_disagreement: bool = False
    open_status: OpenStatus = OpenStatus.UNKNOWN
    last_verified: datetime | None = None
    verification_note: str = ""

    # requirements
    degree_requirements: str = ""
    year_of_study: str = ""
    expected_graduation: str = ""     # years the posting admits, e.g. "2029"; never assumed
    graduation_basis: str = ""        # "stated" by the employer | "derived" from a year of study
    # what the posting says about itself and who may apply (tracker/eligibility.py)
    university_year: str = ""         # the eligibility sentence, as written
    degree_subject: str = ""
    programme_dates: str = ""
    right_to_work: str = ""
    visa_sponsorship: str = ""        # "Yes" | "No" | "" (not stated)
    first_year_eligible: str = ""
    second_year_eligible: str = ""
    placement_year_eligible: str = ""
    eligible_for_you: str = ""        # "Yes" | "No" | "REQUIRES VERIFICATION"
    eligibility_reason: str = ""
    classification_basis: str = ""    # what decided work_type
    experience_requirements: str = ""
    description: str = ""
    # The listing URL (one of `sources`) the description was read from. The
    # role filter only trusts text it can tie to this row's own posting.
    description_source: str = ""
    skills: list[str] = field(default_factory=list)
    programming_languages: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)

    # application materials, as the platform states them: "Yes" | "No" |
    # "Optional" | "" (not stated - never assumed either way)
    cv: str = ""
    written_answers: str = ""
    cover_letter: str = ""

    # provenance
    sources: list[SourceRef] = field(default_factory=list)
    primary_source: str = ""
    source_url: str = ""
    application_url: str = ""

    # lifecycle
    date_found: date | None = None
    last_updated: date | None = None
    status: ApplicationStatus = ApplicationStatus.FOUND
    change_type: ChangeType = ChangeType.NEW

    # scoring
    match_score: int | None = None
    match_category: str = ""
    priority: Priority = Priority.LOW
    strengths: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    eligible: bool = True
    notes: str = ""                   # user-owned; the pipeline never fills it
    # The email behind an automatic status change, rendered from the status-sync
    # ledger on every write (tracker/status_sync.py). Never user input.
    status_evidence: str = ""

    # Assessment (tracker/relevance.py, deadline_extract.py, role_filter.py):
    # separate findings, each with its reason and quotes from the posting.
    role_relevance: str = ""          # TECHNICAL | NOT_TECHNICAL | UNCLEAR
    role_relevance_reason: str = ""
    role_relevance_evidence: str = ""
    study_level: str = ""             # CURRENT_UNDERGRADUATE | GRADUATE_ONLY | POSTGRADUATE_ONLY | ...
    study_level_reason: str = ""
    study_level_evidence: str = ""
    degree_subject_fit: str = ""      # CS_REQUIRED | CS_ACCEPTED | ANY_DISCIPLINE | EQUIVALENCE_UNCLEAR | ...
    degree_subject_reason: str = ""
    degree_subject_evidence: str = ""
    assessment_text_source: str = ""  # which listing the text came from, and any not used
    # Deadline evidence. `deadline` is what the sheet shows: the employer's date
    # where confirmed; `deadline_provisional` is an earlier conflicting date,
    # kept apart and never written over it.
    deadline_status: str = ""         # CONFIRMED | LISTED | CONFLICT | AMBIGUOUS | ... | NONE
    deadline_reason: str = ""
    deadline_confirmed: date | None = None
    deadline_provisional: date | None = None
    deadline_evidence: list[dict] = field(default_factory=list)
    # Explicit closure seen on the employer's own posting (closed marker, 404/410).
    closed_evidence: str = ""
    # Whether the row stays on the Internships sheet, and why.
    sheet_decision: str = ""          # SHOW | HIDE
    sheet_reason: str = ""
    # Verification coverage, carried between runs.
    last_checked: str = ""
    check_outcome: str = ""
    checks_attempted: int = 0

    def fingerprint(self) -> str:
        """Stable content hash used to look up this job's permanent ID.

        Built only from fields that *identify* a role, not ones that describe
        it, so a company retitling "Software Engineer Intern" to "Software
        Engineering Intern" does not mint a new ID and duplicate the row.
        Deadline is excluded for the same reason: it moves.
        """
        basis = "|".join(
            [
                self.normalized_company or self.company.casefold(),
                self.normalized_title or self.job_title.casefold(),
                (self.location or "").casefold(),
            ]
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    def source_names(self) -> list[str]:
        return sorted({s.source for s in self.sources})

    def get_source(self, name: str) -> SourceRef | None:
        return next((s for s in self.sources if s.source == name), None)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict, for snapshots and run reports."""
        return {k: _encode(v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        """Inverse of to_dict. Unknown keys are ignored, so a snapshot written
        by an older version stays loadable after the schema grows a field."""
        known = {f.name for f in fields(cls)}
        dates = {"start_date", "date_posted", "deadline", "listed_deadline", "date_found", "last_updated",
                 "deadline_confirmed", "deadline_provisional"}
        enums = {"open_status": OpenStatus, "priority": Priority,
                 "status": ApplicationStatus, "change_type": ChangeType}
        kwargs: dict[str, Any] = {}

        for key, value in data.items():
            if key not in known:
                continue
            if key == "sources":
                kwargs[key] = [_source_from_dict(s) for s in (value or [])]
            elif key == "last_verified":
                kwargs[key] = _parse_datetime(value)
            elif key in dates:
                kwargs[key] = parse_iso_date(value)
            elif key == "status":
                # Snapshots written before the status wording changed say "APPLIED".
                kwargs[key] = ApplicationStatus.parse(value) or ApplicationStatus.FOUND
            elif key in enums:
                kwargs[key] = enums[key](value) if value else next(iter(enums[key]))
            else:
                kwargs[key] = value

        return cls(**kwargs)


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


def _source_from_dict(s: dict[str, Any]) -> SourceRef:
    return SourceRef(
        source=s.get("source", ""),
        source_url=s.get("source_url", ""),
        application_url=s.get("application_url", ""),
        external_id=s.get("external_id", ""),
        listed_deadline=parse_iso_date(s.get("listed_deadline")),
        first_seen=parse_iso_date(s.get("first_seen")),
        last_seen=parse_iso_date(s.get("last_seen")),
        via=s.get("via", ""),
    )


def parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
