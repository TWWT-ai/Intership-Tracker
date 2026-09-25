"""Normalization and the untrusted-input trust boundary."""

from datetime import date

import pytest

from tracker import normalize


class TestTitles:
    @pytest.mark.parametrize(
        "raw",
        [
            "Software Engineering Intern",
            "Software Engineer Internship",
            "SWE Intern - Summer 2027",
            "Software Engineering Internship (12 month, London)",
            "Software Engineer Intern, Summer 2027",
        ],
    )
    def test_variants_converge(self, raw):
        """The spec's core dedupe case: these are all the same role."""
        assert normalize.title(raw)[1] == "software engineer"

    def test_different_roles_stay_distinct(self):
        assert normalize.title("Machine Learning Intern")[1] != normalize.title(
            "Software Engineering Intern"
        )[1]

    def test_display_title_is_preserved(self):
        display, _ = normalize.title("SWE Intern - Summer 2027")
        assert display == "SWE Intern - Summer 2027"

    def test_title_of_only_noise_does_not_collapse_to_empty(self):
        """An empty key would merge every such listing into one row."""
        assert normalize.title("Internship")[1] != ""


class TestCompanies:
    @pytest.mark.parametrize(
        "raw", ["NVIDIA", "NVIDIA Corporation", "Nvidia Ltd", "nvidia technologies"]
    )
    def test_suffixes_and_case_are_stripped(self, raw):
        assert normalize.company(raw)[1] == "nvidia"

    def test_alias_maps_to_canonical_display_name(self):
        assert normalize.company("jpmorgan chase")[0] == "J.P. Morgan"

    def test_distinct_companies_stay_distinct(self):
        assert normalize.company("Arm")[1] != normalize.company("Amazon")[1]


class TestLocations:
    def test_remote_is_an_arrangement_not_a_location(self):
        location, arrangement = normalize.location("Remote (UK)")
        assert location == "United Kingdom"
        assert arrangement == "Remote"

    def test_multipart_location_resolves_to_the_city(self):
        assert normalize.location("London, United Kingdom")[0] == "London"


class TestDates:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("3rd April 2027", date(2027, 4, 3)),
            ("03/04/2027", date(2027, 4, 3)),      # UK sites: day first
            ("2027-04-03", date(2027, 4, 3)),
            ("April 3, 2027", date(2027, 4, 3)),
            ("Deadline: 3 April 2027", date(2027, 4, 3)),
        ],
    )
    def test_real_dates_parse(self, raw, expected):
        assert normalize.parse_date(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "rolling", "ASAP", "April", "2027", "TBC"])
    def test_unparseable_input_yields_none_never_a_guess(self, raw):
        """A fabricated deadline is worse than a missing one: it looks actionable."""
        assert normalize.parse_date(raw) is None

    def test_absurd_dates_are_rejected(self):
        assert normalize.parse_date("1 January 1899") is None


class TestSkillExtraction:
    def test_finds_known_skills_only(self):
        skills, languages, _ = normalize.extract_skills(
            "We use Python, PyTorch and C++ on Linux. Blorptech experience a plus."
        )
        assert {"Python", "C++", "PyTorch", "Linux"} <= set(skills)
        assert "Blorptech" not in skills          # never invents a skill
        assert set(languages) == {"Python", "C++"}

    def test_does_not_match_inside_words(self):
        skills, _, _ = normalize.extract_skills("We are going to the gorge")
        assert "Go" not in skills


class TestSanitisation:
    @pytest.mark.parametrize("payload", ["=1+1", "+cmd", "-2", "@SUM(A1)"])
    def test_formula_injection_is_defused(self, payload):
        """Scraped text lands in a spreadsheet the user opens."""
        assert normalize.excel_safe(payload).startswith("'")

    def test_ordinary_text_is_untouched(self):
        assert normalize.excel_safe("Software Engineer") == "Software Engineer"

    def test_control_characters_are_stripped(self):
        assert "‮" not in normalize.clean_text("Intern‮evil")

    def test_long_fields_are_capped(self):
        assert len(normalize.clean_text("x" * 9000, limit=100)) <= 104


def test_board_spellings_of_one_employer_normalize_together():
    """TargetJobs lists "JPMC" and "Carter Jonas LLP" for rows other platforms name
    "J.P. Morgan" and "Carter Jonas"."""
    from tracker import normalize
    assert normalize.company("JPMC")[1] == normalize.company("J.P. Morgan")[1]
    assert normalize.company("Carter Jonas LLP")[1] == normalize.company("Carter Jonas")[1]
