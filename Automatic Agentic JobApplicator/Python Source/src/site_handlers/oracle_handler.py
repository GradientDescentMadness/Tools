"""
Oracle Cloud and Taleo ATS specialist handler supporting nested iframes and Oracle JET components.
"""

import asyncio
from typing import Any, Dict, List
from .base_handler import BaseHandler


class OracleHandler(BaseHandler):
    """Handles Oracle Cloud HCM and Taleo application portals."""

    async def wait_for_page_ready(self, timeout: int = 25000) -> None:
        """Wait for Oracle JET SPA framework and embedded iframes to settle."""
        await asyncio.sleep(2.5)

    def _get_active_frame(self) -> Any:
        """Find main Taleo / Oracle recruitment iframe if present."""
        for frame in self.page.frames:
            if any(k in frame.url.lower() for k in ["recruit", "taleo", "candidate", "apply"]):
                return frame
        return self.page

    async def fill_current_section(self) -> Dict[str, Any]:
        """Fill inputs across Oracle frame hierarchy."""
        target = self._get_active_frame()

        # Update FormFiller target page temporarily if frame differs
        original_page = self.form_filler.page
        self.form_filler.page = target

        try:
            result = await self.form_filler.fill_all_fields()
        finally:
            self.form_filler.page = original_page

        return result

    async def go_to_next_section(self) -> bool:
        """Oracle specific navigation buttons inside or outside frame."""
        target = self._get_active_frame()
        oracle_btn_selectors = [
            "button:has-text('Save and Continue')",
            "button:has-text('Save & Continue')",
            "button:has-text('Next')",
            "a:has-text('Next')",
            "input[value='Next']",
        ]
        for sel in oracle_btn_selectors:
            try:
                btn = await target.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(3)
                    return True
            except Exception:
                pass
        return await super().go_to_next_section()

