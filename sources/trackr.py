"""The Trackr adapter.

Reads the JSON endpoint the Trackr web app itself calls:

    GET https://api.the-trackr.com/programmes?region=UK&industry=Tech
        &season=2027&type=summer-internships

No browser needed, no login, and the response carries a `url` field pointing at
the employer's own application page - which is the single most valuable field
any of the three platforms gives us, because it is where the real deadline
lives.

Terms note: Trackr's Terms of Use restrict automated access. This adapter is
enabled at the user's explicit direction for personal, non-commercial use, at a
conservative request rate. See README.md "Scraping limitations".
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime

from sources.base import build_job
from sources.fetch import SourceError
from tracker import config, normalize
from tracker.models import Job

log = logging.getLogger(__name__)

name = "trackr"

#: The API returns an empty list unless the request carries a `sessionid`
#: header. This is NOT authentication: the board is public, no login exists for
#: it, and the app mints the value itself as `sessionId` in localStorage. We do
#: the same, and persist ours so we present as one steady client across runs
#: rather than a new one on every request.
_SESSION_FILE = "trackr_session.txt"


def _session_id() -> str:
    path = config.STATE_DIR / _SESSION_FILE
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    fresh = str(uuid.uuid4())
    config.ensure_dirs()
    path.write_text(fresh, encoding="utf-8")
    return fresh

#: Trackr's `type` slug -> our canonical work type.
_WORK_TYPES = {
    "summer-internships": "Summer Internship",
    "industrial-placements": "Industrial Placement",
    "graduate-schemes": "Graduate Scheme",
    "spring-weeks": "Spring Week",
    "pre-uni": "Pre-university Programme",
    "events": "Event",
}


def search_jobs(session, settings: dict) -> list[Job]:
    """Sweep the configured region x industry x season x type matrix."""
    api = settings.get("api_base", "https://api.the-trackr.com").rstrip("/")
    board = settings.get("board_base", "https://app.the-trackr.com").rstrip("/")

    jobs: list[Job] = []
    seen: set[str] = set()
    empty_slices = attempted = 0

    for region in settings.get("regions", ["UK"]):
        for industry in settings.get("industries", ["Tech"]):
            for season in settings.get("seasons", []):
                for type_slug in settings.get("types", []):
                    attempted += 1
                    url = (
                        f"{api}/programmes?region={region}&industry={industry}"
                        f"&season={season}&type={type_slug}"
                    )
                    try:
                        programmes = _get_programmes(session, url)
                    except SourceError as exc:
                        # One slice failing must not lose the other slices.
                        log.warning("Trackr slice %s/%s failed: %s", season, type_slug, exc)
                        empty_slices += 1
                        continue

                    if not programmes:
                        empty_slices += 1

                    board_url = f"{board}/{region.lower()}-{industry.lower()}/{type_slug}"
                    for entry in programmes:
                        job = _to_job(entry, board_url, type_slug)
                        if job is None:
                            continue
                        key = entry.get("id") or f"{job.normalized_company}|{job.normalized_title}"
                        if key in seen:
                            continue
                        seen.add(key)
                        jobs.append(job)

    # Trackr answers a bare `[]` rather than an error when it will not serve a
    # request - most often because the `sessionid` header was missing or has
    # been rejected. Reporting that as "0 internships today" would be wrong and
    # would quietly wipe the workbook's Trackr rows, so it is raised as a
    # failure and the caller falls back to the last good snapshot.
    if attempted and empty_slices == attempted:
        raise SourceError(
            "Trackr returned no programmes for any query. Delete "
            f"data/state/{_SESSION_FILE} to mint a fresh session id and retry; if that "
            "does not help the API contract has changed - check what "
            "app.the-trackr.com sends and update sources/trackr.py."
        )

    return jobs


def get_job(session, url: str) -> Job | None:
    """Trackr has no per-job API route; listings only arrive via search."""
    return None


def _get_programmes(session, url: str) -> list[dict]:
    """The programme records for one query.

    The endpoint answers with either {"programmes": [...]} or a bare list, so
    both shapes are accepted rather than assuming the one seen most often.
    """
    response = session.http.get(
        url,
        expect_json=True,
        headers={"sessionid": _session_id(), "Referer": "https://app.the-trackr.com/"},
    )
    if not response.ok:
        raise SourceError(f"Trackr returned HTTP {response.status} for {url}")

    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise SourceError(f"Trackr returned invalid JSON for {url}: {exc}") from exc

    if isinstance(payload, dict):
        return payload.get("programmes") or []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _to_job(entry: dict, board_url: str, type_slug: str) -> Job | None:
    """Convert one API record. Returns None for records too incomplete to use."""
    title = entry.get("name") or ""
    company_data = entry.get("company") or {}
    company = company_data.get("name") or entry.get("companyId") or ""

    if not title or not company:
        return None

    deadline = _parse_api_date(entry.get("closingDate"))
    opens = _parse_api_date(entry.get("openingDate"))

    job = build_job(
        source=name,
        company=company,
        title=title,
        source_url=board_url,
        # The employer's own application page. Trackr appends its own UTM
        # parameters; they are harmless and left intact so the link works.
        application_url=entry.get("url") or "",
        external_id=entry.get("id") or "",
        location=_location(entry),
        work_type=_WORK_TYPES.get(type_slug, type_slug.replace("-", " ").title()),
        deadline=deadline,
        date_posted=opens,
        description=_description(entry, company_data, opens),
        cv=_material(entry.get("cv")),
        written_answers=_material(entry.get("writtenAnswers")),
        cover_letter=_material(entry.get("coverLetter")),
    )

    # Trackr states its categories explicitly; they are better evidence of what
    # the role is than anything inferred from the title.
    categories = [c for c in (entry.get("categories") or []) if c]
    if categories:
        job.skills = normalize.skills(job.skills + categories)

    return job


def _location(entry: dict) -> str:
    """Trackr's `locations` is often empty, but `region` is always set and is
    real data from the API - not a guess."""
    locations = [l for l in (entry.get("locations") or []) if l]
    if locations:
        return ", ".join(locations)
    region = entry.get("region") or ""
    return "United Kingdom" if region.upper() == "UK" else region


def _description(entry: dict, company_data: dict, opens: date | None) -> str:
    """Trackr describes the company, not the role, so the record is assembled
    from the structured fields it does provide rather than invented prose."""
    parts: list[str] = []

    if entry.get("rolling"):
        parts.append("Rolling applications - apply early")
    if opens:
        parts.append(f"Opens {opens.isoformat()}")
    if company_data.get("sponsorsVisa") is not None:
        parts.append("Sponsors visa" if company_data["sponsorsVisa"] else "No visa sponsorship")

    divisions = [d.replace("|", " / ") for d in (entry.get("divisions") or []) if d]
    if divisions:
        parts.append("Divisions: " + "; ".join(divisions))

    categories = [c for c in (entry.get("categories") or []) if c]
    if categories:
        parts.append("Categories: " + ", ".join(categories))

    for label, key in (("Eligibility", "eligibility"), ("Process", "process"), ("Notes", "notes")):
        if entry.get(key):
            parts.append(f"{label}: {entry[key]}")

    if company_data.get("description"):
        parts.append("About " + (company_data.get("name") or "the company") + ": "
                     + company_data["description"])

    return " | ".join(parts)


def _material(value) -> str:
    """One application-material flag (cv / writtenAnswers / coverLetter).

    Trackr sends booleans for `cv` and "Yes" / "No" / "Optional" strings for
    the others. A missing or null value stays "" - not stated is not "No".
    """
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value).strip()
    return {"yes": "Yes", "no": "No", "optional": "Optional",
            "true": "Yes", "false": "No"}.get(text.casefold(), text)


def _parse_api_date(value) -> date | None:
    """Trackr returns ISO-8601 with a Z suffix. Anything else yields None
    rather than a guessed date."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return normalize.parse_date(value)
