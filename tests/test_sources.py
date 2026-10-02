"""Adapter parsing, against fixtures captured from the real sites. No network."""

import json
from datetime import date
from pathlib import Path

import pytest

from sources import brightnetwork, gradcracker, manual, trackr
from sources.fetch import KNOWN_ROBOTS, RobotsGuard, SourceError

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, text, status=200):
        self.text, self.status = text, status

    @property
    def ok(self):
        return 200 <= self.status < 300


class FakeSession:
    def __init__(self, text="", status=200):
        self.response = FakeResponse(text, status)
        self.http = self
        self.browser = self

    def get(self, url, **kwargs):
        return self.response


class TestTrackr:
    @pytest.fixture
    def jobs(self):
        payload = (FIXTURES / "trackr_programmes.json").read_text(encoding="utf-8")
        session = FakeSession(payload)
        settings = {
            "api_base": "https://api.the-trackr.com",
            "board_base": "https://app.the-trackr.com",
            "regions": ["UK"], "industries": ["Tech"],
            "seasons": ["2027"], "types": ["summer-internships"],
        }
        return trackr.search_jobs(session, settings)

    def test_skips_records_with_no_company(self, jobs):
        assert len(jobs) == 2

    def test_maps_the_core_fields(self, jobs):
        job = jobs[0]
        assert job.company == "J.P. Morgan"
        assert job.deadline == date(2026, 11, 1)
        assert job.work_type == "Summer Internship"

    def test_application_url_points_at_the_employer_not_trackr(self, jobs):
        """The most valuable field Trackr gives us: where the real deadline lives."""
        assert "jpmc.fa.oraclecloud.com" in jobs[0].application_url
        assert jobs[0].source_url.startswith("https://app.the-trackr.com/")

    def test_missing_deadline_stays_none(self, jobs):
        """280 of 295 live listings had no deadline; none may be invented."""
        assert jobs[1].deadline is None

    def test_region_supplies_location_when_the_list_is_empty(self, jobs):
        assert jobs[0].location == "United Kingdom"

    def test_explicit_locations_are_used_when_present(self, jobs):
        assert jobs[1].location == "London"

    def test_application_materials_get_their_own_fields(self, jobs):
        assert (jobs[0].cv, jobs[0].written_answers, jobs[0].cover_letter) == ("Yes", "No", "Optional")
        assert (jobs[1].cv, jobs[1].cover_letter) == ("Yes", "No")

    def test_unstated_material_stays_blank(self, jobs):
        """The second record has no writtenAnswers key: that is unknown, not No."""
        assert jobs[1].written_answers == ""

    def test_other_requirements_stay_in_the_description(self, jobs):
        assert "Sponsors visa" in jobs[0].description
        assert "Rolling applications" in jobs[1].description

    def test_notes_are_left_for_the_user(self, jobs):
        assert all(job.notes == "" for job in jobs)

    def test_categories_become_skills(self, jobs):
        assert "Machine Learning" in jobs[1].skills

    def test_requests_carry_a_session_id(self, monkeypatch):
        """Without the sessionid header the API returns an empty list."""
        payload = (FIXTURES / "trackr_programmes.json").read_text(encoding="utf-8")
        session = FakeSession(payload)
        seen = {}

        def capture(url, **kwargs):
            seen.update(kwargs.get("headers") or {})
            return session.response

        session.get = capture
        trackr.search_jobs(session, {
            "regions": ["UK"], "industries": ["Tech"],
            "seasons": ["2027"], "types": ["summer-internships"],
        })
        assert seen.get("sessionid")

    def test_session_id_is_stable_across_calls(self):
        """One steady client identity, not a new one on every request."""
        assert trackr._session_id() == trackr._session_id()

    def test_bare_empty_list_is_reported_as_failure_not_zero_jobs(self):
        """The API answers [] when it will not serve a request. Reporting that
        as '0 internships today' would silently wipe the Trackr rows."""
        session = FakeSession("[]")
        with pytest.raises(SourceError, match="no programmes"):
            trackr.search_jobs(session, {
                "regions": ["UK"], "industries": ["Tech"],
                "seasons": ["2027"], "types": ["summer-internships"],
            })


class TestGradcracker:
    @pytest.fixture
    def jobs(self):
        html = (FIXTURES / "gradcracker_search.html").read_text(encoding="utf-8")
        return gradcracker._parse_search_page(html, "https://www.gradcracker.com")

    def test_parses_the_listings(self, jobs):
        assert len(jobs) == 2          # the robots-disallowed demo hub is skipped

    def test_skips_the_robots_disallowed_demo_hub(self, jobs):
        assert all("9000001" not in job.source_url for job in jobs)

    def test_reads_labelled_fields(self, jobs):
        job = next(j for j in jobs if j.company == "Blackrock")
        assert job.deadline == date(2026, 9, 30)
        assert job.location == "Edinburgh"
        assert job.duration == "12 months"
        assert "Bachelor" in job.degree_requirements

    def test_company_slug_becomes_a_readable_name(self, jobs):
        assert any(j.company == "Bank of America" for j in jobs)

    def test_twelve_month_placement_is_classified(self, jobs):
        job = next(j for j in jobs if j.company == "Blackrock")
        assert job.work_type == "Industrial Placement"

    def test_start_date_is_read_when_given(self, jobs):
        job = next(j for j in jobs if j.company == "Bank of America")
        assert job.start_date is not None


class TestBrightNetwork:
    @pytest.fixture
    def job(self):
        html = (FIXTURES / "brightnetwork_job.html").read_text(encoding="utf-8")
        return brightnetwork._parse_job_page(
            html, "https://www.brightnetwork.co.uk/graduate-jobs/arm/software-engineering-intern-2027/"
        )

    def test_reads_the_json_ld_posting(self, job):
        assert job.company == "Arm"
        assert job.job_title == "Software Engineering Intern 2027"

    def test_valid_through_becomes_the_deadline(self, job):
        assert job.deadline == date(2026, 12, 15)

    def test_picks_jobposting_past_other_json_ld_blocks(self, job):
        assert job is not None

    def test_location_comes_from_the_address(self, job):
        assert job.location == "Cambridge"

    def test_description_html_is_flattened(self, job):
        assert "<p>" not in job.description
        assert "compiler team" in job.description

    def test_skills_are_extracted_from_the_description(self, job):
        assert {"Python", "C++", "Linux", "CUDA"} <= set(job.skills)

    def test_salary_is_assembled(self, job):
        assert "2500" in job.salary

    def test_intern_employment_type_is_recognised(self, job):
        assert job.work_type == "Internship"

    def test_refuses_robots_disallowed_paths(self):
        for url in ("https://www.brightnetwork.co.uk/search/?q=x",
                    "https://www.brightnetwork.co.uk/api/jobs"):
            with pytest.raises(SourceError, match="disallow"):
                brightnetwork._reject_disallowed(url)


class TestRobotsGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.brightnetwork.co.uk/search/?content_types=jobs",
            "https://www.brightnetwork.co.uk/api/jobs",
            "https://www.gradcracker.com/out/12345",
            "https://www.gradcracker.com/hub/9000001/demo",
        ],
    )
    def test_known_disallowed_paths_are_refused_offline(self, url):
        """The rules are transcribed locally, so the guard works even when
        robots.txt itself cannot be fetched."""
        assert RobotsGuard().allowed(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.brightnetwork.co.uk/graduate-jobs/arm/swe-intern/",
            "https://www.gradcracker.com/search/computing-technology/work-placements-internships",
        ],
    )
    def test_allowed_paths_pass(self, url):
        assert RobotsGuard().allowed(url) is True

    def test_every_configured_platform_has_local_rules(self):
        assert {"www.brightnetwork.co.uk", "www.gradcracker.com"} <= set(KNOWN_ROBOTS)


class TestManualIngest:
    def test_reads_a_saved_gradcracker_page(self):
        jobs = manual.from_html(FIXTURES / "gradcracker_search.html", "gradcracker")
        assert len(jobs) == 2

    def test_reads_a_saved_brightnetwork_page(self):
        jobs = manual.from_html(FIXTURES / "brightnetwork_job.html", "brightnetwork")
        assert jobs[0].company == "Arm"

    def test_recovers_the_real_url_from_a_saved_page(self):
        """Without this an ingested job would carry a local file path, not a link."""
        jobs = manual.from_html(FIXTURES / "brightnetwork_job.html", "brightnetwork")
        assert jobs[0].source_url.startswith("https://www.brightnetwork.co.uk/")

    def test_reads_a_csv_with_varied_headers(self, tmp_path):
        csv_path = tmp_path / "jobs.csv"
        csv_path.write_text(
            "Employer,Role,Where,Closes,Apply Link\n"
            "NVIDIA,Software Engineering Intern,London,3rd April 2027,https://nvidia.com/apply\n",
            encoding="utf-8",
        )
        jobs = manual.from_csv(csv_path)
        assert jobs[0].company == "NVIDIA"
        assert jobs[0].deadline == date(2027, 4, 3)
        assert jobs[0].application_url == "https://nvidia.com/apply"

    def test_csv_rows_without_a_company_or_title_are_skipped(self, tmp_path):
        csv_path = tmp_path / "jobs.csv"
        csv_path.write_text("Company,Title\n,Software Intern\nNVIDIA,\n", encoding="utf-8")
        assert manual.from_csv(csv_path) == []

    def test_unparseable_csv_deadline_becomes_none(self, tmp_path):
        csv_path = tmp_path / "jobs.csv"
        csv_path.write_text(
            "Company,Title,Deadline\nNVIDIA,Software Engineering Intern,rolling\n",
            encoding="utf-8",
        )
        assert manual.from_csv(csv_path)[0].deadline is None

    def test_unknown_source_raises_a_useful_error(self):
        with pytest.raises(ValueError, match="No HTML parser"):
            manual.from_html(FIXTURES / "gradcracker_search.html", "trackr")
