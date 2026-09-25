"""TargetJobs: postings from its sitemap, which lists every page it has ever
published - the sitemap dates keep the sweep to recent ones."""

from __future__ import annotations

from sources import jobboards
from tracker.models import Job

name = "targetjobs"


def search_jobs(session, settings: dict) -> list[Job]:
    """Recent student postings; see sources.jobboards."""
    return jobboards.sweep(session, settings, name)


def get_job(session, url: str) -> Job | None:
    """Postings arrive through the sitemap sweep; there is no single-job route."""
    return None
