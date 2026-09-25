"""The source adapter contract.

Each adapter is a module exposing:

    search_jobs(session, settings) -> list[Job]
    get_job(session, url) -> Job | None

Nothing downstream imports an adapter directly; the pipeline looks them up in
`ADAPTERS`. Adding a fourth platform means writing one module and adding one
line here and in config/sources.yaml.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from tracker import normalize
from tracker.models import Job, SourceRef


class Adapter(Protocol):
    name: str

    def search_jobs(self, session, settings: dict) -> list[Job]: ...
    def get_job(self, session, url: str) -> Job | None: ...


def build_job(
    *,
    source: str,
    company: str,
    title: str,
    source_url: str = "",
    application_url: str = "",
    external_id: str = "",
    location: str = "",
    work_type: str = "",
    deadline: date | None = None,
    salary: str = "",
    duration: str = "",
    start_date: date | None = None,
    date_posted: date | None = None,
    description: str = "",
    degree_requirements: str = "",
    year_of_study: str = "",
    experience_requirements: str = "",
    cv: str = "",
    written_answers: str = "",
    cover_letter: str = "",
) -> Job:
    """Turn one platform's raw fields into a normalized `Job`.

    Every adapter funnels through here so normalization and sanitisation happen
    exactly once, in one place, for all sources. There is deliberately no
    `notes` parameter: Notes belongs to the user.
    """
    display_company, normalized_company = normalize.company(company)
    display_title, normalized_title = normalize.title(title)
    display_location, arrangement = normalize.location(location)
    description = normalize.clean_description(description)

    skills, languages, technologies = normalize.extract_skills(
        f"{display_title} {description} {degree_requirements} {experience_requirements}"
    )

    today = date.today()
    ref = SourceRef(
        source=source,
        source_url=source_url,
        application_url=application_url,
        external_id=str(external_id or ""),
        listed_deadline=deadline,
        first_seen=today,
        last_seen=today,
    )

    return Job(
        company=display_company,
        job_title=display_title,
        raw_title=normalize.clean_text(title, limit=300),
        normalized_company=normalized_company,
        normalized_title=normalized_title,
        location=display_location,
        work_arrangement=arrangement,
        work_type=normalize.work_type(work_type),
        salary=normalize.clean_text(salary, limit=120),
        duration=normalize.clean_text(duration, limit=80),
        start_date=start_date,
        date_posted=date_posted,
        deadline=deadline,
        listed_deadline=deadline,
        deadline_source=f"{source}-listing" if deadline else "none",
        degree_requirements=normalize.clean_text(degree_requirements, limit=400),
        year_of_study=normalize.clean_text(year_of_study, limit=200),
        experience_requirements=normalize.clean_text(experience_requirements, limit=400),
        description=description,
        skills=skills,
        programming_languages=languages,
        technologies=technologies,
        sources=[ref],
        primary_source=source,
        source_url=source_url,
        application_url=application_url or source_url,
        cv=normalize.clean_text(cv, limit=40),
        written_answers=normalize.clean_text(written_answers, limit=40),
        cover_letter=normalize.clean_text(cover_letter, limit=40),
        date_found=today,
        last_updated=today,
    )
