"""Date Posted: when applications opened, read from each feed, never inferred.

The column sits before Deadline so a row shows how long it has been open. A
source that states no posting date leaves it blank - the day this tool first
saw the row is Date Found, a different fact.
"""

from datetime import date
from pathlib import Path

import pytest

from sources import brightnetwork, careers, trackr
from tests.conftest import make_job
from tests.test_jobboards import BLACKROCK, BOARDS, FakeResponse, RoutedSession, page
from tests.test_sources import FakeSession
from sources import targetjobs
from tracker import dedupe, excel

FIXTURES = Path(__file__).parent / "fixtures"


def test_trackr_opening_date_is_the_posting_date():
    session = FakeSession((FIXTURES / "trackr_programmes.json").read_text(encoding="utf-8"))
    jobs = trackr.search_jobs(session, {
        "api_base": "https://api.the-trackr.com", "board_base": "https://app.the-trackr.com",
        "regions": ["UK"], "industries": ["Tech"], "seasons": ["2027"], "types": ["summer-internships"]})
    posted = {j.company: j.date_posted for j in jobs}
    assert posted["J.P. Morgan"] == date(2026, 8, 31)
    assert posted["Castleton Commodities International"] is None     # Trackr states none: blank


def test_job_boards_read_the_json_ld_date_posted():
    urlset = f"<urlset><url><loc>{BLACKROCK}</loc><lastmod>{date.today().isoformat()}</lastmod></url></urlset>"
    session = RoutedSession({"https://targetjobs.co.uk/sitemap-0.xml": FakeResponse(urlset),
                             BLACKROCK: page("targetjobs_job.html")})
    [job] = targetjobs.search_jobs(session, BOARDS["targetjobs"])
    assert job.date_posted == date(2026, 8, 7)


def test_bright_network_date_posted_is_a_field_not_only_prose():
    html = (FIXTURES / "brightnetwork_job.html").read_text(encoding="utf-8")
    job = brightnetwork._parse_job_page(html, "https://www.brightnetwork.co.uk/graduate-jobs/x/y")
    assert job.date_posted == date(2026, 9, 1)


@pytest.mark.parametrize("raw,expected", [
    ("2026-09-01T09:00:00Z", date(2026, 9, 1)),       # ISO (Greenhouse, Ashby, Workday)
    (1788220800000, date(2026, 9, 1)),                 # epoch milliseconds (Lever)
    (1788220800, date(2026, 9, 1)),                    # epoch seconds (Eightfold)
    ("September 1, 2026", date(2026, 9, 1)),           # written out (Amazon)
    ("Posted 30+ Days Ago", None),                     # Workday's relative text is not a date
    (None, None), ("", None),
])
def test_a_feed_date_is_parsed_or_left_blank(raw, expected):
    assert careers._posted(raw) == expected


def test_greenhouse_uses_first_published_not_the_last_edit():
    board = careers.board_from_url("https://boards.greenhouse.io/acme", "Acme")
    session = RoutedSession({"https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": {"jobs": [
        {"id": 7001001, "title": "Software Engineering Intern", "absolute_url": "https://x/7001001",
         "location": {"name": "London"}, "content": "",
         "updated_at": "2026-09-20T10:00:00Z", "first_published": "2026-08-15T10:00:00Z"},
        {"id": 7001002, "title": "Data Intern", "absolute_url": "https://x/7001002",
         "location": {"name": "London"}, "content": "", "updated_at": "2026-09-20T10:00:00Z"}]}})
    postings = careers._greenhouse(session, board, {}, None)
    assert [p["posted"] for p in postings] == [date(2026, 8, 15), None]


def test_merged_row_keeps_the_earliest_stated_posting_date():
    later = make_job(source="higherin")
    later.date_posted = date(2026, 9, 10)
    earlier = make_job(source="trackr")
    earlier.date_posted = date(2026, 8, 31)
    silent = make_job(source="gradcracker")
    merged, _ = dedupe.deduplicate([later, silent, earlier])
    assert len(merged) == 1 and merged[0].date_posted == date(2026, 8, 31)


def test_date_posted_sits_just_before_deadline():
    labels = [label for label, _ in excel.COLUMNS]
    assert labels.index("Date Posted") == labels.index("Deadline") - 1
