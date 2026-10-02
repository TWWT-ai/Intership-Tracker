"""The assessment applied to the workbook - and the read-only dry run that
shows what applying it would do.

Until `apply_to_workbook: true` in config/relevance.yaml nothing here changes
the workbook, the nightly scan, verification or the calendar. The dry run
reads the last scan, the per-source snapshots, the workbook and the calendar
ledger, opens nothing on the network, and writes only its own report folder.

Which rows stay on the Internships sheet (the user's rule):

* a row with the user's status or note - always;
* firm, quoted evidence against it (relevance.STRONG_AGAINST) - hidden;
* an employer-confirmed deadline that has passed, or a posting the employer
  page shows closed (closed marker, 404/410) - hidden;
* everything else stays, including roles not yet assessed and roles whose
  only passed date is a platform-listed one ("listed deadline has passed -
  not confirmed by the employer"), which verification checks first.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import re
from collections import Counter
from pathlib import Path

from tracker import config, deadline_extract as dx, relevance
from tracker.models import USER_OWNED_STATUSES, ApplicationStatus, Job, OpenStatus

SHOW, HIDE = "SHOW", "HIDE"


def enabled() -> bool:
    return bool(config.load("relevance").get("apply_to_workbook"))


def touched(job: Job) -> bool:
    """A row the user has put something on. It never leaves the sheet."""
    return job.status in USER_OWNED_STATUSES or bool(job.notes)


# ---------------------------------------------------------------------------
# Deadlines: evidence and resolution
# ---------------------------------------------------------------------------

def aggregator(url: str) -> bool:
    skip = [str(h).casefold() for h in config.load("verification").get("eligibility_skip_hosts") or []]
    host = re.sub(r"^https?://", "", url or "").split("/")[0].casefold()
    return any(host == h or host.endswith("." + h) for h in skip)


def row_places(job: Job) -> set[str]:
    return relevance.places(job.job_title) or relevance.places(job.location)


def gather_deadlines(job: Job, text_ok: bool) -> list[dict]:
    """Every deadline candidate known for this row without opening anything:
    each platform's own deadline field, the row's posting text (only when it is
    the row's own - `text_ok`), and employer-page evidence kept from earlier
    verification. Nothing is dropped; `resolve` decides what applies."""
    evidence = [c for c in job.deadline_evidence if c.get("method") == "employer page"]
    for ref in job.sources:
        c = dx.field_candidate(ref.listed_deadline, source=ref.source, url=ref.source_url)
        if c:
            evidence.append(c.as_dict())
    if job.deadline and job.deadline_source in ("job-page", "employer-ats"):
        evidence.append(dx.Candidate(date=job.deadline.isoformat(), status=dx.OK, source=job.deadline_source,
                                     url=job.application_url, method="page (earlier verification)",
                                     employer_page=False, quote="read by an earlier verification run",
                                     order="unknown").as_dict())
    if text_ok and job.description:
        url = job.description_source.removeprefix(relevance.PAGE) or job.source_url
        source = next((r.source for r in job.sources if url in (r.source_url, r.application_url)),
                      job.primary_source)
        # A URL's display-language segment is not evidence of date order; only
        # a platform whose convention has been checked (config) is.
        order = dx.platform_order(source)
        basis = f"{source} convention (checked)" if order else ""
        evidence += [c.as_dict() for c in dx.candidates(
            relevance._clean(job.description), source=source, url=url, method="description text",
            employer_page=False, order=order, order_basis=basis, places_pattern=relevance._uk_places())]
    return evidence


def assess_deadline(job: Job, text_ok: bool) -> dx.Resolution:
    """Set the deadline findings; the Deadline column changes only to an
    employer-confirmed date, or from blank to a date the posting states."""
    evidence = gather_deadlines(job, text_ok)
    result = dx.resolve(evidence, row_places(job), job.deadline)
    job.deadline_evidence = evidence
    job.deadline_status, job.deadline_reason = result.status, result.reason
    job.deadline_confirmed, job.deadline_provisional = result.confirmed, result.provisional
    if result.deadline and result.deadline != job.deadline:
        if job.deadline is None:
            job.deadline_source = "description-text"
        elif result.confirmed:
            job.deadline_source = "employer-page"
        job.deadline = result.deadline
    return result


def closed_by_employer(job: Job) -> str:
    """Explicit closure on the employer's own posting, or ""."""
    if job.closed_evidence:
        return job.closed_evidence
    if job.open_status is OpenStatus.CLOSED:
        m = re.search(r"(\S+) returned (404|410)", job.verification_note or "")
        if m and not aggregator("https://" + m.group(1)):
            return f"{m.group(1)} returned {m.group(2)}"
    return ""


# ---------------------------------------------------------------------------
# The sheet
# ---------------------------------------------------------------------------

def decide(job: Job, today: dt.date) -> tuple[str, str]:
    """(SHOW | HIDE, reason) under the user's rule."""
    if touched(job):
        return SHOW, "kept: you set a status or note"
    against = [(name, value, reason) for name, value, reason in (
        ("role", job.role_relevance, job.role_relevance_reason),
        ("study level", job.study_level, job.study_level_reason),
        ("degree", job.degree_subject_fit, job.degree_subject_reason)) if value in relevance.STRONG_AGAINST]
    if against:
        return HIDE, "; ".join(f"{name}: {value} - {reason}" for name, value, reason in against)
    if closed := closed_by_employer(job):
        return HIDE, f"the employer's posting is closed ({closed})"
    # The employer's own date decides, even when another source listed a
    # different one (CONFLICT): a platform date never outranks it.
    if job.deadline_confirmed and job.deadline_confirmed < today:
        return HIDE, f"employer-confirmed deadline {job.deadline_confirmed} has passed"
    review = []
    if job.deadline and job.deadline < today and not job.deadline_confirmed:
        review.append("listed deadline has passed - not confirmed by the employer")
    if job.role_relevance != relevance.TECHNICAL:
        review.append(f"role {job.role_relevance or 'not assessed'}")
    if job.study_level != relevance.CURRENT_UNDERGRADUATE:
        review.append(f"study level {job.study_level or 'not assessed'}")
    if job.degree_subject_fit not in (relevance.CS_REQUIRED, relevance.CS_ACCEPTED, relevance.ANY_DISCIPLINE):
        review.append(f"degree {job.degree_subject_fit or 'not assessed'}")
    if job.deadline_status not in (dx.CONFIRMED, dx.LISTED, dx.NONE, ""):
        review.append(f"deadline {job.deadline_status}")
    return SHOW, ("REVIEW: " + "; ".join(review)) if review else "all findings positive"


def keep_on_sheet(job: Job, today: dt.date, filtering: bool) -> bool:
    """With the assessment on: the user's rule (`decide`). Off: the old rule -
    any passed deadline hides a row the user has not touched."""
    if not filtering:
        return touched(job) or not (job.deadline and job.deadline < today)
    job.sheet_decision, job.sheet_reason = decide(job, today)
    return job.sheet_decision == SHOW


def assess_row(job: Job, listings: relevance.Listings, today: dt.date) -> None:
    """All findings for one row, then its sheet decision."""
    relevance.assess(job, listings, today)
    assess_deadline(job, text_ok=not job.role_relevance_reason.startswith("not assessed"))
    job.closed_evidence = closed_by_employer(job)
    job.sheet_decision, job.sheet_reason = decide(job, today)


def assess_all(jobs: list[Job], today: dt.date | None = None,
               listings: relevance.Listings | None = None) -> None:
    today = today or dt.date.today()
    listings = listings or relevance.Listings.load()
    for job in jobs:
        assess_row(job, listings, today)


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

UNCHECKED_REASONS = {
    "no_employer_url": "No employer posting URL - only an aggregator link",
    "never_checked": "Not checked yet - queued for a later run",
    "blocked": "Site blocked or challenged - skipped, not bypassed; retried next run",
    "robots": "robots.txt disallows the page - never read",
    "application_step": "Stopped at an application step (form, login or upload)",
    "unreachable": "Page unreachable or error",
    "no_text": "No readable posting text without a browser (page built by script)",
    "over_budget": "Over this run's budget - queued for a later run",
}


def coverage(jobs: list[Job], today: dt.date) -> dict:
    """Who has been checked on the employer's own page, who has not, and why.
    A platform-listed date is not employer-confirmed."""
    from tracker import verify

    settings = config.load("verification")
    budget = int(settings.get("budget", {}).get("max_verifications", 120))
    reasons: Counter = Counter()
    checked = queued = 0
    for job in jobs:
        url = job.application_url
        if job.check_outcome in UNCHECKED_REASONS:
            reasons[job.check_outcome] += 1
            queued += job.check_outcome in ("blocked", "unreachable", "over_budget", "never_checked", "no_text")
        elif not (verify._single_posting(url) and not aggregator(url)):
            reasons["no_employer_url"] += 1
        elif job.last_verified is None and not job.last_checked:
            reasons["never_checked"] += 1
            queued += 1
        else:
            checked += 1
    return {
        "rule": "A platform-listed date is NOT employer-confirmed. Unverified roles may be settled by later "
                "runs, or may need your review.",
        "checked_on_a_page_at_least_once": checked,
        "unchecked_by_reason": {UNCHECKED_REASONS[k]: v for k, v in reasons.items()},
        "deadline_confidence": dict(Counter(job.deadline_status or "NONE" for job in jobs)),
        "verification_budget_per_run": budget,
        "estimated_runs_to_check_every_queued_row_once": math.ceil(queued / budget) if budget else None,
    }


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def _load_jobs() -> list[Job]:
    path = config.STATE_DIR / "last_run.json"
    return [Job.from_dict(d) for d in json.loads(path.read_text(encoding="utf-8")).get("jobs", [])]


def _fetch(jobs: list[Job], out_dir: Path, today: dt.date, limit: int) -> dict:
    """Read up to `limit` public posting pages into memory and the report."""
    from sources.fetch import Session
    from tracker import verify

    settings = dict(config.load("verification"))
    settings["budget"] = dict(settings.get("budget", {}), max_verifications=limit)
    chosen, _ = verify.select_assessed(jobs, settings, today)
    listings = relevance.Listings.load()
    pages = out_dir / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    outcomes: Counter = Counter()
    blocked: set[str] = set()
    with Session() as session:
        for job in chosen:
            outcome = verify.verify_assessed(session, job, settings, blocked, listings, today, browser=False)
            job.check_outcome = outcome
            outcomes[outcome] += 1
            (pages / f"{job.id}.json").write_text(json.dumps({
                "url": job.application_url, "outcome": outcome, "note": job.verification_note,
                "closed_evidence": job.closed_evidence, "checked_at": job.last_checked,
                "deadline_evidence": [c for c in job.deadline_evidence if c.get("method") == "employer page"]},
                indent=1, ensure_ascii=False), encoding="utf-8")
    return {"pages_read": len(chosen), "outcomes": dict(outcomes)}


def merge_fetched(jobs: list[Job], pages: Path) -> int:
    """Bring the employer pages a dry run read (`--fetch`) into the rows, so
    applying keeps what they showed. Returns how many rows gained evidence.

    Only employer-page evidence, closure and the check outcome are taken -
    the page text itself stays in the report."""
    records = {}
    for path in sorted(pages.glob("*.json")) if pages.exists() else []:
        try:
            records[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    merged = 0
    for job in jobs:
        rec = records.get(job.id)
        if not rec:
            continue
        page = [c for c in rec.get("deadline_evidence") or [] if c.get("method") == "employer page"]
        job.deadline_evidence = [c for c in job.deadline_evidence if c.get("method") != "employer page"] + page
        job.check_outcome = rec.get("outcome", "")
        job.last_checked = rec.get("checked_at") or job.last_checked
        closed = rec.get("closed_evidence") or ""
        if not closed and rec.get("outcome") == "checked" and re.search(
                r"returned (404|410)|no longer on its", rec.get("note") or ""):
            closed = rec["note"]
        job.closed_evidence = closed or job.closed_evidence
        merged += bool(page or closed)
    return merged


def platform_locale_evidence(jobs: list[Job]) -> dict:
    """Per platform, numeric deadline dates whose order a part above 12
    settles - the evidence to check before any platform is set day-first."""
    found: dict[str, Counter] = {}
    examples: dict[str, list] = {}
    for job in jobs:
        for c in job.deadline_evidence:
            order = c.get("order", "")
            if order in ("first part above 12", "second part above 12"):
                kind = "day-first" if order.startswith("first") else "month-first"
                found.setdefault(c["source"], Counter())[kind] += 1
                examples.setdefault(c["source"], []).append(f"{job.id}: {c['raw']}")
    ambiguous = Counter(c["source"] for job in jobs for c in job.deadline_evidence if c["status"] == dx.AMBIGUOUS)
    return {src: {"settled_by_a_part_above_12": dict(found.get(src, {})), "examples": examples.get(src, [])[:5],
                  "ambiguous_deadlines": ambiguous.get(src, 0), "configured": bool(dx.platform_order(src))}
            for src in sorted(set(found) | set(ambiguous))}


def dry_run(out_dir: Path | None = None, today: dt.date | None = None, fetch: int = 0) -> dict:
    """Assess every row of the last scan and report what applying it would
    change. Reads only; writes only `out_dir`.

    `fetch` > 0 also reads up to that many public posting pages (plain
    requests, robots.txt respected, no browser, stopping at application
    steps), in the order verification would, keeping their text in
    `out_dir/pages/` - never in the tracker's own files."""
    from scripts import calendar_sync
    from tracker import excel

    today = today or dt.date.today()
    out_dir = out_dir or config.DATA_DIR / "checkpoint" / "assess-dry-run"
    jobs = _load_jobs()
    before_deadline = {job.id: job.deadline for job in jobs}
    state = excel.read_user_state()
    on_sheet = set(state)
    for job in jobs:
        cells = state.get(job.id)
        if cells is not None:
            parsed = ApplicationStatus.parse(cells.get(excel.STATUS_COLUMN))
            job.status = parsed if parsed is not None else job.status
            job.notes = str(cells.get("Notes") or job.notes or "")
    assess_all(jobs, today)
    fetched = _fetch(jobs, out_dir, today, fetch) if fetch else {}

    would = {job.id for job in jobs if job.sheet_decision == SHOW}
    by_id = {job.id: job for job in jobs}
    leaving = sorted(on_sheet - would)
    returning = sorted(would - on_sheet)

    out_dir.mkdir(parents=True, exist_ok=True)
    columns = ["ID", "Company", "Job Title", "On sheet now", "On sheet after", "Sheet reason",
               "Role relevance", "Role reason", "Role evidence", "Study level", "Study reason", "Study evidence",
               "Degree subject", "Degree reason", "Degree evidence", "Eligible for You", "Eligibility reason",
               "Deadline before", "Deadline after", "Deadline status", "Deadline reason", "Employer-confirmed",
               "Provisional (earlier, kept apart)", "Deadline candidates", "Text source", "Your status", "Your note"]
    with (out_dir / "assessment.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        for job in sorted(jobs, key=lambda j: (j.sheet_decision, j.company.casefold(), j.job_title)):
            w.writerow([job.id, job.company, job.job_title, "Yes" if job.id in on_sheet else "No",
                        "Yes" if job.id in would else "No", job.sheet_reason,
                        job.role_relevance, job.role_relevance_reason, job.role_relevance_evidence,
                        job.study_level, job.study_level_reason, job.study_level_evidence,
                        job.degree_subject_fit, job.degree_subject_reason, job.degree_subject_evidence,
                        job.eligible_for_you, job.eligibility_reason,
                        before_deadline[job.id] or "", job.deadline or "", job.deadline_status, job.deadline_reason,
                        job.deadline_confirmed or "", job.deadline_provisional or "",
                        " || ".join(f"{c['date'] or c['status']} [{c['source']}, {c['method']}] {c['quote'][:120]}"
                                    for c in job.deadline_evidence),
                        job.assessment_text_source,
                        "" if job.status is ApplicationStatus.FOUND else str(job.status), "Yes" if job.notes else ""])

    # Calendar: which deadline days would gain or lose roles, by Tracker ID.
    # Report only - nothing is sent.
    rows = [(job.deadline, "" if job.status is ApplicationStatus.FOUND else str(job.status),
             job.company, job.job_title, job.id) for job in jobs if job.id in would]
    ledger = calendar_sync.load_ledger()["events"]
    now_ids = {day: {r[2] for r in roles} for day, roles in calendar_sync.deadline_roles(today).items()}
    then_ids = {day: {r[2] for r in roles} for day, roles in calendar_sync.deadline_roles(today, rows=rows).items()}
    calendar = []
    for day in sorted(set(now_ids) | set(then_ids)):
        gone = now_ids.get(day, set()) - then_ids.get(day, set())
        new = then_ids.get(day, set()) - now_ids.get(day, set())
        if gone or new:
            calendar.append({
                "date": day,
                "action": "create" if day not in ledger else "clear" if not then_ids.get(day) else "update",
                "roles removed": "; ".join(f"{by_id[i].company} - {by_id[i].job_title} ({i})" for i in sorted(gone)),
                "roles added": "; ".join(f"{by_id[i].company} - {by_id[i].job_title} ({i})" for i in sorted(new))})
    with (out_dir / "calendar-impact.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "action", "roles removed", "roles added"])
        w.writeheader()
        w.writerows(calendar)

    profile = config.load_profile()
    passed_listed = [i for i in would if (j := by_id[i]).deadline and j.deadline < today
                     and j.deadline_status != dx.CONFIRMED]
    summary = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "rows_assessed": len(jobs),
        "findings": {
            "role_relevance": dict(Counter(j.role_relevance for j in jobs)),
            "study_level": dict(Counter(j.study_level for j in jobs)),
            "degree_subject": dict(Counter(j.degree_subject_fit for j in jobs)),
            "eligible_for_you": dict(Counter(j.eligible_for_you or "not assessed" for j in jobs)),
            "deadline_status": dict(Counter(j.deadline_status for j in jobs)),
        },
        "sheet": {
            "on_sheet_now": len(on_sheet), "on_sheet_after": len(would),
            "leaving": [f"{i} {by_id[i].company} - {by_id[i].job_title}: {by_id[i].sheet_reason}"
                        for i in leaving if i in by_id],
            "returning": len(returning),
            "returning_by_reason": dict(Counter(
                "listed deadline passed - not confirmed by the employer"
                if (by_id[i].deadline and by_id[i].deadline < today) else "previously hidden by the title filter"
                for i in returning)),
            "kept_because_of_your_status_or_note": [
                f"{j.id} {j.company} - {j.job_title}" for j in jobs if touched(j) and any(
                    v in relevance.STRONG_AGAINST for v in (j.role_relevance, j.study_level, j.degree_subject_fit))],
            "listed_deadline_passed_not_confirmed": len(passed_listed),
        },
        "deadlines": {
            "recovered_from_posting_text": sorted(
                f"{j.id} {j.company} - {j.job_title}: {j.deadline}" for j in jobs
                if before_deadline[j.id] is None and j.deadline),
            "changed": sorted(f"{j.id}: {before_deadline[j.id]} -> {j.deadline}" for j in jobs
                              if before_deadline[j.id] and j.deadline != before_deadline[j.id]),
            "conflicts": sorted(f"{j.id} {j.company} - {j.job_title}: {j.deadline_reason}"
                                for j in jobs if j.deadline_status == dx.CONFLICT),
            "unresolved": dict(Counter(j.deadline_status for j in jobs if j.deadline_status in (
                dx.AMBIGUOUS, dx.NOT_SUPPORTED, dx.NO_YEAR, dx.MONTH_ONLY, dx.ROLLING))),
            "provisional_reminders_kept_apart": [
                f"{j.id} {j.company} - {j.job_title}: shown {j.deadline}, provisional {j.deadline_provisional}"
                for j in jobs if j.id in would and j.deadline_provisional and j.deadline_provisional >= today],
        },
        "coverage": coverage(jobs, today),
        "fetched_this_dry_run": fetched,
        "platform_locale_to_check": platform_locale_evidence(jobs),
        "calendar_days_changed": calendar,
        "profile": {"graduation_year": profile.get("graduation_year"),
                    "note_for_your_review": (config.load("relevance").get("profile_review_note") or "").strip()},
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False, default=str),
                                          encoding="utf-8")
    return summary
