"""Deadline reminders: the plan the calendar run carries out. Offline - the
script never talks to Google, it only reads the workbook and its ledger."""
from datetime import date, timedelta

from tests.conftest import make_job
from tracker import dedupe, excel
from tracker.models import ApplicationStatus

from scripts import calendar_sync as cs

TODAY = date.today()


def write(*jobs):
    dedupe.assign_ids(list(jobs))
    excel.write(list(jobs))


def job(company, title, days, status=None):
    j = make_job(company=company, title=title, deadline=TODAY + timedelta(days=days) if days is not None else None)
    if status:
        j.status = status
    return j


def actions():
    return {a["date"]: a for a in cs.plan(TODAY)["actions"]}


def carry_out(plan_actions):
    cs.plan_path().write_text(__import__("json").dumps({"actions": list(plan_actions.values())}), encoding="utf-8")
    return cs.record({d: a["event_id"] or f"ev-{d}" for d, a in plan_actions.items()})


def test_one_event_per_day_for_roles_still_to_apply_to():
    write(job("Arm", "Software Intern", 5), job("Arm", "Hardware Intern", 5), job("BAE", "Data Intern", 9),
          job("Applied Co", "ML Intern", 9, ApplicationStatus.APPLIED), job("Old Co", "Web Intern", -1),
          job("Undated Co", "Quant Intern", None))
    plan = actions()
    soon, later = (TODAY + timedelta(days=5)).isoformat(), (TODAY + timedelta(days=9)).isoformat()
    assert set(plan) == {soon, later}
    assert plan[soon]["summary"] == "2 deadlines: Arm" and plan[soon]["action"] == "create"
    assert "Applied Co" not in plan[later]["description"] and plan[later]["summary"].startswith("Deadline: ")
    assert plan[soon]["reminders"] == [10080, 4320, 1440]


def test_a_written_day_is_left_alone_then_updated_when_its_roles_change():
    write(job("Arm", "Software Intern", 5))
    carry_out(actions())
    assert actions() == {}

    write(job("Arm", "Software Intern", 5), job("Arm", "Hardware Intern", 5))
    plan = actions()
    day = (TODAY + timedelta(days=5)).isoformat()
    assert plan[day]["action"] == "update" and plan[day]["event_id"] == f"ev-{day}"


def test_a_day_with_nothing_left_is_cleared_not_deleted_and_can_come_back():
    write(job("Arm", "Software Intern", 5))
    carry_out(actions())
    day = (TODAY + timedelta(days=5)).isoformat()

    applied = job("Arm", "Software Intern", 5)
    dedupe.assign_ids([applied])
    excel.write([applied], status_updates={applied.id: ApplicationStatus.APPLIED})    # as if typed in the sheet
    plan = actions()
    assert plan[day]["action"] == "clear" and plan[day]["reminders"] == []
    carry_out(plan)
    assert actions() == {}                               # cleared once, not again

    write(job("Nvidia", "AI Intern", 5))
    assert actions()[day]["action"] == "update"


def test_only_recorded_results_count_so_a_failure_is_retried():
    write(job("Arm", "Software Intern", 5), job("BAE", "Data Intern", 9))
    plan = actions()
    cs.plan_path().write_text(__import__("json").dumps({"actions": list(plan.values())}), encoding="utf-8")
    first = sorted(plan)[0]
    assert cs.record({first: "ev-1"}) == 1
    assert set(actions()) == set(plan) - {first}
