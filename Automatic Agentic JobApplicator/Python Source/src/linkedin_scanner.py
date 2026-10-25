"""
LinkedIn job page scanner and application interaction controller using Playwright.
"""

import asyncio
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse


class LinkedInScanner:
    """Navigates LinkedIn job postings, parses details, and triggers application flows."""

    def __init__(self, page: Any):
        self.page = page

    async def navigate_to_job(self, url: str) -> Dict[str, Any]:
        """Navigate to a LinkedIn job URL and extract metadata."""
        await self.page.goto(url, wait_until="domcontentloaded", timeout=35000)
        await asyncio.sleep(2)
        return await self.extract_job_info()

    async def extract_job_info(self) -> Dict[str, Any]:
        """Scrape job title, company, location, and apply button characteristics."""
        info = {
            "title": "Unknown Title",
            "company": "Unknown Company",
            "location": "Unknown Location",
            "description_snippet": "",
            "is_easy_apply": False,
        }

        # Multi-selector fallbacks for dynamic LinkedIn DOM
        title_selectors = [
            "h1.job-details-jobs-unified-top-card__job-title",
            "h1.top-card-layout__title",
            "h1.t-24",
            "h1[class*='jobTitle']",
        ]
        for sel in title_selectors:
            try:
                el = await self.page.query_selector(sel)
                if el:
                    t = await el.inner_text()
                    if t.strip():
                        info["title"] = t.strip()
                        break
            except Exception:
                pass

        company_selectors = [
            ".job-details-jobs-unified-top-card__company-name a",
            ".topcard__flavor--black-link",
            "span.company-name",
            "div.jobs-unified-top-card__company-name",
        ]
        for sel in company_selectors:
            try:
                el = await self.page.query_selector(sel)
                if el:
                    c = await el.inner_text()
                    if c.strip():
                        info["company"] = c.strip()
                        break
            except Exception:
                pass

        location_selectors = [
            ".job-details-jobs-unified-top-card__bullet",
            ".topcard__flavor--bullet",
            "span.job-location",
        ]
        for sel in location_selectors:
            try:
                el = await self.page.query_selector(sel)
                if el:
                    loc = await el.inner_text()
                    if loc.strip():
                        info["location"] = loc.strip()
                        break
            except Exception:
                pass

        # Check Easy Apply
        try:
            easy_btn = await self.page.query_selector(".jobs-apply-button")
            if easy_btn:
                btn_txt = await easy_btn.inner_text()
                if "Easy Apply" in btn_txt:
                    info["is_easy_apply"] = True
        except Exception:
            pass

        return info

    async def click_apply(self) -> str:
        """Click apply button and identify if redirecting externally or launching modal."""
        apply_btn_selectors = [
            "button.jobs-apply-button",
            "button[aria-label*='Apply']",
            "button:has-text('Apply')",
            ".jobs-apply-button--top-card",
        ]

        target_btn = None
        for sel in apply_btn_selectors:
            try:
                target_btn = await self.page.query_selector(sel)
                if target_btn and await target_btn.is_visible():
                    break
            except Exception:
                pass

        if not target_btn:
            return "not_found"

        btn_text = (await target_btn.inner_text()).strip()

        # Listen for potential new tab
        try:
            context = self.page.context
            async with context.expect_page(timeout=6000) as new_page_info:
                await target_btn.click()
            new_page = await new_page_info.value
            await new_page.wait_for_load_state("domcontentloaded")
            return f"external:{new_page.url}"
        except Exception:
            # Did not spawn a new window, stay on current page
            pass

        await asyncio.sleep(2)
        curr_url = self.page.url

        if "linkedin.com/jobs" not in curr_url:
            return f"external:{curr_url}"

        # Check if Easy Apply dialog opened
        modal = await self.page.query_selector("div.jobs-easy-apply-modal")
        if modal:
            return "easy_apply"

        return "unknown"

    async def detect_external_site(self) -> str:
        """Analyze current URL and identify target Applicant Tracking System (ATS)."""
        url = self.page.url.lower()
        if "myworkdayjobs" in url or "myworkday.com" in url:
            return "workday"
        if "oraclecloud" in url or "taleo" in url:
            return "oracle"
        if "greenhouse.io" in url:
            return "greenhouse"
        if "lever.co" in url:
            return "lever"
        if "icims.com" in url:
            return "icims"
        if "smartrecruiters" in url:
            return "smartrecruiters"
        return "generic"

    async def is_login_required(self) -> bool:
        """Detect whether current page demands authentication."""
        url = self.page.url.lower()
        if any(term in url for term in ["login", "signin", "auth", "session"]):
            return True

        indicators = [
            "input[type='password']",
            "button:has-text('Sign In')",
            "button:has-text('Log In')",
            "input[name*='password']",
        ]
        for sel in indicators:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    return True
            except Exception:
                pass

        return False

    async def handle_easy_apply(self, resume_data: Dict[str, Any]) -> bool:
        """Step through LinkedIn Easy Apply dialog, stopping before final submission."""
        for step in range(15):
            await asyncio.sleep(1.5)

            # Check for Submit button -> ALWAYS STOP for safety
            submit_btn = await self.page.query_selector("button:has-text('Submit application')")
            if submit_btn and await submit_btn.is_visible():
                print("  [i] Reached final Easy Apply submit screen. Handing over to user.")
                return True

            # Attempt clicking Next or Review
            next_btn = await self.page.query_selector("button[aria-label*='Continue to next step'], button:has-text('Next'), button:has-text('Review')")
            if next_btn and await next_btn.is_visible():
                await next_btn.click()
                await asyncio.sleep(1)
            else:
                # No forward button found
                break

        return True
