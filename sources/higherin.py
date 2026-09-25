"""Higherin (formerly RateMyPlacement): postings from its job sitemap.

robots.txt allows job pages and disallows /redirect, /api and /ajax, so the
employer's apply link (a redirect) is never followed from here.
"""

from __future__ import annotations

from sources import jobboards
from tracker.models import Job

name = "higherin"


def search_jobs(session, settings: dict) -> list[Job]:
    """Recent student postings; see sources.jobboards."""
    return jobboards.sweep(session, settings, name)


def get_job(session, url: str) -> Job | None:
    """Postings arrive through the sitemap sweep; there is no single-job route."""
    return None
