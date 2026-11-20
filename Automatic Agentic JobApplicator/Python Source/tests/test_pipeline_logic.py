"""
Offline tests for the email pipeline pieces: JD experience parsing, LinkedIn link extraction from
alert mails, and the JSON job tracker.  Run:  python -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.email_reader import extract_linkedin_job_urls, job_id_from_url  # noqa: E402
from src.jd_screener import required_years  # noqa: E402
from src.job_tracker import JobTracker  # noqa: E402


class ExperienceTests(unittest.TestCase):
    def check(self, text, years, open_ended, more_than_12):
        r = required_years(text)
        self.assertEqual((r.years, r.open_ended, r.more_than(12)), (years, open_ended, more_than_12), f"{text!r} -> {r}")

    def test_phrasings(self):
        self.check("We need 15+ years of experience in banking analytics.", 15, True, True)
        self.check("Minimum 12 years of relevant experience.", 12, False, False)
        self.check("12+ years experience leading data teams", 12, True, True)
        self.check("More than 12 years of experience in AI/ML", 12, True, True)
        self.check("8-10 years of experience in Python", 10, False, False)
        self.check("10 to 15 years of work experience", 15, False, True)
        self.check("At least fifteen years of experience managing teams", 15, False, True)
        self.check("Bachelor's degree with 5 years experience; 18 years of leadership experience preferred", 18, False, True)
        self.check("13 yrs exp in risk", 13, False, True)

    def test_unrelated_numbers_and_unstated(self):
        r = required_years("Join our 5 year transformation plan. Hybrid, 3 days in office.")
        self.assertIsNone(r.years)
        self.assertIsNone(r.more_than(12))


class MailLinkTests(unittest.TestCase):
    def test_extract_and_canonicalise(self):
        html = """
        <a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc&amp;refId=x">Job A</a>
        <a href="https://www.linkedin.com/jobs/view/senior-director-data-at-hsbc-4012345679?trk=eml">Job B</a>
        <a href="https://nam12.safelinks.protection.outlook.com/?url=https%3A%2F%2Fwww.linkedin.com%2Fcomm%2Fjobs%2Fview%2F4012345680%2F&data=1">C</a>
        <a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=dup">A again</a>
        <a href="https://www.linkedin.com/comm/jobs/search?keywords=data">See all jobs</a>
        """
        urls = extract_linkedin_job_urls(html)
        self.assertEqual(urls, ["https://www.linkedin.com/jobs/view/4012345678/",
                                "https://www.linkedin.com/jobs/view/4012345679/",
                                "https://www.linkedin.com/jobs/view/4012345680/"])
        self.assertEqual(job_id_from_url(urls[1]), "linkedin:4012345679")


class TrackerTests(unittest.TestCase):
    def test_statuses_dates_and_persistence(self):
        path = Path(tempfile.mkdtemp()) / "tracker.json"
        t = JobTracker(str(path))
        t.add("linkedin:1", "pending", title="Head of AI", company="Acme", fit="8 of 10 skills match")
        t.add("linkedin:2", "dropped", title="Analyst", note="experience 5 years")
        t.set_status("linkedin:1", "deferred", "deferred by you")
        again = JobTracker(str(path))  # reloaded from disk
        job = again.get("linkedin:1")
        self.assertEqual(job["status"], "deferred")
        self.assertEqual([h["status"] for h in job["history"]], ["pending", "deferred"])
        self.assertTrue(job["scraped_at"] and job["status_updated_at"])
        self.assertEqual([j["job_id"] for j in again.with_status("pending", "deferred")], ["linkedin:1"])
        with self.assertRaises(ValueError):
            again.set_status("linkedin:1", "bogus")


if __name__ == "__main__":
    unittest.main()
