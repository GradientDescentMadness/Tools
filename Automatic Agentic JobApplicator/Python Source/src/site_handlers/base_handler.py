"""
Base handler defining common ATS interactions and polymorphic factory method.
"""

import asyncio
from typing import Any, Dict, List


class BaseHandler:
    """Base class for generic Applicant Tracking System (ATS) automation."""

    def __init__(self, page: Any, form_filler: Any, resume_data: Dict[str, Any], settings: Dict[str, Any]):
        self.page = page
        self.form_filler = form_filler
        self.resume_data = resume_data
        self.settings = settings

    async def wait_for_page_ready(self, timeout: int = 15000) -> None:
        """Wait for network and DOM stabilization."""
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
            await asyncio.sleep(1.5)
        except Exception:
            pass

    async def get_all_sections(self) -> List[str]:
        """Detect progression steps or wizard breadcrumb titles."""
        sections = []
        try:
            step_elements = await self.page.query_selector_all(".step-title, .nav-item, [role='tab'], li.progress-step")
            for el in step_elements:
                txt = await el.inner_text()
                if txt.strip():
                    sections.append(txt.strip())
        except Exception:
            pass
        return sections

    async def get_current_section(self) -> str:
        """Identify the active wizard stage name."""
        try:
            active = await self.page.query_selector(".active, [aria-selected='true'], .current-step")
            if active:
                txt = await active.inner_text()
                if txt.strip():
                    return txt.strip().splitlines()[0]
        except Exception:
            pass
        return "Current Section"

    async def fill_current_section(self) -> Dict[str, Any]:
        """Delegate input population to FormFiller engine."""
        return await self.form_filler.fill_all_fields()

    async def go_to_next_section(self) -> bool:
        """Locate and click standard Next/Continue navigation buttons."""
        next_button_selectors = [
            "button:has-text('Save and Continue')",
            "button:has-text('Save & Continue')",
            "button:has-text('Continue')",
            "button:has-text('Next')",
            "input[type='submit'][value*='Next']",
            "button[aria-label*='Next']",
        ]
        for sel in next_button_selectors:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible() and await btn.is_enabled():
                    await btn.click()
                    await asyncio.sleep(2)
                    return True
            except Exception:
                pass
        return False

    async def is_review_page(self) -> bool:
        """Determine if page reached final confirmation or submission stage."""
        url = self.page.url.lower()
        if any(term in url for term in ["review", "confirm", "summary", "final"]):
            return True

        indicators = [
            "button:has-text('Submit Application')",
            "button:has-text('Submit')",
            "h1:has-text('Review')",
            "h2:has-text('Review and Submit')",
        ]
        for sel in indicators:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    return True
            except Exception:
                pass
        return False


def get_handler(page: Any, form_filler: Any, resume_data: Dict[str, Any], settings: Dict[str, Any]) -> BaseHandler:
    """Factory creating specialized ATS handlers based on target page URL."""
    from .workday_handler import WorkdayHandler
    from .oracle_handler import OracleHandler

    url = page.url.lower()
    if "myworkdayjobs" in url or "myworkday.com" in url:
        return WorkdayHandler(page, form_filler, resume_data, settings)
    if "oraclecloud" in url or "taleo" in url:
        return OracleHandler(page, form_filler, resume_data, settings)

    return BaseHandler(page, form_filler, resume_data, settings)

