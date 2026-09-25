"""Deadline and open/closed verification.

Aggregators routinely leave a role listed after the employer has closed it, and
often carry no deadline at all - a live Trackr sweep returned 295 listings with
only 15 deadlines between them. So after deduplication (one visit per real job,
not one per platform) this stage opens the actual job page and, when that does
not settle it, follows through to the employer's own application page.

It never guesses. If the state cannot be established it records UNKNOWN, and
the workbook shows UNKNOWN rather than implying the role is open.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from sources.fetch import SourceBlocked, SourceError
from tracker import config, eligibility, graduation, normalize, programme
from tracker.models import Job, OpenStatus, Priority

log = logging.getLogger(__name__)

#: Boilerplate regions whose text says nothing about THIS role's status. A
#: "no longer accepting applications" in a site-wide footer would otherwise
#: close every job on the platform.
_STRIP_TAGS = ("script", "style", "nav", "footer", "header", "form", "noscript")


class VerificationStats:
    def __init__(self) -> None:
        self.checked = 0
        self.opened = 0
        self.closed = 0
        self.unknown = 0
        self.deadlines_found = 0
        self.deadlines_corrected = 0
        self.errors = 0

    def as_dict(self) -> dict:
        return {
            "checked": self.checked, "open": self.opened, "closed": self.closed,
            "unknown": self.unknown, "deadlines_found": self.deadlines_found,
            "deadlines_corrected": self.deadlines_corrected, "errors": self.errors,
        }

    def summary(self) -> str:
        return (
            f"{self.checked} checked - {self.opened} open, {self.closed} closed, "
            f"{self.unknown} unknown, {self.deadlines_found} deadlines found, "
            f"{self.deadlines_corrected} corrected"
        )


def select(jobs: list[Job], settings: dict, today: date | None = None) -> list[Job]:
    """Choose which jobs to verify this run, within budget.

    Re-checking every job daily would mean thousands of requests, so the budget
    is spent where staleness actually costs something: new jobs, urgent
    deadlines, high-priority rows and anything still unknown.
    """
    today = today or date.today()
    budget = settings.get("budget", {})
    always = budget.get("always_verify", {})
    revalidate_days = int(budget.get("revalidate_after_days", 3))
    limit = int(config.env_override("MAX_VERIFICATIONS") or budget.get("max_verifications", 120))

    urgent_within = int(always.get("deadline_within_days", 14))
    always_priorities = set(always.get("priority", []))
    always_status = set(always.get("open_status", []))

    def must_verify(job: Job) -> bool:
        if always.get("new_jobs", True) and job.last_verified is None:
            return True
        if str(job.priority) in always_priorities:
            return True
        if str(job.open_status) in always_status:
            return True
        if job.deadline and 0 <= (job.deadline - today).days <= urgent_within:
            return True
        return False

    def is_stale(job: Job) -> bool:
        if job.last_verified is None:
            return True
        return (datetime.now() - job.last_verified) > timedelta(days=revalidate_days)

    candidates = [job for job in jobs if must_verify(job) or is_stale(job)]

    # Spend the budget on what matters most: soonest deadline first, then
    # priority, then score.
    rank = {Priority.HIGH: 0, Priority.MEDIUM: 1, Priority.LOW: 2}
    far = date(2099, 12, 31)
    candidates.sort(
        key=lambda j: (
            j.deadline or far,
            rank.get(j.priority, 3),
            -(j.match_score or 0),
        )
    )
    return candidates[:limit]


def verify_all(session, jobs: list[Job], settings: dict | None = None) -> VerificationStats:
    """Verify the selected subset, updating jobs in place."""
    settings = settings or config.load("verification")
    stats = VerificationStats()

    if not settings.get("enabled", True):
        return stats

    for job in select(jobs, settings):
        try:
            verify_job(session, job, settings)
        except SourceBlocked as exc:
            # A challenged host means every remaining check would fail too.
            log.warning("Verification stopped early: %s", exc)
            stats.errors += 1
            break
        except (SourceError, Exception) as exc:
            log.warning("Verification failed for %s (%s): %s", job.id, job.company, exc)
            job.verification_note = f"Verification failed: {type(exc).__name__}"
            stats.errors += 1
            continue

        stats.checked += 1
        if job.open_status is OpenStatus.OPEN:
            stats.opened += 1
        elif job.open_status is OpenStatus.CLOSED:
            stats.closed += 1
        else:
            stats.unknown += 1

    return stats


def verify_job(session, job: Job, settings: dict) -> None:
    """Establish this job's open/closed state and its real deadline."""
    previous_deadline = job.deadline
    status = OpenStatus.UNKNOWN
    notes: list[str] = []

    # 1. The listing page on the platform we found it on.
    if job.source_url:
        status, deadline, note, _ = _inspect(session, job.source_url, settings)
        if note:
            notes.append(note)
        if deadline:
            _set_deadline(job, deadline, "job-page")

    # 2. The employer's own application page, when the listing did not settle
    #    it. For Trackr rows this is where the truth actually lives.
    needs_employer_check = status is OpenStatus.UNKNOWN or job.deadline is None
    application_url = job.application_url

    if (
        settings.get("follow_application_url", True)
        and needs_employer_check
        and application_url
        and application_url != job.source_url
    ):
        ats_status, ats_deadline, note, page_text = _inspect(session, application_url, settings)
        if note:
            notes.append(note)
        # The employer's posting says who may apply, which names the programme
        # when its title does not: a "Discovery Programme" for first-years is a
        # spring week. Aggregator listing pages are not read for this - one
        # board page can describe several programmes.
        programme.refine(job, page_text)
        graduation.apply(job, page_text, config.load_profile())
        eligibility.assess(job, page_text, config.load_profile())
        if ats_deadline:
            _set_deadline(job, ats_deadline, "employer-ats")
        if ats_status is not OpenStatus.UNKNOWN:
            status = ats_status

    # A deadline that has already passed is itself proof the role has closed.
    if status is not OpenStatus.CLOSED and job.deadline and job.deadline < date.today():
        status = OpenStatus.CLOSED
        notes.append("Deadline has passed")

    job.open_status = status
    job.last_verified = datetime.now().replace(microsecond=0)
    job.verification_note = "; ".join(notes)[:300]

    # Flag a disagreement rather than silently preferring one source.
    tolerance = int(settings.get("deadline_disagreement_flag_days", 2))
    if job.listed_deadline and job.deadline and job.deadline != job.listed_deadline:
        job.deadline_disagreement = abs((job.deadline - job.listed_deadline).days) > tolerance

    if previous_deadline != job.deadline and job.deadline:
        log.info("%s %s: deadline %s -> %s", job.id, job.company, previous_deadline, job.deadline)


def _set_deadline(job: Job, deadline: date, source: str) -> None:
    """Adopt a deadline read from a real page, keeping the platform's claim."""
    if job.listed_deadline is None:
        job.listed_deadline = job.deadline
    job.deadline = deadline
    job.deadline_source = source


def _inspect(session, url: str, settings: dict,
             allow_browser: bool = True) -> tuple[OpenStatus, date | None, str, str]:
    """Fetch one page and read its status and deadline.

    Returns (status, deadline, note, page text). Any failure yields UNKNOWN
    rather than an assumption in either direction, and no text.
    """
    if not url.startswith("http"):
        return OpenStatus.UNKNOWN, None, "", ""

    try:
        response = session.http.get(url)
    except SourceBlocked:
        if not allow_browser:
            return OpenStatus.UNKNOWN, None, f"{_host(url)} refused a plain request", ""
        # Cloudflare and friends; retry through the browser, which is the same
        # request a person would make.
        response = session.browser.get(url)
    except SourceError as exc:
        return OpenStatus.UNKNOWN, None, f"could not reach {_host(url)}", ""

    if response.status in (404, 410):
        if settings.get("treat_404_as_closed", True):
            return OpenStatus.CLOSED, None, f"{_host(url)} returned {response.status}", ""
        return OpenStatus.UNKNOWN, None, f"{_host(url)} returned {response.status}", ""

    if not response.ok:
        return OpenStatus.UNKNOWN, None, f"{_host(url)} returned {response.status}", ""

    text = _main_text(response.text)
    status = _status_from_text(text, settings)
    deadline = _deadline_from_text(text, settings)
    return status, deadline, "", text


def _main_text(html: str) -> str:
    """The page's own content, with site-wide boilerplate removed."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    return " ".join(main.get_text(" ", strip=True).split())


def _status_from_text(text: str, settings: dict) -> OpenStatus:
    low = text.casefold()

    if any(marker in low for marker in settings.get("closed_markers", [])):
        return OpenStatus.CLOSED
    if any(marker in low for marker in settings.get("open_markers", [])):
        return OpenStatus.OPEN
    return OpenStatus.UNKNOWN


def _deadline_from_text(text: str, settings: dict) -> date | None:
    """Find a date following one of the configured deadline labels.

    Only labelled dates are accepted. A bare date elsewhere on the page is as
    likely to be a start date or a posting date, and a wrong deadline is worse
    than none.
    """
    dayfirst = bool(settings.get("dayfirst", True))

    for label in settings.get("deadline_labels", []):
        for match in re.finditer(re.escape(label), text, flags=re.IGNORECASE):
            window = text[match.end(): match.end() + 80]
            deadline = normalize.parse_date(window, dayfirst=dayfirst)
            if deadline:
                return deadline
    return None


def _host(url: str) -> str:
    return urlparse(url).netloc or url


#: A page listing many postings. Reading one would lend a row another role's
#: graduation year, so eligibility is only read from single postings.
_SEARCH_PAGE = re.compile(
    r"[?&](?:keyword|keywords|q|query|search|searchtext|location)=|/search\b|/jobs/?(?:\?|$)|/careers/?(?:\?|$)",
    re.IGNORECASE)


def _single_posting(url: str, skip_hosts=()) -> bool:
    """An employer's own posting. Aggregators (Trackr, Gradcracker...) are never
    read for eligibility: a row with no employer link points at the platform's
    board of many programmes."""
    if not (url or "").startswith("http"):
        return False
    host = urlparse(url).netloc.casefold()
    if any(host == h or host.endswith("." + h) for h in skip_hosts):
        return False
    return bool(normalize.posting_key(url)) or not _SEARCH_PAGE.search(url)


def sweep_eligibility(session, jobs: list[Job], settings: dict, profile: dict, limit: int = 0) -> str:
    """Open each open row's own employer posting once and read who may apply.

    Aggregators rarely carry eligibility; the employer's posting states the
    graduation year. Rows a careers feed already described are skipped, as are
    closed and expired rows and pages listing many postings. A plain request
    only: a site that refuses one is left alone rather than driven through a
    browser hundreds of times. Returns a one-line summary.
    """
    budget = limit or int(settings.get("eligibility_max_pages", 600))
    skip_hosts = tuple(str(h).casefold() for h in settings.get("eligibility_skip_hosts") or [])
    today = date.today()
    candidates = [
        job for job in jobs
        if job.open_status is not OpenStatus.CLOSED
        and not (job.deadline and job.deadline < today)
        and job.eligible_for_you in ("", eligibility.CHECK)
        and "careers" not in job.source_names()
        and _single_posting(job.application_url, skip_hosts)
    ]
    candidates.sort(key=lambda job: (job.deadline is None, job.deadline or today))

    opened = stated = empty = settled = 0
    for job in candidates[:budget]:
        _, _, _, text = _inspect(session, job.application_url, settings, allow_browser=False)
        opened += 1
        if not text:
            empty += 1
            continue
        programme.refine(job, text)
        graduation.apply(job, text, profile)
        eligibility.assess(job, text, profile)
        if job.expected_graduation:
            stated += 1
        if job.eligible_for_you in (eligibility.YES, eligibility.NO):
            settled += 1

    return (f"{opened} employer pages read of {len(candidates)} rows not settled for you - "
            f"{stated} have a graduation year, {settled} now Yes/No, {empty} gave no readable text")
