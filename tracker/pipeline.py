"""The daily orchestration.

    collect -> normalize -> deduplicate -> verify -> eligibility
            -> match -> priority -> excel -> deadlines -> report

A failure in one source never stops the run: it is logged, the last good
snapshot for that source is reused, and the remaining sources still reach the
workbook. That is the difference between a pipeline you can schedule and one
you have to babysit.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from sources import brightnetwork, careers, gradcracker, trackr, higherin, targetjobs
from sources.fetch import Session, SourceBlocked, SourceError
from tracker import config, dedupe, deadlines, eligibility, excel, graduation, match, programme, role_filter, verify
from tracker.models import ApplicationStatus, ChangeType, Job, OpenStatus, Priority

log = logging.getLogger("tracker")

ADAPTERS = {
    "trackr": trackr,
    "gradcracker": gradcracker,
    "brightnetwork": brightnetwork,
    "higherin": higherin,
    "targetjobs": targetjobs,
    # Last on purpose: it discovers boards from the other sources' fresh links.
    "careers": careers,
}


def setup_logging(verbose: bool = False) -> Path:
    """Log to console and to a dated file. Returns the log path."""
    config.ensure_dirs()
    log_path = config.LOG_DIR / f"{date.today().isoformat()}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    # The Windows console is cp1252 by default, so anything non-ASCII would
    # raise mid-run. Errors are replaced rather than allowed to kill the run.
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(message)s"))
    stream.setLevel(logging.DEBUG if verbose else logging.WARNING)
    root.addHandler(stream)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    return log_path


def echo(message: str = "") -> None:
    """Print without ever dying on a console that cannot encode a character."""
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"))


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def collect(session: Session, only: str | None = None) -> tuple[list[Job], list[str], list[str]]:
    """Run every enabled adapter. Returns (jobs, sources_ok, sources_failed)."""
    sources_config = config.load("sources").get("sources", {})
    jobs: list[Job] = []
    ok: list[str] = []
    failed: list[str] = []

    for key, settings in sources_config.items():
        if only and key != only:
            continue
        if not settings.get("enabled", True) and not only:
            log.info("[%s] disabled in config", key)
            continue

        adapter = ADAPTERS.get(key)
        if adapter is None:
            log.warning("[%s] no adapter registered", key)
            continue

        label = settings.get("display_name", key)
        try:
            found = adapter.search_jobs(session, settings)
        except SourceBlocked as exc:
            failed.append(key)
            echo(f"[{label}] BLOCKED - {exc}")
            log.error("[%s] blocked: %s", key, exc)
            found = _load_snapshot(key)
            if found:
                echo(f"[{label}] reusing {len(found)} listings from the last successful run")
                jobs.extend(found)
            continue
        except (SourceError, Exception) as exc:
            failed.append(key)
            echo(f"[{label}] FAILED - {exc}")
            # Console already carries the message; the traceback goes to the
            # log file only, so a handled failure does not look like a crash.
            log.error("[%s] failed: %s", key, exc)
            log.debug("[%s] traceback", key, exc_info=True)
            found = _load_snapshot(key)
            if found:
                echo(f"[{label}] reusing {len(found)} listings from the last successful run")
                jobs.extend(found)
            continue

        ok.append(key)
        echo(f"[{label}] {len(found)} listings found")
        _save_snapshot(key, found)
        jobs.extend(found)

    return jobs, ok, failed


def _snapshot_path(source: str) -> Path:
    return config.SNAPSHOT_DIR / f"{source}.json"


def _save_snapshot(source: str, jobs: list[Job]) -> None:
    """Keep the last good result so a later failure degrades instead of losing data."""
    config.ensure_dirs()
    path = _snapshot_path(source)
    payload = {"saved_at": datetime.now().isoformat(), "jobs": [j.to_dict() for j in jobs]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(path)


def _load_snapshot(source: str) -> list[Job]:
    path = _snapshot_path(source)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [Job.from_dict(d) for d in payload.get("jobs", [])]
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        log.warning("Could not read snapshot for %s: %s", source, exc)
        return []


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run_daily(only: str | None = None, skip_verify: bool = False, dry_run: bool = False) -> dict:
    """The full pipeline. Safe to run repeatedly: it never duplicates a row."""
    started = datetime.now()
    filters = config.load("search_filters")
    weights = config.load("matching_weights")
    profile = config.load_profile()

    echo(f"[{started:%H:%M}] Starting internship search")
    echo("")

    with Session() as session:
        raw, ok, failed = collect(session, only=only)
        echo("")

        if not raw:
            echo("No listings collected. Nothing to update.")
            return _summary(started, ok, failed, [], None, 0)

        echo(f"[Normalize] {len(raw)} valid listings")

        merged, duplicate_count = dedupe.deduplicate(raw)
        echo(f"[Deduplicate] {duplicate_count} duplicate listings merged -> {len(merged)} unique")

        # Restore prior state BEFORE scoring, so priority reflects what the user
        # has already done and verification can skip recently-checked rows.
        dedupe.assign_ids(merged)
        last_run = _load_previous(merged)
        previous = _match_previous(merged, last_run)
        _restore_state(merged, previous)
        # A platform that states who may apply settles the programme type
        # before scoring; the employer's page can still settle it in verify.
        for job in merged:
            programme.refine(job, f"{job.year_of_study}. {job.description}")
            graduation.apply(job, f"{job.year_of_study}. {job.description}", profile)
            eligibility.assess(job, job.description, profile)

        # The assessment reads each row's own posting text; off until approved.
        from tracker import role_filter
        if role_filter.enabled():
            role_filter.assess_all(merged)
            shown = sum(1 for j in merged if j.sheet_decision == role_filter.SHOW)
            echo(f"[Assessment] {len(merged)} roles assessed - {shown} shown, {len(merged) - shown} hidden "
                 "on firm evidence")

        # Provisional scoring so the verification budget can be spent on the
        # rows that matter most.
        _score_all(merged, profile, filters, weights)

        stats = None
        if not skip_verify:
            stats = verify.verify_all(session, merged)
            echo(f"[Verify] {stats.summary()}")

    eligible = sum(1 for j in merged if j.eligible)
    echo(f"[Eligibility] {eligible} eligible/relevant listings")

    # Re-score now that verification has settled deadlines and open status.
    _score_all(merged, profile, filters, weights)
    echo(f"[Matcher] {len(merged)} jobs analyzed")

    changes = _classify_changes(merged, previous, len(last_run))
    echo("")

    if dry_run:
        echo("[Dry run] Excel not written")
        _print_priorities(merged)
        return _summary(started, ok, failed, merged, stats, duplicate_count, changes)

    summary = _summary(started, ok, failed, merged, stats, duplicate_count, changes)
    written = excel.write(merged, run_summary=summary)

    echo(f"[Excel] {changes[ChangeType.NEW]} new, {changes[ChangeType.UPDATED]} updated, "
         f"{changes[ChangeType.EXPIRED]} expired")
    if written != config.WORKBOOK:
        echo(f"[Excel] WARNING: {config.WORKBOOK.name} is open in Excel. Wrote {written.name} instead.")
    else:
        echo(f"[Excel] {written}")

    echo("")
    _print_priorities(merged)

    urgent = deadlines.urgent(merged, within_days=7)
    echo("")
    echo(f"[Deadlines] {len(urgent)} applications require attention within 7 days")

    if not config.profile_is_configured(profile):
        echo("")
        echo("NOTE: data/profile.json is not configured, so scores reflect role relevance")
        echo("      only and are capped at 60. Strengths and missing skills are left empty")
        echo("      rather than guessed. Fill it in to switch on full CV matching.")

    echo("")
    echo(f"Search completed in {(datetime.now() - started).seconds}s.")
    return summary


def _score_all(jobs: list[Job], profile: dict, filters: dict, weights: dict) -> None:
    for job in jobs:
        job.eligible, reasons = match.check_eligibility(job, profile, filters)
        match.score(job, profile, filters, weights)
        if reasons:
            job.concerns.extend(reasons)
        match.assign_priority(job, weights)


def _load_previous(jobs: list[Job]) -> list[Job]:
    """Last run's merged view.

    Read from the snapshot rather than the workbook because it carries the full
    Job, including last_verified, which the workbook only partly represents.
    """
    path = config.STATE_DIR / "last_run.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [Job.from_dict(d) for d in payload.get("jobs", [])]


def _match_previous(jobs: list[Job], previous: list[Job]) -> dict[str, Job]:
    """{job id: last run's record of the same listing}.

    Matched by id first, then by platform listing id, and kept only when the
    two records are one listing. A record from before the season fix may hold
    one round of a programme under an id that now belongs to another round;
    carrying its verified status across would show the 2027 round as closed
    because the 2026 round is.
    """
    by_id: dict[str, Job] = {}
    by_ref: dict[str, Job] = {}
    for old in previous:
        by_id.setdefault(old.id, old)
        for key in dedupe.ref_keys(old):
            by_ref.setdefault(key, old)

    matched: dict[str, Job] = {}
    used: set[int] = set()
    for job in jobs:
        candidates = [by_id.get(job.id)] + [by_ref.get(k) for k in dedupe.ref_keys(job)]
        for old in candidates:
            if old is not None and id(old) not in used and dedupe.is_same_listing(job, old):
                matched[job.id] = old
                used.add(id(old))
                break
    return matched


def _save_previous(jobs: list[Job]) -> None:
    path = config.STATE_DIR / "last_run.json"
    payload = {
        "saved_at": datetime.now().isoformat(),
        "jobs": [{**j.to_dict(), "_fingerprint": j.fingerprint()} for j in jobs],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(path)


#: Deadline sources that come from reading a real page. Only these carry
#: forward: a listed deadline is re-read from its listing every run.
_VERIFIED_DEADLINE_SOURCES = ("job-page", "employer-ats", "employer-page")


def _restore_state(jobs: list[Job], previous: dict[str, Job]) -> None:
    """Carry forward everything that must survive between runs.

    `previous` is `_match_previous` output. Ids are not touched here: they come
    from `dedupe.assign_ids` alone, which keeps them unique.
    """
    workbook_state = excel.read_user_state()

    for job in jobs:
        old = previous.get(job.id)
        if old is not None:
            job.date_found = old.date_found or job.date_found
            job.date_posted = job.date_posted or old.date_posted
            job.last_verified = old.last_verified
            # A carried CLOSED needs the employer's recorded words. An older
            # one without them came from a passed deadline or a 404 and is
            # UNKNOWN (role_filter.open_status); expiry is not closure.
            job.open_status = role_filter.open_status(old)
            # A deadline with no page behind it may have been merged in from
            # another round of the programme, so it is not resurrected.
            if (old.deadline and not job.deadline
                    and old.deadline_source in _VERIFIED_DEADLINE_SOURCES):
                job.deadline = old.deadline
                job.deadline_source = old.deadline_source
            if old.notes and not job.notes:
                job.notes = old.notes
            programme.carry(job, old)
            graduation.carry(job, old)
            eligibility.carry(job, old)
            # Assessment evidence from pages read on earlier nights: a page is
            # not re-read every run, so what it said must travel with the row.
            job.deadline_evidence = [c for c in old.deadline_evidence if c.get("method") == "employer page"
                                     or str(c.get("method", "")).startswith("link audit")]
            job.closed_evidence = old.closed_evidence
            # The hiring finding and the page behind it travel too: without
            # them an old CLOSED loses its reason overnight and cannot be told
            # apart from the employer's own word.
            job.hiring_status, job.hiring_evidence = old.hiring_status, old.hiring_evidence
            job.checked_url = old.checked_url
            job.last_checked, job.check_outcome = old.last_checked, old.check_outcome
            job.checks_attempted = old.checks_attempted
            if old.description_source.startswith("page:") and not job.description_source.startswith("page:"):
                job.description, job.description_source = old.description, old.description_source

        # The workbook is authoritative for status: the user may have edited it
        # since the last run.
        saved = workbook_state.get(job.id)
        if saved and excel.STATUS_COLUMN in saved:
            status = ApplicationStatus.parse(saved[excel.STATUS_COLUMN])
            if status is not None:
                job.status = status


def _classify_changes(jobs: list[Job], previous: dict[str, Job],
                      previous_count: int | None = None) -> dict[ChangeType, int]:
    """Count NEW / UPDATED / EXPIRED against last run's matched records.

    `previous_count` is how many records last run held; the ones no job
    matched are the REMOVED listings.
    """
    counts = {change: 0 for change in ChangeType}
    today = date.today()

    for job in jobs:
        old = previous.get(job.id)
        # Expiry is checked first: a job found for the first time whose deadline
        # has already passed is EXPIRED, not NEW. Calling it new would put a
        # dead listing at the top of a "what's new today" report.
        if job.open_status is OpenStatus.CLOSED or (job.deadline and job.deadline < today):
            job.change_type = ChangeType.EXPIRED
        elif old is None:
            job.change_type = ChangeType.NEW
        elif _has_changed(job, old):
            job.change_type = ChangeType.UPDATED
            job.last_updated = today
        else:
            job.change_type = ChangeType.UNCHANGED
            job.last_updated = old.last_updated or today
        counts[job.change_type] += 1

    if previous_count is None:
        previous_count = len(previous)
    counts[ChangeType.REMOVED] = max(previous_count - len(previous), 0)

    _save_previous(jobs)
    return counts


def _has_changed(job: Job, old: Job) -> bool:
    return any(
        getattr(job, field) != getattr(old, field)
        for field in ("deadline", "open_status", "salary", "location", "work_type",
                      "duration", "application_url", "match_score")
    )


def _print_priorities(jobs: list[Job]) -> None:
    counts = {p: sum(1 for j in jobs if j.priority is p) for p in Priority}
    echo("[Priority]")
    for priority in (Priority.HIGH, Priority.MEDIUM, Priority.LOW):
        echo(f"  {priority}: {counts[priority]}")


def _summary(
    started: datetime,
    ok: list[str],
    failed: list[str],
    jobs: list[Job],
    stats,
    duplicates: int,
    changes: dict | None = None,
) -> dict:
    changes = changes or {}
    return {
        "started": started.isoformat(),
        "sources_ok": ok,
        "sources_failed": failed,
        "collected": len(jobs) + duplicates,
        "merged": duplicates,
        "verified": stats.checked if stats else 0,
        "verification": stats.as_dict() if stats else {},
        "new": changes.get(ChangeType.NEW, 0),
        "updated": changes.get(ChangeType.UPDATED, 0),
        "expired": changes.get(ChangeType.EXPIRED, 0),
        "removed": changes.get(ChangeType.REMOVED, 0),
        "high": sum(1 for j in jobs if j.priority is Priority.HIGH),
        "medium": sum(1 for j in jobs if j.priority is Priority.MEDIUM),
        "low": sum(1 for j in jobs if j.priority is Priority.LOW),
        "notes": "; ".join(filter(None, [("blocked/failed: " + ", ".join(failed)) if failed else "",
                                         _coverage_note(jobs, stats)])),
    }


def _coverage_note(jobs: list[Job], stats) -> str:
    """The Runs sheet line saying how much the employer pages confirmed, and
    what is still unchecked and why. Only once the assessment is on."""
    from tracker import role_filter

    if not role_filter.enabled() or not jobs:
        return ""
    from collections import Counter

    confirmed = sum(1 for j in jobs if j.deadline_status == "CONFIRMED")
    listed = sum(1 for j in jobs if j.deadline_status == "LISTED")
    outcomes = (stats.outcomes if stats else {}) or {}
    unchecked = Counter(j.check_outcome for j in jobs if j.check_outcome and j.check_outcome != "checked")
    return (f"employer pages checked this run {outcomes.get('checked', 0)}; deadlines employer-confirmed "
            f"{confirmed}, listed only (NOT confirmed) {listed}; unchecked: "
            + ", ".join(f"{k} {v}" for k, v in unchecked.most_common()))
