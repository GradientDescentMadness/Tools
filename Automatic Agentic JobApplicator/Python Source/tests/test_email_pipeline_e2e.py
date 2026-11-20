"""
End-to-end test of the email pipeline with a real (headless) browser and fake LinkedIn job pages
served through request routing. Your keypresses are scripted. Two sources are exercised:

- Outlook on this PC (a fake stands in for classic Outlook's COM interface): permission prompt,
  folder choice, mails moved to Deleted Items only when fully processed, apply / defer / cancel.
- A folder of saved emails (real DOCX / PDF / EML files): processed files go to the Recycle Bin.

Both check experience screening (> 12 years), closed postings, the fit check and the JSON tracker.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import src.main as main_mod  # noqa: E402
from src.email_reader import extract_linkedin_job_urls  # noqa: E402
from src.job_tracker import JobTracker  # noqa: E402
from test_mail_sources import make_docx, make_eml, make_pdf  # noqa: E402

JOBS = {  # job id -> (title, company, location, description, closed, fit-card)
    "4100000001": ("Head of Data & AI", "HSBC", "Bengaluru, India", "We need 15+ years of experience leading analytics.", False,
                   "Job match: High. 8 of 10 skills match your profile"),
    "4100000002": ("Data Analyst", "Globex", "Pune, India", "3-5 years of experience in SQL and Python.", False, ""),
    "4100000003": ("Director Analytics", "Initech", "Mumbai, India", "Minimum 14 years of experience.", True, ""),
    "4100000004": ("VP Machine Learning", "Umbrella", "Bengaluru, India", "20 years of experience in ML.", False,
                   "4 of 10 skills match your profile"),
    "4100000005": ("Principal Data Scientist", "Hooli", "Hyderabad, India", "16 years of experience required.", False, ""),
    "4100000006": ("Chief Data Officer", "Stark", "Bengaluru, India", "13+ years of experience in data leadership.", False, ""),
}


def job_page(jid: str) -> str:
    title, company, location, jd, closed, fit = JOBS[jid]
    return f"""<html><body><nav>Home My Network Jobs Me</nav>
      <h1 class="top-card-layout__title">{title}</h1>
      <span class="topcard__flavor--black-link">{company}</span> <span class="topcard__flavor--bullet">{location}</span>
      {'<div class="closed-job">No longer accepting applications</div>' if closed else '<button>Apply</button>'}
      {f'<div class="job-details-fit-level-card">{fit}</div>' if fit else ''}
      <div class="show-more-less-html__markup"><p>About the role.</p><p>{jd}</p><p>{'Lorem ipsum. ' * 30}</p></div>
    </body></html>"""


def url(jid: str) -> str:
    return f"https://www.linkedin.com/comm/jobs/view/{jid}/?trackingId=t&refId=r"


def link(jid: str) -> str:
    return f'<a href="{url(jid).replace("&", "&amp;")}">job</a>'


class FakeDesktopSource:
    """Stands in for OutlookDesktopSource (classic Outlook via COM) with three LinkedIn alert mails."""
    instances = []
    trash_name = "Deleted Items"

    def __init__(self, notify=print, wait_hint_after=25.0):
        self.moved, self.label = [], "Outlook"
        FakeDesktopSource.instances.append(self)

    async def open(self):
        pass

    async def inbox_folders(self):
        return [{"name": "Newsletters", "total": 4}, {"name": "LinkedIn", "total": 3}]

    async def use_folder(self, name):
        self.label = f"Outlook: Inbox/{name}"
        return name == "LinkedIn"

    async def messages(self):
        return [
            {"id": "m1", "subject": "Head of Data & AI and 2 more", "received": "2026-10-30T08:00:00",
             "body": link("4100000001") + link("4100000002") + link("4100000003")},
            {"id": "m2", "subject": "VP Machine Learning", "received": "2026-10-31T08:00:00",
             "body": link("4100000004") + link("4100000005") + link("4100000001")},
            {"id": "m3", "subject": "Chief Data Officer", "received": "2026-11-01T08:00:00", "body": link("4100000006")},
        ]

    def links(self, msg):
        return extract_linkedin_job_urls(msg["body"])

    async def dispose(self, msg):
        self.moved.append(msg["id"])
        return True

    async def close(self):
        pass


class Script:
    """Scripted answers: each entry is (text expected in the prompt, answer)."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.log = []

    def __call__(self, prompt, *args, **kwargs):
        expected, answer = self.steps.pop(0)
        assert expected.lower() in prompt.lower(), f"expected prompt containing {expected!r}, got {prompt!r}"
        self.log.append((prompt.strip()[:60], answer))
        return answer


SCREENING_ANSWERS = [
    ("check your linkedin fit", "y"),             # job 1: senior + open
    ("add this job", True),                       #   -> pending
    ("check your linkedin fit", "y"),             # job 4 (job 2 dropped: experience, job 3 dropped: closed)
    ("add this job", False),                      #   -> not_interested
    ("check your linkedin fit", "n"),             # job 5 -> skipped
    ("check your linkedin fit", "q"),             # job 6 -> stop reading mail (that mail is kept)
]
EXPECTED = {"4100000001": "pending", "4100000002": "dropped", "4100000003": "dropped",
            "4100000004": "not_interested", "4100000005": "not_interested"}


def run_pipeline(answers, tmp, apply_phase=True):
    """Run mail collection (+ apply phase) in a real headless browser with fake LinkedIn pages."""
    tracker_path = tmp / "tracker.json"
    main_mod.confirm = answers
    main_mod.ask_user = answers

    async def run():
        app = main_mod.JobAutoApply(config_dir=str(ROOT / "config"), headless=True, screenshots=False)
        await app.initialize()
        app.settings["browser"].update({"user_data_dir": str(tmp / "profile"), "cdp_url": "http://127.0.0.1:9", "slow_mo": 0})
        await app.start_pipeline_browser()

        async def fake_linkedin(route):
            jid = route.request.url.rstrip("/").split("/")[-1].split("?")[0]
            await route.fulfill(status=200, content_type="text/html", body=job_page(jid))
        await app.context.route("https://www.linkedin.com/jobs/view/**", fake_linkedin)

        applied = []

        async def fake_apply(job, skip_confirm=False):
            applied.append(job["link"])
            return {"status": "reached_review", "ats": "workday", "auth": "signed_in"}
        app.process_job = fake_apply
        tracker = JobTracker(str(tracker_path))
        try:
            await app.collect_from_mail(tracker)
            if apply_phase:
                await app.apply_from_tracker(tracker)
        finally:
            await app.cleanup()
        return applied

    applied = asyncio.run(run())
    jobs = {j["job_id"]: j for j in json.loads(tracker_path.read_text(encoding="utf-8"))["jobs"]}
    return applied, jobs


class EmailPipelineE2E(unittest.TestCase):
    def test_outlook_desktop(self):
        tmp = Path(tempfile.mkdtemp(prefix="aaj-pipeline-"))
        pre = JobTracker(str(tmp / "tracker.json"))  # a job deferred in an earlier run
        pre.add("linkedin:4099999999", "deferred", title="Deferred Role", company="Old Co",
                url="https://www.linkedin.com/jobs/view/4099999999/")
        main_mod.OutlookDesktopSource = FakeDesktopSource
        answers = Script([
            ("read new job-alert emails from", "o"),      # source: Outlook on this PC
            ("allow this tool to open outlook", True),    # permission
            ("which folder", "LinkedIn"),
            *SCREENING_ANSWERS,
            ("start applying now", True),
            ("deferred role", "c"),                       # earlier deferred job -> cancelled
            ("head of data", "a"),                        # apply
            ("did you submit", True),                     #   -> applied
        ])
        applied, jobs = run_pipeline(answers, tmp)
        status = {k.split(":")[1]: v["status"] for k, v in jobs.items()}
        print("\nOutlook statuses:", status)
        self.assertEqual(answers.steps, [], "every scripted prompt should have been asked")
        self.assertEqual(status, {**EXPECTED, "4100000001": "applied", "4099999999": "cancelled"})
        self.assertNotIn("linkedin:4100000006", jobs, "job 6 was not decided (user stopped)")
        self.assertEqual(FakeDesktopSource.instances[-1].moved, ["m1", "m2"], "m3 was not fully processed, so it stays")
        a = jobs["linkedin:4100000001"]
        self.assertEqual(a["required_years"], "15+ years")
        self.assertIn("8 of 10 skills", a["fit"])
        self.assertEqual([h["status"] for h in a["history"]], ["pending", "applied"])
        self.assertTrue(a["scraped_at"] and a["status_updated_at"] and a["email_received"].startswith("2026-10-30"))
        self.assertEqual(a["source"], "Outlook: Inbox/LinkedIn")
        self.assertIn("needs more than 12", jobs["linkedin:4100000002"]["history"][-1]["note"])
        self.assertIn("no longer accepting", jobs["linkedin:4100000003"]["history"][-1]["note"])
        self.assertEqual(applied, ["https://www.linkedin.com/jobs/view/4100000001/"])

    def test_saved_email_files(self):
        tmp = Path(tempfile.mkdtemp(prefix="aaj-pipeline-"))
        mails = tmp / "saved mails"
        mails.mkdir()
        make_docx(mails / "alert 1.docx", {"Head of Data & AI": url("4100000001"), "Data Analyst": url("4100000002"),
                                           "Director Analytics": url("4100000003")})
        make_pdf(mails / "alert 2.pdf", {"VP Machine Learning": url("4100000004"), "Principal DS": url("4100000005"),
                                         "Head of Data again": url("4100000001")})
        make_eml(mails / "alert 3.eml", {"Chief Data Officer": url("4100000006")})
        for i, name in enumerate(["alert 1.docx", "alert 2.pdf", "alert 3.eml"]):  # oldest first
            os.utime(mails / name, (1_790_000_000 + i * 60, 1_790_000_000 + i * 60))
        answers = Script([
            ("read new job-alert emails from", "f"),      # source: folder of saved emails
            ("folder that holds", str(mails)),
            *SCREENING_ANSWERS,
        ])
        applied, jobs = run_pipeline(answers, tmp, apply_phase=False)
        status = {k.split(":")[1]: v["status"] for k, v in jobs.items()}
        left = sorted(p.name for p in mails.iterdir())
        print("\nSaved-file statuses:", status, "| left in folder:", left)
        self.assertEqual(answers.steps, [])
        self.assertEqual(status, EXPECTED)
        self.assertEqual(left, ["alert 3.eml"], "processed files went to the Recycle Bin; the unfinished one stays")
        self.assertTrue(jobs["linkedin:4100000001"]["source"].startswith("Folder:"))


if __name__ == "__main__":
    unittest.main()
