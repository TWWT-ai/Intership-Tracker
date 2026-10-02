"""The weekly discovery matrix: coverage, rotation, and no reliance on 'hackathon'."""

from datetime import date, timedelta

from tests.conftest import make_event as cand
from tracker import config, event_search, events

T = date(2026, 9, 30)
TEN = {"Event Format Search", "City Search", "Technical Domain Search", "University Search",
       "Society Search", "Company Search", "Organisation Search", "Platform Search",
       "Recurring Event Search", "Previous Event Follow-up"}


def rnd(plan, method):
    return next(r for r in plan if r["discovery_method"] == method)["queries"]


def test_plan_runs_all_ten_strategies_in_order():
    plan = event_search.sweep_plan([], [], T)
    assert [r["round"] for r in plan] == list(range(0, 11))
    assert {r["discovery_method"] for r in plan} >= TEN


def test_every_plan_method_is_a_valid_discovery_method():
    allowed = set(config.load("events")["discovery_methods"])
    assert {r["discovery_method"] for r in event_search.sweep_plan([], [], T)} - {""} <= allowed


def test_search_does_not_depend_on_the_word_hackathon():
    plan = event_search.sweep_plan([], [], T)
    qs = [q for r in plan for q in r["queries"]]
    assert sum("hackathon" not in q.casefold() for q in qs) > 0.6 * len(qs)
    fmt = " ".join(rnd(plan, "Event Format Search"))
    for term in ("data sprint", "capture the flag", "engineering design challenge", "athon"):
        assert term in fmt


def test_rotation_covers_every_long_list_within_the_window():
    s = config.load("event_search")
    seen = set()
    for w in range(s["rotation_weeks"]):
        qs = rnd(event_search.sweep_plan([], [], T + timedelta(weeks=w)), "University Search")
        seen |= {q.split('"')[1] for q in qs}
    assert seen == set(s["universities"])


def test_no_generated_round_is_silently_truncated():
    s = config.load("event_search")
    for w in range(s["rotation_weeks"]):
        for r in event_search.sweep_plan([], [], T + timedelta(weeks=w)):
            assert len(r["queries"]) < s["per_round_cap"] or r["round"] in (0, 9, 10)


def test_extra_rounds_take_the_next_slice():
    a = rnd(event_search.sweep_plan([], [], T), "Company Search")
    b = rnd(event_search.sweep_plan([], [], T, offset=1), "Company Search")
    assert a and b and not set(a) & set(b)


def test_same_week_same_plan():
    assert event_search.sweep_plan([], [], T) == event_search.sweep_plan([], [], T)


def test_finished_recurring_event_seeds_a_next_edition_search():
    past = events.clean(cand(name="HackSheffield 10", start_date="2025-11-15", recurring="Yes"))
    assert '"HackSheffield" 2026' in rnd(event_search.sweep_plan([past], [], T),
                                          "Recurring Event Search")


def test_open_leads_and_due_rows_come_first():
    stale = events.clean(cand(last_verified="2026-08-01"))
    stale["id"] = "EVT-0001"
    plan = event_search.sweep_plan([stale], [{"name": "Student AI Challenge 2027",
                                              "organisation": "University X"}], T)
    assert plan[0]["round"] == 0
    assert any("EVT-0001" in q for q in plan[0]["queries"])
    assert any("Student AI Challenge 2027" in q for q in plan[0]["queries"])


def test_missed_event_log_uses_known_reasons():
    s = config.load("event_search")
    assert all(m["reason"] in s["miss_reasons"] for m in s["misses"] or [])
