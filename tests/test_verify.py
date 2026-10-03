"""Deadline and open/closed verification, against fake pages (no network)."""

from datetime import date, datetime, timedelta

import pytest

from tests.conftest import make_job
from tracker import config, verify
from tracker.models import OpenStatus, Priority

SETTINGS = config.load("verification")


class FakeResponse:
    def __init__(self, text="", status=200):
        self.text = text
        self.status = status

    @property
    def ok(self):
        return 200 <= self.status < 300


class FakeSession:
    """Serves canned pages by URL, and records what was requested."""

    def __init__(self, pages: dict[str, FakeResponse]):
        self.pages = pages
        self.requested: list[str] = []
        self.http = self
        self.browser = self

    def get(self, url, **kwargs):
        self.requested.append(url)
        if url not in self.pages:
            return FakeResponse("<html><body><main>nothing here</main></body></html>", 200)
        return self.pages[url]


def page(body: str) -> FakeResponse:
    return FakeResponse(f"<html><body><main>{body}</main></body></html>")


class TestOpenStatus:
    def test_closed_marker_marks_job_closed(self):
        job = make_job()
        session = FakeSession({job.source_url: page("Applications closed for this role.")})

        verify.verify_job(session, job, SETTINGS)
        assert job.open_status is OpenStatus.CLOSED

    def test_open_marker_marks_job_open(self):
        job = make_job()
        session = FakeSession({job.source_url: page("Apply now for this internship.")})

        verify.verify_job(session, job, SETTINGS)
        assert job.open_status is OpenStatus.OPEN

    def test_ambiguous_page_stays_unknown(self):
        """Never guesses in either direction."""
        job = make_job()
        session = FakeSession({job.source_url: page("A great place to work.")})

        verify.verify_job(session, job, SETTINGS)
        assert job.open_status is OpenStatus.UNKNOWN

    def test_404_on_application_page_is_unavailable_not_closed(self):
        job = make_job()
        session = FakeSession({
            job.source_url: page("A great place to work."),
            job.application_url: FakeResponse("Not found", 404),
        })

        verify.verify_job(session, job, SETTINGS)
        assert job.open_status is OpenStatus.UNKNOWN
        assert "returned 404" in job.verification_note                # read as UNAVAILABLE by the assessment

    def test_footer_boilerplate_does_not_close_every_job(self):
        """A site-wide footer would otherwise close the whole platform."""
        job = make_job()
        html = (
            "<html><body><main>Apply now for this role.</main>"
            "<footer>Some roles are no longer accepting applications.</footer>"
            "</body></html>"
        )
        session = FakeSession({job.source_url: FakeResponse(html)})

        verify.verify_job(session, job, SETTINGS)
        assert job.open_status is OpenStatus.OPEN

    def test_a_passed_deadline_is_expiry_not_closure(self):
        job = make_job(deadline=date.today() - timedelta(days=3))
        session = FakeSession({job.source_url: page("Nothing conclusive.")})

        verify.verify_job(session, job, SETTINGS)
        assert job.open_status is OpenStatus.UNKNOWN
        assert "deadline passed (expiry, not closure)" in job.verification_note
        assert job.closed_evidence == "" and job.hiring_status != "EMPLOYER_CLOSED"

    def test_closure_wording_on_the_employer_page_is_recorded_with_its_words(self):
        job = make_job(deadline=date.today() - timedelta(days=3))
        session = FakeSession({job.source_url: page("Applications closed for this role.")})

        verify.verify_job(session, job, SETTINGS)
        assert job.open_status is OpenStatus.CLOSED and job.hiring_status == "EMPLOYER_CLOSED"
        assert '"applications closed"' in job.closed_evidence

    def test_an_aggregator_saying_closed_is_only_a_listing_claim(self):
        job = make_job()
        job.source_url = job.sources[0].source_url = "https://www.gradcracker.com/hub/1/x/job/9"
        session = FakeSession({job.source_url: page("Applications closed for this role."),
                               job.application_url: page("A great place to work.")})

        verify.verify_job(session, job, SETTINGS)
        assert job.application_url in session.requested                 # the employer is asked
        assert job.open_status is OpenStatus.UNKNOWN and job.hiring_status == "LISTING_CLOSED"
        assert job.closed_evidence == ""


class TestDeadlineDiscovery:
    def test_deadline_is_read_from_the_job_page(self):
        """The Trackr case: 280 of 295 listings arrive with no deadline."""
        job = make_job(deadline=None)
        session = FakeSession({
            job.source_url: page("Apply now. Application deadline: 3rd April 2027.")
        })

        verify.verify_job(session, job, SETTINGS)
        assert job.deadline == date(2027, 4, 3)
        assert job.deadline_source == "job-page"

    def test_falls_through_to_the_employer_page(self):
        job = make_job(deadline=None)
        session = FakeSession({
            job.source_url: page("See the employer site."),
            job.application_url: page("Apply now. Closing date 15 May 2027."),
        })

        verify.verify_job(session, job, SETTINGS)
        assert job.deadline == date(2027, 5, 15)
        assert job.deadline_source == "employer-ats"
        assert job.application_url in session.requested

    def test_verified_deadline_overrides_the_listed_one(self):
        job = make_job(deadline=date(2027, 3, 1))
        session = FakeSession({
            job.source_url: page("Apply now. Application deadline: 3rd April 2027.")
        })

        verify.verify_job(session, job, SETTINGS)
        assert job.deadline == date(2027, 4, 3)
        assert job.listed_deadline == date(2027, 3, 1)   # the platform's claim is kept
        assert job.deadline_disagreement is True

    def test_small_disagreement_is_not_flagged(self):
        job = make_job(deadline=date(2027, 4, 4))
        session = FakeSession({
            job.source_url: page("Apply now. Application deadline: 3rd April 2027.")
        })

        verify.verify_job(session, job, SETTINGS)
        assert job.deadline_disagreement is False

    def test_unlabelled_date_is_not_taken_as_a_deadline(self):
        """A bare date is as likely to be a start date or a posting date."""
        job = make_job(deadline=None)
        session = FakeSession({
            job.source_url: page("Apply now. This programme begins 12 June 2027.")
        })

        verify.verify_job(session, job, SETTINGS)
        assert job.deadline is None

    def test_unreachable_page_leaves_state_unknown(self):
        job = make_job(deadline=None)
        session = FakeSession({job.source_url: FakeResponse("boom", 500)})

        verify.verify_job(session, job, SETTINGS)
        assert job.open_status is OpenStatus.UNKNOWN
        assert job.deadline is None


class TestBudget:
    def _settings(self, **budget):
        base = dict(SETTINGS["budget"])
        base.update(budget)
        return {**SETTINGS, "budget": base}

    def test_budget_caps_the_number_of_checks(self):
        jobs = [make_job(company=f"Company{i}") for i in range(20)]
        selected = verify.select(jobs, self._settings(max_verifications=5))
        assert len(selected) == 5

    def test_new_jobs_are_always_selected(self):
        job = make_job()
        assert job in verify.select([job], SETTINGS)

    def test_recently_verified_jobs_are_skipped(self):
        job = make_job(deadline=date.today() + timedelta(days=200), open_status=OpenStatus.OPEN)
        job.last_verified = datetime.now()
        job.priority = Priority.LOW
        assert verify.select([job], SETTINGS) == []

    def test_urgent_deadline_is_rechecked_even_when_fresh(self):
        job = make_job(deadline=date.today() + timedelta(days=3), open_status=OpenStatus.OPEN)
        job.last_verified = datetime.now()
        job.priority = Priority.LOW
        assert job in verify.select([job], SETTINGS)

    def test_unknown_status_is_always_rechecked(self):
        job = make_job(deadline=date.today() + timedelta(days=200),
                       open_status=OpenStatus.UNKNOWN)
        job.last_verified = datetime.now()
        assert job in verify.select([job], SETTINGS)

    def test_soonest_deadline_is_verified_first(self):
        soon = make_job(company="Soon", deadline=date.today() + timedelta(days=2))
        later = make_job(company="Later", deadline=date.today() + timedelta(days=60))
        selected = verify.select([later, soon], self._settings(max_verifications=1))
        assert selected[0].company == "Soon"
