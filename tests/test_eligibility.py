"""Classification by what a posting says, and eligibility for the user's year. No network."""

import pytest
from openpyxl import load_workbook

from tests.conftest import make_job
from tracker import config, dedupe, eligibility, excel

YOU = {"graduation_year": 2029, "degree": "Computer Science (4-year, with industrial placement year)",
       "current_year": 2}


def blank_job(title="Software Engineering Intern"):
    return make_job(title=title, work_type="", description="")


class TestClassify:
    @pytest.mark.parametrize("title,text,kind,basis", [
        ("2027 Spring Insight Event - EMEA", "Our Spring Insight Event is a week-long event taking place in April 2027.",
         "Spring Week", "duration"),
        ("Credit Analyst Intern", "This is a 10 week internship program starting from 29th June 2027.",
         "Summer Internship", "duration"),
        ("2027 Placement Programme", "BlackRock offers placement opportunities for a duration of 12 months.",
         "Industrial Placement", "duration"),
        ("Technology Programme", "The placement year will take place from June 2027 - June 2028.",
         "Industrial Placement", "placement year"),
        ("Technology Programme", "The programme runs from June 2027 to June 2028.",
         "Industrial Placement", "dates"),
        ("2027 - Explore Nomura", "You will have the opportunity to be fast-tracked to first round interviews "
                                  "for our 2027 Summer Internship Programme.", "Spring Week", "leads"),
        ("Cyber Programme", "Open to first-year students.", "Spring Week", "who may apply"),
        ("Global Markets Summer Analyst 2027", "Join us.", "Summer Internship", "title only"),
    ])
    def test_the_posting_decides_before_the_title(self, title, text, kind, basis):
        found, why = eligibility.classify(title, text)
        assert found == kind and why.startswith(basis)

    def test_a_contradicting_title_is_named_in_the_basis(self):
        _, why = eligibility.classify("2027 Summer Internship", "A week-long insight event in April 2027.")
        assert why.endswith("(title says Summer Internship)")


class TestFacts:
    def test_reads_duration_dates_visa_and_right_to_work(self):
        job = blank_job("2027 - Risk - Industrial Placement - London")
        text = ("As a Placement Student, you should be required to complete a 12 month year in industry from "
                "June 2027 - June 2028 whilst enrolled at university. You must have the right to work in the UK, "
                "as we cannot provide sponsorship for this role.")
        eligibility.assess(job, text, YOU)
        assert job.duration == "12 months"
        assert job.programme_dates == "June 2027 - June 2028"
        assert job.visa_sponsorship == "No"
        assert "right to work" in job.right_to_work
        assert job.eligible_for_you == "Yes"

    def test_an_aggregators_visa_note_counts(self):
        job = blank_job()
        eligibility.assess(job, "Opens 2026-08-18 | No visa sponsorship | Divisions: Tech", YOU)
        assert job.visa_sponsorship == "No"

    def test_nothing_stated_stays_blank(self):
        job = blank_job()
        eligibility.assess(job, "Build great software with us.", YOU)
        assert (job.duration, job.programme_dates, job.visa_sponsorship, job.degree_subject) == ("", "", "", "")
        assert job.eligible_for_you == "REQUIRES VERIFICATION"

    def test_holiday_and_office_days_are_not_durations(self):
        job = blank_job()
        eligibility.assess(job, "Enjoy 6 weeks of paid vacation. Hybrid roles require three days in-office weekly.", YOU)
        assert job.duration == ""


class TestYourEligibility:
    @pytest.mark.parametrize("title,text,verdict", [
        ("2027 Spring Insight Event - EMEA",
         "For students who will graduate with an undergraduate or master's degree in 2029.", "Yes"),
        ("2027 Summer Internship Program - EMEA", "For students graduating from their degree program in 2028.", "No"),
        ("2027 Equity Research Spring Week",
         "Our programme is aimed at students in their first year of study (or second year of a four-year degree).", "Yes"),
        ("2027 Technology Spring Week", "Open to first-year students only.", "REQUIRES VERIFICATION"),
        ("Software Engineering Industrial Placement (Leeds)", "Be in your penultimate year at university.",
         "REQUIRES VERIFICATION"),
        ("2027 Finance Industrial Placement Programme",
         "The programme is for penultimate year students with a year in industry as part of their degree.", "Yes"),
        ("2026 Summer Internship Programme", "Open to penultimate-year students.", "No"),
        ("Graduate Analyst", "Final year students only.", "No"),
    ])
    def test_translated_into_your_year(self, title, text, verdict):
        assert eligibility.assess(blank_job(title), text, YOU) == verdict

    def test_a_degree_the_user_does_not_study_needs_checking(self):
        job = blank_job("2027 Spring Week")
        verdict = eligibility.assess(job, "For students graduating in 2029. You must be studying a degree in Law.", YOU)
        assert verdict == "REQUIRES VERIFICATION" and "Law" in job.degree_subject

    def test_a_placement_needs_a_placement_degree(self):
        you = {**YOU, "degree": "Computer Science (3-year)"}
        text = "For candidates graduating in 2029 who are required by their degree to complete a long-term internship."
        assert eligibility.assess(blank_job("2027 Placement Programme"), text, you) == "No"

    def test_no_graduation_year_in_the_profile_decides_nothing(self):
        assert eligibility.assess(blank_job("2027 Spring Week"), "For students graduating in 2029.", {}) == ""

    def test_a_derived_year_never_says_yes(self):
        """'First-year students' on a 2027 programme derives 2029 for a three-year
        degree; a Year 2 student on a four-year degree must still check."""
        job = blank_job("2027 Technology Spring Week")
        assert eligibility.assess(job, "Open to first-year students.", YOU) == "REQUIRES VERIFICATION"
        assert "2029" in job.expected_graduation


class TestWorkbook:
    def test_summary_columns_and_detail_sheet(self):
        job = blank_job("2027 Spring Insight Event - EMEA")
        eligibility.assess(job, "A week-long event in April 2027 for students who will graduate in 2029. "
                                "No visa sponsorship.", YOU)
        dedupe.assign_ids([job])
        wb = load_workbook(excel.write([job]))
        col = excel.col_index()
        ws = wb[excel.SHEET_JOBS]
        assert ws.cell(2, col["Eligible for You"]).value == "Yes"
        assert ws.cell(2, col["Visa Sponsorship"]).value == "No"
        detail = wb[excel.SHEET_ELIGIBILITY]
        row = dict(zip([c.value for c in detail[1]], [c.value for c in detail[2]]))
        assert row["Eligible for you"] == "Yes"
        assert row["Duration"] == "1 week"
        assert row["Graduation year"] == "2029"

    def test_layout_places_the_summary_after_expected_graduation(self):
        layout = config.load("workbook")["columns"]
        i = layout.index("Expected Graduation")
        assert layout[i + 1: i + 4] == ["Eligible for You", "Duration", "Visa Sponsorship"]

    def test_facts_survive_a_run_that_did_not_read_the_page(self):
        old = blank_job("2027 Placement Programme")
        old.id = "INT-000001"
        eligibility.assess(old, "A 12 month placement for candidates graduating in 2029. No visa sponsorship.", YOU)
        new = blank_job("2027 Placement Programme")
        new.id = "INT-000001"
        eligibility.carry(new, old)
        assert (new.duration, new.visa_sponsorship, new.classification_basis) == (
            old.duration, "No", old.classification_basis)


class TestReviewFixes:
    def test_a_derived_year_on_an_older_row_is_not_taken_as_stated(self):
        """Credit Agricole: 'Open to first-year students', with 2028/2029 derived
        by an earlier run that did not record the basis."""
        job = blank_job("Summer Internship 2027 - IT")
        job.expected_graduation, job.year_of_study = "2029, 2030", "Open to first-year students"
        job.university_year = "Open to first-year students"
        eligibility.settle(job, YOU)
        assert job.eligible_for_you == "REQUIRES VERIFICATION"

    def test_an_older_row_whose_sentence_names_the_year_is_stated(self):
        job = blank_job("2027 Placement Programme")
        job.expected_graduation = "2029"
        job.year_of_study = "This programme is for candidates graduating in 2029."
        eligibility.settle(job, YOU)
        assert job.eligible_for_you == "Yes"

    def test_new_rows_record_the_basis(self):
        stated, derived = blank_job("2027 Spring Week"), blank_job("2027 Technology Spring Week")
        eligibility.assess(stated, "For students graduating in 2029.", YOU)
        eligibility.assess(derived, "Open to first-year students.", YOU)
        assert (stated.graduation_basis, derived.graduation_basis) == ("stated", "derived")

    def test_an_unrecognised_subject_sentence_does_not_downgrade(self):
        job = blank_job("2027 Equity Research Spring Week")
        text = ("Our programme is aimed at students in their first year of study (or second year of a four-year "
                "degree). Candidates studying towards exciting careers are welcome.")
        assert eligibility.assess(job, text, YOU) == "Yes"

    def test_a_long_off_cycle_internship_is_not_a_placement_by_duration(self):
        kind, _ = eligibility.classify("2027 Quantitative Finance Off-Cycle Internship", "A 9 month internship.")
        assert kind != "Industrial Placement"

    def test_a_corrected_rule_withdraws_a_company_site_rows_old_type(self):
        job = make_job(title="2027 Quantitative Finance Off-Cycle Internship", work_type="", source="careers",
                       description="A 9 month internship for students graduating in 2027.")
        job.work_type, job.classification_basis = "Industrial Placement", "duration: 9 months"
        eligibility.reclassify(job)
        assert job.work_type != "Industrial Placement"

    def test_an_aggregator_rows_firm_type_is_kept_when_its_text_says_nothing(self):
        job = make_job(title="Technology Programme", work_type="", source="trackr", description="No visa sponsorship")
        job.work_type, job.classification_basis = "Spring Week", "duration: 1 week"
        eligibility.reclassify(job)
        assert job.work_type == "Spring Week"


class TestDatesThatStartWithADay:
    """A range written "14 June 2027 - 28th August 2027" starts with a day. It
    once crashed the whole TargetJobs source (KeyError '14'), which then quietly
    reused its last snapshot for days."""

    @pytest.mark.parametrize("text", [
        "The internship runs 14 June 2027 - 20 August 2027 in London.",
        "The internship runs 28th June 2027 to 3rd September 2027.",
    ])
    def test_a_day_first_range_is_read_not_a_crash(self, text):
        kind, basis = eligibility.classify("Technology Intern", text)
        assert kind == "Summer Internship" and "dates:" in basis
