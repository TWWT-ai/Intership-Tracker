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

from sources.fetch import RobotsDisallowed, SourceBlocked, SourceError
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
        # Assessment mode: why each selected row was, or was not, checked.
        self.outcomes: dict[str, int] = {}

    def as_dict(self) -> dict:
        return {
            "checked": self.checked, "open": self.opened, "closed": self.closed,
            "unknown": self.unknown, "deadlines_found": self.deadlines_found,
            "deadlines_corrected": self.deadlines_corrected, "errors": self.errors,
            "outcomes": dict(self.outcomes),
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

    from tracker import role_filter
    if role_filter.enabled():
        return _verify_all_assessed(session, jobs, settings, stats)

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
    from tracker import role_filter

    previous_deadline = job.deadline
    status = OpenStatus.UNKNOWN
    notes: list[str] = []
    said_closed = ""                      # the page whose wording said closed
    texts: dict[str, str] = {}

    # 1. The listing page on the platform we found it on.
    if job.source_url:
        status, deadline, note, texts[job.source_url] = _inspect(session, job.source_url, settings)
        if note:
            notes.append(note)
        if deadline:
            _set_deadline(job, deadline, "job-page")
        if status is OpenStatus.CLOSED:
            said_closed = job.source_url

    # 2. The employer's own application page, when the listing did not settle
    #    it. For Trackr rows this is where the truth actually lives - and an
    #    aggregator saying "closed" is only a claim, so the employer is asked.
    needs_employer_check = (status is OpenStatus.UNKNOWN or job.deadline is None
                            or (status is OpenStatus.CLOSED and role_filter.aggregator(job.source_url)))
    application_url = job.application_url

    if (
        settings.get("follow_application_url", True)
        and needs_employer_check
        and application_url
        and application_url != job.source_url
    ):
        ats_status, ats_deadline, note, page_text = _inspect(session, application_url, settings)
        texts[application_url] = page_text
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
            said_closed = application_url if ats_status is OpenStatus.CLOSED else ""

    # Expiry and hiring are separate findings. A passed deadline is noted,
    # never turned into CLOSED: that was how 323 rows came to say CLOSED with
    # no employer ever saying so. Only closure wording on the employer's own
    # page is CLOSED; an aggregator's is recorded as a listing claim.
    if job.deadline and job.deadline < date.today():
        notes.append("deadline passed (expiry, not closure)")
    if status is OpenStatus.CLOSED:
        text_page = said_closed or job.source_url
        marker = next((m for m in settings.get("closed_markers", [])
                       if m in (texts.get(text_page) or "").casefold()), "closed wording")
        evidence = f'"{marker}" on {text_page} ({date.today().isoformat()})'
        if role_filter.aggregator(text_page):
            job.hiring_status, job.hiring_evidence = role_filter.LISTING_CLOSED, evidence + " - listing only"
            status = OpenStatus.UNKNOWN
        else:
            job.hiring_status, job.hiring_evidence = role_filter.EMPLOYER_CLOSED, evidence
            job.closed_evidence = f'employer page says "{marker}" ({_host(text_page)})'
        job.checked_url = text_page

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
        if settings.get("treat_404_as_closed", False):
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
    from tracker import relevance, role_filter

    budget = limit or int(settings.get("eligibility_max_pages", 600))
    skip_hosts = tuple(str(h).casefold() for h in settings.get("eligibility_skip_hosts") or [])
    today = date.today()
    filtering = role_filter.enabled()
    listings = relevance.Listings.load() if filtering else None
    candidates = [
        job for job in jobs
        if job.open_status is not OpenStatus.CLOSED
        and not (job.deadline and job.deadline < today)
        and ((job.eligible_for_you in ("", eligibility.CHECK) and "careers" not in job.source_names())
             or (filtering and relevance.needs_text(job)))
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
        if filtering and relevance.needs_text(job) and relevance.own_page(job, job.application_url):
            # The row's own employer posting, for its own place: the role
            # filter may read it from now on.
            job.description = normalize.clean_description(text)
            job.description_source = relevance.PAGE + job.application_url
            role_filter.assess_row(job, listings, today)
        programme.refine(job, text)
        graduation.apply(job, text, profile)
        eligibility.assess(job, text, profile)
        if job.expected_graduation:
            stated += 1
        if job.eligible_for_you in (eligibility.YES, eligibility.NO):
            settled += 1

    return (f"{opened} employer pages read of {len(candidates)} rows not settled for you - "
            f"{stated} have a graduation year, {settled} now Yes/No, {empty} gave no readable text")


# ---------------------------------------------------------------------------
# Assessment-mode verification (config/relevance.yaml apply_to_workbook)
# ---------------------------------------------------------------------------
#
# Reads the row's own public employer posting first. A page is judged by what
# it contains, not by its URL: a posting at ".../apply" is read; a page whose
# main content is a sign-in, upload or applicant-data form is an application
# step and is not read further. One GET per page - no link is followed, no
# button pressed, no field filled. A blocked site is skipped for the rest of
# the run instead of stopping everything; known-challenging aggregator sites
# are never retried in a browser. Every selected row records why it was, or
# was not, checked.

from dataclasses import dataclass as _dataclass
from datetime import datetime as _datetime


#: Less posting text than this is a script-built shell, not a read posting.
_MIN_PAGE_TEXT = 300
#: select_assessed: rows past a reliable (labelled) deadline - last, and only
#: with budget no live row needs.
EXPIRED_TIER = 9


@_dataclass
class PageRead:
    kind: str            # posting | application_step | closed | blocked | robots | unreachable | no_text
    text: str = ""
    locale: str = ""
    note: str = ""
    valid_through: str = ""


def _gate(soup: BeautifulSoup, settings: dict) -> str:
    """What makes a page an application step, or "" for none."""
    markers = settings.get("application_step_markers") or {}
    if soup.select_one("input[type=password]"):
        return "password field"
    if soup.select_one("input[type=file]"):
        return "file upload"
    sign_in = [str(w).casefold() for w in markers.get("sign_in") or []]
    fields = [str(w).casefold() for w in markers.get("applicant_fields") or []]
    for form in soup.find_all("form"):
        words = " ".join(form.get_text(" ", strip=True).split()).casefold()
        labels = " ".join(filter(None, [words] + [" ".join(filter(None, (
            i.get("name"), i.get("placeholder"), i.get("aria-label"), i.get("id")))) for i in form.find_all(
                ["input", "textarea", "select"])])).casefold()
        if any(w in words for w in sign_in) and form.find("input"):
            return "sign-in form"
        asked = {f for f in fields if f in labels}
        submits = form.select("button, input[type=submit]")
        if len(asked) >= 3 and submits:
            return f"applicant data entry ({', '.join(sorted(asked)[:4])})"
    return ""


def read_page(session, url: str, settings: dict, allow_browser: bool = True) -> PageRead:
    """One GET, then decide what the page is. Never interacts with it."""
    if not (url or "").startswith("http"):
        return PageRead("unreachable", note="no URL")
    try:
        response = session.http.get(url)
    except RobotsDisallowed:
        return PageRead("robots", note=f"robots.txt disallows {_host(url)}")
    except SourceBlocked:
        if not allow_browser:
            return PageRead("blocked", note=f"{_host(url)} refused a plain request")
        try:
            response = session.browser.get(url)
        except SourceBlocked:
            return PageRead("blocked", note=f"{_host(url)} served an anti-bot check; skipped, not bypassed")
        except SourceError as exc:
            return PageRead("unreachable", note=f"could not reach {_host(url)}: {type(exc).__name__}")
    except SourceError as exc:
        return PageRead("unreachable", note=f"could not reach {_host(url)}: {type(exc).__name__}")
    if response.status in (404, 410):
        return PageRead("closed", note=f"{_host(url)} returned {response.status}")
    if not response.ok:
        return PageRead("unreachable", note=f"{_host(url)} returned {response.status}")

    soup = BeautifulSoup(response.text, "lxml")
    locale = (soup.html.get("lang") if soup.html else "") or ""
    posting_ld = ""
    valid_through = ""
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or ""
        if "JobPosting" in raw:
            posting_ld = raw
            m = re.search(r'"validThrough"\s*:\s*"([^"]+)"', raw)
            valid_through = m.group(1)[:10] if m else ""
    gate = _gate(soup, settings)
    text = _main_text(response.text)
    enough = len(text) >= int((settings.get("application_step_markers") or {}).get("posting_text_chars", 800))
    if gate and not (posting_ld or enough):
        return PageRead("application_step", locale=locale, note=f"stopped at application step: {gate}")
    if len(text) < _MIN_PAGE_TEXT and not posting_ld:
        return PageRead("no_text", locale=locale,
                        note=f"{_host(url)}: no readable posting text without a browser (the page is built by script)")
    return PageRead("posting", text=text, locale=locale, valid_through=valid_through,
                    note=f"application form also present ({gate}); only posting text read" if gate else "")


_WORKDAY = re.compile(r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-zA-Z]{2}[-_][a-zA-Z]{2}/)?"
                      r"([^/?#]+)(/job/[^?#]+?)(?:/apply[^?#]*)?/?(?:[?#].*)?$", re.I)
_GREENHOUSE = re.compile(r"https://(?:job-boards|boards)\.greenhouse\.io/([^/?#]+)/jobs/(\d{5,})", re.I)
_LEVER = re.compile(r"https://jobs\.(eu\.)?lever\.co/([^/?#]+)/([0-9a-f-]{36})", re.I)
_ASHBY = re.compile(r"https://jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", re.I)


def ats_posting(session, url: str) -> PageRead | None:
    """The posting through its own platform's public job-data feed - the data
    the page itself loads, with the same robots.txt check as any request.
    None when the URL is not a posting on a platform with such a feed."""
    from sources import careers

    def fetch(api: str):
        try:
            response = session.http.get(api, expect_json=True)
        except RobotsDisallowed:
            return PageRead("robots", note=f"robots.txt disallows {_host(api)}")
        except SourceBlocked:
            return PageRead("blocked", note=f"{_host(api)} refused the request; skipped, not bypassed")
        except SourceError as exc:
            return PageRead("unreachable", note=f"could not reach {_host(api)}: {type(exc).__name__}")
        if response.status in (404, 410):
            return PageRead("closed", note=f"{_host(api)} returned {response.status} for this posting")
        if not response.ok:
            return PageRead("unreachable", note=f"{_host(api)} returned {response.status}")
        try:
            return __import__("json").loads(response.text)
        except ValueError:
            return PageRead("unreachable", note=f"{_host(api)} returned unreadable data")

    def done(text: str, source: str, valid_through: str = "", note: str = "") -> PageRead:
        text = normalize.clean_description(text)
        if len(text) < _MIN_PAGE_TEXT:
            return PageRead("no_text", note=f"{source}: the posting has no description text")
        return PageRead("posting", text=text, valid_through=valid_through, note=note or f"read via {source}")

    if m := _WORKDAY.match(url or ""):
        tenant, wd, site, path = m.groups()
        data = fetch(f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}")
        if isinstance(data, PageRead):
            return data
        info = (data or {}).get("jobPostingInfo") or {}
        note = "read via the Workday job feed" + ("" if info.get("canApply", True) else "; Workday reports it cannot be applied to")
        return done(" ".join(filter(None, [info.get("title"), info.get("location"),
                                           careers._text(info.get("jobDescription") or "")])), "Workday", note=note)
    if m := _GREENHOUSE.match(url or ""):
        token, job_id = m.groups()
        data = fetch(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?content=true")
        if isinstance(data, PageRead):
            return data
        import html as _html
        return done(" ".join(filter(None, [(data or {}).get("title"), ((data or {}).get("location") or {}).get("name"),
                                           careers._text(_html.unescape((data or {}).get("content") or ""))])),
                    "Greenhouse")
    if m := _LEVER.match(url or ""):
        eu, token, job_id = m.groups()
        data = fetch(f"https://api.{'eu.' if eu else ''}lever.co/v0/postings/{token}/{job_id}?mode=json")
        if isinstance(data, PageRead):
            return data
        j = data or {}
        return done(" ".join(filter(None, [j.get("text"), j.get("descriptionPlain"), *(
            f"{i.get('text') or ''}: {careers._text(i.get('content') or '')}" for i in j.get("lists") or []),
            j.get("additionalPlain")])), "Lever")
    if m := _ASHBY.match(url or ""):
        token, job_id = m.groups()
        data = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false")
        if isinstance(data, PageRead):
            return data
        job = next((j for j in (data or {}).get("jobs", []) if j.get("id") == job_id), None)
        if job is None:
            return PageRead("closed", note="the posting is no longer on its Ashby job board")
        return done(" ".join(filter(None, [job.get("title"), job.get("location"), job.get("descriptionPlain")])),
                    "Ashby")
    return None


def select_assessed(jobs: list[Job], settings: dict, today: date | None = None) -> tuple[list[Job], list[Job]]:
    """(to check this run, left for later) - a rotating, resumable queue.

    Each row keeps when it was last checked (`last_checked`, carried between
    runs), so the queue resumes where the last run stopped. Most of the budget
    goes by priority - an approaching deadline the employer has not confirmed,
    then a stale UNKNOWN hiring status, then conflicts, then never-checked
    rows - and a quarter always goes to whatever was checked longest ago, so
    no employer posting waits forever behind higher tiers. Rows past an
    unreliable date (validThrough only, a conflict) come after those, and
    rows past a labelled deadline (`role_filter.expiry` EXPIRED) only take
    budget no live row needs. Rows without an own employer posting are not
    queued; they are reported instead.
    """
    from tracker import role_filter

    today = today or date.today()
    budget = settings.get("budget", {})
    limit = int(config.env_override("MAX_VERIFICATIONS") or budget.get("max_verifications", 120))
    revalidate = int(budget.get("revalidate_after_days", 3))
    soon = int((budget.get("always_verify") or {}).get("deadline_within_days", 14))
    far = date(2099, 12, 31)

    def last(job: Job):
        if job.last_checked:
            try:
                return _datetime.fromisoformat(job.last_checked)
            except ValueError:
                pass
        return job.last_verified

    def tier(job: Job) -> int | None:
        checked = last(job)
        stale = checked is None or (_datetime.now() - checked) > timedelta(days=revalidate)
        if not stale:
            return None
        expired, _ = role_filter.expiry(job, today)
        if expired == "EXPIRED":
            # Past a labelled deadline: a check can change little, so it waits
            # until every live row has had its turn (EXPIRED_TIER).
            return EXPIRED_TIER
        if expired == "REVIEW":
            return 5            # passed on an unreliable date: worth settling, after the live rows
        if job.deadline and 0 <= (job.deadline - today).days <= soon and not job.deadline_confirmed:
            return 0
        if checked is not None and job.hiring_status in ("", role_filter.UNKNOWN):
            return 1
        if job.deadline_status in ("CONFLICT", "AMBIGUOUS", "UNATTRIBUTED"):
            return 2
        if checked is None:
            return 3
        return 4

    due = []
    for job in jobs:
        url = job.application_url
        if not (_single_posting(url) and not role_filter.aggregator(url)):
            job.check_outcome = "no_employer_url"
            continue
        rank = tier(job)
        if rank is None:
            job.check_outcome = job.check_outcome or "not_due"
            continue
        due.append((rank, job))
    oldest = lambda job: (last(job) or _datetime.min, job.id)  # noqa: E731
    # Rows past a labelled deadline are checked only once no live row is
    # waiting: the 3 October fetch spent 98 of its 100 pages on them.
    live = [(rank, job) for rank, job in due if rank != EXPIRED_TIER]
    by_priority = sorted(live, key=lambda item: (item[0], item[1].deadline or far, oldest(item[1])))
    rotation = max(1, limit // 4) if limit > 1 else 0
    chosen = [job for _, job in by_priority[:max(0, limit - rotation)]]
    picked = {id(job) for job in chosen}
    rest = sorted((job for _, job in live if id(job) not in picked), key=oldest)
    chosen += rest[:limit - len(chosen)]
    expired = sorted((job for rank, job in due if rank == EXPIRED_TIER), key=oldest)
    chosen += expired[:max(0, limit - len(chosen))]
    picked = {id(job) for job in chosen}
    later = [job for _, job in due if id(job) not in picked]
    for job in later:
        job.check_outcome = "over_budget"
    return chosen, later


def verify_assessed(session, job: Job, settings: dict, blocked: set[str], listings, today: date,
                    browser: bool = True) -> str:
    """Check one row on its own employer posting; returns the check outcome."""
    from tracker import deadline_extract as dx
    from tracker import relevance, role_filter

    job.checks_attempted += 1
    job.last_checked = _datetime.now().replace(microsecond=0).isoformat()
    url = job.application_url
    job.checked_url = url
    host = _host(url).casefold()
    if host in blocked:
        job.verification_note = f"{host} refused an earlier request this run; skipped, not bypassed"
        _keep_or_unknown(job, job.verification_note)
        return "blocked"
    page = ats_posting(session, url) or read_page(
        session, url, settings, allow_browser=browser and not role_filter.aggregator(url))
    job.verification_note = page.note[:300]
    if page.kind in ("blocked", "robots", "unreachable", "application_step", "no_text"):
        if page.kind == "blocked":
            blocked.add(host)
        _keep_or_unknown(job, page.note)
        return page.kind
    if page.kind == "closed":
        # Gone from its URL. Not evidence that hiring closed: UNAVAILABLE.
        job.hiring_status, job.hiring_evidence = "UNAVAILABLE", f"{page.note} - checked {url}"
        job.last_verified = _datetime.now().replace(microsecond=0)
        role_filter.assess_row(job, listings, today)
        return "checked"

    own = relevance.own_page(job, url)
    # Only the page's own declared language. A URL segment such as Workday's
    # /en-US/ is the site's display language, not how the recruiter wrote the
    # date - a UK posting shown under /en-US/ would read 03/10 as 10 March.
    order = dx.order_from_locale(page.locale)
    basis = f"page language {page.locale}" if order else ""
    found = dx.tie_to_vacancy(
        dx.candidates(page.text, source="employer page", url=url, method="employer page", employer_page=own,
                      order=order, order_basis=basis, places_pattern=relevance._uk_places()),
        page.text, job.job_title)
    board = len({c.date for c in found if c.status == dx.OK and c.date}) >= 2 and not any(c.vacancy for c in found)

    status = _status_from_text(page.text, settings)
    when = job.last_checked[:10]
    marker = next((m for m in settings.get("closed_markers" if status is OpenStatus.CLOSED else "open_markers", [])
                   if m in page.text.casefold()), "")
    if status is OpenStatus.UNKNOWN:
        job.hiring_status, job.hiring_evidence = "UNKNOWN", f"posting read on {url} ({when}); no open/closed wording"
    elif board:
        # A page of several vacancies: its open/closed wording may be another
        # role's, exactly as its dates are (INT-001101).
        status = OpenStatus.UNKNOWN
        job.hiring_status = "UNKNOWN"
        job.hiring_evidence = (f'"{marker}" on {url} ({when}), but the page lists several vacancies with different '
                               "closing dates - not tied to this role")
    elif not own:
        # Not the row's own posting for its place: a claim, not the employer's word on this role.
        job.hiring_status = "LISTING_OPEN" if status is OpenStatus.OPEN else "LISTING_CLOSED"
        job.hiring_evidence = f'"{marker}" on {url} ({when}) - not this row\'s own posting for its place'
        status = OpenStatus.UNKNOWN
    elif status is OpenStatus.CLOSED:
        job.closed_evidence = f"employer page says \"{marker}\" ({_host(url)})"
        job.hiring_status, job.hiring_evidence = "EMPLOYER_CLOSED", f'"{marker}" on {url} ({when})'
    else:
        job.hiring_status, job.hiring_evidence = "EMPLOYER_OPEN", f'"{marker}" on {url} ({when})'
    job.open_status = status
    # Fresh employer-page evidence replaces the last read of the same page.
    evidence = [c for c in job.deadline_evidence if not (c.get("method") == "employer page" and c.get("url") == url)]
    if page.valid_through:
        structured = dx.mentions(page.valid_through)
        if structured and structured[0].value:
            found.append(dx.Candidate(date=structured[0].value.isoformat(), status=dx.OK, source="employer page",
                                      url=url, method="employer page", employer_page=own,
                                      quote=f"validThrough {page.valid_through}", order="structured field"))
    job.deadline_evidence = evidence + [c.as_dict() for c in found]
    if own and relevance.needs_text(job):
        job.description = normalize.clean_description(page.text)
        job.description_source = relevance.PAGE + url
    profile = config.load_profile()
    programme.refine(job, page.text)
    graduation.apply(job, page.text, profile)
    eligibility.assess(job, page.text, profile)
    job.last_verified = _datetime.now().replace(microsecond=0)
    role_filter.assess_row(job, listings, today)
    return "checked"


def _keep_or_unknown(job: Job, why: str) -> None:
    """A failed check leaves an earlier employer result standing; otherwise the
    hiring status is UNKNOWN, with the reason."""
    if job.hiring_status not in ("EMPLOYER_OPEN", "EMPLOYER_CLOSED", "UNAVAILABLE"):
        job.hiring_status, job.hiring_evidence = "UNKNOWN", f"not read: {why}"


def _verify_all_assessed(session, jobs: list[Job], settings: dict, stats: VerificationStats) -> VerificationStats:
    from tracker import relevance

    today = date.today()
    listings = relevance.Listings.load()
    chosen, later = select_assessed(jobs, settings, today)
    blocked: set[str] = set()
    outcomes = {}
    for job in chosen:
        try:
            outcome = verify_assessed(session, job, settings, blocked, listings, today)
        except Exception as exc:  # one bad page never stops the run
            log.warning("Verification failed for %s (%s): %s", job.id, job.company, exc)
            outcome = "unreachable"
            stats.errors += 1
        job.check_outcome = outcome
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if outcome == "checked":
            stats.checked += 1
            if job.open_status is OpenStatus.OPEN:
                stats.opened += 1
            elif job.open_status is OpenStatus.CLOSED:
                stats.closed += 1
            else:
                stats.unknown += 1
            if job.deadline_status == "CONFIRMED":
                stats.deadlines_found += 1
    outcomes["over_budget"] = len(later)
    stats.outcomes = outcomes
    return stats
