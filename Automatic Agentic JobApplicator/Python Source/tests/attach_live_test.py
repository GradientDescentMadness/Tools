"""
Live check of --attach: start a separate debug Chrome (test profile), open three tabs, run
`run.py --attach --url <target>` against it, then verify the right tab was filled and that Chrome
and every tab are still open afterwards.

Usage (from 'Python Source'):  python tests/attach_live_test.py [--headed]
"""

import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from urllib.parse import quote

import requests
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.attach import cdp_alive, launch_debug_chrome  # noqa: E402

CDP = "http://127.0.0.1:9333"
REG = ROOT / "tests" / "regression"
TARGET = "https://career2.successfactors.eu/careers?company=hsbcholdin&career_ns=job_application&src=Eightfold&career_job_req_id=64895&lang=en_GB"
OTHER_TABS = ["https://example.com/",
              "https://careers.anz.com/job/Bengaluru-General-Manager-Data-&-AI-Platform/1369423066/?jobPipeline=Indeed"]


def tabs():
    return [t for t in requests.get(f"{CDP}/json/list", timeout=5).json() if t.get("type") == "page"]


async def main() -> None:
    headed = "--headed" in sys.argv
    profile = tempfile.mkdtemp(prefix="aaj-attach-test-")
    assert launch_debug_chrome(CDP, profile, url=OTHER_TABS[0], extra_args=[] if headed else ["--headless=new"]), "Chrome did not start"
    for url in OTHER_TABS[1:] + [TARGET]:
        requests.put(f"{CDP}/json/new?{quote(url, safe='')}", timeout=10)  # encoded, or Chrome cuts at '&'
    time.sleep(8)
    before = tabs()
    print("Tabs before:", [t["url"][:70] for t in before])

    cmd = [sys.executable, "run.py", "--attach", "--cdp", CDP, "--url", TARGET, "--auto",
           "--resume-data", str(REG / "resume_runtime_hsbc.yaml"), "--settings", str(REG / "settings_runtime_hsbc.yaml")]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("\n".join(l for l in out.stdout.splitlines() if l.strip())[-3000:])

    print("\nChrome still running:", cdp_alive(CDP))
    after = tabs()
    print("Tabs after: ", [t["url"][:70] for t in after])
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP)
        for page in browser.contexts[0].pages:
            if "successfactors.eu" in page.url:
                vals = await page.evaluate("""() => [...document.querySelectorAll('input[type=text], input[type=email]')]
                    .filter(e => e.getBoundingClientRect().height > 0 && e.value).map(e => (e.getAttribute('aria-label') || e.id) + '=' + e.value).slice(0, 30)""")
                print("Filled values in the matched tab:", vals)
        session = await browser.new_browser_cdp_session()
        await session.send("Browser.close")  # shut down the TEST Chrome only


if __name__ == "__main__":
    asyncio.run(main())
