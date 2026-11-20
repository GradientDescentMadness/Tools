"""
Read-only check of the JD screener against real LinkedIn job pages (guest view: no login).
Takes a few postings from a public LinkedIn search page and prints what the screener reads.

Usage (from 'Python Source'):  python tests/linkedin_live_check.py [search-page-url]
"""

import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.email_reader import extract_linkedin_job_urls  # noqa: E402
from src.jd_screener import read_job_page, required_years  # noqa: E402
from src.linkedin_scanner import LinkedInScanner  # noqa: E402

SEARCH = "https://in.linkedin.com/jobs/director-data-science-jobs-bengaluru"


async def main() -> None:
    search = sys.argv[1] if len(sys.argv) > 1 else SEARCH
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await (await browser.new_context(viewport={"width": 1280, "height": 1000}, locale="en-US")).new_page()
        await page.goto(search, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)
        html = await page.content()
        urls = extract_linkedin_job_urls(html.replace("in.linkedin.com", "www.linkedin.com"))[:5]
        print(f"{len(urls)} postings from {search}")
        for url in urls:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
            info = await LinkedInScanner(page).extract_job_info()
            job = await read_job_page(page)
            req = required_years(job["description"])
            print(f"\n{url}\n  title={info['title'][:60]!r} company={info['company'][:40]!r} location={info['location'][:40]!r}"
                  f"\n  JD chars={len(job['description'])} accepting={job['accepting']} signed_out={job['signed_out']}"
                  f"\n  experience={req.describe()} more_than_12={req.more_than(12)} evidence={req.evidence[:3]}")
            if re.search(r"authwall|login", page.url):
                print("  (LinkedIn showed its sign-in wall to this guest browser)")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
