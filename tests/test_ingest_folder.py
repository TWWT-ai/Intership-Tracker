"""Importing saved Gradcracker pages and Bright Network deadline lists. No network."""

from datetime import date

from sources import manual
from tests.conftest import make_job

YOU = {"graduation_year": 2029, "degree": "Computer Science (4-year, with industrial placement year)",
       "current_year": 2}

BN_LIST = """<html><body><div>
<p>We've rounded up the essential application deadlines. See all
<a href="https://www.brightnetwork.co.uk/graduate-jobs/technology/">tech grad jobs</a>.</p>
<div><h3>30th Sep 2026</h3><ul>
<li><div><div><span>Volkswagen Financial Services</span></div>
<a href="https://www.brightnetwork.co.uk/graduate-jobs/volkswagen-financial-services/project-management-placement-2027">Project Management Placement 2027</a></div></li>
</ul><h3>11th Oct 2026</h3><ul>
<li><div><div><span>Bank of America</span></div>
<a href="https://www.brightnetwork.co.uk/graduate-jobs/bank-of-america/cyber-security-industrial-placement">Cyber Security Industrial Placement</a></div></li>
<li><div><div><span>Acme</span></div>
<a href="https://www.brightnetwork.co.uk/graduate-jobs/acme/webinar-week-application-success">Webinar Week: Application Success</a></div></li>
</ul></div></div></body></html>"""


def save(folder, name, text):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestBrightNetworkDeadlineList:
    def test_reads_each_programme_with_its_deadline_as_a_lead(self, tmp_path):
        jobs = manual.from_html(save(tmp_path, "deadlines.html", BN_LIST), "brightnetwork")
        by_title = {j.job_title: j for j in jobs}
        vw = by_title["Project Management Placement 2027"]
        assert vw.company == "Volkswagen Financial Services" and vw.deadline == date(2026, 9, 30)
        assert by_title["Cyber Security Industrial Placement"].deadline == date(2026, 10, 11)
        assert vw.source_url.endswith("/project-management-placement-2027")
        assert vw.sources[0].application_url == ""          # a lead, not the employer's apply link

    def test_the_intro_link_to_all_tech_jobs_is_not_a_programme(self, tmp_path):
        jobs = manual.from_html(save(tmp_path, "deadlines.html", BN_LIST), "brightnetwork")
        assert all(j.company != "technology" for j in jobs)


class TestFolder:
    def test_reads_every_page_once_and_skips_browser_files_and_webinars(self, tmp_path):
        save(tmp_path, "Internships Deadlines.html", BN_LIST)
        save(tmp_path, "Internships Deadlines copy.html", BN_LIST)
        save(tmp_path, "Internships Deadlines_files/saved_resource.html", BN_LIST)
        jobs = manual.from_folder(tmp_path, "brightnetwork")
        assert sorted(j.job_title for j in jobs) == ["Cyber Security Industrial Placement",
                                                     "Project Management Placement 2027"]


class TestPrepare:
    def test_a_card_is_classified_by_its_own_duration_and_start(self):
        job = make_job(title="Global Technology Software Engineer Summer 2027 Analyst", work_type="Internship",
                       description="", source="gradcracker")
        job.duration, job.start_date, job.degree_requirements = "3 months", date(2027, 6, 1), "2:1 and above"
        manual.prepare([job], YOU)
        assert job.work_type == "Summer Internship"
        assert job.classification_basis.startswith("duration")
        assert job.programme_dates == "June 2027"
        assert job.eligible_for_you == "REQUIRES VERIFICATION"

    def test_a_twelve_month_card_is_a_placement(self):
        job = make_job(title="2027 Placement Programme - Technology - Software Engineering", work_type="",
                       description="", source="gradcracker")
        job.duration = "12 months"
        manual.prepare([job], YOU)
        assert job.work_type == "Industrial Placement"
