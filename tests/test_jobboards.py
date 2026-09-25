"""Higherin and TargetJobs sitemap sweeps, against saved pages. No network."""

from datetime import date
from pathlib import Path

from tests.conftest import make_job
from tests.test_careers import FakeResponse, RoutedSession
from sources import higherin, targetjobs
from tracker import config, dedupe

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date.today().isoformat()
BOARDS = config.load("sources")["sources"]

BOA = ("https://higherin.com/jobs/43894/bank-of-america/"
       "global-technology-software-engineer-6-month-industrial-placement-2027-analyst-dublin")
BLACKROCK = "https://targetjobs.co.uk/jobs/2027-spring-insight-event-emea-235487"


def page(name):
    return FakeResponse((FIXTURES / name).read_text(encoding="utf-8"))


class TestHigherin:
    def test_only_recent_student_postings_are_opened_and_non_uk_ones_dropped(self):
        index = ("<sitemapindex><sitemap><loc>https://higherin.com/sitemaps/job-sitemap-1.xml</loc>"
                 f"<lastmod>{TODAY}</lastmod></sitemap>"
                 "<sitemap><loc>https://higherin.com/sitemaps/reviews-sitemap-index.xml</loc></sitemap></sitemapindex>")
        urlset = (f"<urlset><url><loc>{BOA}</loc><lastmod>{TODAY}</lastmod></url>"
                  "<url><loc>https://higherin.com/jobs/1/acme/accountancy-graduate-programme-2027</loc>"
                  f"<lastmod>{TODAY}</lastmod></url>"
                  "<url><loc>https://higherin.com/jobs/2/acme/software-summer-internship-2024</loc>"
                  "<lastmod>2024-01-01</lastmod></url></urlset>")
        session = RoutedSession({
            "https://higherin.com/sitemaps/sitemap-index.xml": FakeResponse(index),
            "https://higherin.com/sitemaps/job-sitemap-1.xml": FakeResponse(urlset),
            BOA: page("higherin_job.html"),
        })
        jobs = higherin.search_jobs(session, BOARDS["higherin"])

        opened = [u for _, u in session.calls if "/jobs/" in u]
        assert opened == [BOA]              # the graduate programme and the 2024 posting never opened
        assert jobs == []                   # Dublin is not in the UK


class TestTargetJobs:
    def _jobs(self):
        urlset = f"<urlset><url><loc>{BLACKROCK}</loc><lastmod>{TODAY}T07:32:08.000Z</lastmod></url></urlset>"
        session = RoutedSession({"https://targetjobs.co.uk/sitemap-0.xml": FakeResponse(urlset),
                                 BLACKROCK: page("targetjobs_job.html")})
        return targetjobs.search_jobs(session, BOARDS["targetjobs"])

    def test_a_uk_spring_insight_is_kept_as_a_lead(self):
        jobs = self._jobs()
        assert [(j.company, j.job_title, j.primary_source) for j in jobs] == [
            ("BlackRock", "2027 Spring Insight Event - EMEA", "targetjobs")]
        job = jobs[0]
        assert job.sources[0].application_url == ""          # never claims to be the apply link
        assert job.expected_graduation == "2029"

    def test_merges_with_the_official_row_and_keeps_its_link(self):
        official = make_job(company="BlackRock", title="2027 Spring Insight Event", source="trackr",
                            location="United Kingdom", work_type="Spring Week", external_id="trk-bk",
                            application_url="https://blackrock.tal.net/vx/candidate/so/pm/1/pl/1/opp/12223-x/en-GB")
        merged, count = dedupe.deduplicate([official] + self._jobs())
        assert count == 1
        assert merged[0].application_url == official.application_url
        assert merged[0].source_names() == ["targetjobs", "trackr"]


class TestSitemapAndRelevance:
    def test_a_stale_date_on_a_sitemap_index_entry_does_not_hide_its_postings(self):
        from sources import jobboards
        index = ("<sitemapindex><sitemap><loc>https://higherin.com/sitemaps/job-sitemap-1.xml</loc>"
                 "<lastmod>2020-01-01</lastmod></sitemap></sitemapindex>")
        urlset = f"<urlset><url><loc>{BOA}</loc><lastmod>{TODAY}</lastmod></url></urlset>"
        session = RoutedSession({"https://higherin.com/sitemaps/sitemap-index.xml": FakeResponse(index),
                                 "https://higherin.com/sitemaps/job-sitemap-1.xml": FakeResponse(urlset)})
        assert jobboards.sitemap_urls(session, "https://higherin.com/sitemaps/sitemap-index.xml", "2026-01-01") == [BOA]

    def _posting(self, title, text):
        return {"title": title, "company": "Acme", "employment": "INTERN", "location": "London",
                "description": text, "deadline": None, "id": "1", "url": "https://x"}

    def test_a_generic_mention_of_data_is_not_computing(self):
        from sources import careers, jobboards
        wanted = careers._Filter(BOARDS["careers"])
        assert not jobboards.keep(self._posting("Channel Operations Internship",
                                                "Use data and technology to grow our retail channels."), wanted)

    def test_a_named_degree_or_any_degree_counts(self):
        from sources import careers, jobboards
        wanted = careers._Filter(BOARDS["careers"])
        assert jobboards.keep(self._posting("Operations Internship", "Open to students of any degree."), wanted)
        assert jobboards.keep(self._posting("Analyst Internship", "Studying Computer Science or similar."), wanted)

    def test_a_marketer_role_is_excluded(self):
        from sources import careers
        assert careers._Filter(BOARDS["careers"]).excluded("Local Marketer - Internship")

    def test_child_sitemaps_listed_in_url_tags_are_followed(self):
        """Higherin's index uses <url> entries for its child sitemaps, with stale dates."""
        from sources import jobboards
        index = ("<urlset><url><loc>https://higherin.com/sitemaps/job-sitemap-index.xml</loc>"
                 "<lastmod>2020-01-01</lastmod></url>"
                 "<url><loc>https://higherin.com/sitemaps/reviews-sitemap-index.xml</loc></url></urlset>")
        child = "<urlset><url><loc>https://higherin.com/sitemaps/job-sitemap-1.xml</loc></url></urlset>"
        urlset = f"<urlset><url><loc>{BOA}</loc><lastmod>{TODAY}</lastmod></url></urlset>"
        session = RoutedSession({"https://higherin.com/sitemaps/sitemap-index.xml": FakeResponse(index),
                                 "https://higherin.com/sitemaps/job-sitemap-index.xml": FakeResponse(child),
                                 "https://higherin.com/sitemaps/job-sitemap-1.xml": FakeResponse(urlset)})
        assert jobboards.sitemap_urls(session, "https://higherin.com/sitemaps/sitemap-index.xml", "2026-01-01") == [BOA]
