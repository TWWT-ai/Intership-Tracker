"""Gradcracker adapter.

Search results live at:

    /search/<discipline>/<job-type>?page=N
    e.g. /search/computing-technology/work-placements-internships

Note the slug order: "work-placements-internships". The reverse
("internships-work-placements") silently redirects to the homepage and yields
nothing, which is an easy hour to lose.

Pages are server-rendered, but Cloudflare returns 403 to non-browser clients,
so fetching goes through the shared Playwright session.

Cards are parsed by their visible field labels ("Deadline", "Salary",
"Location", ...) rather than by CSS class. The classes are Tailwind-generated
and change whenever the site is rebuilt; the labels are user-facing text and
are far more stable.

Terms note: Gradcracker's legal page restricts scraping. This adapter is
enabled at the user's explicit direction for personal, non-commercial use, at a
conservative request rate. See README.md "Scraping limitations".
"""

from __future__ import annotations

import logging
import re
from datetime import date

from bs4 import BeautifulSoup

from sources.base import build_job
from sources.fetch import SourceBlocked, SourceError
from tracker import normalize
from tracker.models import Job

log = logging.getLogger(__name__)

name = "gradcracker"

_JOB_PATH = re.compile(r"/(?:work-placement-internship|graduate-job)/\d+/")

#: /hub/<employer-id>/<employer-slug>/<type>/<job-id>/<job-slug>
_HUB_URL = re.compile(r"/hub/(\d+)/([^/]+)/(?:work-placement-internship|graduate-job)/(\d+)/")

#: robots.txt disallows /hub/9000001 - it is Gradcracker's demo employer.
_DEMO_HUB = "/hub/9000001"

#: Field labels as they appear on a card, in the order they are rendered. Each
#: value runs until the next label begins, which is what makes a flat text
#: parse reliable here.
_LABELS = [
    "Deadline", "Salary", "Location", "Degree required", "Duration",
    "Starting", "Start date", "Job type", "Disciplines",
]

_SMALL_WORDS = {"of", "and", "the", "for", "de", "at", "in", "on"}


def search_jobs(session, settings: dict) -> list[Job]:
    base = settings.get("base_url", "https://www.gradcracker.com").rstrip("/")
    max_pages = int(settings.get("max_pages", 10))

    jobs: list[Job] = []
    seen_urls: set[str] = set()

    for discipline in settings.get("disciplines", ["computing-technology"]):
        for job_type in settings.get("job_types", ["work-placements-internships"]):
            for page in range(1, max_pages + 1):
                url = f"{base}/search/{discipline}/{job_type}"
                if page > 1:
                    url += f"?page={page}"

                try:
                    response = session.browser.get(url)
                except SourceBlocked:
                    raise                       # abort the source; never retry a challenge
                except SourceError as exc:
                    log.warning("Gradcracker page %s failed: %s", page, exc)
                    break

                found = _parse_search_page(response.text, base)
                fresh = [j for j in found if j.source_url not in seen_urls]
                if not fresh:
                    break                       # ran past the last page of results

                for job in fresh:
                    seen_urls.add(job.source_url)
                jobs.extend(fresh)

    return jobs


def get_job(session, url: str) -> Job | None:
    """Fetch and parse a single Gradcracker job page."""
    if _DEMO_HUB in url:
        return None
    response = session.browser.get(url)
    if not response.ok:
        return None
    return _parse_job_page(response.text, url)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_search_page(html: str, base: str) -> list[Job]:
    soup = BeautifulSoup(html, "lxml")
    jobs: list[Job] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not _JOB_PATH.search(href) or _DEMO_HUB in href:
            continue

        url = href if href.startswith("http") else base + href
        if url in seen:
            continue
        seen.add(url)

        title = " ".join(anchor.get_text(" ", strip=True).split())
        if not title:
            continue

        card = _card_for(anchor)
        fields = _fields(card.get_text(" ", strip=True)) if card else {}

        job = _build(url, title, fields)
        if job is not None:
            jobs.append(job)

    return jobs


def _card_for(anchor):
    """Walk up to the element holding the whole listing's detail fields."""
    node = anchor.parent
    for _ in range(5):
        if node is None:
            return None
        text = node.get_text(" ", strip=True)
        if "Deadline" in text or "Location" in text:
            # Stop before the container grows to include the whole results list.
            if len(text) < 1200:
                return node
        node = node.parent
    return None


def _fields(text: str) -> dict[str, str]:
    """Split a card's flat text into {label: value}.

    Each label's value runs until the next label starts.
    """
    text = " ".join(text.split())
    positions: list[tuple[int, str]] = []
    for label in _LABELS:
        for match in re.finditer(rf"\b{re.escape(label)}\b", text):
            positions.append((match.start(), label))
    positions.sort()

    out: dict[str, str] = {}
    for i, (start, label) in enumerate(positions):
        value_start = start + len(label)
        value_end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        value = text[value_start:value_end].strip(" :–-")
        if value and label not in out:
            out[label] = value[:200]
    return out


def _build(url: str, title: str, fields: dict[str, str]) -> Job | None:
    match = _HUB_URL.search(url)
    if not match:
        return None
    _, company_slug, job_id = match.groups()

    deadline = normalize.parse_date(fields.get("Deadline"))
    # Employers state start dates as "June 2027"; month precision is fine here
    # and is never allowed for the deadline.
    start_date = normalize.parse_date(
        fields.get("Starting") or fields.get("Start date"), allow_month_precision=True
    )

    work_type = "Internship"
    if "/work-placement-internship/" in url:
        duration = (fields.get("Duration") or "").lower()
        work_type = "Industrial Placement" if "12 month" in duration else "Internship"
    elif "/graduate-job/" in url:
        work_type = "Graduate Scheme"

    return build_job(
        source=name,
        company=_company_from_slug(company_slug),
        title=title,
        source_url=url,
        # Gradcracker routes outbound clicks through /out, which robots.txt
        # disallows. The listing page is therefore the application entry point;
        # verification follows the employer link from the job page itself.
        application_url="",
        external_id=job_id,
        location=fields.get("Location", ""),
        work_type=work_type,
        deadline=deadline,
        salary=fields.get("Salary", ""),
        duration=fields.get("Duration", ""),
        start_date=start_date,
        description=fields.get("Disciplines", ""),
        degree_requirements=fields.get("Degree required", ""),
    )


def _parse_job_page(html: str, url: str) -> Job | None:
    soup = BeautifulSoup(html, "lxml")

    heading = soup.find(["h1", "h2"])
    title = " ".join(heading.get_text(" ", strip=True).split()) if heading else ""
    if not title:
        return None

    body = soup.find("main") or soup.body or soup
    text = " ".join(body.get_text(" ", strip=True).split())
    fields = _fields(text[:4000])

    job = _build(url, title, fields)
    if job is None:
        return None

    job.description = normalize.clean_description(text[:8000])
    job.skills, job.programming_languages, job.technologies = normalize.extract_skills(
        job.description
    )

    # The employer's own link, when the page exposes it directly rather than
    # through the robots-disallowed /out redirector.
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if href.startswith("http") and "gradcracker.com" not in href:
            label = anchor.get_text(" ", strip=True).casefold()
            if any(word in label for word in ("apply", "website", "vacancy", "more details")):
                job.application_url = href
                for ref in job.sources:
                    ref.application_url = href
                break

    return job


def _company_from_slug(slug: str) -> str:
    """'bank-of-america' -> 'Bank of America'. Normalization handles the rest."""
    words = slug.replace("_", "-").split("-")
    return " ".join(
        word if word in _SMALL_WORDS and i > 0 else word.capitalize()
        for i, word in enumerate(words)
    )
