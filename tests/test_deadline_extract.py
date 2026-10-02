"""Deadlines found anywhere in a posting, with evidence - and never guessed.
Offline: every test works on text."""
from datetime import date

import pytest

from tracker import deadline_extract as dx


def found(text, order="", basis="test"):
    return [(c.date, c.status) for c in dx.candidates(text, source="s", url="u", method="description text",
                                                       employer_page=False, order=order, order_basis=basis)]


# ---------------------------------------------------------------- formats

@pytest.mark.parametrize("written", ["3 Oct 2026", "3 October 2026", "October 3, 2026", "2026-10-03", "2026/10/03",
                                     "3rd October 2026", "3rd of October 2026", "October 3rd, 2026"])
def test_unambiguous_forms(written):
    assert found(f"Apply by {written}.") == [("2026-10-03", dx.OK)]


@pytest.mark.parametrize("written", ["3/10/2026", "03/10/2026", "03.10.2026", "03-10-2026"])
def test_numeric_forms_need_a_stated_order(written):
    assert found(f"Apply by {written}.") == [("", dx.AMBIGUOUS)]
    assert found(f"Apply by {written}.", order=dx.DAYFIRST, basis="page language en-GB") == [("2026-10-03", dx.OK)]


def test_us_page_reads_month_first():
    assert found("Apply by 03/04/2026.", order=dx.MONTHFIRST) == [("2026-03-04", dx.OK)]


def test_a_part_above_twelve_settles_the_order_without_a_locale():
    assert found("Apply by 13/04/2026.") == [("2026-04-13", dx.OK)]
    assert found("Apply by 04/13/2026.") == [("2026-04-13", dx.OK)]


@pytest.mark.parametrize("written, status", [("03/04/26", dx.NOT_SUPPORTED), ("October 2026", dx.MONTH_ONLY),
                                             ("3 October", dx.NO_YEAR), ("32/13/2026", dx.NOT_SUPPORTED)])
def test_unusable_dates_are_flagged_never_guessed(written, status):
    assert found(f"Applications close {written}.") == [("", status)]


def test_locale_comes_from_what_the_source_states():
    assert dx.order_from_locale("en-GB") == dx.DAYFIRST
    assert dx.order_from_locale("en-US") == dx.MONTHFIRST
    assert dx.order_from_locale("en") == ""                          # plain English says nothing
    assert dx.locale_of_url("https://x.wd3.myworkdayjobs.com/en-GB/careers/job/1") == "en-GB"
    assert dx.platform_order("gradcracker") == ""                    # not configured until checked


# ---------------------------------------------------------------- phrases

@pytest.mark.parametrize("phrase", ["apply before", "apply by", "applications close", "application deadline",
                                    "closing date", "applications must be received by", "apply no later than",
                                    "last date to apply"])
def test_each_deadline_phrase(phrase):
    assert found(f"{phrase.capitalize()}: 3 October 2026.") == [("2026-10-03", dx.OK)]
    assert found(f"• {phrase.capitalize()} 3 October 2026 • Start date 1 July 2027") == [("2026-10-03", dx.OK)]


def test_a_deadline_heading_then_the_date():
    assert found("Application deadline 3 October 2026 Salary 25,000") == [("2026-10-03", dx.OK)]


def test_the_date_can_come_before_the_phrase():
    assert found("3 October 2026 is the closing date for applications.") == [("2026-10-03", dx.OK)]


def test_found_in_the_description_not_only_near_a_label():
    text = ("We welcome students from all backgrounds. The application deadline for this role is "
            "30th October 2026 at 11:59PM GMT. Please apply early.")
    assert found(text) == [("2026-10-30", dx.OK)]


@pytest.mark.parametrize("text", [
    "Deadline: rolling. Start date 1 July 2027.",
    "Posted 3 September 2026. Join our friendly team.",
    "The programme starts on 1 July 2027.",
    "Insight event held on 3 October 2026 in London.",
    "Assessment centres take place on 12 November 2026.",
])
def test_other_dates_are_never_deadlines(text):
    assert [f for f in found(text) if f[1] == dx.OK] == []


def test_event_date_and_deadline_in_one_sentence():
    assert found("Insight day on 3 October 2026; apply by 20 September 2026.") == [("2026-09-20", dx.OK)]


def test_rolling_recruitment_is_noted():
    assert ("", dx.ROLLING) in found("We review applications on a rolling basis and may close early.")


# ---------------------------------------------------------------- one row's deadline

def c(date_, source, employer=False, method="feed field", places=()):
    return dx.Candidate(date=date_, status=dx.OK, source=source, url=f"https://{source}.example/1", method=method,
                        employer_page=employer, quote="q", places=list(places)).as_dict()


def test_employer_page_confirms():
    r = dx.resolve([c("2026-10-03", "trackr"), c("2026-10-03", "employer page", True, "employer page")],
                   set(), date(2026, 10, 3))
    assert (r.status, r.deadline, r.confirmed, r.provisional) == (dx.CONFIRMED, date(2026, 10, 3), date(2026, 10, 3), None)


def test_an_earlier_aggregator_date_is_kept_apart_as_provisional():
    r = dx.resolve([c("2026-09-28", "trackr"), c("2026-10-03", "employer page", True, "employer page")],
                   set(), date(2026, 9, 28))
    assert r.status == dx.CONFLICT
    assert r.deadline == r.confirmed == date(2026, 10, 3)            # the column shows the employer's date
    assert r.provisional == date(2026, 9, 28)                        # the earlier one is kept, labelled apart
    assert len(r.applicable) == 2                                     # nothing dropped


def test_listed_only_is_never_confirmed():
    r = dx.resolve([c("2026-10-03", "gradcracker")], set(), date(2026, 10, 3))
    assert (r.status, r.confirmed) == (dx.LISTED, None)


def test_platforms_that_disagree_are_a_conflict_with_the_current_date_kept():
    r = dx.resolve([c("2026-10-03", "trackr"), c("2026-10-10", "higherin")], set(), date(2026, 10, 3))
    assert (r.status, r.deadline, r.confirmed) == (dx.CONFLICT, date(2026, 10, 3), None)


def test_a_place_specific_clause_applies_only_to_that_place():
    ev = [c("2026-11-08", "careers", method="description text"),
          c("2026-10-04", "careers", method="description text", places=["london"])]
    assert dx.resolve(ev, {"leeds"}, None).status == dx.LISTED
    assert dx.resolve(ev, {"leeds"}, None).deadline == date(2026, 11, 8)
    assert dx.resolve(ev, {"london"}, None).status == dx.CONFLICT


def test_nothing_usable_keeps_the_note_and_invents_nothing():
    note = dx.Candidate(date="", status=dx.MONTH_ONLY, source="s", url="u", method="description text",
                        employer_page=False, quote="closes in October 2026").as_dict()
    r = dx.resolve([note], set(), None)
    assert (r.status, r.deadline) == (dx.MONTH_ONLY, None)
