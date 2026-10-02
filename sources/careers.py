"""Company careers sites: employers' own job boards, read from their public feeds.

Most employers publish openings through a hosted careers platform (Greenhouse,
Lever, Ashby, Workable, Workday, Oracle Recruiting Cloud, Eightfold) or their
own public search (Amazon). Each exposes the JSON its own careers page is
rendered from, with no login. Reading it gets the posting straight from the
employer - the same page the user applies on - which is better evidence than
any aggregator's copy.

Boards come from two places:

* discovered: application links the other sources already hold. A Trackr
  listing that links to boards.greenhouse.io/point72/... tells us Point72's
  board, so every company already in the workbook is searched in full. That is
  how a firm's spring week and placement are found when an aggregator only
  listed its summer internship;
* configured: `boards` under `careers` in config/sources.yaml, e.g. companies
  found by web search that no platform lists yet.

Only student roles (internships, placements, spring weeks) in technology, based
in the UK, are kept; the filters live in config. A programme named some other
way ("Discovery Programme") counts when its requirements say which students
may apply - see tracker/programme.py. SmartRecruiters and Google Careers are
not supported because their robots.txt disallows the routes a feed needs.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, quote, urlparse

from bs4 import BeautifulSoup

from sources.base import build_job
from sources.fetch import SourceBlocked, SourceError
from tracker import config, eligibility, graduation, normalize, programme
from tracker.models import Job, parse_iso_date

log = logging.getLogger(__name__)

name = "careers"

PLATFORM_NAMES = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "workable": "Workable",
    "workday": "Workday",
    "oracle": "Oracle",
    "eightfold": "Eightfold",
    "amazon": "Amazon Jobs",
    "oleeo": "Oleeo",
}

#: How a board is recognised in an application link. Groups form the board id.
_BOARD_PATTERNS = {
    "greenhouse": re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?!embed/)([A-Za-z0-9_-]+)"),
    "lever": re.compile(r"jobs\.(eu\.)?lever\.co/([A-Za-z0-9_.-]+)"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    "workable": re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    "workday": re.compile(
        r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"),
    "oracle": re.compile(
        r"https?://([a-z0-9-]+\.fa\.(?:[a-z0-9-]+\.)*oraclecloud\.com)"
        r"/hcmUI/CandidateExperience/[A-Za-z_-]+/sites/([A-Za-z0-9_-]+)"),
    "eightfold": re.compile(r"https?://([a-z0-9-]+\.eightfold\.ai|[a-z]+\.careers\.microsoft\.com)/"),
    "amazon": re.compile(r"https?://(?:www\.)?amazon\.jobs/"),
    "oleeo": re.compile(
        r"https?://([a-z0-9-]+\.tal\.net)(/vx/[^?#]*?/candidate/)(?:so/pm/\d+/pl/(\d+)|jobboard/vacancy/(\d+))"),
}


@dataclass(frozen=True)
class Board:
    platform: str
    ident: tuple[str, ...]      # the platform's own board id, e.g. ("point72",)
    company: str
    url: str = field(default="", compare=False)    # where the board is read, when its id is not enough

    @property
    def key(self) -> str:
        return f"{self.platform}:{':'.join(self.ident)}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def search_jobs(session, settings: dict) -> list[Job]:
    """Read every known board and keep the UK student tech roles.

    One board failing never loses the others: its postings from the last good
    run are reused, as the pipeline does for a whole source.
    """
    platforms = set(settings.get("platforms") or PLATFORM_NAMES)
    boards = [b for b in discover_boards(settings) if b.platform in platforms]
    if not boards:
        log.info("[careers] no boards to read")
        return []

    seconds = settings.get("rate_limit_seconds")
    if seconds:
        hosts = {"boards-api.greenhouse.io", "api.lever.co", "api.eu.lever.co",
                 "api.ashbyhq.com", "apply.workable.com"}
        hosts |= {b.ident[0] for b in boards if b.platform in ("oracle", "eightfold", "amazon")}
        for host in sorted(hosts):
            session.limiter.configure(host, seconds)

    wanted = _Filter(settings)
    target_year = config.load_profile().get("graduation_year")
    previous = _previous_by_board()
    jobs: list[Job] = []
    postings_seen: set[str] = set()
    failed = 0

    def add(job: Job) -> None:
        # A tenant can publish one requisition on several career sites (BNY's
        # CX_1001 and BNY-Careers). One employer posting is one row.
        key = normalize.posting_key(job.application_url)
        if key and key in postings_seen:
            return
        if key:
            postings_seen.add(key)
        jobs.append(job)

    # In key order, so when two boards show one posting the same board keeps
    # it on every run and the row's ID never moves.
    for board in sorted(boards, key=lambda b: b.key):
        try:
            postings = _READERS[board.platform](session, board, settings, wanted)
        except SourceError as exc:
            failed += 1
            reused = previous.get(board.key, [])
            log.warning("[careers] %s (%s) failed: %s - reusing %d from last run",
                        board.company, board.key, exc, len(reused))
            for job in reused:
                add(job)
            continue

        for posting in postings:
            if wanted.keep(posting, board.company) or wanted.for_graduate(posting, target_year):
                add(_to_job(board, posting, wanted))

    if failed == len(boards):
        raise SourceError(f"every careers board failed ({failed})")
    log.info("[careers] %d boards read, %d failed, %d roles kept", len(boards) - failed, failed, len(jobs))
    return jobs


def get_job(session, url: str) -> Job | None:
    """Postings arrive through their board's feed; there is no single-job route."""
    return None


# ---------------------------------------------------------------------------
# Boards
# ---------------------------------------------------------------------------


def discover_boards(settings: dict) -> list[Board]:
    """Configured boards first, then any found in other sources' application
    links. The first company name seen for a board is kept."""
    found: dict[str, Board] = {}

    for spec in settings.get("boards") or []:
        board = board_from_url(str(spec.get("url", "")), str(spec.get("company", "")))
        if board and board.company:
            found.setdefault(board.key, board)
        else:
            log.warning("[careers] board entry not recognised, skipped: %s", spec)

    if settings.get("discover_from_sources", True):
        for company, url in _known_application_links():
            board = board_from_url(url, company)
            if board:
                found.setdefault(board.key, board)

    return list(found.values())


def board_from_url(url: str, company: str) -> Board | None:
    """The careers board a posting URL belongs to, if it is on a known platform."""
    for platform, pattern in _BOARD_PATTERNS.items():
        match = pattern.search(url or "")
        if not match:
            continue
        if platform == "lever":
            region, token = match.groups()
            return Board(platform, ("eu" if region else "global", token.casefold()), company)
        if platform == "eightfold":
            ident = _eightfold_ident(match.group(1).casefold(), url)
            return Board(platform, ident, company) if ident else None
        if platform == "amazon":
            return Board(platform, ("www.amazon.jobs",), company)
        if platform == "oleeo":
            # The link's session tokens vary, so the board is keyed by host and
            # board number; the first link seen supplies the address.
            host, prefix, from_posting, from_board = match.groups()
            number = from_posting or from_board
            return Board(platform, (host.casefold(), number), company,
                         f"https://{host}{prefix}jobboard/vacancy/{number}/adv/")
        return Board(platform, tuple(g for g in match.groups()), company)
    return None


def _eightfold_ident(host: str, url: str) -> tuple[str, str] | None:
    """(api host, employer domain). Eightfold's search is scoped by the
    employer's domain, which the link usually names; a tenant host without one
    is tried as <tenant>.com, and a wrong guess simply finds nothing."""
    if host.endswith("careers.microsoft.com"):
        return ("apply.careers.microsoft.com", "microsoft.com")
    domain = (parse_qs(urlparse(url).query).get("domain") or [""])[0].strip().casefold()
    if not domain:
        tenant = host.split(".", 1)[0]
        if tenant == "app":
            return None     # the shared host names no employer
        domain = f"{tenant}.com"
    return (host, domain)


def _known_application_links() -> list[tuple[str, str]]:
    """(company, application url) from every other source's last snapshot and
    the last merged run."""
    paths = [p for p in sorted(config.SNAPSHOT_DIR.glob("*.json")) if p.stem != name]
    paths.append(config.STATE_DIR / "last_run.json")

    links: list[tuple[str, str]] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for job in payload.get("jobs", []):
            company = job.get("company") or ""
            for url in [job.get("application_url")] + [s.get("application_url")
                                                      for s in job.get("sources", [])]:
                if company and url:
                    links.append((company, url))
    return links


def _previous_by_board() -> dict[str, list[Job]]:
    """Last good run's postings per board, for a board that fails today."""
    path = config.SNAPSHOT_DIR / f"{name}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, list[Job]] = {}
    for data in payload.get("jobs", []):
        job = Job.from_dict(data)
        for ref in job.sources:
            if ref.source == name and ref.external_id.count(":") >= 2:
                board_key = ref.external_id.rsplit(":", 1)[0]
                out.setdefault(board_key, []).append(job)
    return out


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _terms(words) -> re.Pattern | None:
    words = [re.escape(str(w).casefold()) for w in (words or []) if str(w).strip()]
    return re.compile(r"\b(?:" + "|".join(words) + r")\b") if words else None


class _Filter:
    """Student role + technology + UK, as configured."""

    def __init__(self, settings: dict) -> None:
        self.student = _terms(settings.get("student_terms"))
        self.tech_title = _terms(settings.get("tech_terms"))
        self.tech_team = _terms((settings.get("tech_terms") or []) + (settings.get("tech_team_terms") or []))
        self.exclude = _terms(settings.get("exclude_terms"))
        self.non_tech = _terms(settings.get("non_tech_function_terms"))
        self.other_field = _terms(settings.get("other_field_terms"))
        self.uk = _terms(settings.get("uk_terms"))
        self.uk_places = _terms(settings.get("uk_places"))
        self.foreign = _terms(settings.get("foreign_markers"))
        self.foreign_places = _terms(settings.get("oleeo_foreign_places"))
        self.false_friends = _terms(settings.get("uk_false_friends"))

    def excluded(self, title: str) -> bool:
        return bool(self.exclude and self.exclude.search(title.casefold()))

    def student_title(self, title: str, employment: str = "") -> bool:
        text = f"{title} {employment}".casefold()
        if self.excluded(title):
            return False
        return bool(self.student and self.student.search(text))

    def tech_in_title(self, title: str) -> bool:
        return bool(self.tech_title and self.tech_title.search(title.casefold()))

    def generic_programme(self, title: str, company: str = "") -> bool:
        """A firm-wide programme names no function of its own, whether it is
        called "Spring Insight Programme 2027" or given a brand name ("Future
        Leaders", "Discovery"). On a board the other sources already tied to
        tech it is the tech intake too, so it is not dropped for lacking a tech
        word. Only a title naming a function of its own is - a brand name names
        none, and dropping what cannot be parsed hides the first-year intake
        aggregators leave out."""
        text = title.casefold()
        if not (self.student_title(title) or programme.is_programme_title(title)):
            return False
        for pattern in (self.student, self.uk, self.uk_places):
            if pattern:
                text = pattern.sub(" ", text)
        return not any(p and p.search(text) for p in (self.non_tech, self.other_field))

    def screen(self, title: str, company: str = "", employment: str = "") -> bool:
        """Title-only check for platforms whose search results must be opened
        one by one (Workday, Oracle, Eightfold). Only these are opened, or one
        board costs hundreds of requests."""
        if self.excluded(title):
            return False
        student = self.student_title(title, employment) or programme.is_programme_title(title)
        return student and (self.tech_in_title(title) or self.generic_programme(title, company))

    def keep(self, p: dict, company: str = "") -> bool:
        title, employment = p.get("title") or "", p.get("employment") or ""
        if self.excluded(title):
            return False
        # A title may not say "intern" at all; requirements naming an eligible
        # year of study show it is a student programme all the same.
        student = self.student_title(title, employment) or bool(
            programme.classify(title, p.get("description") or "", employment).evidence)
        if not student:
            return False
        tech = (self.tech_in_title(title)
                or bool(self.tech_team and self.tech_team.search((p.get("team") or "").casefold()))
                or self.generic_programme(title, company))
        return tech and self.is_uk(p.get("location") or "")

    def for_graduate(self, p: dict, target_year) -> bool:
        """A UK posting whose requirements admit the user's graduation year,
        whatever it is called and whatever team it sits in. The requirement
        the user set outranks the student and tech screens."""
        title = p.get("title") or ""
        if not target_year or self.excluded(title):
            return False
        years, _ = graduation.expected_years(title, p.get("description") or "")
        return int(target_year) in years and self.is_uk(p.get("location") or "")

    def foreign_listing(self, text: str) -> bool:
        """A board row naming a place outside the UK and no UK place."""
        low = text.casefold()
        if (self.uk and self.uk.search(low)) or (self.uk_places and self.uk_places.search(low)):
            return False
        return bool(self.foreign_places and self.foreign_places.search(low))

    def is_uk(self, location: str) -> bool:
        """A UK country name anywhere, or a UK city with no sign of a
        same-named place abroad (Cambridge MA, London Ontario). Places abroad
        whose names contain a UK country ("New South Wales") are removed first."""
        text = location.casefold()
        if self.false_friends:
            text = self.false_friends.sub(" ", text)
        if self.uk and self.uk.search(text):
            return True
        if self.uk_places and self.uk_places.search(text):
            return not (self.foreign and self.foreign.search(text))
        return False


# ---------------------------------------------------------------------------
# Platform readers: each returns normalized posting dicts
# ---------------------------------------------------------------------------


def _json(session, url: str) -> object:
    response = session.http.get(url, expect_json=True)
    if not response.ok:
        raise SourceError(f"HTTP {response.status} for {url}")
    try:
        return json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise SourceError(f"invalid JSON from {url}: {exc}") from exc


def _queries(settings: dict) -> list[str]:
    return list(settings.get("search_queries") or ["intern"])


def _greenhouse(session, board: Board, settings: dict, wanted: _Filter) -> list[dict]:
    token = board.ident[0]
    data = _json(session, f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    out = []
    for j in (data or {}).get("jobs", []):
        offices = ", ".join(o.get("location") or o.get("name") or "" for o in j.get("offices") or [])
        out.append({
            "id": str(j.get("id", "")),
            "title": j.get("title", ""),
            "location": "; ".join(x for x in ((j.get("location") or {}).get("name"), offices) if x),
            "url": j.get("absolute_url", ""),
            "team": " ".join(d.get("name", "") for d in j.get("departments") or []),
            "description": _text(html.unescape(j.get("content") or "")),
            # updated_at changes on every edit; only first_published is the posting date.
            "posted": _posted(j.get("first_published")),
        })
    return out


def _lever(session, board: Board, settings: dict, wanted: _Filter) -> list[dict]:
    region, token = board.ident
    host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
    data = _json(session, f"https://{host}/v0/postings/{token}?mode=json")
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        locations = cats.get("allLocations") or [cats.get("location") or ""]
        out.append({
            "id": j.get("id", ""),
            "title": j.get("text", ""),
            "location": "; ".join(filter(None, locations + [j.get("country") or ""])),
            "url": j.get("hostedUrl", ""),
            "apply_url": j.get("applyUrl", ""),
            "team": f"{cats.get('department') or ''} {cats.get('team') or ''}",
            "employment": cats.get("commitment") or "",
            # Lever keeps "What We Require" and similar sections in `lists`, apart
            # from the description - that is where "Graduating in 2028" lives.
            "description": normalize.clean_description(" ".join(filter(None, [
                j.get("descriptionPlain") or "",
                *(f"{item.get('text') or ''}: {_text(item.get('content') or '')}" for item in j.get("lists") or []),
                j.get("additionalPlain") or ""]))),
            "posted": _posted(j.get("createdAt")),
        })
    return out


def _ashby(session, board: Board, settings: dict, wanted: _Filter) -> list[dict]:
    token = board.ident[0]
    data = _json(session, f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false")
    out = []
    for j in (data or {}).get("jobs", []):
        if j.get("isListed") is False:
            continue
        locations = [j.get("location") or ""] + [
            s.get("location") or "" for s in j.get("secondaryLocations") or []]
        country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or "")
        out.append({
            "id": j.get("id", ""),
            "title": j.get("title", ""),
            "location": "; ".join(filter(None, locations + [country])),
            "url": j.get("jobUrl", ""),
            "apply_url": j.get("applyUrl", ""),
            "team": f"{j.get('department') or ''} {j.get('team') or ''}",
            "employment": j.get("employmentType") or "",
            "description": normalize.clean_description(j.get("descriptionPlain") or ""),
            "posted": _posted(j.get("publishedAt")),
        })
    return out


def _workable(session, board: Board, settings: dict, wanted: _Filter) -> list[dict]:
    token = board.ident[0]
    data = _json(session, f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true")
    out = []
    for j in (data or {}).get("jobs", []):
        places = [", ".join(filter(None, (l.get("city"), l.get("country"))))
                  for l in j.get("locations") or []]
        places.append(", ".join(filter(None, (j.get("city"), j.get("country")))))
        out.append({
            "id": j.get("shortcode", ""),
            "title": j.get("title", ""),
            "location": "; ".join(filter(None, places)),
            "url": j.get("url") or j.get("shortlink") or "",
            "apply_url": j.get("application_url", ""),
            "team": j.get("department") or "",
            "employment": j.get("employment_type") or "",
            "description": _text(j.get("description") or ""),
            "posted": _posted(j.get("published_on") or j.get("created_at")),
        })
    return out


def _workday(session, board: Board, settings: dict, wanted: _Filter) -> list[dict]:
    """Workday has no list-everything feed, only the search its careers page
    calls. Each configured query is paged, titles are screened first, and only
    screened postings are opened for location and description."""
    tenant, wd, site = board.ident
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}"
    page_size, max_pages = 20, int(settings.get("workday_max_pages", 5))

    seen: dict[str, dict] = {}
    for query in settings.get("workday_queries") or ["intern"]:
        for page in range(max_pages):
            response = session.http.post_json(
                f"{api}/jobs",
                {"appliedFacets": {}, "limit": page_size, "offset": page * page_size, "searchText": query},
            )
            if not response.ok:
                raise SourceError(f"HTTP {response.status} for {api}/jobs")
            try:
                data = json.loads(response.text)
            except json.JSONDecodeError as exc:
                raise SourceError(f"invalid JSON from {api}/jobs: {exc}") from exc
            postings = data.get("jobPostings") or []
            for p in postings:
                # Workday search results carry no department, so the title alone
                # must show a student tech role before the posting is opened.
                path, title = p.get("externalPath") or "", p.get("title") or ""
                if path and wanted.screen(title, board.company):
                    seen.setdefault(path, p)
            if len(postings) < page_size or (page + 1) * page_size >= int(data.get("total") or 0):
                break

    out = []
    for path, p in seen.items():
        info = (_json(session, f"{api}{path}") or {}).get("jobPostingInfo") or {}
        locations = [info.get("location") or p.get("locationsText") or ""] + list(
            info.get("additionalLocations") or [])
        country = (info.get("country") or {}).get("descriptor") or ""
        out.append({
            "id": info.get("jobReqId") or path.rsplit("_", 1)[-1],
            "title": info.get("title") or p.get("title") or "",
            "location": "; ".join(filter(None, locations + [country])),
            "url": info.get("externalUrl") or f"{base}/{site}{path}",
            "employment": info.get("timeType") or "",
            "description": _text(info.get("jobDescription") or ""),
            # startDate is the posting's start; postedOn is relative text ("30+ Days Ago").
            "posted": _posted(info.get("startDate")),
        })
    return out


def _oracle(session, board: Board, settings: dict, wanted: _Filter) -> list[dict]:
    """Oracle Recruiting Cloud: the candidate site's own search, UK only.
    Search results carry no description, so screened titles are opened."""
    host, site = board.ident
    api = f"https://{host}/hcmRestApi/resources/latest"
    page_size, max_pages = 25, int(settings.get("search_max_pages", 4))

    seen: dict[str, dict] = {}
    for query in _queries(settings):
        for page in range(max_pages):
            data = _json(session, (
                f"{api}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations"
                f"&finder=findReqs;siteNumber={site},keyword=%22{quote(query)}%22,"
                f"location=United%20Kingdom,limit={page_size},offset={page * page_size}"))
            head = ((data or {}).get("items") or [{}])[0]
            requisitions = head.get("requisitionList") or []
            for r in requisitions:
                if r.get("Id") and wanted.screen(r.get("Title") or "", board.company):
                    seen.setdefault(str(r["Id"]), r)
            if len(requisitions) < page_size or (page + 1) * page_size >= int(head.get("TotalJobsCount") or 0):
                break

    out = []
    for req_id, r in seen.items():
        detail = _json(session, (
            f"{api}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
            f"&finder=ById;Id=%22{quote(req_id)}%22,siteNumber={site}"))
        info = ((detail or {}).get("items") or [{}])[0]
        description = " ".join(filter(None, (info.get("ExternalDescriptionStr"),
                                             info.get("ExternalResponsibilitiesStr"),
                                             info.get("ExternalQualificationsStr"))))
        out.append({
            "id": req_id,
            "title": info.get("Title") or r.get("Title") or "",
            "location": "; ".join(filter(None, (r.get("PrimaryLocation"), r.get("PrimaryLocationCountry")))),
            "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{req_id}",
            "team": r.get("JobFunction") or "",
            "description": _text(description),
            "posted": _posted(r.get("PostedDate") or info.get("ExternalPostedStartDate")),
        })
    return out


def _eightfold(session, board: Board, settings: dict, wanted: _Filter) -> list[dict]:
    """Eightfold (Microsoft, AstraZeneca, PayPal...): the careers page's own
    search API, UK only, then each screened position's details."""
    host, domain = board.ident
    base = f"https://{host}"
    page_size, max_pages = 10, int(settings.get("search_max_pages", 4))

    seen: dict[str, dict] = {}
    for query in _queries(settings):
        for page in range(max_pages):
            data = _json(session, (
                f"{base}/api/pcsx/search?domain={quote(domain)}&query={quote(query)}"
                f"&location=United%20Kingdom&start={page * page_size}&num={page_size}"))
            positions = ((data or {}).get("data") or {}).get("positions") or []
            for p in positions:
                if p.get("id") and wanted.screen(p.get("name") or "", board.company):
                    seen.setdefault(str(p["id"]), p)
            if len(positions) < page_size:
                break

    out = []
    for position_id, p in seen.items():
        info = (_json(session, (
            f"{base}/api/pcsx/position_details?position_id={position_id}"
            f"&domain={quote(domain)}&hl=en")) or {}).get("data") or {}
        locations = list(info.get("locations") or p.get("locations") or []) + list(
            info.get("standardizedLocations") or p.get("standardizedLocations") or [])
        out.append({
            "id": position_id,
            "title": info.get("name") or p.get("name") or "",
            "location": "; ".join(filter(None, locations)),
            "url": f"{base}/careers/job/{position_id}",
            "team": info.get("department") or p.get("department") or "",
            "description": _text(info.get("jobDescription") or ""),
            "posted": _posted(info.get("postedTs") or p.get("postedTs")),
        })
    return out


def _amazon(session, board: Board, settings: dict, wanted: _Filter) -> list[dict]:
    """amazon.jobs search, UK only. Descriptions arrive with the results, so
    nothing needs opening."""
    host = board.ident[0]
    page_size, max_pages = 100, int(settings.get("search_max_pages", 4))

    seen: dict[str, dict] = {}
    for query in _queries(settings):
        for page in range(max_pages):
            data = _json(session, (
                f"https://{host}/en/search.json?base_query={quote(query)}&country=GBR"
                f"&result_limit={page_size}&offset={page * page_size}"))
            results = (data or {}).get("jobs") or []
            for j in results:
                job_id = str(j.get("id_icims") or j.get("id") or "")
                if job_id:
                    seen.setdefault(job_id, j)
            if len(results) < page_size:
                break

    out = []
    for job_id, j in seen.items():
        description = " ".join(filter(None, (j.get("description"), j.get("basic_qualifications"),
                                             j.get("preferred_qualifications"))))
        out.append({
            "id": job_id,
            "title": j.get("title") or "",
            "location": "; ".join(filter(None, (j.get("normalized_location"), j.get("location")))),
            "url": f"https://{host}{j.get('job_path') or ''}",
            "team": j.get("job_category") or "",
            "employment": "Intern" if j.get("is_intern") is True else "",
            "description": _text(description),
            "posted": _posted(j.get("posted_date")),
        })
    return out


def _oleeo_page(session, url: str) -> BeautifulSoup:
    response = session.http.get(url)
    if not response.ok:
        raise SourceError(f"HTTP {response.status} for {url}")
    low = response.text.casefold()
    markers = config.load("sources").get("browser", {}).get("challenge_markers") or []
    if any(str(marker).casefold() in low for marker in markers):
        raise SourceBlocked(f"{urlparse(url).netloc} served an anti-bot check; this board is skipped, not bypassed")
    return BeautifulSoup(response.text, "lxml")


def _oleeo_fields(page: BeautifulSoup) -> dict[str, str]:
    """Oleeo renders a posting as label / value pairs: Countries, Cities,
    Program, Job description."""
    out: dict[str, str] = {}
    for label in page.select("label.eform_field_label"):
        value = label.find_next_sibling("div")
        name = label.get_text(" ", strip=True)
        if name and value is not None:
            out.setdefault(name, " ".join(value.get_text(" ", strip=True).split()))
    return out


def _labelled_date(text: str):
    """A deadline only where the posting labels one (verification.yaml's
    labels); a bare date is as likely a start date."""
    for label in config.load("verification").get("deadline_labels") or []:
        for match in re.finditer(re.escape(label), text or "", flags=re.IGNORECASE):
            window = re.sub(r"^s?\s*[:\-]?\s*", "", text[match.end(): match.end() + 60])
            found = normalize.parse_date(window)
            if found:
                return found
    return None


def _oleeo(session, board: Board, settings: dict, wanted: _Filter) -> list[dict]:
    """Oleeo job boards (BlackRock, Morgan Stanley, Nomura, Jefferies...):
    plain HTML, read at the crawl delay their robots.txt asks for. The board
    lists no requirements, so every posting is opened - except rows naming a
    place outside the UK."""
    host = board.ident[0]
    session.limiter.configure(host, float(settings.get("oleeo_rate_limit_seconds", 10)))

    rows: dict[str, tuple[str, str]] = {}
    start = 0
    for page_number in range(int(settings.get("oleeo_max_pages", 10))):
        try:
            page = _oleeo_page(session, f"{board.url}?start={start}" if start else board.url)
        except SourceBlocked:
            raise
        except SourceError:
            if page_number == 0:
                raise
            break       # a later page failing keeps what the earlier pages listed
        for row in page.select("tr.search_res") or page.select("a[href*='/opp/']"):
            link = row if row.name == "a" else row.select_one("a[href*='/opp/']")
            found = re.search(r"/opp/(\d+)", link.get("href", "")) if link else None
            if not found or found.group(1) in rows:
                continue
            title = " ".join((row.get("data-title") or link.get_text(" ", strip=True)).split())
            if not wanted.foreign_listing(f"{title} {row.get_text(' ', strip=True)}"):
                rows[found.group(1)] = (title, link["href"].split("?")[0])
        following = next((a for a in page.select("a[href*='start=']")
                          if "next" in a.get_text().casefold()), None)
        if following is None:
            break
        start = int(re.search(r"start=(\d+)", following["href"]).group(1))

    out = []
    for posting_id, (title, url) in rows.items():
        try:
            page = _oleeo_page(session, url)
        except SourceBlocked:
            raise
        except SourceError as exc:
            log.info("[careers] %s posting %s skipped: %s", board.company, posting_id, exc)
            continue
        fields = _oleeo_fields(page)
        description = fields.get("Job description") or ""
        place = fields.get("Cities") or fields.get("City") or fields.get("Location") or ""
        country = fields.get("Countries") or fields.get("Region") or ""
        out.append({
            "id": posting_id,
            "title": title,
            "location": "; ".join(filter(None, (place, country))),
            "url": url,
            "team": fields.get("Business Unit") or fields.get("Business unit(s)") or fields.get("Division") or "",
            "employment": fields.get("Program") or fields.get("Program type") or "",
            "description": normalize.clean_description(description),
            "deadline": _labelled_date(description),
        })
    return out


_READERS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "workable": _workable,
    "workday": _workday,
    "oracle": _oracle,
    "eightfold": _eightfold,
    "amazon": _amazon,
    "oleeo": _oleeo,
}


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def _to_job(board: Board, p: dict, wanted: _Filter) -> Job:
    found = programme.classify(p.get("title", ""), p.get("description", ""), p.get("employment") or "")
    job = build_job(
        source=name,
        company=board.company,
        title=p.get("title", ""),
        source_url=p.get("url", ""),
        application_url=p.get("apply_url") or p.get("url", ""),
        external_id=f"{board.key}:{p.get('id', '')}",
        location=_first_uk_location(p.get("location") or "", wanted),
        work_type=found.work_type,
        description=p.get("description", ""),
        year_of_study=found.evidence,
        deadline=p.get("deadline"),
        date_posted=p.get("posted"),
    )
    job.sources[0].via = PLATFORM_NAMES[board.platform]
    graduation.apply(job, p.get("description", ""), config.load_profile())
    eligibility.assess(job, p.get("description", ""), config.load_profile())
    return job


def work_type_from_title(title: str, employment: str = "") -> str:
    """Only what the posting's own title says; see programme.type_from_title."""
    return programme.type_from_title(title, employment)


def _posted(value) -> date | None:
    """A feed's posting date: ISO text, epoch seconds or milliseconds, or a
    written date ("September 1, 2026"). Anything else is None, never a guess."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        seconds = float(value) / (1000 if float(value) > 1e11 else 1)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    return parse_iso_date(value) or normalize.parse_date(str(value))


def _first_uk_location(location: str, wanted: _Filter) -> str:
    """A posting may list several offices; show the UK one this row is about.

    An office list can also be one comma-separated run ("Abu Dhabi, Amsterdam,
    Edinburgh, London"), and the sheet shows a location's first comma token, so
    the UK city is picked out of the run. "London, England, United Kingdom" is
    left whole: its first token is already the UK city.
    """
    parts = [p.strip() for p in re.split(r"[;|]", location) if p.strip()]
    for part in parts:
        if not wanted.is_uk(part):
            continue
        tokens = [t.strip() for t in part.split(",") if t.strip()]
        if len(tokens) > 1 and not wanted.is_uk(tokens[0]):
            return next((t for t in tokens if wanted.is_uk(t)), part)
        return part
    return parts[0] if parts else ""


def _text(value: str) -> str:
    if value and "<" in value:
        value = BeautifulSoup(value, "lxml").get_text(" ", strip=True)
    return normalize.clean_description(value)
