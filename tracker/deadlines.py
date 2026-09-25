"""Deadline bucketing and the urgent-applications report."""

from __future__ import annotations

from datetime import date

from tracker.models import USER_OWNED_STATUSES, Job, OpenStatus, Priority

BUCKETS = ("Today", "Within 2 days", "Within 7 days", "Within 14 days", "Expired")


def days_left(job: Job, today: date | None = None) -> int | None:
    if not job.deadline:
        return None
    return (job.deadline - (today or date.today())).days


def bucket(jobs: list[Job], today: date | None = None) -> dict[str, list[Job]]:
    """Group jobs by deadline urgency.

    Only buckets that have members are returned, so the Deadlines sheet has no
    empty sections. Jobs with no deadline are omitted — there is nothing to
    warn about, and their missing deadline is already flagged in Concerns.
    """
    today = today or date.today()
    out: dict[str, list[Job]] = {}

    for job in jobs:
        days = days_left(job, today)
        if days is None:
            continue
        if days < 0:
            label = "Expired"
        elif days == 0:
            label = "Today"
        elif days <= 2:
            label = "Within 2 days"
        elif days <= 7:
            label = "Within 7 days"
        elif days <= 14:
            label = "Within 14 days"
        else:
            continue
        out.setdefault(label, []).append(job)

    order = {Priority.HIGH: 0, Priority.MEDIUM: 1, Priority.LOW: 2}
    for label in out:
        out[label].sort(key=lambda j: (order.get(j.priority, 3), -(j.match_score or 0)))

    return {label: out[label] for label in BUCKETS if label in out}


def urgent(jobs: list[Job], within_days: int = 7, today: date | None = None) -> list[Job]:
    """Jobs that need attention now: open (or unconfirmed), not yet applied to,
    and closing soon. A closed job is not urgent, and neither is one already
    submitted."""
    today = today or date.today()
    out = []
    for job in jobs:
        days = days_left(job, today)
        if days is None or not (0 <= days <= within_days):
            continue
        if job.open_status is OpenStatus.CLOSED:
            continue
        if job.status in USER_OWNED_STATUSES:
            continue
        out.append(job)

    order = {Priority.HIGH: 0, Priority.MEDIUM: 1, Priority.LOW: 2}
    out.sort(key=lambda j: (days_left(j, today) or 0, order.get(j.priority, 3), -(j.match_score or 0)))
    return out


def report(jobs: list[Job], within_days: int = 7, today: date | None = None) -> str:
    """Human-readable urgent-applications summary for the console and logs."""
    items = urgent(jobs, within_days, today)
    if not items:
        return f"No applications closing within {within_days} days."

    lines = ["URGENT APPLICATIONS", ""]
    for job in items:
        days = days_left(job, today)
        when = "today" if days == 0 else f"{days} day{'s' if days != 1 else ''}"
        lines.append(f"{job.company} - {job.job_title}")
        lines.append(f"  Match: {job.match_score}%  Priority: {job.priority}  Deadline: {job.deadline} ({when})")
        if job.open_status is OpenStatus.UNKNOWN:
            lines.append("  NOTE: could not confirm this role is still open")
        url = job.application_url or job.source_url
        if url:
            lines.append(f"  Apply: {url}")
        lines.append("")
    return "\n".join(lines).rstrip()
