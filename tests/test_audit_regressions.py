"""Regression cases from the 2026-10-03 link audit
(data/checkpoint/link-audit-20261003/). Offline: the posting wording is copied
into the tests; nothing is fetched."""
from datetime import date

from tests.conftest import make_job
from tracker import deadline_extract as dx
from tracker import dedupe, relevance, role_filter

TODAY = date(2026, 10, 3)


def deadlines(text, **kw):
    return [(c.date, c.status) for c in dx.candidates(text, source="employer page", url="u", method="employer page",
                                                       employer_page=True, **kw)]


# ---------------------------------------------------------------- INT-000378 Maven Securities

MAVEN = ("Programme details: Location: London Programme dates: 21st June - 20 August 2027 "
         "Application deadline: 21st October 2026 Key requirements: You must have the right to work in the UK.")


def test_int_000378_programme_end_date_is_not_a_deadline():
    assert deadlines(MAVEN) == [("2026-10-21", dx.OK)]


def test_a_label_after_a_date_belongs_to_the_next_date():
    assert deadlines("Internship runs 1 July 2027 - 30 August 2027 Closing date 1 November 2026.") == [
        ("2026-11-01", dx.OK)]


def test_start_and_event_dates_need_an_application_label():
    assert deadlines("Start date: 6 July 2027. Assessment centre: 12 November 2026. Apply by 31 October 2026.") == [
        ("2026-10-31", dx.OK)]


# ---------------------------------------------------------------- INT-000372 London Stock Exchange Group

LSEG = ("You will be graduating with a degree summer 2028 and studying a 'STEM' subject (science, technology, "
        "engineering or mathematics). Application close: 30th October, however we recommend an early application.")


def test_int_000372_stem_with_mathematics_spelled_out_is_broad_not_an_exclusion():
    value, reason, quotes = relevance.degree_subject(LSEG)
    assert value == relevance.BROAD_FIELD and "STEM" in quotes[0]
    assert value not in relevance.STRONG_AGAINST


def test_int_000372_deadline_without_a_year_is_noted_not_guessed():
    assert ("", dx.NO_YEAR) in deadlines(LSEG)


def test_only_a_clear_incompatible_requirement_excludes_cs():
    assert relevance.degree_subject("You must be studying Mechanical Engineering.")[0] == relevance.OTHER_SUBJECT_ONLY
    assert relevance.degree_subject("A numerate degree such as Maths or Physics.")[0] == relevance.BROAD_FIELD
    assert relevance.degree_subject("Studying Maths, Physics or a related subject.")[0] != relevance.OTHER_SUBJECT_ONLY


def test_a_graduate_title_alone_does_not_exclude():
    assert relevance.study_level("Graduate Programme - Graduate Developer", "We build systems.", TODAY)[0] \
        == relevance.LEVEL_UNCLEAR


# ---------------------------------------------------------------- BAE Systems (Higherin)

def test_bae_listing_dates_that_disagree_are_kept_with_their_sources():
    listed = dx.field_candidate(date(2026, 11, 30), source="higherin",
                                url="https://higherin.com/jobs/44381/bae-systems/hardware-engineer-summer-internship")
    structured = dx.Candidate(date="2026-09-22", status=dx.OK, source="higherin page",
                              url="https://higherin.com/jobs/44381/bae-systems/hardware-engineer-summer-internship",
                              method="structured field validThrough", employer_page=False,
                              quote="validThrough 2026-09-22", order="structured field")
    r = dx.resolve([listed.as_dict(), structured.as_dict()], set(), date(2026, 11, 30))
    assert r.status == dx.CONFLICT and r.confirmed is None             # neither is the employer's word
    assert r.deadline == date(2026, 11, 30)                           # nothing silently replaced
    assert r.provisional == date(2026, 9, 22)                         # the earlier date kept apart
    assert "2026-09-22: higherin page structured field validThrough" in r.reason
    assert "2026-11-30: higherin feed field" in r.reason
    assert len(r.applicable) == 2


# ---------------------------------------------------------------- hiring status vs expiry

def row(**kw):
    job = make_job(company="X", title="Software Intern", **kw)
    dedupe.assign_ids([job])
    return job


def test_a_passed_deadline_alone_is_not_closed():
    from tracker.models import OpenStatus

    job = row(deadline=date(2026, 9, 1))
    job.open_status, job.verification_note = OpenStatus.CLOSED, "Deadline has passed"
    assert role_filter.hiring_status(job)[0] == role_filter.UNKNOWN
    assert role_filter.deadline_marker(job, TODAY).startswith("EXPIRED")


def test_a_404_is_unavailable_not_closed():
    from tracker.models import OpenStatus

    job = row()
    job.open_status, job.verification_note = OpenStatus.CLOSED, "careers.amd.com returned 404"
    assert role_filter.hiring_status(job)[0] == role_filter.UNAVAILABLE
    assert role_filter.closed_by_employer(job) == ""


def test_an_earlier_unrecorded_open_reading_is_only_a_listing_claim():
    from tracker.models import OpenStatus

    job = row()
    job.open_status = OpenStatus.OPEN
    assert role_filter.hiring_status(job)[0] == role_filter.LISTING_OPEN


def test_employer_wording_decides_open_or_closed():
    job = row()
    job.hiring_status, job.closed_evidence = role_filter.EMPLOYER_CLOSED, 'employer page says "applications are closed"'
    assert role_filter.hiring_status(job)[0] == role_filter.EMPLOYER_CLOSED
    decision, reason = role_filter.decide(job, TODAY)
    assert decision == role_filter.SHOW and reason.startswith("CLOSED")   # kept at the bottom, marked CLOSED


def test_a_link_audit_is_merged_as_labelled_evidence_and_rechecked(tmp_path):
    import csv

    bae = row(deadline=date(2026, 11, 30))
    bae.sources[0].listed_deadline = date(2026, 11, 30)
    bae.sources[0].source = "higherin"
    maven = make_job(company="Maven Securities", title="Software Developer Summer Internship",
                     application_url="https://job-boards.greenhouse.io/maven/jobs/7806987")
    dedupe.assign_ids([maven])
    path = tmp_path / "role-link-audit.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["Tracker ID", "Application URL", "Link type", "Page result",
                                           "Open status checked", "Status evidence", "Check note",
                                           "Deadline evidence"])
        w.writeheader()
        w.writerow({"Tracker ID": bae.id, "Application URL": "https://higherin.com/jobs/44381/bae-systems/x",
                    "Link type": "aggregator/search/other", "Page result": "posting",
                    "Open status checked": "LISTING_OPEN", "Status evidence": "", "Check note": "",
                    "Deadline evidence": "Structured posting field validThrough: 2026-09-22"})
        w.writerow({"Tracker ID": maven.id, "Application URL": maven.application_url, "Link type": "employer posting",
                    "Page result": "posting", "Open status checked": "OPEN", "Status evidence": "apply now",
                    "Check note": "", "Deadline evidence": MAVEN})
    counts = role_filter.merge_audit([bae, maven], path, "2026-10-03")
    assert counts["rows"] == 2
    role_filter.assess_row(bae, relevance.Listings([]), TODAY)
    assert bae.deadline_status == dx.CONFLICT and bae.deadline == date(2026, 11, 30)
    assert "higherin.com listing page link audit: structured field validThrough" in bae.deadline_reason
    assert bae.hiring_status == role_filter.LISTING_OPEN and bae.checked_url.startswith("https://higherin.com")
    audit_dates = [c["date"] for c in maven.deadline_evidence if c["method"].startswith("link audit")]
    assert audit_dates == ["2026-10-21"]                              # re-read: the programme end date is gone
    assert maven.hiring_status == role_filter.EMPLOYER_OPEN and maven.last_checked.startswith("2026-10-03")


def test_an_audit_closure_without_the_employers_words_stays_for_review(tmp_path):
    import csv

    job = make_job(company="AWE", title="Engineering Year in Industry", location="Reading",
                   application_url="https://awepeople.wd3.myworkdayjobs.com/Grad_Careers/job/Reading/X_R30537")
    dedupe.assign_ids([job])
    path = tmp_path / "role-link-audit.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["Tracker ID", "Application URL", "Link type", "Page result",
                                           "Open status checked", "Status evidence", "Check note", "Deadline evidence"])
        w.writeheader()
        w.writerow({"Tracker ID": job.id, "Application URL": job.application_url, "Link type": "employer posting",
                    "Page result": "posting", "Open status checked": "CLOSED",
                    "Status evidence": "read via the Workday job feed", "Check note": "", "Deadline evidence": ""})
    role_filter.merge_audit([job], path, "2026-10-03")
    role_filter.assess_row(job, relevance.Listings([]), TODAY)
    assert job.hiring_status == role_filter.UNKNOWN and "without quoting" in job.hiring_evidence
    assert job.sheet_decision == role_filter.SHOW


# ================================================================ pre-apply review fixes (2026-10-03 14:09)
# data/checkpoint/pre-apply-review-20261003-1409/README.md, "Defects to fix before any apply".

# ---------------------------------------------------------------- 1. INT-001101 South Wales Police

POLICE_BOARD = ("Senior Data Analyst ID: 9860 Role Type: Police Staff Location: HQ, Bridgend Grade: 6SO1 "
                "Closing Date: 5 Oct 2026 15:00 BST South Wales Police Probationary Constable Campaign September "
                "2026 ID: 9868 Role Type: Police Officer Location: Forcewide Closing Date: 7 Oct 2026 17:00 BST "
                "Front Office Services Manager ID: 9905 Role Type: Police Staff Closing Date: 12 Oct 2026 12:00 BST")
POLICE_URL = "https://policejobswales.tal.net/vx/candidate/jobboard/vacancy/6/adv/"


def police_row():
    job = make_job(company="South Wales Police", title="Year-Long Internship", deadline=date(2026, 1, 25),
                   application_url=POLICE_URL)
    job.sources[0].listed_deadline, job.sources[0].source = date(2026, 1, 25), "trackr"
    dedupe.assign_ids([job])
    return job


def test_int_001101_a_label_belongs_to_one_date_not_the_next_vacancys_month():
    found = deadlines(POLICE_BOARD)
    assert [d for d, _ in found] == ["2026-10-05", "2026-10-07", "2026-10-12"]   # no "September 2026" note
    assert dx.MONTH_ONLY not in [s for _, s in found]


def test_dates_listed_under_one_label_still_share_it():
    assert deadlines("Applications close 30 October 2026 (London), 6 November 2026 (Leeds).") == [
        ("2026-10-30", dx.OK), ("2026-11-06", dx.OK)]


def test_int_001101_a_board_of_vacancies_never_sets_this_roles_deadline():
    job = police_row()
    job.deadline_evidence = [c.as_dict() for c in dx.tie_to_vacancy(
        dx.candidates(POLICE_BOARD, source="employer page", url=POLICE_URL, method="employer page",
                      employer_page=True), POLICE_BOARD, job.job_title)]
    role_filter.assess_row(job, relevance.Listings([]), TODAY)
    assert job.deadline == date(2026, 1, 25)                          # the existing value is kept
    assert job.deadline_status == dx.UNATTRIBUTED and job.deadline_confirmed is None
    assert "none can be tied to this role" in job.deadline_reason and "2026-10-05" in job.deadline_reason
    assert job.sheet_decision == role_filter.SHOW and "deadline UNATTRIBUTED" in job.sheet_reason
    assert role_filter.expiry(job, TODAY)[0] == "REVIEW"


def test_a_board_date_that_follows_this_roles_own_title_is_used():
    found = dx.tie_to_vacancy(dx.candidates(POLICE_BOARD, source="employer page", url=POLICE_URL,
                                            method="employer page", employer_page=True),
                              POLICE_BOARD, "Front Office Services Manager")
    r = dx.resolve([c.as_dict() for c in found], set(), None)
    assert (r.status, r.deadline) == (dx.CONFIRMED, date(2026, 10, 12))


def test_employer_pages_that_disagree_change_nothing():
    pages = [dx.Candidate(date=d, status=dx.OK, source="employer page", url=u, method="employer page",
                          employer_page=True, quote="Closing date").as_dict()
             for d, u in (("2026-10-05", "https://a.example/job/1"), ("2026-10-20", "https://b.example/job/1"))]
    r = dx.resolve(pages, set(), date(2026, 11, 1))
    assert (r.status, r.deadline, r.confirmed) == (dx.CONFLICT, date(2026, 11, 1), None)


def test_int_001101_closed_wording_on_a_board_is_not_this_roles():
    from tests.test_role_filter import FakeSession
    from tracker import config, verify
    from tracker.models import OpenStatus

    job = police_row()
    html = (f"<html lang='en-GB'><body><main><p>{POLICE_BOARD} " + "More about policing. " * 30 +
            "Applications are closed for this campaign.</p></main></body></html>")
    outcome = verify.verify_assessed(FakeSession({POLICE_URL: html}), job, config.load("verification"), set(),
                                     relevance.Listings([]), TODAY)
    assert outcome == "checked"
    assert job.hiring_status == role_filter.UNKNOWN and "not tied to this role" in job.hiring_evidence
    assert job.deadline == date(2026, 1, 25) and job.deadline_status == dx.UNATTRIBUTED
    assert job.open_status is not OpenStatus.CLOSED


# ---------------------------------------------------------------- 2. expiry is not closure

def test_an_old_closed_with_no_recorded_wording_is_unknown_not_closed():
    # 221 rows of the 26 September scan: CLOSED by the old checker because the
    # deadline had passed, with the reason lost on the next night's rebuild.
    from tracker.models import OpenStatus

    job = row(deadline=date(2026, 9, 1))
    job.open_status, job.verification_note = OpenStatus.CLOSED, ""
    role_filter.assess_row(job, relevance.Listings([]), TODAY)
    assert job.hiring_status == role_filter.UNKNOWN and "not treated as closed" in job.hiring_evidence
    assert job.open_status is OpenStatus.UNKNOWN                      # the Open Status column, too
    assert job.deadline_marker.startswith("EXPIRED") and job.sheet_decision == role_filter.SHOW


def test_only_the_employers_wording_makes_open_status_closed():
    from tracker.models import OpenStatus

    job = row(deadline=date(2026, 9, 1))
    job.hiring_status, job.hiring_evidence = role_filter.EMPLOYER_CLOSED, '"no longer accepting applications" on u'
    job.closed_evidence = 'employer page says "no longer accepting applications"'
    role_filter.assess_row(job, relevance.Listings([]), TODAY)
    assert job.open_status is OpenStatus.CLOSED


def test_a_listing_page_closure_is_not_open_status_closed():
    from tracker.models import OpenStatus

    job = row()
    job.open_status = OpenStatus.CLOSED
    job.hiring_status, job.hiring_evidence = role_filter.LISTING_CLOSED, "link audit 2026-10-03: closed (higherin)"
    role_filter.assess_row(job, relevance.Listings([]), TODAY)
    assert job.hiring_status == role_filter.LISTING_CLOSED and job.open_status is OpenStatus.UNKNOWN


def test_the_hiring_finding_survives_the_nightly_rebuild():
    from tracker import pipeline

    old = row()
    old.hiring_status, old.hiring_evidence = role_filter.EMPLOYER_OPEN, '"apply now" on https://x/job/1'
    old.checked_url = "https://x/job/1"
    new = make_job(company="X", title="Software Intern")
    new.id = old.id
    pipeline._restore_state([new], {old.id: old})
    assert (new.hiring_status, new.hiring_evidence, new.checked_url) == (
        old.hiring_status, old.hiring_evidence, old.checked_url)


# ---------------------------------------------------------------- 3. the checking queue

def test_the_queue_checks_live_rows_before_rows_past_a_labelled_deadline():
    from datetime import datetime, timedelta

    from tracker import config, verify

    real_today = date.today()
    settings = dict(config.load("verification"), budget={"max_verifications": 3, "revalidate_after_days": 3,
                                                          "always_verify": {"deadline_within_days": 14}})

    def queued(company, deadline=None, source="trackr"):
        job = make_job(company=company, title="Software Intern", deadline=deadline,
                       application_url=f"https://jobs.{company.casefold()}.com/job/1")
        job.sources[0].source, job.sources[0].listed_deadline = source, deadline
        return job

    gone = [queued(f"Gone{i}", real_today - timedelta(days=30 + i)) for i in range(3)]   # Trackr deadline field
    shaky = queued("Shaky", real_today - timedelta(days=3), source="higherin")         # validThrough only
    live = [queued("Live1", real_today + timedelta(days=60)), queued("Live2")]
    jobs = gone + [shaky] + live
    dedupe.assign_ids(jobs)
    role_filter.assess_all(jobs, real_today, relevance.Listings([]))
    assert [role_filter.expiry(j, real_today)[0] for j in jobs] == ["EXPIRED"] * 3 + ["REVIEW", "", ""]
    chosen, later = verify.select_assessed(jobs, settings, real_today)
    assert [j.company for j in chosen] == ["Live1", "Live2", "Shaky"]  # nothing spent on the expired three
    assert {j.company for j in later} == {"Gone0", "Gone1", "Gone2"}
    for j in chosen:
        j.last_checked = datetime.now().isoformat()
    again, _ = verify.select_assessed(jobs, settings, real_today)
    assert {j.company for j in again} == {"Gone0", "Gone1", "Gone2"}  # their turn comes once the live rows are done


# ---------------------------------------------------------------- 4. clauses and context

PAD = "About us: we are a large employer with offices across the UK. " * 8


def role(text, title="Undergraduate Placement 2027"):
    return relevance.role_relevance(relevance._clean(PAD + text), title)


def test_int_001838_commercial_tools_and_a_portfolio_of_projects_are_not_business_work():
    # MBDA Propulsion Engineer: excluded on (commercial, portfolio, propulsion).
    value, reason, _ = role("The opportunity: You'll be involved in understanding areas such as: Application and "
                            "development of bespoke and commercial modelling tools to evaluate propulsion "
                            "performance. Working with leading UK and international propulsion suppliers, you'll "
                            "contribute to a wide portfolio of projects that underpin the UK's defence capability. ")
    assert value != relevance.NOT_TECHNICAL and "commercial" in reason and "not counted" in reason


def test_int_001984_a_skills_sentence_is_not_a_duty():
    value, reason, _ = role("Key responsibilities: This placement will give you the opportunity to develop your "
                            "analytical, commercial, communication and presentation skills. Track key business "
                            "metrics including sales funnel, revenue and margin. Support strategic planning. ")
    assert value != relevance.NOT_TECHNICAL


def test_int_001934_teams_the_role_works_with_and_degree_subjects_are_not_duties():
    value, reason, _ = role("Role Overview: Are you studying towards a Bachelor's degree in Food Science, "
                            "Engineering, or other Scientific discipline (e.g., Chemistry, Biology, Physics)? "
                            "Learn the product development lifecycle. Works with other cross functional teams in "
                            "the business e.g., Marketing, Consumer Science & Insights, Procurement & Manufacturing. ")
    assert value != relevance.NOT_TECHNICAL
    assert "names who the role works with" in reason


def test_int_001935_a_personal_brand_and_social_events_are_not_duties():
    value, reason, _ = role("The role: Has a clear understanding of their personal brand - office attire and "
                            "etiquette. You'll attend courses, lectures, networking and social events. You will be "
                            "asked for evidence of your right to work during the recruitment process. ")
    assert value != relevance.NOT_TECHNICAL


def test_commercial_brand_or_portfolio_never_decides_on_its_own():
    # Two genuine kinds of work plus "commercial": not enough to exclude.
    assert role("Responsibilities: Draft contracts and support litigation teams with our clients and commercial "
                "matters. ")[0] == relevance.UNCLEAR


def test_genuine_business_duties_still_exclude():
    value, _, _ = role("Responsibilities: Prepare marketing campaigns, manage client relationships, support audit "
                       "and tax work, and analyse pricing for deals. ")
    assert value == relevance.NOT_TECHNICAL


ENVISICS = ("Key Responsibilities Supporting testing and validation of the display system Designing experiments "
            "and necessary rigs/fixtures for component and display system calibration and validation. Developing "
            "mechanical parts, assemblies and systems from concept to sample production Conceptualising, "
            "developing and detailing ideas for future mechanical parts Minimum Requirements Studying engineering "
            "at undergraduate level with excellent academic results.")


def test_int_001403_duties_run_into_requirements_are_split_back_apart():
    pieces = [ENVISICS[a:b].strip() for a, b in relevance.clauses(ENVISICS)]
    assert any(p.startswith("Conceptualising") for p in pieces)
    assert any(p.startswith("Studying engineering") for p in pieces)
    value, reason, quotes = relevance.degree_subject(ENVISICS)
    assert value == relevance.BROAD_FIELD and "mechanical" not in quotes[0].casefold()


def test_a_subject_must_be_what_the_study_clause_names():
    assert relevance.degree_subject("You must have a degree. You will design mechanical parts.")[0] \
        != relevance.OTHER_SUBJECT_ONLY
    assert relevance.degree_subject("You must be studying Mechanical Engineering.")[0] == relevance.OTHER_SUBJECT_ONLY


def test_a_degree_list_is_read_whole_through_its_commas():
    # "Electrical and Electronic Engineering, ... Computer Science" was cut at
    # "Engineering," and lost its Computer Science (MBDA INT-001750).
    text = ("What we're looking for from you: Current undergraduate working towards a degree in Electrical and "
            "Electronic Engineering, Electronics with Communications, Aerospace Engineering, Physics, Mathematics "
            "or Computer Science.")
    assert relevance.degree_subject(text)[0] == relevance.CS_ACCEPTED


def test_a_subject_list_under_a_requirements_heading_does_not_exclude_computer_science():
    # Airbus INT-001168 / -001889 / -001903 / -001960: a positive list, no
    # "only" or "must" - Computer Science is not excluded in so many words.
    listed = (" Successful candidates will be: Currently pursuing an undergraduate degree in Mechanical or "
              "Aerospace Engineering; Good attitude with others and a team player;")
    for heading in ("Requirements:", "What we're looking for from you:"):
        value = relevance.degree_subject(heading + listed)[0]
        assert value == relevance.EQUIVALENCE_UNCLEAR and value not in relevance.STRONG_AGAINST
    # The clause's own words still exclude.
    assert relevance.degree_subject("Requirements: This placement is only open to candidates studying "
                                    "Mechanical or Aerospace Engineering.")[0] == relevance.OTHER_SUBJECT_ONLY
    # And a heading still records Computer Science as required - a positive finding.
    assert relevance.degree_subject("Requirements: Currently studying for a degree in Computer Science.")[0] \
        == relevance.CS_REQUIRED


# ================================================================ post-fix review (2026-10-03 16:18)

def test_a_title_tie_break_is_review_never_an_exclusion():
    # Airbus INT-001757: two kinds of other work plus "Mechanical" in the title.
    value, reason, _ = role("The role: The philosophy of the Fuel Systems Domain is to have the best systems, people "
                            "and partnerships. You will be part of the Fluid Mechanical department. ",
                            title="Fuel Systems Fluid Mechanical Engineering Placement")
    assert value == relevance.UNCLEAR and "not clear enough from the posting" in reason


def reviewed(monkeypatch, ids):
    """config/relevance.yaml with `review_not_exclude` set to `ids`."""
    listed = dict(role_filter.config.load("relevance"), review_not_exclude=ids)
    original = role_filter.config.load
    monkeypatch.setattr(role_filter.config, "load", lambda name: listed if name == "relevance" else original(name))


def test_an_exclusion_read_as_unclear_in_context_stays_visible_for_review(monkeypatch):
    job, other = row(), row()
    other.id = "INT-999999"
    for j in (job, other):
        j.role_relevance, j.role_relevance_reason = relevance.NOT_TECHNICAL, "duties are clients, property work"
    assert role_filter.decide(job, TODAY)[0] == role_filter.HIDE
    reviewed(monkeypatch, {job.id: "a sector list"})
    decision, reason = role_filter.decide(job, TODAY)
    assert decision == role_filter.SHOW and reason.startswith("REVIEW: exclusion not clearly supported")
    assert "a sector list" in reason and "NOT_TECHNICAL" in reason   # the finding is kept as evidence
    assert role_filter.decide(other, TODAY)[0] == role_filter.HIDE    # only the listed row


def test_the_review_list_never_hides_a_row(monkeypatch):
    job = row()
    job.role_relevance = relevance.TECHNICAL
    before = role_filter.decide(job, TODAY)
    reviewed(monkeypatch, {job.id: "x"})
    assert role_filter.decide(job, TODAY) == before


def test_a_closed_role_with_a_strong_finding_is_still_kept_at_the_bottom():
    job = row()
    job.role_relevance, job.role_relevance_reason = relevance.NOT_TECHNICAL, "duties are sales work"
    job.hiring_status, job.hiring_evidence = role_filter.EMPLOYER_CLOSED, '"this job has expired" on u'
    decision, reason = role_filter.decide(job, TODAY)
    assert decision == role_filter.SHOW and reason.startswith("CLOSED") and "NOT_TECHNICAL" in reason
