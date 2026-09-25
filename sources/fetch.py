"""Shared fetching: robots guard, rate limiting, HTTP and browser.

Every outbound request in this project goes through here, which is what makes
the safety rails enforceable rather than aspirational:

* `RobotsGuard` checks robots.txt before any URL is requested. A disallowed URL
  raises and is never fetched.
* `RateLimiter` spaces requests per host. These sites provide this data for
  free; hammering them is both rude and the fastest way to get blocked.
* Anti-bot challenges are detected and raise `SourceBlocked`. They are never
  solved, worked around, or retried in a loop.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from tracker import config

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class SourceError(RuntimeError):
    """A source failed in a way that should be logged and skipped."""


class SourceBlocked(SourceError):
    """An anti-bot challenge was served. Never bypassed - the source is skipped
    and the previous run's data is kept."""


class RobotsDisallowed(SourceError):
    """The URL is disallowed by robots.txt. It is not fetched."""


# ---------------------------------------------------------------------------
# Robots
# ---------------------------------------------------------------------------

#: Fallback rules for hosts that refuse to serve robots.txt to non-browser
#: clients (Bright Network 403s it). Transcribed from the live files on
#: 2026-09-08. The guard fails CLOSED against these: if we cannot read a host's
#: robots.txt and have no entry here, only the known-safe paths are allowed.
KNOWN_ROBOTS: dict[str, list[str]] = {
    "www.brightnetwork.co.uk": [
        "/search/", "/interstitial", "/interstitial/", "/certificates/",
        "/api/", "/htmx/", "/login/", "/getting-ahead-in-publishing/",
        "/member-success-stories/", "/a-day-in-their-life/",
    ],
    "www.gradcracker.com": [
        "/out", "/campaign", "/download", "/gctv/download",
        "/keyword-search", "/hub/9000001",
    ],
    "the-trackr.com": [],
    "app.the-trackr.com": [],
    "api.the-trackr.com": [],
}


class RobotsGuard:
    """Checks URLs against robots.txt. One instance per run."""

    def __init__(self) -> None:
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        host, path = parsed.netloc, (parsed.path or "/")

        # Explicit local rules always win: they are the transcribed truth for
        # hosts whose robots.txt we cannot re-fetch reliably.
        for prefix in KNOWN_ROBOTS.get(host, []):
            if path.startswith(prefix):
                return False

        parser = self._parser_for(parsed.scheme, host)
        if parser is None:
            # Could not read robots.txt. Known hosts have been checked above;
            # for anything else (an employer's ATS page reached from a listing)
            # allow it, since we are fetching one page a user would open anyway.
            return True
        return parser.can_fetch(USER_AGENT, url)

    def check(self, url: str) -> None:
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

    def _parser_for(self, scheme: str, host: str):
        with self._lock:
            if host in self._parsers:
                return self._parsers[host]

        parser = None
        robots_url = f"{scheme or 'https'}://{host}/robots.txt"
        try:
            response = httpx.get(
                robots_url,
                headers={"User-Agent": USER_AGENT},
                timeout=15,
                follow_redirects=True,
            )
            if response.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
            else:
                log.debug("robots.txt for %s returned %s", host, response.status_code)
        except httpx.HTTPError as exc:
            log.debug("could not fetch robots.txt for %s: %s", host, exc)

        with self._lock:
            self._parsers[host] = parser
        return parser


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class RateLimiter:
    """Minimum spacing between requests to the same host."""

    def __init__(self, default_seconds: float = 3.0) -> None:
        self.default = default_seconds
        self._per_host: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def configure(self, host: str, seconds: float) -> None:
        self._per_host[host] = seconds

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        interval = self._per_host.get(host, self.default)
        with self._lock:
            elapsed = time.monotonic() - self._last.get(host, 0.0)
            delay = interval - elapsed
            if delay > 0:
                time.sleep(delay)
            self._last[host] = time.monotonic()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@dataclass
class Response:
    url: str
    status: int
    text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Fetcher:
    """Rate-limited, robots-checked HTTP with retries on transient failures."""

    def __init__(self, guard: RobotsGuard, limiter: RateLimiter, defaults: dict) -> None:
        self.guard = guard
        self.limiter = limiter
        self.timeout = defaults.get("timeout_seconds", 30)
        self.max_retries = defaults.get("max_retries", 2)
        self.backoff = defaults.get("retry_backoff_seconds", 5)
        self._client = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
            },
            timeout=self.timeout,
            follow_redirects=True,
        )

    def get(self, url: str, *, expect_json: bool = False,
            headers: dict[str, str] | None = None) -> Response:
        return self._request("GET", url, expect_json=expect_json, headers=headers)

    def post_json(self, url: str, payload: dict, *,
                  headers: dict[str, str] | None = None) -> Response:
        """POST a JSON query, for search APIs that take one (Workday's job
        board). Same robots, rate-limit and challenge rules as a GET."""
        return self._request("POST", url, expect_json=True, headers=headers, json_body=payload)

    def _request(self, method: str, url: str, *, expect_json: bool,
                 headers: dict[str, str] | None, json_body: dict | None = None) -> Response:
        self.guard.check(url)

        extra = dict(headers or {})
        if expect_json:
            extra.setdefault("Accept", "application/json, text/plain, */*")

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.wait(url)
            try:
                response = self._client.request(method, url, headers=extra or None, json=json_body)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(self.backoff * (attempt + 1))
                    continue
                raise SourceError(f"{method} {url} failed: {exc}") from exc

            if response.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                time.sleep(self.backoff * (attempt + 1))
                continue

            if response.status_code == 403:
                raise SourceBlocked(
                    f"{method} {url} returned 403 - this host rejects non-browser clients"
                )

            return Response(url=str(response.url), status=response.status_code, text=response.text)

        raise SourceError(f"{method} {url} failed after retries: {last_error}")

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------


class Browser:
    """A single persistent Playwright context, shared across sources.

    Cloudflare rejects plain HTTP clients on two of the three platforms, so
    their pages have to be fetched by a real browser. The persistent profile
    keeps cookies between runs, which means the check is presented far less
    often. It stores no credentials and performs no login.
    """

    def __init__(self, guard: RobotsGuard, limiter: RateLimiter, settings: dict) -> None:
        self.guard = guard
        self.limiter = limiter
        self.settings = settings
        self.markers = [m.casefold() for m in settings.get("challenge_markers", [])]
        self.challenge_grace_seconds = int(settings.get("challenge_grace_seconds", 20))
        self._playwright = None
        self._context = None
        self._page = None

    def _ensure_started(self):
        if self._page is not None:
            return self._page

        from playwright.sync_api import sync_playwright

        profile = config.env_override("BROWSER_PROFILE") or self.settings.get(
            "user_data_dir", "sessions/chrome"
        )
        profile_dir = config.ROOT / profile if not str(profile).startswith(("/", "\\")) else profile
        (config.ROOT / "sessions").mkdir(parents=True, exist_ok=True)

        self._playwright = sync_playwright().start()
        viewport = self.settings.get("viewport", {"width": 1440, "height": 900})

        launch: dict = {
            "user_data_dir": str(profile_dir),
            "headless": bool(self.settings.get("headless", False)),
            "viewport": viewport,
            "user_agent": USER_AGENT,
            "locale": "en-GB",
        }
        channel = self.settings.get("channel")
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                channel=channel, **launch
            )
        except Exception:
            # Chrome may not be installed; fall back to the bundled Chromium.
            log.warning("Chrome channel '%s' unavailable, using bundled Chromium", channel)
            self._context = self._playwright.chromium.launch_persistent_context(**launch)

        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def get(self, url: str) -> Response:
        self.guard.check(url)
        self.limiter.wait(url)

        page = self._ensure_started()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            raise SourceError(f"browser GET {url} failed: {exc}") from exc

        status = response.status if response else 0

        # Cloudflare's interstitial either clears itself after a few seconds or
        # settles into a challenge that needs a human. Waiting for a page to
        # finish loading is ordinary browsing, so we are patient; what we never
        # do is try to solve or bypass the challenge.
        html = ""
        for _ in range(int(self.challenge_grace_seconds / 2)):
            page.wait_for_timeout(2000)
            html = page.content()
            if not self._is_challenge(html):
                break

        if self._is_challenge(html):
            raise SourceBlocked(
                f"{urlparse(url).netloc} served an anti-bot challenge that did not clear in "
                f"{self.challenge_grace_seconds}s. This source is skipped, not bypassed. "
                "To fix it once: set browser.headless to false in config/sources.yaml, run "
                "`python main.py search --source gradcracker`, and complete the check yourself "
                "in the window that opens. The persistent profile remembers it afterwards."
            )

        return Response(url=page.url, status=status or 200, text=html)

    def _is_challenge(self, html: str) -> bool:
        low = html.casefold()
        # Real pages are large; a challenge page is small. Requiring both
        # avoids flagging a legitimate page that merely mentions the phrase.
        return len(html) < 30000 and any(m in low for m in self.markers)

    def close(self) -> None:
        for closer in (self._context, self._playwright):
            if closer is None:
                continue
            try:
                closer.close() if hasattr(closer, "close") else closer.stop()
            except Exception:
                pass
        self._page = self._context = self._playwright = None


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class Session:
    """Everything a source adapter needs to fetch, in one object.

    The browser is created lazily, so a run that only touches Trackr never
    launches Chrome.
    """

    def __init__(self) -> None:
        cfg = config.load("sources")
        defaults = cfg.get("defaults", {})

        self.guard = RobotsGuard()
        self.limiter = RateLimiter(defaults.get("rate_limit_seconds", 3.0))

        for spec in cfg.get("sources", {}).values():
            seconds = spec.get("rate_limit_seconds")
            for key in ("base_url", "api_base", "board_base"):
                if spec.get(key) and seconds:
                    self.limiter.configure(urlparse(spec[key]).netloc, seconds)

        self.http = Fetcher(self.guard, self.limiter, defaults)
        self._browser: Browser | None = None
        self._browser_settings = cfg.get("browser", {})

    @property
    def browser(self) -> Browser:
        if self._browser is None:
            self._browser = Browser(self.guard, self.limiter, self._browser_settings)
        return self._browser

    def close(self) -> None:
        self.http.close()
        if self._browser is not None:
            self._browser.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
