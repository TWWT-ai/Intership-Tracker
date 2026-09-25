"""Shared test fixtures. No network access anywhere in the suite."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tracker import config, normalize
from tracker.models import Job, OpenStatus, SourceRef


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Point every data path at a temp dir so tests never touch real state."""
    data = tmp_path / "data"
    state = data / "state"
    (state / "snapshots").mkdir(parents=True)
    (data / "logs").mkdir(parents=True)

    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "SNAPSHOT_DIR", state / "snapshots")
    monkeypatch.setattr(config, "LOG_DIR", data / "logs")
    monkeypatch.setattr(config, "WORKBOOK", data / "internships.xlsx")
    monkeypatch.setattr(config, "PROFILE", data / "profile.json")
    monkeypatch.setattr(config, "ID_REGISTRY", state / "id_registry.json")
    monkeypatch.setattr(config, "APPLICATIONS_DIR", data / "applications")
    return data


@pytest.fixture(autouse=True)
def full_layout(monkeypatch):
    """Show every column, so tests do not depend on the user's own layout in
    config/workbook.yaml. Tests of the layout itself override this."""
    from tracker import excel

    real = excel._layout()
    every = [label for label, _ in excel.COLUMNS]
    monkeypatch.setattr(excel, "_layout", lambda: {**real, "columns": every})


@pytest.fixture
def today() -> date:
    return date.today()


def make_job(
    company: str = "NVIDIA",
    title: str = "Software Engineering Intern",
    source: str = "trackr",
    location: str = "London",
    deadline: date | None = None,
    work_type: str = "Internship",
    description: str = "Python and C++ required. Linux preferred.",
    application_url: str = "https://employer.example.com/apply",
    open_status: OpenStatus = OpenStatus.UNKNOWN,
    external_id: str = "",
) -> Job:
    """Build a normalized Job the way an adapter would."""
    display_company, normalized_company = normalize.company(company)
    display_title, normalized_title = normalize.title(title)
    display_location, arrangement = normalize.location(location)
    skills, languages, technologies = normalize.extract_skills(f"{title} {description}")

    return Job(
        company=display_company,
        normalized_company=normalized_company,
        job_title=display_title,
        normalized_title=normalized_title,
        raw_title=title,
        location=display_location,
        work_arrangement=arrangement,
        work_type=normalize.work_type(work_type),
        deadline=deadline,
        listed_deadline=deadline,
        description=description,
        skills=skills,
        programming_languages=languages,
        technologies=technologies,
        open_status=open_status,
        date_found=date.today(),
        last_updated=date.today(),
        primary_source=source,
        source_url=f"https://{source}.example.com/job/1",
        application_url=application_url,
        sources=[
            SourceRef(
                source=source,
                source_url=f"https://{source}.example.com/job/1",
                application_url=application_url,
                external_id=external_id,
                listed_deadline=deadline,
            )
        ],
    )


@pytest.fixture
def blank_profile() -> dict:
    return {"profile_configured": False}


@pytest.fixture
def filled_profile() -> dict:
    return {
        "profile_configured": True,
        "degree": "BSc Computer Science",
        "university": "UCL",
        "graduation_year": 2028,
        "current_year": 2,
        "languages": [
            {"name": "Python", "evidence": "project"},
            {"name": "C++", "evidence": "project"},
            {"name": "Java", "evidence": "coursework"},
        ],
        "libraries": [{"name": "PyTorch", "evidence": "project"}],
        "systems": [{"name": "Linux", "evidence": "demonstrated"},
                    {"name": "Git", "evidence": "demonstrated"}],
        "projects": [{"name": "Gesture recognition", "technologies": ["Python", "PyTorch", "OpenCV"]}],
        "experience": [{"role": "ML Team Assistant", "technologies": ["Python"]}],
        "preferred_locations": ["London"],
    }
