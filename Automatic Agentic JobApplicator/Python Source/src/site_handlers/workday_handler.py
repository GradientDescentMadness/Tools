"""
Workday ATS specialist handler supporting data-automation-id and custom widgets.
"""

import asyncio
from typing import Any, Dict, List
from .base_handler import BaseHandler


class WorkdayHandler(BaseHandler):
    """Handles Workday application portals (e.g. *.myworkdayjobs.com)."""

    async def wait_for_page_ready(self, timeout: int = 20000) -> None:
        """Wait for Workday SPA animations, spinners, and data-automation components."""
        try:
            # Wait for Workday spinner to disappear
            await self.page.wait_for_selector("[data-automation-id='loadingAnimation'], .workday-spinner", state="detached", timeout=6000)
        except Exception:
            pass
        await asyncio.sleep(2)

    async def get_all_sections(self) -> List[str]:
        sections = []
        try:
            steps = await self.page.query_selector_all("[data-automation-id='progressBar'] li, [data-automation-id='step']")
            for s in steps:
                t = await s.inner_text()
                if t.strip():
                    sections.append(t.strip())
        except Exception:
            pass
        return sections or ["My Information", "My Experience", "Application Questions", "Review"]

    async def fill_current_section(self) -> Dict[str, Any]:
        """Fill Workday sections including custom data-automation inputs."""
        # 1. First run general form filler
        result = await self.form_filler.fill_all_fields()

        # 2. Targeted Workday data-automation-id inputs
        personal = self.resume_data.get("personal", {})
        try:
            # Source dropdown
            src_dd = await self.page.query_selector("[data-automation-id='sourceDropdown']")
            if src_dd:
                await src_dd.click()
                await asyncio.sleep(0.5)
                opt = await self.page.query_selector("li[data-automation-id*='LinkedIn'], [role='option']:has-text('LinkedIn')")
                if opt:
                    await opt.click()

            # First name & last name overrides if not matched
            fname_el = await self.page.query_selector("[data-automation-id='legalNameSection_firstName']")
            if fname_el and not await fname_el.input_value():
                await fname_el.fill(personal.get("first_name", ""))

            lname_el = await self.page.query_selector("[data-automation-id='legalNameSection_lastName']")
            if lname_el and not await lname_el.input_value():
                await lname_el.fill(personal.get("last_name", ""))

        except Exception:
            pass

        return result

    async def go_to_next_section(self) -> bool:
        """Workday specific navigation button matching."""
        workday_next_selectors = [
            "[data-automation-id='bottom-navigation-next-button']",
            "button[data-automation-id='nextButton']",
            "button:has-text('Save and Continue')",
            "button:has-text('Next')",
        ]
        for sel in workday_next_selectors:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(2.5)
                    return True
            except Exception:
                pass
        return False

    async def is_review_page(self) -> bool:
        """Check for Workday review container."""
        try:
            rev = await self.page.query_selector("[data-automation-id='reviewPage'], [data-automation-id='reviewAndSubmit']")
            if rev and await rev.is_visible():
                return True
        except Exception:
            pass
        return await super().is_review_page()

