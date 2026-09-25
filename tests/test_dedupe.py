"""Deduplication: one row per real job, and never merging distinct jobs."""

import json
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from sources import trackr
from tests.conftest import make_job
from tracker import config, dedupe

FIXTURES = Path(__file__).parent / "fixtures"


class _Response:
    def __init__(self, text):
        self.text, self.status, self.ok = text, 200, True


class SeasonSession:
    """Answers each Trackr season query with that season's recorded response."""

    def __init__(self):
        self.http = self

    def get(self, url, **kwargs):
        season = parse_qs(urlparse(url).query)["season"][0]
        return _Response((FIXTURES / f"trackr_season_{season}.json").read_text(encoding="utf-8"))


def sweep_two_seasons():
    """A Trackr sweep of 2026 and 2027, as config/sources.yaml runs it."""
    settings = {"regions": ["UK"], "industries": ["Tech"],
                "seasons": ["2026", "2027"], "types": ["summer-internships"]}
    return trackr.search_jobs(SeasonSession(), settings)


def fixture_ids() -> set[str]:
    return {
        entry["id"]
        for season in ("2026", "2027")
        for entry in json.loads((FIXTURES / f"trackr_season_{season}.json")
                                .read_text(encoding="utf-8"))["programmes"]
    }


def by_listing(jobs) -> dict:
    """{external_id: the job holding that listing}."""
    return {ref.external_id: job for job in jobs for ref in job.sources}


class TestCrossPlatformMerge:
    def test_same_job_from_three_platforms_becomes_one_row(self):
        """Spec 26: one Excel row, three source references, all URLs kept."""
        deadline = date(2027, 1, 10)
        jobs = [
            make_job(company="NVIDIA Corporation", title="Software Engineering Intern",
                     source="trackr", deadline=deadline),
            make_job(company="Nvidia Ltd", title="SWE Intern - Summer 2027",
                     source="gradcracker", deadline=deadline + timedelta(days=2)),
            make_job(company="NVIDIA", title="Software Engineer Internship",
                     source="brightnetwork", deadline=deadline),
        ]

        merged, duplicates = dedupe.deduplicate(jobs)

        assert len(merged) == 1
        assert duplicates == 2
        assert merged[0].source_names() == ["brightnetwork", "gradcracker", "trackr"]

        # Every platform's URLs survive the merge.
        assert len(merged[0].sources) == 3
        for ref in merged[0].sources:
            assert ref.source_url
            assert ref.application_url

    def test_merge_keeps_the_earliest_deadline(self):
        """If platforms disagree, the later date risks missing the real cutoff."""
        early, late = date(2027, 1, 5), date(2027, 1, 20)
        jobs = [
            make_job(source="trackr", deadline=late),
            make_job(source="gradcracker", deadline=early),
        ]
        merged, _ = dedupe.deduplicate(jobs)
        assert merged[0].deadline == early

    def test_merge_fills_gaps_from_the_other_source(self):
        a = make_job(source="trackr", deadline=date(2027, 1, 10))
        a.salary = ""
        b = make_job(source="gradcracker", deadline=date(2027, 1, 10))
        b.salary = "GBP 3,000/month"

        merged, _ = dedupe.deduplicate([a, b])
        assert merged[0].salary == "GBP 3,000/month"

    def test_skills_are_combined_across_platforms(self):
        a = make_job(source="trackr", description="Python required")
        b = make_job(source="gradcracker", description="PyTorch and Linux required")
        merged, _ = dedupe.deduplicate([a, b])
        assert {"Python", "PyTorch", "Linux"} <= set(merged[0].skills)


class TestConservativeMerging:
    def test_different_roles_at_same_company_do_not_merge(self):
        """A wrongly merged pair hides a job the user could have applied to."""
        jobs = [
            make_job(title="Software Engineering Intern", source="trackr"),
            make_job(title="Machine Learning Intern", source="trackr"),
        ]
        merged, duplicates = dedupe.deduplicate(jobs)
        assert len(merged) == 2
        assert duplicates == 0

    def test_same_role_in_different_cities_stays_separate(self):
        jobs = [
            make_job(location="London", source="trackr"),
            make_job(location="Cambridge", source="trackr"),
        ]
        assert len(dedupe.deduplicate(jobs)[0]) == 2

    def test_different_intakes_stay_separate(self):
        """Deadlines months apart mean different rounds, not one job."""
        jobs = [
            make_job(deadline=date(2026, 11, 1), source="trackr"),
            make_job(deadline=date(2027, 6, 1), source="trackr"),
        ]
        assert len(dedupe.deduplicate(jobs)[0]) == 2

    def test_different_work_types_stay_separate(self):
        jobs = [
            make_job(work_type="Summer Internship", source="trackr"),
            make_job(work_type="Industrial Placement", source="trackr"),
        ]
        assert len(dedupe.deduplicate(jobs)[0]) == 2

    def test_different_companies_never_merge(self):
        jobs = [make_job(company="NVIDIA"), make_job(company="Arm")]
        assert len(dedupe.deduplicate(jobs)[0]) == 2

    def test_two_ids_on_one_platform_are_two_listings(self):
        """However alike they read, a platform lists one job under one id."""
        jobs = [make_job(source="trackr", external_id="4uxcmyysh8"),
                make_job(source="trackr", external_id="aa3sguncgj")]
        merged, duplicates = dedupe.deduplicate(jobs)
        assert len(merged) == 2
        assert duplicates == 0

    def test_one_listing_seen_twice_keeps_one_ref(self):
        jobs = [make_job(source="trackr", external_id="fhfnv05uki"),
                make_job(source="trackr", external_id="fhfnv05uki")]
        merged, _ = dedupe.deduplicate(jobs)
        assert len(merged) == 1
        assert len(merged[0].sources) == 1

    def test_titles_naming_different_years_stay_separate_across_platforms(self):
        """Without deadlines, the year in the title is all that tells rounds apart."""
        jobs = [make_job(company="GIC", title="GIC Internship Programme 2026", source="trackr"),
                make_job(company="GIC", title="GIC Internship Programme 2027", source="gradcracker")]
        assert len(dedupe.deduplicate(jobs)[0]) == 2

    @pytest.mark.parametrize("title,years", [
        ("GIC Internship Programme 2027", {2027}),
        ("2026/27 Industrial Placement", {2026, 2027}),
        ("2026-2027 Industrial Placement Programme", {2026, 2027}),
        ("Technology Internship (Summer 2026) - 12 months", {2026}),
        ("Software Engineering Intern", set()),
    ])
    def test_title_years(self, title, years):
        assert dedupe.title_years(make_job(title=title)) == years


class TestSeasons:
    """Trackr is swept for 2026 and 2027; each round of a programme has its own id."""

    def test_each_round_of_a_programme_keeps_its_own_row(self):
        merged, duplicates = dedupe.deduplicate(sweep_two_seasons())
        gic = sorted(j.job_title for j in merged if j.company == "GIC")
        assert gic == ["GIC Internship Programme 2026", "GIC Internship Programme 2027"]
        assert duplicates == 0

    def test_no_listing_is_dropped(self):
        """BNY Mellon's 2027 round used to vanish, its SourceRef discarded."""
        merged, _ = dedupe.deduplicate(sweep_two_seasons())
        assert set(by_listing(merged)) == fixture_ids()
        assert by_listing(merged)["jh8fwwxier"].job_title == "2027 Summer Internship Program"

    def test_fields_are_not_mixed_across_rounds(self):
        """Jefferies showed the 2026 title with the 2027 deadline."""
        rows = by_listing(dedupe.deduplicate(sweep_two_seasons())[0])
        old, new = rows["lw5imjdxbb"], rows["mu3og4g0uc"]
        assert (old.job_title, old.deadline) == ("2026 Information Technology Summer Internship", None)
        assert new.deadline == date(2026, 11, 15)
        assert new.application_url.startswith("https://jefferies.tal.net/")

    def test_every_row_gets_its_own_id(self):
        """Samsara's two listings share a fingerprint and used to share an id."""
        merged, _ = dedupe.deduplicate(sweep_two_seasons())
        dedupe.assign_ids(merged)
        assert len({j.id for j in merged}) == len(merged)

    def test_ids_follow_listings_across_runs(self):
        first, _ = dedupe.deduplicate(sweep_two_seasons())
        dedupe.assign_ids(first)
        second, _ = dedupe.deduplicate(list(reversed(sweep_two_seasons())))
        dedupe.assign_ids(second)
        assert {k: j.id for k, j in by_listing(second).items()} == \
               {k: j.id for k, j in by_listing(first).items()}


class TestPrimarySource:
    def test_most_complete_source_wins_over_preference_order(self):
        """Trackr is preferred, but only when it actually carries more data."""
        thin = make_job(source="trackr", deadline=None, application_url="")
        thin.sources[0].listed_deadline = None
        thin.sources[0].application_url = ""

        rich = make_job(source="gradcracker", deadline=date(2027, 1, 10),
                        application_url="https://employer.example.com/apply")

        merged, _ = dedupe.deduplicate([thin, rich])
        assert merged[0].primary_source == "gradcracker"
        assert merged[0].application_url == "https://employer.example.com/apply"

    def test_preference_order_breaks_ties(self):
        a = make_job(source="gradcracker", deadline=date(2027, 1, 10))
        b = make_job(source="trackr", deadline=date(2027, 1, 10))
        merged, _ = dedupe.deduplicate([a, b])
        assert merged[0].primary_source == "trackr"


class TestStableIds:
    def test_ids_are_assigned_and_sequential(self):
        jobs = [make_job(company="NVIDIA"), make_job(company="Arm")]
        dedupe.assign_ids(jobs)
        assert sorted(j.id for j in jobs) == ["INT-000001", "INT-000002"]

    def test_same_job_keeps_its_id_across_runs(self):
        """The ID links the workbook row to the user's notes and status."""
        first = make_job()
        dedupe.assign_ids([first])

        second = make_job()                 # a fresh scrape of the same job
        dedupe.assign_ids([second])
        assert second.id == first.id

    def test_id_survives_a_deadline_change(self):
        first = make_job(deadline=date(2027, 1, 10))
        dedupe.assign_ids([first])

        moved = make_job(deadline=date(2027, 2, 20))
        dedupe.assign_ids([moved])
        assert moved.id == first.id

    def test_id_survives_a_title_rewording(self):
        first = make_job(title="Software Engineering Intern")
        dedupe.assign_ids([first])

        reworded = make_job(title="Software Engineer Internship")
        dedupe.assign_ids([reworded])
        assert reworded.id == first.id

    def test_different_jobs_get_different_ids(self):
        a, b = make_job(title="Software Engineering Intern"), make_job(title="Robotics Intern")
        dedupe.assign_ids([a, b])
        assert a.id != b.id

    def test_listings_sharing_a_fingerprint_get_different_ids(self):
        """One id per row: the workbook matches the user's status back by id."""
        a = make_job(title="Software Engineering Intern", external_id="4uxcmyysh8")
        b = make_job(title="Software Engineering Internship", external_id="aa3sguncgj")
        assert a.fingerprint() == b.fingerprint()
        dedupe.assign_ids([a, b])
        assert a.id != b.id

    def test_an_id_from_before_the_fix_goes_to_the_latest_round(self):
        """A pre-fix row hid both rounds under one fingerprint-keyed id. The
        status on it was set against the open round, so that round keeps it."""
        old = make_job(company="GIC", title="GIC Internship Programme 2026", external_id="es8mj468yy")
        new = make_job(company="GIC", title="GIC Internship Programme 2027", external_id="fhfnv05uki")
        config.ID_REGISTRY.write_text(
            json.dumps({old.fingerprint(): "INT-000213", "_next": 214}), encoding="utf-8")

        dedupe.assign_ids([old, new])
        assert new.id == "INT-000213"
        assert old.id == "INT-000214"

    def test_a_new_round_never_inherits_the_id_of_another_listing(self):
        """If the 2027 round drops off Trackr for a day, a 2028 round with the
        same fingerprint must not pick up its id - and the APPLIED with it."""
        applied = make_job(company="GIC", title="GIC Internship Programme 2027", external_id="fhfnv05uki")
        dedupe.assign_ids([applied])

        next_round = make_job(company="GIC", title="GIC Internship Programme 2028", external_id="n3xtr0und0")
        assert next_round.fingerprint() == applied.fingerprint()
        dedupe.assign_ids([next_round])
        assert next_round.id != applied.id

        # ...while the 2027 round, when it comes back, still has its own.
        returning = make_job(company="GIC", title="GIC Internship Programme 2027", external_id="fhfnv05uki")
        dedupe.assign_ids([returning, next_round])
        assert returning.id == applied.id
