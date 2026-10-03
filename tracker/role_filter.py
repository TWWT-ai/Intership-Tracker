"""The assessment applied to the workbook - and the read-only dry run that
shows what applying it would do.

Until `apply_to_workbook: true` in config/relevance.yaml nothing here changes
the workbook, the nightly scan, verification or the calendar. The dry run
reads the last scan, the per-source snapshots, the workbook and the calendar
ledger, opens nothing on the network, and writes only its own report folder.

Which rows stay on the Internships sheet (the user's rule):

* a row with the user's status or note - always;
* a posting the employer's own page shows closed - kept, at the bottom,
  Open Status CLOSED (a 404/410 is UNAVAILABLE, not closed);
* firm, quoted evidence against it (relevance.STRONG_AGAINST) - hidden,
  unless a reading in context found the posting does not clearly support it
  (`review_not_exclude` in config/relevance.yaml) - then REVIEW;
* a passed deadline never hides a row: it is kept at the bottom, EXPIRED;
* everything else stays, including roles not yet assessed.
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
    evidence = [c for c in job.deadline_evidence if kept_evidence(c)]
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


def kept_evidence(candidate: dict) -> bool:
    """Evidence that came from reading a page - kept across reassessment and
    rebuilds; feed fields and posting text are re-derived every time."""
    method = str(candidate.get("method", ""))
    return method == "employer page" or method.startswith("link audit")


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


EMPLOYER_OPEN, EMPLOYER_CLOSED = "EMPLOYER_OPEN", "EMPLOYER_CLOSED"
LISTING_OPEN, LISTING_CLOSED = "LISTING_OPEN", "LISTING_CLOSED"
UNKNOWN, UNAVAILABLE = "UNKNOWN", "UNAVAILABLE"


def closed_by_employer(job: Job) -> str:
    """Explicit closure wording on the employer's own posting, or "". A 404 or
    410 means the posting is unavailable, not that hiring has closed."""
    return job.closed_evidence if job.hiring_status in ("", EMPLOYER_CLOSED) else ""


def employer_closed(job: Job) -> bool:
    """Closed on the employer's own posting: kept at the bottom, marked CLOSED."""
    return job.hiring_status == EMPLOYER_CLOSED or bool(closed_by_employer(job))


def hiring_status(job: Job) -> tuple[str, str]:
    """(status, evidence) from what has been seen. An employer-page result
    stands, and a listing claim recorded with its page stays a claim.

    An older "closed" with no recorded wording is UNKNOWN, never closed: the
    pre-assessment verifier closed every row whose deadline had passed, and
    its note ("Deadline has passed") is not carried between runs - 221 rows
    of the 26 September scan say CLOSED with no reason, all past their date."""
    if job.hiring_evidence and (job.hiring_status in (EMPLOYER_OPEN, EMPLOYER_CLOSED, UNAVAILABLE, LISTING_OPEN,
                                                      LISTING_CLOSED) or (job.hiring_status == UNKNOWN
                                                                          and job.checked_url)):
        return job.hiring_status, job.hiring_evidence
    note = job.verification_note or ""
    if m := re.search(r"(\S+) returned (404|410)", note):
        return UNAVAILABLE, f"{m.group(1)} returned {m.group(2)} (unavailable - not evidence of closure)"
    if job.open_status is OpenStatus.OPEN:
        return LISTING_OPEN, "an earlier check read it as open; the page it read was not recorded"
    if job.open_status is OpenStatus.CLOSED:
        why = ("its deadline had passed" if "Deadline has passed" in note
               else "no wording or page was recorded" + (" and its deadline has passed - the old checker closed "
                                                         "every such row" if job.deadline else ""))
        return UNKNOWN, f"an earlier check marked it closed, but {why}; not treated as closed"
    return job.hiring_status or UNKNOWN, job.hiring_evidence or "no employer page has been read"


def open_status(job: Job) -> OpenStatus:
    """The Open Status column under the assessment: CLOSED only on the
    employer's own wording, never because a deadline passed."""
    if employer_closed(job):
        return OpenStatus.CLOSED
    if job.hiring_status == EMPLOYER_OPEN:
        return OpenStatus.OPEN
    if job.open_status is OpenStatus.CLOSED:
        return OpenStatus.UNKNOWN
    return job.open_status


#: Platform fields that hold a listing's schema.org validThrough - when the
#: listing expires, not a deadline the employer stated (config verification.yaml).
def _listing_expiry(c: dict) -> bool:
    expiry_fields = set(config.load("verification").get("listing_expiry_fields") or [])
    return ("validThrough" in str(c.get("method", "")) or str(c.get("quote", "")).startswith("validThrough")
            or (c.get("method") == "feed field" and c.get("source") in expiry_fields))


def expiry(job: Job, today: dt.date) -> tuple[str, str]:
    """("", "") when the deadline has not passed; else ("EXPIRED" | "REVIEW", why).

    EXPIRED: the shown date is a labelled application deadline (the employer's
    page, a platform's own deadline field, a deadline-worded sentence), and no
    source disagrees. REVIEW: only a listing's validThrough, sources that
    disagree, a date from a page of several vacancies, or no recorded source."""
    if not (job.deadline and job.deadline < today):
        return "", ""
    shown = job.deadline.isoformat()
    match = [c for c in job.deadline_evidence if c.get("date") == shown and c.get("status") == dx.OK]
    labelled = [c for c in match if not _listing_expiry(c)]
    if job.deadline_status == dx.UNATTRIBUTED:
        return "REVIEW", job.deadline_reason
    if job.deadline_status == dx.CONFLICT:
        return "REVIEW", f"sources disagree - {job.deadline_reason}"
    if job.deadline_status not in (dx.CONFIRMED, dx.LISTED):
        return "REVIEW", f"deadline status {job.deadline_status or 'not assessed'}: {job.deadline_reason}"
    if not match:
        return "REVIEW", "the shown date has no recorded source (existing tracker value only)"
    if not labelled:
        return "REVIEW", "the only source is a listing's validThrough field - a listing expiry, not a stated deadline"
    return "EXPIRED", "an application deadline from a labelled source has passed"


def deadline_marker(job: Job, today: dt.date) -> str:
    """The sheet's marker beside Deadline. Expiry says nothing about hiring."""
    if job.deadline and job.deadline < today:
        return ("EXPIRED (employer-confirmed date)" if job.deadline_confirmed
                else "EXPIRED (listed date, not employer-confirmed)")
    return job.deadline_status or ""


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
    found = "; ".join(f"{name}: {value} - {reason}" for name, value, reason in against)
    if employer_closed(job):
        # Closed on the employer's own words: kept, sorted to the bottom with
        # Open Status CLOSED - never removed (the user's rule).
        return SHOW, (f"CLOSED (kept at the bottom): {job.closed_evidence or job.hiring_evidence}"
                      + (f"; also {found}" if found else ""))
    if against and (why := (config.load("relevance").get("review_not_exclude") or {}).get(job.id)):
        # Read in context, the posting does not clearly support the exclusion.
        return SHOW, f"REVIEW: exclusion not clearly supported by the posting - {why} (finding: {found})"
    if against:
        return HIDE, found
    # A passed deadline never hides a row: it stays, sorted to the bottom with
    # an EXPIRED marker, and says nothing about whether hiring has closed.
    review = []
    if job.deadline and job.deadline < today:
        review.append("deadline passed - " + ("employer-confirmed" if job.deadline_confirmed
                                               else "listed date, not confirmed by the employer"))
    if job.hiring_status in (UNKNOWN, UNAVAILABLE, LISTING_OPEN, LISTING_CLOSED, ""):
        review.append(f"hiring {job.hiring_status or UNKNOWN}")
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
    job.deadline_marker = deadline_marker(job, today)
    job.sheet_decision, job.sheet_reason = decide(job, today)
    return job.sheet_decision == SHOW


def assess_row(job: Job, listings: relevance.Listings, today: dt.date) -> None:
    """All findings for one row, then its sheet decision."""
    relevance.assess(job, listings, today)
    assess_deadline(job, text_ok=not job.role_relevance_reason.startswith("not assessed"))
    job.hiring_status, job.hiring_evidence = hiring_status(job)
    job.open_status = open_status(job)
    job.deadline_marker = deadline_marker(job, today)
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
    where: Counter = Counter()
    listing_reads: Counter = Counter()
    for job in jobs:
        url = job.application_url
        if not (verify._single_posting(url) and not aggregator(url)):
            # Decided by the link, every run: no employer posting to check. A
            # listing page an audit read is counted apart - it is listing-only.
            reasons["no_employer_url"] += 1
            if job.checked_url and aggregator(job.checked_url):
                listing_reads[job.check_outcome or "read"] += 1
        elif job.check_outcome in UNCHECKED_REASONS:
            reasons[job.check_outcome] += 1
            queued += job.check_outcome in ("blocked", "unreachable", "over_budget", "never_checked", "no_text")
        elif job.last_verified is None and not job.last_checked:
            reasons["never_checked"] += 1
            queued += 1
        else:
            checked += 1
            where["employer posting" if job.checked_url and not aggregator(job.checked_url)
                  else "earlier check, page not recorded"] += 1
    return {
        "rule": "A platform-listed date is NOT employer-confirmed. Unverified roles may be settled by later "
                "runs, or may need your review.",
        "checked_on_a_page_at_least_once": checked,
        "checked_where": dict(where),
        "aggregator_only_rows_whose_listing_page_was_read": dict(listing_reads),
        "unchecked_by_reason": {UNCHECKED_REASONS[k]: v for k, v in reasons.items()},
        "deadline_confidence": dict(Counter(job.deadline_status or "NONE" for job in jobs)),
        "hiring_status": dict(Counter(job.hiring_status or "UNKNOWN" for job in jobs)),
        "hiring_rule": "Employer-confirmed OPEN/CLOSED only from the employer's own posting; a 404/410 is "
                       "UNAVAILABLE and a passed deadline is EXPIRED - neither means closed.",
        "verification_budget_per_run": budget,
        "estimated_runs_to_check_every_queued_row_once": math.ceil(queued / budget) if budget else None,
    }


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def audit_date(path: Path | None) -> str:
    """The date the audit checked its links (its summary), else the CSV's date."""
    if not path:
        return ""
    summary = path.with_name(path.stem + "-summary.json")
    try:
        return json.loads(summary.read_text(encoding="utf-8"))["checked_at"]
    except (OSError, ValueError, KeyError):
        return dt.date.fromtimestamp(path.stat().st_mtime).isoformat()


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
                "checked_url": job.checked_url, "hiring_status": job.hiring_status,
                "hiring_evidence": job.hiring_evidence,
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
        job.checked_url = rec.get("checked_url") or rec.get("url") or job.checked_url
        if rec.get("hiring_status"):
            job.hiring_status, job.hiring_evidence = rec["hiring_status"], rec.get("hiring_evidence", "")
        closed = rec.get("closed_evidence") or ""
        if re.search(r"returned (404|410)|no longer on its", closed):
            closed = ""                          # an older record: unavailable, not closed
        if not rec.get("hiring_status") and re.search(r"returned (404|410)|no longer on its", rec.get("note") or ""):
            job.hiring_status, job.hiring_evidence = UNAVAILABLE, rec["note"]
        job.closed_evidence = closed or job.closed_evidence
        merged += bool(page or closed)
    return merged


_AUDIT_OUTCOME = {"posting": "checked", "closed": "checked", "blocked": "blocked", "robots": "robots",
                  "no_text": "no_text", "unreachable": "unreachable", "application_step": "application_step"}


def merge_audit(jobs: list[Job], path: Path, checked_at: str) -> dict:
    """Bring a link audit's results (data/checkpoint/link-audit-*/role-link-audit.csv)
    in as evidence, so what it found is kept rather than re-guessed.

    Employer-posting results become employer evidence; aggregator results stay
    listing-only claims. Deadline snippets are re-read with the current
    extractor - the audit's own candidate list predates its fixes - and a
    structured validThrough is kept as a structured field of the page it came
    from. A newer check of the same row always wins. Returns counts."""
    rows = {r["Tracker ID"]: r for r in csv.DictReader(path.open(encoding="utf-8-sig"))}
    when = f"{checked_at}T00:00:00"
    counts: Counter = Counter()
    for job in jobs:
        r = rows.get(job.id)
        if not r:
            continue
        counts["rows"] += 1
        url = r.get("Application URL") or ""
        employer = r.get("Link type") == "employer posting" and relevance.own_page(job, url)
        host = re.sub(r"^https?://", "", url).split("/")[0]
        source = f"{host} {'employer page' if employer else 'listing page'}"
        found = []
        for snippet in dict.fromkeys(s.strip() for s in (r.get("Deadline evidence") or "").split(" | ") if s.strip()):
            if m := re.match(r"Structured posting field validThrough:\s*(\d{4}-\d{2}-\d{2})", snippet):
                found.append(dx.Candidate(date=m.group(1), status=dx.OK, source=source, url=url,
                                          method="link audit: structured field validThrough", employer_page=employer,
                                          quote=snippet, order="structured field").as_dict())
                continue
            found += [c.as_dict() for c in dx.candidates(
                snippet, source=source, url=url, method="link audit: page text", employer_page=employer,
                places_pattern=relevance._uk_places())]
        seen = set()
        found = [c for c in found if (key := (c["date"], c["status"], c["method"])) not in seen and not seen.add(key)]
        job.deadline_evidence = [c for c in job.deadline_evidence if not str(c.get("method", "")).startswith(
            "link audit")] + found
        counts["rows_with_deadline_evidence"] += bool(found)
        if job.last_checked and job.last_checked > when:
            continue                                   # a newer check of this row stands
        job.last_checked, job.checked_url = when, url
        job.check_outcome = _AUDIT_OUTCOME.get(r.get("Page result", ""), job.check_outcome)
        claimed, note = r.get("Open status checked", ""), (r.get("Status evidence") or r.get("Check note") or "")
        stamp = f"link audit {checked_at}: {note or r.get('Page result', '')} ({url})"
        if employer and claimed == "OPEN":
            job.hiring_status, job.hiring_evidence = EMPLOYER_OPEN, stamp
        elif employer and claimed == "CLOSED":
            markers = [m for m in config.load("verification").get("closed_markers", []) if m in note.casefold()]
            if markers:
                job.hiring_status, job.hiring_evidence = EMPLOYER_CLOSED, stamp
                job.closed_evidence = stamp
            else:
                # Closed, says the audit, but without the employer's words: not
                # enough to act on. Kept for review with the reason.
                job.hiring_status = UNKNOWN
                job.hiring_evidence = f"{stamp} - the audit reported CLOSED without quoting the wording; review"
        elif claimed == "UNAVAILABLE":
            job.hiring_status, job.hiring_evidence = (UNAVAILABLE if employer else UNKNOWN), stamp
        elif claimed in ("LISTING_OPEN", "LISTING_CLOSED"):
            job.hiring_status, job.hiring_evidence = claimed, stamp
        elif job.hiring_status not in (EMPLOYER_OPEN, EMPLOYER_CLOSED, UNAVAILABLE):
            job.hiring_status, job.hiring_evidence = UNKNOWN, stamp
        counts[f"hiring {job.hiring_status}"] += 1
    return dict(counts)


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


def dry_run(out_dir: Path | None = None, today: dt.date | None = None, fetch: int = 0,
            audit: Path | None = None, pages: Path | None = None) -> dict:
    """Assess every row of the last scan and report what applying it would
    change. Reads only; writes only `out_dir`.

    `fetch` > 0 also reads up to that many public posting pages (plain
    requests, robots.txt respected, no browser, stopping at application
    steps), in the order verification would, keeping their text in
    `out_dir/pages/` - never in the tracker's own files. `pages` reads earlier
    pages from another report folder, so a report can be regenerated into a new
    folder without touching the old one."""
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
    # Pages earlier dry runs read stay in the report folder: they count now,
    # and the next --fetch resumes with what has not been read.
    earlier = merge_fetched(jobs, pages or out_dir / "pages")
    audit_counts = merge_audit(jobs, audit, audit_date(audit)) if audit else {}
    assess_all(jobs, today)
    fetched = _fetch(jobs, out_dir, today, fetch) if fetch else {}
    fetched["pages_from_earlier_dry_runs"] = earlier

    would = {job.id for job in jobs if job.sheet_decision == SHOW}
    by_id = {job.id: job for job in jobs}
    leaving = sorted(on_sheet - would)
    returning = sorted(would - on_sheet)

    out_dir.mkdir(parents=True, exist_ok=True)
    columns = ["ID", "Company", "Job Title", "On sheet now", "On sheet after", "Sheet reason",
               "Role relevance", "Role reason", "Role evidence", "Study level", "Study reason", "Study evidence",
               "Degree subject", "Degree reason", "Degree evidence", "Eligible for You", "Eligibility reason",
               "Deadline before", "Deadline after", "Deadline status", "Deadline reason", "Employer-confirmed",
               "Provisional (earlier, kept apart)", "Deadline marker", "Deadline candidates", "Hiring status",
               "Hiring evidence", "Last checked", "Checked URL", "Check outcome", "Text source", "Your status",
               "Your note"]
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
                        job.deadline_confirmed or "", job.deadline_provisional or "", job.deadline_marker,
                        " || ".join(f"{c['date'] or c['status']} [{c['source']}, {c['method']}] {c['quote'][:120]}"
                                    for c in job.deadline_evidence),
                        job.hiring_status, job.hiring_evidence, job.last_checked, job.checked_url, job.check_outcome,
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
                "expired - kept, sorted to the bottom with an EXPIRED marker"
                if (by_id[i].deadline and by_id[i].deadline < today) else "previously hidden by the title filter"
                for i in returning)),
            "expired_rows_kept_at_bottom": sum(1 for i in would if (j := by_id[i]).deadline and j.deadline < today),
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
        "link_audit": {"path": str(audit), "checked_at": audit_date(audit), **audit_counts} if audit else {},
        "platform_locale_to_check": platform_locale_evidence(jobs),
        "calendar_days_changed": calendar,
        "profile": {"graduation_year": profile.get("graduation_year"),
                    "note_for_your_review": (config.load("relevance").get("profile_review_note") or "").strip()},
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False, default=str),
                                          encoding="utf-8")
    return summary
