"""Manual ingest: the fallback for when a platform cannot be fetched.

Two of the three platforms sit behind Cloudflare and will sometimes serve a
challenge that only a human can clear. Rather than trying to defeat that, this
adapter lets you hand the data over yourself:

    # a page you saved from the browser (Ctrl+S, "Webpage, Complete")
    python main.py ingest --file "saved-search.html" --source gradcracker

    # a plain text file of job URLs, one per line
    python main.py ingest --urls my-jobs.txt --source brightnetwork

    # a CSV you exported from anywhere
    python main.py ingest --csv jobs.csv

Everything downstream - normalization, dedupe, verification, scoring - is
identical to the automated path, so a manually ingested job is a first-class
row in the workbook.
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

from bs4 import BeautifulSoup

from sources import brightnetwork, gradcracker
from sources.base import build_job
from tracker import eligibility, graduation, normalize, programme
from tracker.models import Job

log = logging.getLogger(__name__)

name = "manual"

#: CSV headers accepted for each field, so an export from anywhere has a chance
#: of working without being reshaped by hand first.
_CSV_FIELDS = {
    "company": ("company", "employer", "organisation", "organization"),
    "title": ("title", "job title", "role", "job", "position", "name"),
    "location": ("location", "city", "where"),
    "deadline": ("deadline", "closing date", "closes", "apply by", "valid through"),
    "application_url": ("application url", "apply url", "apply link", "url", "link"),
    "source_url": ("source url", "listing url", "listing"),
    "work_type": ("work type", "type", "job type", "contract"),
    "salary": ("salary", "pay", "compensation"),
    "duration": ("duration", "length"),
    "description": ("description", "summary", "details"),
    "source": ("source", "platform", "site"),
}


def search_jobs(session, settings: dict) -> list[Job]:
    """Manual ingest is never part of an automated sweep."""
    return []


def get_job(session, url: str) -> Job | None:
    """Fetch a single job URL, routing to whichever adapter owns that host."""
    if "gradcracker.com" in url:
        return gradcracker.get_job(session, url)
    if "brightnetwork.co.uk" in url:
        return brightnetwork.get_job(session, url)
    return None


def from_html(path: Path, source: str) -> list[Job]:
    """Parse a saved search-results or job page using its platform's parser.

    This is the highest-value fallback: a page you saved in your own browser
    contains exactly the same markup the adapter expects, so the normal parser
    does the work with no special cases.
    """
    html = _read(path)

    if source == "gradcracker":
        jobs = gradcracker._parse_search_page(html, "https://www.gradcracker.com")
        if not jobs:
            job = gradcracker._parse_job_page(html, _guess_url(html) or str(path))
            jobs = [job] if job else []
        return jobs

    if source == "brightnetwork":
        job = brightnetwork._parse_job_page(html, _guess_url(html) or str(path))
        # A deadlines page ("Internships Deadlines 2027") lists many programmes.
        return [job] if job else from_brightnetwork_list(html)

    raise ValueError(
        f"No HTML parser for source '{source}'. Use --source gradcracker or brightnetwork, "
        "or supply the listings as CSV with --csv."
    )


#: A deadline heading on a Bright Network list: "30th Sep 2026".
_DEADLINE = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Z][a-z]{2,8}\s+20\d\d\b")

#: Saved result pages also list events that are not programmes.
_NOT_A_PROGRAMME = re.compile(r"\bwebinar\b", re.IGNORECASE)


def from_brightnetwork_list(html: str) -> list[Job]:
    """Programmes on a Bright Network application-deadlines page, grouped under
    date headings. Each is a lead - employer, programme, deadline and the Bright
    Network page - and never claims to be the employer's apply link."""
    soup = BeautifulSoup(html, "lxml")
    jobs: list[Job] = []
    seen: set[str] = set()
    for link in soup.select('li a[href*="/graduate-jobs/"]'):
        href = link.get("href", "")
        parts = [p for p in href.split("/graduate-jobs/", 1)[-1].split("/") if p]
        item = link.find_parent("li")
        name = item.find("span") if item else None
        title = " ".join(link.get_text(" ", strip=True).split())
        if len(parts) < 2 or href in seen or not title or name is None:
            continue
        seen.add(href)
        heading = link.find_previous(string=_DEADLINE)
        deadline = None
        if heading:
            written = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", _DEADLINE.search(heading).group(0))
            deadline = normalize.parse_date(written)
        jobs.append(build_job(source="brightnetwork", company=name.get_text(" ", strip=True), title=title,
                              source_url=href, external_id="/".join(parts[:2]), deadline=deadline,
                              work_type=programme.type_from_title(title)))
    return jobs


def from_folder(folder: Path, source: str) -> list[Job]:
    """Every page saved in a folder with Ctrl+S, skipping the "_files" folders a
    browser writes beside each page. A programme on several saved pages, or a
    page saved twice, becomes one listing; webinars are dropped."""
    found: dict[str, Job] = {}
    for path in sorted(Path(folder).glob("*.htm*")):
        for job in from_html(path, source):
            if _NOT_A_PROGRAMME.search(job.job_title):
                continue
            found.setdefault(job.sources[0].external_id or job.source_url, job)
    return list(found.values())


def prepare(jobs: list[Job], profile: dict) -> None:
    """Classify imported listings and read their eligibility, as scraped ones are.

    A search-results card has no description, so its own fields - duration,
    start, degree - are the text that is read.
    """
    for job in jobs:
        parts = [job.description or ""]
        if job.duration:
            parts.append(f"The programme duration is {job.duration}.")
        if job.start_date:
            parts.append(f"Starting {job.start_date:%B %Y}.")
        if job.degree_requirements:
            parts.append(f"Degree required: {job.degree_requirements}.")
        text = " ".join(p for p in parts if p)
        programme.refine(job, text)
        graduation.apply(job, text, profile)
        eligibility.assess(job, text, profile)


def from_urls(session, path: Path) -> list[Job]:
    """Fetch each URL in a text file, one per line. Blank lines and # ignored."""
    jobs: list[Job] = []
    for line in _read(path).splitlines():
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        try:
            job = get_job(session, url)
        except Exception as exc:                    # one bad URL must not stop the batch
            log.warning("Could not fetch %s: %s", url, exc)
            continue
        if job is None:
            log.warning("No parser matched %s - skipped", url)
        else:
            jobs.append(job)
    return jobs


def from_csv(path: Path, default_source: str = "manual") -> list[Job]:
    """Read listings from a CSV, matching columns by fuzzy header name."""
    jobs: list[Job] = []

    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return []
        lookup = {name.strip().casefold(): name for name in reader.fieldnames}

        for row in reader:
            def value(field: str) -> str:
                for candidate in _CSV_FIELDS[field]:
                    if candidate in lookup:
                        return (row.get(lookup[candidate]) or "").strip()
                return ""

            company, title = value("company"), value("title")
            if not company or not title:
                continue                            # not enough to identify a job

            source_url = value("source_url") or value("application_url")
            jobs.append(
                build_job(
                    source=value("source") or default_source,
                    company=company,
                    title=title,
                    source_url=source_url,
                    application_url=value("application_url") or source_url,
                    location=value("location"),
                    work_type=value("work_type") or "Internship",
                    # A date that will not parse becomes None, never a guess.
                    deadline=normalize.parse_date(value("deadline")),
                    salary=value("salary"),
                    duration=value("duration"),
                    description=value("description"),
                )
            )

    return jobs


def _read(path: Path) -> str:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No such file: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def _guess_url(html: str) -> str:
    """Recover the page's own URL from its canonical link or og:url.

    A saved page keeps these, which is what lets an ingested job carry a real
    application link rather than a local file path.
    """
    soup = BeautifulSoup(html, "lxml")

    canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
    if canonical and canonical.get("href", "").startswith("http"):
        return canonical["href"]

    og = soup.find("meta", property="og:url")
    if og and og.get("content", "").startswith("http"):
        return og["content"]

    return ""
