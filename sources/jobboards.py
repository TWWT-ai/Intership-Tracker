"""Student job boards that list every posting in a sitemap: Higherin (formerly
RateMyPlacement) and TargetJobs.

Their search pages are rendered in the browser, but each posting page carries
schema.org JobPosting data, and each board advertises a sitemap in its
robots.txt. The sitemap's own dates and each address's words narrow the list to
recent student programmes before any posting is opened, so a sweep reads
hundreds of pages rather than thousands.

A board's copy is a lead, not the employer's word. Its row points at the board
page and never claims to be the application link, so when the same programme is
found on Trackr or a company's own site, that row's official link is kept.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from sources import careers
from sources.base import build_job
from sources.fetch import SourceBlocked, SourceError
from tracker import config, eligibility, graduation, normalize, programme
from tracker.models import Job, parse_iso_date

log = logging.getLogger(__name__)

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_ENTRY = re.compile(r"<(url|sitemap)>(.*?)</\1>", re.DOTALL)

#: A description that names a computing degree, or opens the role to any
#: degree. Words like "data" or "technology" appear in almost every posting and
#: say nothing about who may apply, so they only count in the title.
_RELEVANT = re.compile(
    r"computer science|computing|software engineering|software development|informatics"
    r"|\bany degree|\bany discipline|\ball degree|\ball disciplines|\ball academic backgrounds"
    r"|all subjects|any subject", re.IGNORECASE)


def sweep(session, settings: dict, source: str) -> list[Job]:
    """Recent student postings from one board, kept when based in the UK and
    open to a computing student."""
    host = urlparse(settings["sitemap"]).netloc
    session.limiter.configure(host, float(settings.get("rate_limit_seconds", 3)))
    since = (date.today() - timedelta(days=int(settings.get("lastmod_window_days", 60)))).isoformat()

    slug = programme._pattern(settings.get("slug_terms"))
    urls = [u for u in sitemap_urls(session, settings["sitemap"], since)
            if "/jobs/" in u and slug and slug.search(u.rstrip("/").rsplit("/", 1)[-1].replace("-", " "))]
    urls = urls[: int(settings.get("max_job_pages", 600))]
    log.info("[%s] %d recent student postings to read", source, len(urls))

    wanted = careers._Filter(config.load("sources")["sources"]["careers"])
    profile = config.load_profile()
    jobs: list[Job] = []
    for url in urls:
        try:
            posting = read_posting(session, url)
        except SourceBlocked:
            raise
        except SourceError as exc:
            log.info("[%s] %s skipped: %s", source, url, exc)
            continue
        if posting and keep(posting, wanted):
            jobs.append(to_job(posting, source, wanted, profile))
    return jobs


def sitemap_urls(session, url: str, since: str) -> list[str]:
    """Posting addresses from a sitemap or sitemap index, dated `since` or later.
    An entry without a date is kept: the sitemap did not say it is old."""
    response = session.http.get(url)
    if not response.ok:
        raise SourceError(f"HTTP {response.status} for {url}")
    out: list[str] = []
    for kind, body in _ENTRY.findall(response.text):
        loc = _LOC.search(body)
        if not loc:
            continue
        # A sitemap index dates its child sitemaps unreliably (Higherin's jobs
        # sitemap entry is stale while its postings are current), so only
        # postings are filtered by date; child sitemaps are always followed.
        lastmod = re.search(r"<lastmod>\s*([^<\s]+)", body)
        if kind == "url" and not loc.group(1).lower().endswith(".xml") and lastmod and lastmod.group(1)[:10] < since:
            continue
        # Higherin lists its child sitemaps in <url> tags, so an address ending in
        # .xml is followed as a sitemap whichever tag holds it.
        if kind == "sitemap" or loc.group(1).lower().endswith(".xml"):
            if re.search(r"job", loc.group(1), re.IGNORECASE):
                out.extend(sitemap_urls(session, loc.group(1), since))
        else:
            out.append(loc.group(1))
    return out


def read_posting(session, url: str) -> dict | None:
    """The page's JobPosting data as plain fields, or None if it has none."""
    response = session.http.get(url)
    if not response.ok:
        raise SourceError(f"HTTP {response.status} for {url}")
    low = response.text.casefold()
    markers = config.load("sources").get("browser", {}).get("challenge_markers") or []
    if any(str(marker).casefold() in low for marker in markers):
        raise SourceBlocked(f"{urlparse(url).netloc} served an anti-bot check; skipped, not bypassed")

    soup = BeautifulSoup(response.text, "lxml")
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return _fields(item, url)
    return None


def _fields(item: dict, url: str) -> dict:
    organisation = item.get("hiringOrganization") or {}
    places = item.get("jobLocation") or []
    places = places if isinstance(places, list) else [places]
    parts: list[str] = []
    for place in places:
        address = (place or {}).get("address") or {}
        if isinstance(address, dict):
            country = address.get("addressCountry")
            country = country.get("name") if isinstance(country, dict) else country
            parts.append(", ".join(str(x) for x in (address.get("addressLocality"), address.get("addressRegion"),
                                                     country) if x))
    employment = item.get("employmentType") or ""
    employment = " ".join(employment) if isinstance(employment, list) else str(employment)
    description = item.get("description") or ""
    if "<" in description:
        description = BeautifulSoup(description, "lxml").get_text(" ", strip=True)
    posting_id = re.search(r"/jobs/(\d+)/|-(\d+)/?$", url)
    return {
        "id": next((g for g in (posting_id.groups() if posting_id else ()) if g), url),
        "url": url,
        "title": " ".join(str(item.get("title") or "").split()),
        "company": organisation.get("name") if isinstance(organisation, dict) else "",
        "location": "; ".join(p for p in parts if p),
        "employment": employment,
        "deadline": normalize.parse_date(str(item.get("validThrough") or "")[:10]) if item.get("validThrough") else None,
        "posted": parse_iso_date(item.get("datePosted")),
        "description": normalize.clean_description(description),
    }


def keep(posting: dict, wanted) -> bool:
    """A UK student programme a computing student may apply to."""
    title, text = posting["title"], posting["description"]
    if not title or not posting["company"] or wanted.excluded(title):
        return False
    student = (wanted.student_title(title, posting["employment"]) or "intern" in posting["employment"].casefold()
               or bool(programme.classify(title, text).evidence))
    relevant = wanted.tech_in_title(title) or bool(_RELEVANT.search(text))
    return student and relevant and wanted.is_uk(posting["location"])


def to_job(posting: dict, source: str, wanted, profile: dict) -> Job:
    found = programme.classify(posting["title"], posting["description"], posting["employment"])
    job = build_job(
        source=source,
        company=posting["company"],
        title=posting["title"],
        source_url=posting["url"],
        external_id=posting["id"],
        location=careers._first_uk_location(posting["location"], wanted),
        work_type=found.work_type,
        deadline=posting["deadline"],
        date_posted=posting.get("posted"),
        description=posting["description"],
        year_of_study=found.evidence,
    )
    graduation.apply(job, posting["description"], profile)
    eligibility.assess(job, posting["description"], profile)
    return job
