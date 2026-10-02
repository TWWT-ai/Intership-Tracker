"""Role, study-level and degree findings, read from the posting's own text and
only from text that belongs to the Tracker ID's own listing. Each finding is
separate; none is merged into a score. Offline throughout."""
from datetime import date
from pathlib import Path

import pytest

from sources.base import build_job
from tracker import relevance as rv
from tracker import role_filter
from tracker.models import SourceRef

FIXTURES = Path(__file__).parent / "fixtures"
LLOYDS = (FIXTURES / "lloyds_product_partnerships.txt").read_text(encoding="utf-8")
HIGHERIN_LONDON = "https://higherin.com/jobs/44419/lloyds-banking-group/product-and-partnerships-industrial-placement-scheme-london"
WORKDAY_LEEDS = ("https://lbg.wd3.myworkdayjobs.com/Undergraduate_careers/job/Leeds-Wellington-Place/"
                 "Product---Partnerships-Industrial-Placement---Leeds-_163483-1")

SWE_DUTIES = ("What you'll do: Build and test web applications in Python and Java. Write code, take part in "
              "code reviews and work on APIs, databases and cloud infrastructure with the engineering team. "
              "You will debug production issues and automate deployments. ")
PADDING = ("About us: We are a friendly company with offices across the UK and a long history of supporting "
           "our communities, and we care about wellbeing, learning and doing the right thing. ")
TODAY = date(2026, 9, 30)


def job(title="Summer Internship 2027", text="", source="careers", url="https://example.com/jobs/1",
        company="Example Ltd", location="London", more=()):
    j = build_job(source=source, company=company, title=title, source_url=url, description=text, location=location)
    for s, u in more:
        j.sources.append(SourceRef(source=s, source_url=u))
    return j


def listing(source, url, title, text, company="Example Ltd", location="United Kingdom"):
    return rv.Listing(source, url, company, title, location, text)


def assess(j, listings=()):
    role_filter.assess_row(j, rv.Listings(list(listings)), TODAY)
    return j


def posting(duties=SWE_DUTIES, requirements="Requirements: Currently studying for a degree in Computer Science. "):
    return PADDING + duties + requirements + PADDING


# ---------------------------------------------------------------- Lloyds INT-001157

def lloyds_row():
    return job(title="Product and Partnerships Industrial Placement Scheme - London", text=LLOYDS,
               source="higherin", url=HIGHERIN_LONDON, company="Lloyds Banking Group",
               location="United Kingdom", more=[("careers", WORKDAY_LEEDS)])


def test_lloyds_product_and_partnerships_is_not_technical_from_its_own_london_text():
    j = assess(lloyds_row(), [
        listing("higherin", HIGHERIN_LONDON, "Product and Partnerships Industrial Placement Scheme - London", LLOYDS,
                company="Lloyds Banking Group"),
        listing("careers", WORKDAY_LEEDS, "Product & Partnerships Industrial Placement (Leeds)", "other text",
                company="Lloyds Banking Group", location="Leeds Wellington Place")])
    assert (j.role_relevance, j.study_level, j.degree_subject_fit) == (
        rv.NOT_TECHNICAL, rv.CURRENT_UNDERGRADUATE, rv.NOT_STATED)
    assert "propositions" in j.role_relevance_evidence and "penultimate year" in j.study_level_evidence
    assert j.assessment_text_source.startswith("higherin listing (London)")
    assert "listing for Leeds - another location, not used" in j.assessment_text_source
    assert j.sheet_decision == role_filter.HIDE and "NOT_TECHNICAL" in j.sheet_reason


def test_a_programming_requirement_alone_does_not_make_it_technical():
    assert "programming language" in LLOYDS
    assert rv.role_relevance(rv._clean(LLOYDS), "Product and Partnerships Industrial Placement")[0] == rv.NOT_TECHNICAL


def test_text_from_another_citys_posting_is_not_assessed_unless_shown_equivalent():
    no_london = LLOYDS.replace("Watford (London)", "Watford").replace("London location", "Watford location")
    row = job(title="Product and Partnerships Industrial Placement Scheme - London", text=no_london,
              source="higherin", url=HIGHERIN_LONDON, company="Lloyds Banking Group",
              location="United Kingdom", more=[("careers", WORKDAY_LEEDS)])
    row.description_source = ""                              # a row saved before sources were recorded
    leeds = listing("careers", WORKDAY_LEEDS, "Product & Partnerships Industrial Placement (Leeds)", no_london,
                    company="Lloyds Banking Group", location="Leeds Wellington Place")
    london = listing("higherin", HIGHERIN_LONDON, "Product and Partnerships Industrial Placement Scheme - London",
                     "a different London text", company="Lloyds Banking Group")
    assess(row, [leeds, london])
    assert row.role_relevance == rv.UNCLEAR and row.role_relevance_reason.startswith("not assessed")
    assert row.sheet_decision == role_filter.SHOW                  # unassessed stays visible

    named = lloyds_row()
    leeds.description = LLOYDS
    assess(named, [leeds, london])
    assert named.assessment_text_source.startswith("careers listing for Leeds, same programme")
    assert named.role_relevance == rv.NOT_TECHNICAL


# ---------------------------------------------------------------- findings stay separate

def test_each_finding_is_recorded_separately_with_its_evidence():
    j = assess(job(text=posting()))
    assert (j.role_relevance, j.study_level, j.degree_subject_fit) == (
        rv.TECHNICAL, rv.CURRENT_UNDERGRADUATE, rv.CS_REQUIRED)
    assert j.role_relevance_evidence and j.study_level_evidence and j.degree_subject_evidence
    assert j.sheet_decision == role_filter.SHOW and j.sheet_reason == "all findings positive"


def test_no_degree_subject_is_not_stated_and_stays_for_review():
    j = assess(job(text=posting(requirements="Requirements: Currently studying at university. ")))
    assert j.degree_subject_fit == rv.NOT_STATED
    assert j.sheet_decision == role_filter.SHOW and "degree NOT_STATED" in j.sheet_reason


@pytest.mark.parametrize("sentence, value", [
    ("You must be studying Computer Science.", rv.CS_REQUIRED),
    ("You will be studying Computer Science, ideally.", rv.CS_ACCEPTED),
    ("Studying Maths & Computer Science or Physics.", rv.CS_ACCEPTED),
    ("Studying a degree in any discipline.", rv.ANY_DISCIPLINE),
    ("You must be studying Artificial Intelligence.", rv.EQUIVALENCE_UNCLEAR),
    ("Studying Data Science or a related subject.", rv.EQUIVALENCE_UNCLEAR),
    ("Open to STEM degree students.", rv.BROAD_FIELD),
    ("You must be studying an engineering degree.", rv.BROAD_FIELD),
    ("You must be studying Mechanical or Aerospace Engineering.", rv.OTHER_SUBJECT_ONLY),
    ("A degree in Mechanical Engineering is preferred.", rv.EQUIVALENCE_UNCLEAR),
    ("Studying Maths, Physics or a related subject.", rv.EQUIVALENCE_UNCLEAR),
    ("A great team to learn from.", rv.NOT_STATED),
])
def test_degree_subject_wording(sentence, value):
    assert rv.degree_subject(sentence)[0] == value


def test_computer_science_preferred_is_never_a_rejection():
    assert rv.degree_subject("Computer Science degree preferred.")[0] == rv.CS_ACCEPTED
    assert "preferred" in rv.degree_subject("Computer Science degree preferred.")[1]


# ---------------------------------------------------------------- study level

@pytest.mark.parametrize("sentence", [
    "Currently pursuing a BSc in a numerate subject.",
    "You are working towards a degree at a UK university.",
    "Candidates currently undertaking a degree are welcome.",
    "You are a candidate for a BSc in 2028.",
    "You are on track to achieve a 2:1 in your degree.",
    "You are enrolled in a bachelor's programme.",
    "If you have already graduated with a bachelor's degree and are currently studying for a Masters, apply.",
    "Pursuing Bachelors or Master's in Mathematics or Computing.",
    "Open to students graduating in 2028.",
])
def test_current_students_are_recognised(sentence):
    assert rv.study_level("Summer Internship", PADDING + sentence, TODAY)[0] == rv.CURRENT_UNDERGRADUATE


@pytest.mark.parametrize("sentence, value", [
    ("You must have completed a degree in Computer Science.", rv.GRADUATE_ONLY),
    ("This internship is for PhD students in machine learning.", rv.POSTGRADUATE_ONLY),
    ("Requirements: 3+ years of professional software experience.", rv.EXPERIENCED),
    ("Currently studying for an MEng in Computing.", rv.LEVEL_UNCLEAR),
    ("Open to MSci students.", rv.LEVEL_UNCLEAR),
    ("A great team to learn from.", rv.NOT_STATED),
])
def test_study_level_wording(sentence, value):
    assert rv.study_level("Summer Internship", PADDING + sentence, TODAY)[0] == value


def test_a_placement_title_alone_does_not_settle_study_level():
    assert rv.study_level("Software Industrial Placement", PADDING, TODAY)[0] == rv.LEVEL_UNCLEAR


# ---------------------------------------------------------------- relevance

def test_sales_duties_with_a_python_requirement_are_not_technical():
    duties = ("Responsibilities: Support the sales team with client relationships and new business development, "
              "prepare marketing campaigns and pricing proposals for commercial clients, and manage partnerships. ")
    j = assess(job(text=posting(duties, "Requirements: Currently studying any degree. Knowledge of Python. ")))
    assert j.role_relevance == rv.NOT_TECHNICAL and j.sheet_decision == role_filter.HIDE


def test_mixed_duties_are_unclear_and_stay():
    duties = ("Responsibilities: Build dashboards in SQL and Python for the trading desk, support commercial "
              "pricing decisions and client relationships. ")
    j = assess(job(text=posting(duties)))
    assert j.role_relevance == rv.UNCLEAR and j.sheet_decision == role_filter.SHOW


def test_a_digital_company_introduction_does_not_make_legal_work_technical():
    text = ("About us: We are a digital, technology-driven firm building cloud software for banks. " + PADDING
            + "Responsibilities: Draft contracts, research areas of law for lawyers and solicitors, and support "
              "litigation teams with our clients and commercial matters. Requirements: Current undergraduates. ")
    assert assess(job(title="Vacation Placement", text=text)).role_relevance == rv.NOT_TECHNICAL


def test_a_technology_title_is_never_hidden_on_vague_duties():
    duties = ("Purpose of the role: Partner with other functions on operations, controls and client "
              "relationships, supporting strategy and commercial work across the business. ")
    j = assess(job(title="2027 Technology Analyst Summer Internship", text=posting(duties)))
    assert j.role_relevance == rv.UNCLEAR and j.sheet_decision == role_filter.SHOW


# ---------------------------------------------------------------- vague titles, missing text

def test_a_vague_title_is_decided_by_its_duties():
    assert assess(job(title="Summer Internship 2027", text=posting())).role_relevance == rv.TECHNICAL
    empty = assess(job(title="Summer Internship 2027", text=""))
    assert empty.role_relevance == rv.UNCLEAR and empty.sheet_decision == role_filter.SHOW


@pytest.mark.parametrize("text", ["", "Short summary of a company."])
def test_missing_or_short_text_stays_visible_for_review(text):
    j = assess(job(title="Finance Summer Internship", text=text))
    assert j.sheet_decision == role_filter.SHOW and j.sheet_reason.startswith("REVIEW")


def test_a_posting_without_duties_headings_is_unclear():
    text = PADDING * 5 + "We build software and write code in Python and Java for our clients."
    assert assess(job(text=text)).role_relevance == rv.UNCLEAR


def test_a_merged_description_keeps_the_listing_it_came_from():
    from tracker import dedupe
    empty = job(text="", source="trackr", url="https://trackr.example/1")
    full = job(text=posting(), source="careers", url="https://example.com/jobs/1")
    merged = dedupe.merge(empty, full, ["trackr", "careers"])
    assert merged.description_source == "https://example.com/jobs/1"
