"""End-to-end pipeline behaviour, with fake adapters. No network."""

import json
from datetime import date, datetime, timedelta

import pytest
from openpyxl import load_workbook

from tests.conftest import make_job
from tracker import config, excel, pipeline
from tracker.models import ApplicationStatus, ChangeType, OpenStatus
from sources.fetch import SourceBlocked, SourceError


class FakeAdapter:
    """Stands in for a source module."""

    def __init__(self, jobs=None, error=None):
        self._jobs = jobs or []
        self._error = error
        self.calls = 0

    def search_jobs(self, session, settings):
        self.calls += 1
        if self._error:
            raise self._error
        # Fresh copies each call, as a real scrape would return.
        return [make_job(**spec) for spec in self._jobs]

    def get_job(self, session, url):
        return None


class FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


@pytest.fixture
def fake_run(monkeypatch):
    """Install fake adapters and a no-op session, and skip verification."""

    def install(adapters: dict):
        monkeypatch.setattr(pipeline, "ADAPTERS", adapters)
        monkeypatch.setattr(pipeline, "Session", FakeSession)

    return install


NVIDIA = {"company": "NVIDIA Corporation", "title": "Software Engineering Intern",
          "source": "trackr", "deadline": date(2027, 1, 10)}
NVIDIA_ALT = {"company": "Nvidia Ltd", "title": "SWE Intern - Summer 2027",
              "source": "gradcracker", "deadline": date(2027, 1, 10)}
ARM = {"company": "Arm", "title": "Embedded Software Intern",
       "source": "brightnetwork", "deadline": date(2027, 3, 1)}


class TestDailyRun:
    def test_writes_a_workbook_and_reports(self, fake_run):
        fake_run({"trackr": FakeAdapter([NVIDIA, ARM])})
        summary = pipeline.run_daily(skip_verify=True)

        assert config.WORKBOOK.exists()
        assert summary["new"] == 2
        assert summary["sources_ok"] == ["trackr"]

    def test_merges_across_platforms_into_one_row(self, fake_run):
        fake_run({
            "trackr": FakeAdapter([NVIDIA]),
            "gradcracker": FakeAdapter([NVIDIA_ALT]),
        })
        pipeline.run_daily(skip_verify=True)

        ws = load_workbook(config.WORKBOOK)[excel.SHEET_JOBS]
        assert ws.max_row == 2
        sources = ws.cell(2, excel.col_index()["Sources"]).value
        assert "Trackr" in sources and "Gradcracker" in sources

    def test_running_twice_adds_no_rows(self, fake_run):
        """Spec 29.12: the daily workflow must be safe to run repeatedly."""
        fake_run({"trackr": FakeAdapter([NVIDIA, ARM])})

        pipeline.run_daily(skip_verify=True)
        first = _ids()

        second_summary = pipeline.run_daily(skip_verify=True)
        second = _ids()

        assert first == second
        assert len(second) == 2
        assert second_summary["new"] == 0
        assert second_summary["updated"] == 0

    def test_nothing_is_reclassified_on_an_unchanged_second_run(self, fake_run):
        fake_run({"trackr": FakeAdapter([NVIDIA, ARM])})
        pipeline.run_daily(skip_verify=True)

        summary = pipeline.run_daily(skip_verify=True)
        assert (summary["new"], summary["updated"], summary["removed"]) == (0, 0, 0)

    def test_status_survives_a_full_second_run(self, fake_run):
        """The headline guarantee, exercised through the real pipeline."""
        fake_run({"trackr": FakeAdapter([NVIDIA])})
        pipeline.run_daily(skip_verify=True)

        wb = load_workbook(config.WORKBOOK)
        ws = wb[excel.SHEET_JOBS]
        ws.cell(2, excel.col_index()["Application Status"]).value = "Application Submitted"
        ws.cell(2, excel.col_index()["Notes"]).value = "Submitted 8 Sep"
        wb.save(config.WORKBOOK)

        pipeline.run_daily(skip_verify=True)

        ws = load_workbook(config.WORKBOOK)[excel.SHEET_JOBS]
        assert ws.cell(2, excel.col_index()["Application Status"]).value == "Application Submitted"
        assert ws.cell(2, excel.col_index()["Notes"]).value == "Submitted 8 Sep"

    def test_ids_are_stable_across_runs(self, fake_run):
        fake_run({"trackr": FakeAdapter([NVIDIA, ARM])})
        pipeline.run_daily(skip_verify=True)
        before = _ids()

        pipeline.run_daily(skip_verify=True)
        assert _ids() == before

    def test_dry_run_writes_nothing(self, fake_run):
        fake_run({"trackr": FakeAdapter([NVIDIA])})
        pipeline.run_daily(skip_verify=True, dry_run=True)
        assert not config.WORKBOOK.exists()


class TestSourceFailureIsolation:
    def test_one_source_failing_does_not_stop_the_others(self, fake_run):
        """Spec 25: Trackr OK + Gradcracker down + Bright Network OK still updates Excel."""
        fake_run({
            "trackr": FakeAdapter([NVIDIA]),
            "gradcracker": FakeAdapter(error=SourceError("site down")),
            "brightnetwork": FakeAdapter([ARM]),
        })
        summary = pipeline.run_daily(skip_verify=True)

        assert summary["sources_ok"] == ["trackr", "brightnetwork"]
        assert summary["sources_failed"] == ["gradcracker"]
        assert len(_ids()) == 2                     # both good sources reached Excel

    def test_a_blocked_source_is_recorded_not_fatal(self, fake_run):
        fake_run({
            "trackr": FakeAdapter([NVIDIA]),
            "gradcracker": FakeAdapter(error=SourceBlocked("challenge served")),
        })
        summary = pipeline.run_daily(skip_verify=True)

        assert "gradcracker" in summary["sources_failed"]
        assert config.WORKBOOK.exists()

    def test_failed_source_falls_back_to_its_last_good_snapshot(self, fake_run):
        """A transient outage must not silently delete that platform's rows."""
        fake_run({"gradcracker": FakeAdapter([NVIDIA_ALT])})
        pipeline.run_daily(skip_verify=True)
        assert len(_ids()) == 1

        fake_run({"gradcracker": FakeAdapter(error=SourceError("timeout"))})
        summary = pipeline.run_daily(skip_verify=True)

        assert summary["sources_failed"] == ["gradcracker"]
        assert len(_ids()) == 1                     # the row survived

    def test_all_sources_failing_leaves_the_workbook_alone(self, fake_run):
        fake_run({"trackr": FakeAdapter([NVIDIA])})
        pipeline.run_daily(skip_verify=True)
        before = load_workbook(config.WORKBOOK)[excel.SHEET_JOBS].max_row

        # No snapshot to fall back on for a source that never succeeded.
        fake_run({"brightnetwork": FakeAdapter(error=SourceError("down"))})
        pipeline.run_daily(skip_verify=True)

        assert load_workbook(config.WORKBOOK)[excel.SHEET_JOBS].max_row == before


class TestChangeDetection:
    def test_a_moved_deadline_is_an_update_not_a_new_job(self, fake_run):
        fake_run({"trackr": FakeAdapter([NVIDIA])})
        pipeline.run_daily(skip_verify=True)
        original = _ids()

        moved = {**NVIDIA, "deadline": date(2027, 2, 15)}
        fake_run({"trackr": FakeAdapter([moved])})
        summary = pipeline.run_daily(skip_verify=True)

        assert summary["new"] == 0
        assert summary["updated"] == 1
        assert _ids() == original                   # same id, same row

    def test_a_passed_deadline_marks_the_job_expired(self, fake_run):
        stale = {**NVIDIA, "deadline": date.today() - timedelta(days=2)}
        fake_run({"trackr": FakeAdapter([stale])})
        summary = pipeline.run_daily(skip_verify=True)
        assert summary["expired"] == 1

    def test_a_genuinely_new_job_is_counted_as_new(self, fake_run):
        fake_run({"trackr": FakeAdapter([NVIDIA])})
        pipeline.run_daily(skip_verify=True)

        fake_run({"trackr": FakeAdapter([NVIDIA, ARM])})
        summary = pipeline.run_daily(skip_verify=True)
        assert summary["new"] == 1


# One programme, two Trackr seasons, two Trackr ids. Normalization reduces both
# titles to "gic", so they share a fingerprint.
GIC_2026 = {"company": "GIC", "title": "GIC Internship Programme 2026", "source": "trackr",
            "external_id": "es8mj468yy", "deadline": date(2026, 1, 27)}
GIC_2027 = {"company": "GIC", "title": "GIC Internship Programme 2027", "source": "trackr",
            "external_id": "fhfnv05uki"}


class TestSeasonsAndStatus:
    def test_status_stays_on_the_round_it_was_set_on(self, fake_run):
        fake_run({"trackr": FakeAdapter([GIC_2026, GIC_2027])})
        pipeline.run_daily(skip_verify=True)
        rows = _rows_by_title()
        assert rows["GIC Internship Programme 2026"]["ID"] != rows["GIC Internship Programme 2027"]["ID"]

        _set_status("GIC Internship Programme 2027", "Application Submitted")
        pipeline.run_daily(skip_verify=True)

        rows = _rows_by_title()
        assert rows["GIC Internship Programme 2027"]["Status"] == "Application Submitted"
        assert rows["GIC Internship Programme 2026"]["Status"] is None     # not applied

    def test_a_pre_fix_merged_row_hands_its_status_to_the_open_round(self, fake_run):
        """Before the fix both rounds folded into one row showing the 2026
        listing, under an id keyed only by fingerprint. The user applied to the
        open 2027 round. Splitting the row must put APPLIED there, and must not
        put the 2026 round's CLOSED verdict on it."""
        legacy = make_job(**GIC_2026)
        legacy.id = "INT-000213"
        legacy.deadline_source = "trackr-listing"
        legacy.open_status = OpenStatus.CLOSED
        legacy.last_verified = datetime(2026, 9, 8, 20, 30)
        config.ID_REGISTRY.write_text(
            json.dumps({legacy.fingerprint(): "INT-000213", "_next": 214}), encoding="utf-8")
        pipeline._save_previous([legacy])
        excel.write([legacy])
        # Typed in the old code, as the real pre-fix workbook holds it.
        _set_status("GIC Internship Programme 2026", "APPLIED")

        fake_run({"trackr": FakeAdapter([GIC_2026, GIC_2027])})
        pipeline.run_daily(skip_verify=True)

        rows = _rows_by_title()
        new, old = rows["GIC Internship Programme 2027"], rows["GIC Internship Programme 2026"]
        assert (new["ID"], new["Status"], new["Open Status"]) == (
            "INT-000213", "Application Submitted", "UNKNOWN")
        assert new["Deadline"] is None                  # not the 2026 round's date
        assert (old["ID"], old["Status"], old["Open Status"]) == ("INT-000214", None, "CLOSED")

    def test_a_listed_deadline_is_never_carried_onto_a_listing_without_one(self, fake_run):
        """A pre-fix row could hold a deadline merged in from another round."""
        fake_run({"trackr": FakeAdapter([GIC_2027])})
        pipeline.run_daily(skip_verify=True)
        last_run = config.STATE_DIR / "last_run.json"
        payload = json.loads(last_run.read_text(encoding="utf-8"))
        payload["jobs"][0].update(deadline="2026-01-27", deadline_source="none")
        last_run.write_text(json.dumps(payload), encoding="utf-8")

        pipeline.run_daily(skip_verify=True)
        assert _rows_by_title()["GIC Internship Programme 2027"]["Deadline"] is None


def _ids() -> set[str]:
    ws = load_workbook(config.WORKBOOK)[excel.SHEET_JOBS]
    return {ws.cell(r, 1).value for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value}


def _rows_by_title() -> dict[str, dict]:
    ws = load_workbook(config.WORKBOOK)[excel.SHEET_JOBS]
    col = excel.col_index()
    return {
        ws.cell(r, col["Job Title"]).value: {
            key: ws.cell(r, col[label]).value
            for key, label in (("ID", "ID"), ("Status", excel.STATUS_COLUMN),
                               ("Open Status", "Open Status"), ("Deadline", "Deadline"))
        }
        for r in range(2, ws.max_row + 1)
    }


def _set_status(title: str, status: str) -> None:
    """Type a status into the workbook, as the user would."""
    wb = load_workbook(config.WORKBOOK)
    ws = wb[excel.SHEET_JOBS]
    col = excel.col_index()
    row = next(r for r in range(2, ws.max_row + 1) if ws.cell(r, col["Job Title"]).value == title)
    ws.cell(row, col[excel.STATUS_COLUMN]).value = status
    wb.save(config.WORKBOOK)
