"""Eligibility, scoring and priority ranking."""

from datetime import date, timedelta

from tests.conftest import make_job
from tracker import config, match
from tracker.models import ApplicationStatus, OpenStatus, Priority

FILTERS = config.load("search_filters")
WEIGHTS = config.load("matching_weights")


def score(job, profile):
    job.eligible, _ = match.check_eligibility(job, profile, FILTERS)
    match.score(job, profile, FILTERS, WEIGHTS)
    match.assign_priority(job, WEIGHTS)
    return job


class TestBlankProfile:
    def test_never_invents_strengths_or_gaps(self, blank_profile):
        """The single most important guarantee: no evidence, no claims."""
        job = score(make_job(description="Python, C++, PyTorch and Linux required"), blank_profile)
        assert job.strengths == []
        assert job.missing_skills == []

    def test_rescoring_never_writes_to_notes(self, blank_profile):
        """Notes is the user's. Scoring used to append to it on every run."""
        job = make_job(title="Software Engineer Intern")
        for _ in range(3):
            score(job, blank_profile)
        assert job.notes == ""

    def test_score_is_capped(self, blank_profile):
        job = score(make_job(), blank_profile)
        cap = WEIGHTS["blank_profile"]["score_cap"]
        assert job.match_score <= cap

    def test_category_says_unscored(self, blank_profile):
        job = score(make_job(), blank_profile)
        assert "UNSCORED" in job.match_category

    def test_says_why_in_concerns(self, blank_profile):
        job = score(make_job(), blank_profile)
        assert any("not configured" in c.casefold() for c in job.concerns)

    def test_relevant_role_still_outscores_irrelevant_one(self, blank_profile):
        """Relevance ranking must work on day one, before any CV is supplied."""
        relevant = score(make_job(title="Software Engineering Intern"), blank_profile)
        irrelevant = score(
            make_job(title="Marketing Intern", description="Social media and copywriting"),
            blank_profile,
        )
        assert relevant.match_score > irrelevant.match_score


class TestFullProfile:
    def test_matched_skills_become_strengths(self, filled_profile):
        job = score(make_job(description="Python and C++ required. Linux essential."),
                    filled_profile)
        assert "Python" in job.strengths
        assert "C++" in job.strengths

    def test_required_but_absent_skill_is_reported_missing(self, filled_profile):
        job = score(make_job(description="CUDA experience is required for this role."),
                    filled_profile)
        assert any("CUDA" in m for m in job.missing_skills)

    def test_weaker_evidence_is_labelled_not_claimed_outright(self, filled_profile):
        """Java is coursework-only in the fixture profile."""
        job = score(make_job(description="Java is required."), filled_profile)
        assert any(s.startswith("Java") and "coursework" in s for s in job.strengths)

    def test_good_match_scores_above_a_poor_one(self, filled_profile):
        good = score(make_job(description="Python, C++, PyTorch and Linux required"),
                     filled_profile)
        poor = score(make_job(description="Requires COBOL, Fortran and mainframe experience"),
                     filled_profile)
        assert good.match_score > poor.match_score


class TestRequirementClassification:
    def test_required_and_bonus_are_distinguished(self):
        job = make_job(description="Python is essential. Exposure to Rust is a bonus.")
        job.skills = ["Python", "Rust"]
        buckets = match.classify_requirements(job)
        assert buckets["Python"] == "required"
        assert buckets["Rust"] == "nice_to_have"


class TestEligibility:
    def test_excluded_title_word_disqualifies(self, blank_profile):
        job = make_job(title="Senior Software Engineer")
        eligible, reasons = match.check_eligibility(job, blank_profile, FILTERS)
        assert not eligible and reasons

    def test_spring_weeks_are_a_targeted_type(self, blank_profile):
        eligible, reasons = match.check_eligibility(
            make_job(title="Technology Spring Week", work_type="Spring Week"), blank_profile, FILTERS)
        assert not any("Work type" in r for r in reasons)

    def test_missing_data_never_disqualifies(self, blank_profile):
        """Absence of data is not the same as failing a requirement."""
        job = make_job(location="", work_type="")
        assert match.check_eligibility(job, blank_profile, FILTERS)[0]

    def test_remote_outside_configured_regions_is_still_eligible(self, blank_profile):
        job = make_job(location="Remote (Berlin)")
        job.work_arrangement = "Remote"
        assert match.check_eligibility(job, blank_profile, FILTERS)[0]

    def test_graduation_year_mismatch_disqualifies(self, filled_profile):
        job = make_job()
        job.year_of_study = "For students graduating in 2026"
        eligible, reasons = match.check_eligibility(job, filled_profile, FILTERS)
        assert not eligible
        assert "2026" in reasons[0]


class TestPriority:
    def test_near_deadline_outranks_a_better_but_distant_match(self, blank_profile):
        """Spec 13: a 96% closing tomorrow beats a 98% closing in four months."""
        today = date.today()
        # Both confirmed open, so the only difference is deadline proximity.
        urgent = score(
            make_job(deadline=today + timedelta(days=2), open_status=OpenStatus.OPEN),
            blank_profile,
        )
        distant = score(
            make_job(deadline=today + timedelta(days=120), open_status=OpenStatus.OPEN),
            blank_profile,
        )

        assert urgent.match_score == distant.match_score   # identical on merit
        assert urgent.priority is Priority.HIGH            # urgency breaks the tie
        assert distant.priority is not Priority.HIGH

    def test_closed_jobs_drop_to_low(self, blank_profile):
        job = make_job(deadline=date.today() + timedelta(days=2), open_status=OpenStatus.CLOSED)
        assert score(job, blank_profile).priority is Priority.LOW

    def test_applied_jobs_drop_to_low(self, blank_profile):
        job = make_job(deadline=date.today() + timedelta(days=2))
        job.status = ApplicationStatus.APPLIED
        assert score(job, blank_profile).priority is Priority.LOW

    def test_unverified_job_cannot_outrank_a_confirmed_open_one(self, blank_profile):
        job = make_job(deadline=date.today() + timedelta(days=2), open_status=OpenStatus.UNKNOWN)
        cap = WEIGHTS["priority"]["unknown_open_status_cap"]
        assert str(score(job, blank_profile).priority) == cap

    def test_expired_deadline_drops_to_low(self, blank_profile):
        """An expired role must not keep its band - it also has the earliest
        deadline of all, so it would otherwise lead the sheet."""
        job = score(
            make_job(deadline=date.today() - timedelta(days=5), open_status=OpenStatus.OPEN),
            blank_profile,
        )
        assert job.priority is Priority.LOW


class TestVerificationConcerns:
    def test_closed_status_is_surfaced_first(self, blank_profile):
        job = score(make_job(open_status=OpenStatus.CLOSED), blank_profile)
        assert "CLOSED" in job.concerns[0]

    def test_missing_deadline_is_flagged(self, blank_profile):
        job = score(make_job(deadline=None, open_status=OpenStatus.OPEN), blank_profile)
        assert any("No deadline" in c for c in job.concerns)

    def test_deadline_disagreement_is_flagged(self, blank_profile):
        job = make_job(deadline=date(2027, 1, 10), open_status=OpenStatus.OPEN)
        job.listed_deadline = date(2027, 3, 1)
        job.deadline_disagreement = True
        job = score(job, blank_profile)
        assert any("check before applying" in c for c in job.concerns)
