"""Programme type from a title or from who may apply. No network."""

import pytest

from tests.conftest import make_job
from tracker import config, normalize, pipeline, programme, verify


class TestTitle:
    @pytest.mark.parametrize("title,expected", [
        ("Technology Spring Week 2027", "Spring Week"),
        ("Spring Insight Programme", "Spring Week"),
        ("Software Engineering Summer Internship", "Summer Internship"),
        ("12-month Industrial Placement - Software", "Industrial Placement"),
        ("Year in Industry: Data", "Industrial Placement"),
        ("Software Engineering Intern", ""),
    ])
    def test_a_title_naming_the_type_decides_it(self, title, expected):
        assert programme.type_from_title(title) == expected

    def test_insight_is_a_spring_week_name(self):
        """Not a separate type, which no filter targets and flagged ineligible."""
        assert normalize.work_type("Insight Programme") == "Spring Week"


class TestEligibility:
    def test_first_years_make_a_differently_named_programme_a_spring_week(self):
        found = programme.classify("Technology Discovery Programme",
                                   "This programme is open to first-year undergraduates studying any degree.")
        assert found.work_type == "Spring Week"
        assert "first-year" in found.evidence

    def test_penultimate_year_is_a_summer_internship(self):
        found = programme.classify("Developing Talent Programme",
                                   "You must be in the penultimate year of your degree.")
        assert found.work_type == "Summer Internship"

    def test_second_years_on_a_placement_degree_are_a_placement(self):
        found = programme.classify("Technology Programme",
                                   "Open to second-year students whose course has a placement year.")
        assert found.work_type == "Industrial Placement"

    def test_a_sentence_saying_who_cannot_apply_is_not_evidence(self):
        assert programme.classify(
            "Technology Programme", "This programme is not open to first-year students.") == programme.Programme()

    def test_first_year_outside_studying_is_not_evidence(self):
        assert programme.classify(
            "Technology Programme", "In your first year on the team you will ship code.") == programme.Programme()

    def test_two_different_years_settle_nothing(self):
        text = "Open to first-year students. Also open to penultimate-year students."
        assert programme.classify("Technology Programme", text) == programme.Programme()

    def test_who_may_apply_beats_what_the_programme_is_called(self):
        """Trackr files Credit Agricole's "Summer Internship 2026 - IT" as a
        spring week, and its requirements agree."""
        found = programme.classify("Summer Internship 2026 - IT",
                                   "No visa sponsorship | Open to first-year students | About the firm:")
        assert found == programme.Programme("Spring Week", "Open to first-year students")

    def test_a_sentence_sending_students_to_a_sibling_is_not_evidence(self):
        found = programme.classify("Summer Internship 2027",
                                   "First-year students should apply to our Spring Week instead.")
        assert found == programme.Programme("Summer Internship", "")

    def test_when_requirements_name_two_years_the_title_decides(self):
        found = programme.classify("Technology Summer Internship",
                                   "Open to first-year students. Also open to penultimate-year students.")
        assert found.work_type == "Summer Internship" and "penultimate" in found.evidence

    def test_penultimate_year_does_not_turn_a_placement_into_a_summer_internship(self):
        """Lloyds asks placement applicants to be in their penultimate year too."""
        found = programme.classify("Software Engineering Industrial Placement (Leeds)",
                                   "Be in your penultimate year at university.")
        assert found == programme.Programme("Industrial Placement",
                                            "Be in your penultimate year at university.")

    def test_penultimate_year_still_types_an_untyped_intern_title(self):
        found = programme.classify("Credit Analyst Intern",
                                   "A university student in their penultimate year.")
        assert found.work_type == "Summer Internship"

    def test_no_evidence_means_no_type(self):
        assert programme.classify("Technology Programme", "Python and SQL.") == programme.Programme()

    def test_evidence_is_clipped_and_ascii(self):
        text = "word " * 100 + "open to first-year students " + "word " * 100
        found = programme.classify("Technology Programme", text)
        assert found.work_type == "Spring Week"
        assert len(found.evidence) <= 210 and found.evidence.isascii()


class Page:
    def __init__(self, text, status=200):
        self.text, self.status = text, status

    @property
    def ok(self):
        return 200 <= self.status < 300


class Pages:
    def __init__(self, pages):
        self.pages = pages
        self.http = self

    def get(self, url, **kwargs):
        return self.pages.get(url, Page("", 404))


class TestOnJobs:
    def test_the_employer_page_reclassifies_a_row(self):
        job = make_job(title="Technology Discovery Programme", work_type="Summer Internship")
        assert programme.refine(job, "Eligibility: first-year students only.")
        assert job.work_type == "Spring Week"
        assert "first-year" in job.year_of_study

    def test_a_page_without_evidence_changes_nothing(self):
        job = make_job(title="Technology Discovery Programme", work_type="Summer Internship")
        assert not programme.refine(job, "Apply now")
        assert (job.work_type, job.year_of_study) == ("Summer Internship", "")

    def test_verification_reads_who_may_apply_off_the_employer_page(self):
        job = make_job(title="Technology Programme", work_type="Spring Week")
        session = Pages({
            job.source_url: Page("<html><main>Listing</main></html>"),
            job.application_url: Page("<html><main><h2>Who can apply</h2>"
                                      "<p>Open to penultimate-year students.</p>"
                                      "<a>Apply now</a></main></html>"),
        })
        verify.verify_job(session, job, config.load("verification"))
        assert job.work_type == "Summer Internship"
        assert "penultimate" in job.year_of_study

    def test_a_type_read_on_an_earlier_run_survives_an_unverified_run(self):
        old = make_job(title="Technology Discovery Programme", work_type="Spring Week")
        old.id, old.year_of_study = "INT-000001", "Open to first-year students."
        new = make_job(title="Technology Discovery Programme", work_type="Summer Internship")
        new.id = "INT-000001"

        pipeline._restore_state([new], {new.id: old})
        assert (new.work_type, new.year_of_study) == ("Spring Week", old.year_of_study)
