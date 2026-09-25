"""Bright Network adapter.

Enumeration goes through the site's own sitemap, not its search page:

    sitemap-main_jobs.xml  ->  ~1,400 job URLs with <lastmod>
    -> fetch each job page
    -> parse the schema.org JobPosting JSON-LD it publishes

This route is chosen deliberately. `/search/` and `/api/` are Disallowed in
robots.txt, while the sitemap and the job pages are allowed - so the compliant
path and the best-structured data happen to be the same path. The `lastmod`
timestamps also give incremental sync for free: pages that have not changed
since the last run are skipped.

`_reject_disallowed` is a belt-and-braces guard on top of the global robots
check, because accidentally paging into `/search/` is the one easy mistake this
adapter could make.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from sources.base import build_job
from sources.fetch import SourceBlocked, SourceError
from tracker import normalize
from tracker.models import Job

log = logging.getLogger(__name__)

name = "brightnetwork"

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

#: Paths this adapter must never request, whatever a sitemap or link says.
_DISALLOWED = ("/search/", "/api/", "/htmx/", "/login/", "/interstitial", "/certificates/")

#: schema.org employmentType values that indicate an internship or placement.
_INTERN_TYPES = {"INTERN", "INTERNSHIP", "TEMPORARY", "CONTRACTOR"}

_JOB_PATH = re.compile(r"/(graduate-jobs|internships|jobs)/[^/]+/[^/]+")


def search_jobs(session, settings: dict) -> list[Job]:
    base = settings.get("base_url", "https://www.brightnetwork.co.uk").rstrip("/")
    sitemap_url = settings.get("sitemap_url", f"{base}/sitemap-main_jobs.xml")
    window_days = int(settings.get("lastmod_window_days", 14))
    max_pages = int(settings.get("max_job_pages", 200))

    entries = _sitemap_entries(session, sitemap_url)
    log.info("Bright Network sitemap: %d job URLs", len(entries))

    cutoff = date.today() - timedelta(days=window_days)
    recent = [(url, mod) for url, mod in entries if mod is None or mod >= cutoff]
    recent.sort(key=lambda item: item[1] or date.min, reverse=True)

    jobs: list[Job] = []
    for url, _ in recent[:max_pages]:
        try:
            job = get_job(session, url)
        except SourceBlocked:
            raise                       # abort the source; never retry a challenge
        except SourceError as exc:
            log.warning("Bright Network job page failed (%s): %s", url, exc)
            continue
        if job is not None:
            jobs.append(job)

    return jobs


def get_job(session, url: str) -> Job | None:
    _reject_disallowed(url)
    response = session.browser.get(url)
    if not response.ok:
        return None
    return _parse_job_page(response.text, response.url or url)


# ---------------------------------------------------------------------------
# Sitemap
# ---------------------------------------------------------------------------


def _sitemap_entries(session, sitemap_url: str) -> list[tuple[str, date | None]]:
    """Return [(job_url, lastmod)] from the jobs sitemap.

    Follows one level of sitemap index if the configured URL turns out to be an
    index rather than a urlset.
    """
    _reject_disallowed(sitemap_url)

    # Plain HTTP first; Cloudflare 403s non-browser clients here, in which case
    # the browser navigates to it instead. Note that an in-page fetch() to this
    # path is blocked while top-level navigation is not, so navigation it is.
    try:
        text = session.http.get(sitemap_url).text
    except SourceError:
        text = session.browser.get(sitemap_url).text

    entries = _parse_urlset(text)
    if entries:
        return entries

    # Not a urlset: it may be a sitemap index pointing at the real jobs file.
    for child in _parse_index(text):
        if "job" in child.casefold():
            entries.extend(_sitemap_entries(session, child))
    return entries


def _parse_urlset(text: str) -> list[tuple[str, date | None]]:
    """Extract [(url, lastmod)] from sitemap XML.

    Handles both the raw XML and the DOM Chrome produces when it renders XML in
    its viewer, which keeps the original elements but wraps them in HTML.
    """
    pairs: list[tuple[str, str | None]] = []

    try:
        root = ElementTree.fromstring(text.strip())
        if root.tag.split("}")[-1] == "urlset":
            for node in root.findall("sm:url", _SITEMAP_NS):
                loc = node.find("sm:loc", _SITEMAP_NS)
                if loc is None or not loc.text:
                    continue
                mod = node.find("sm:lastmod", _SITEMAP_NS)
                pairs.append((loc.text.strip(), mod.text if mod is not None else None))
    except ElementTree.ParseError:
        soup = BeautifulSoup(text, "lxml")
        for node in soup.find_all("url"):
            loc = node.find("loc")
            if loc is None:
                continue
            mod = node.find("lastmod")
            pairs.append((loc.get_text(strip=True), mod.get_text(strip=True) if mod else None))

    entries: list[tuple[str, date | None]] = []
    for url, mod in pairs:
        if _JOB_PATH.search(url) and not _is_disallowed(url):
            entries.append((url, _parse_lastmod(mod)))
    return entries


def _parse_index(text: str) -> list[str]:
    """Child sitemap URLs, if this document is a sitemap index."""
    try:
        root = ElementTree.fromstring(text.strip())
        if root.tag.split("}")[-1] == "sitemapindex":
            return [(n.text or "").strip() for n in root.findall("sm:sitemap/sm:loc", _SITEMAP_NS)]
    except ElementTree.ParseError:
        soup = BeautifulSoup(text, "lxml")
        if soup.find("sitemapindex") or soup.find("sitemap"):
            return [n.get_text(strip=True) for n in soup.select("sitemap > loc")]
    return []


def _parse_lastmod(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except ValueError:
        return normalize.parse_date(value)


def _is_disallowed(url: str) -> bool:
    return any(part in url for part in _DISALLOWED)


def _reject_disallowed(url: str) -> None:
    if _is_disallowed(url):
        raise SourceError(
            f"Refusing to fetch {url}: Bright Network's robots.txt disallows this path"
        )


# ---------------------------------------------------------------------------
# Job pages
# ---------------------------------------------------------------------------


def _parse_job_page(html: str, url: str) -> Job | None:
    soup = BeautifulSoup(html, "lxml")
    posting = _json_ld_posting(soup)
    if posting is None:
        return None

    title = normalize.clean_text(posting.get("title"), limit=300)
    org = posting.get("hiringOrganization") or {}
    company = normalize.clean_text(
        org.get("name") if isinstance(org, dict) else org, limit=200
    )
    if not title or not company:
        return None

    employment = posting.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(str(e) for e in employment)
    employment = str(employment or "")

    description = _html_to_text(posting.get("description", ""))

    # Notes belongs to the user, so the posting date rides in the description.
    posted = _parse_iso(posting.get("datePosted"))
    if posted:
        description = f"Posted {posted.isoformat()} | {description}"

    return build_job(
        source=name,
        company=company,
        title=title,
        source_url=url,
        # Bright Network hosts its own application flow on the listing page.
        application_url=url,
        external_id=str(posting.get("identifier") or "").strip()[:60],
        location=_location(posting),
        work_type=_work_type(employment, title),
        # validThrough is the publisher's own machine-readable deadline, which
        # makes this the most reliable deadline of the three platforms.
        deadline=_parse_iso(posting.get("validThrough")),
        date_posted=posted,
        salary=_salary(posting),
        description=description,
        degree_requirements=normalize.clean_text(posting.get("educationRequirements"), limit=400),
        experience_requirements=normalize.clean_text(posting.get("experienceRequirements"), limit=400),
    )


def _json_ld_posting(soup: BeautifulSoup) -> dict | None:
    """Find the JobPosting object among the page's JSON-LD blocks."""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for candidate in _iter_ld(data):
            if str(candidate.get("@type", "")).casefold() == "jobposting":
                return candidate
    return None


def _iter_ld(data):
    """Walk JSON-LD, which may be an object, a list, or an @graph wrapper."""
    if isinstance(data, list):
        for item in data:
            yield from _iter_ld(item)
    elif isinstance(data, dict):
        yield data
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in data:
                yield from _iter_ld(data[key])


def _location(posting: dict) -> str:
    places = posting.get("jobLocation")
    if isinstance(places, dict):
        places = [places]
    if not isinstance(places, list):
        return ""

    parts: list[str] = []
    for place in places:
        if not isinstance(place, dict):
            continue
        address = place.get("address") or {}
        if not isinstance(address, dict):
            continue
        for key in ("addressLocality", "addressRegion", "addressCountry"):
            value = address.get(key)
            if isinstance(value, dict):
                value = value.get("name")
            if value and str(value) not in parts:
                parts.append(str(value))
                break

    if not parts and posting.get("jobLocationType") == "TELECOMMUTE":
        return "Remote"
    return ", ".join(parts[:3])


def _work_type(employment: str, title: str) -> str:
    haystack = f"{employment} {title}".casefold()
    if "placement" in haystack:
        return "Industrial Placement"
    if "summer" in haystack:
        return "Summer Internship"
    if any(t in employment.upper() for t in _INTERN_TYPES) or "intern" in haystack:
        return "Internship"
    return ""


def _salary(posting: dict) -> str:
    salary = posting.get("baseSalary")
    if not isinstance(salary, dict):
        return normalize.clean_text(salary, limit=120)

    value = salary.get("value")
    currency = salary.get("currency") or ""
    if isinstance(value, dict):
        amount = value.get("value") or value.get("minValue")
        maximum = value.get("maxValue")
        unit = value.get("unitText") or ""
        if amount and maximum and amount != maximum:
            return f"{currency}{amount}-{maximum} {unit}".strip()
        if amount:
            return f"{currency}{amount} {unit}".strip()
    return ""


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    if "<" in html:
        html = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    return normalize.clean_description(html)


def _parse_iso(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return normalize.parse_date(value)
