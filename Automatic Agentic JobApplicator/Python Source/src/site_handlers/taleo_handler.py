"""
Oracle Taleo Enterprise (careersection) handler.

Flow: 'Apply Online' -> Privacy Agreement (I Accept) -> Login / New User registration
(user name = settings email unless auth.username is set) -> multi-page application with
'Save and Continue' -> 'Review and Submit' (never submitted).
"""

import asyncio
import re
from typing import Any, Dict

from .base_handler import BaseHandler


class TaleoHandler(BaseHandler):
    """Handles Taleo careersection application flows."""

    async def wait_for_page_ready(self, timeout: int = 20000) -> None:
        await super().wait_for_page_ready(timeout)
        await self.accept_privacy_agreement()

    async def accept_privacy_agreement(self) -> bool:
        """Taleo opens applications with a Privacy Agreement page (I Accept / I Decline)."""
        accept = self.page.locator("button:text-is('I Accept'), input[value='I Accept'], a:text-is('I Accept')")
        if await accept.count() and await accept.first.is_visible():
            print("  ✓ Accepting Taleo privacy agreement.")
            await accept.first.click(timeout=5000)
            await super().wait_for_page_ready()
            return True
        return False

    def _username(self) -> str:
        return self.auth.get("username") or self.auth.get("email", "")

    async def is_auth_page(self) -> bool:
        if "accessmanagement" in self.page.url:
            return True
        return await super().is_auth_page()

    async def is_registration_page(self) -> bool:
        loc = self.page.locator("[id$='dialogForm-passwordConfirm']")
        return bool(await loc.count()) and await loc.first.is_visible()

    async def sign_in(self, email: str, password: str) -> bool:
        user = self.page.locator("[id$='login-name1'], input[id*='login-name']").first
        if not await user.count():
            return await super().sign_in(self._username(), password)
        print("  🔑 Taleo sign-in with settings credentials...")
        await user.fill(self._username())
        await self.page.locator("[id$='login-password']").first.fill(password)
        await self.click_first(["button:text-is('Login')", "input[value='Login']", "a:text-is('Login')"], allow_submit=True)
        await self.wait_for_page_ready()
        if await self.is_auth_page():
            errors = await self.validation_errors()
            print(f"  [!] Taleo sign-in did not succeed{': ' + errors[0][:120] if errors else ''}")
            return False
        return True

    async def open_registration(self) -> bool:
        if await self.click_first(["button:text-is('New User')", "a:text-is('New User')", "input[value='New User']"]):
            await self.wait_for_page_ready()
        return await self.is_registration_page()

    async def register(self) -> bool:
        print("  ⚡ Taleo New User registration...")
        email, password = self.auth.get("email", ""), self.auth.get("password", "")
        for suffix, value in (("userName", self._username()), ("password", password), ("passwordConfirm", password),
                              ("email", email), ("emailConfirm", email)):
            loc = self.page.locator(f"[id$='dialogForm-{suffix}']")
            if await loc.count():
                await loc.first.fill(value)
        for cb in await self.page.locator("input[type='checkbox']").all():
            if await cb.is_visible() and not await cb.is_checked():
                await cb.check(force=True)  # e.g. privacy/terms acknowledgement on the registration form
        if not await self.click_first(["button:text-is('Register')", "input[value='Register']", "a:text-is('Register')"],
                                      allow_submit=True):
            return False
        await self.wait_for_page_ready()
        await asyncio.sleep(2)
        if await self.account_already_exists():
            return False
        if await self.is_auth_page():
            errors = await self.validation_errors()
            print(f"  [!] Taleo registration did not complete{': ' + '; '.join(e[:100] for e in errors[:3]) if errors else ''}")
            return False
        print("  ✓ Taleo account registered.")
        return True

    async def get_current_section(self) -> str:
        for sel in (".editablesection h1, .editablesection h2", "#et-ef-content-ftf-saveContinueCmdBottom", "h1", "h2"):
            loc = self.page.locator(sel)
            if await loc.count() and await loc.first.is_visible():
                t = (await loc.first.inner_text()).strip()
                if t:
                    return t.splitlines()[0][:80]
        return await super().get_current_section()

    async def go_to_next_section(self) -> bool:
        clicked = await self.click_first([
            "input[value='Save and Continue']", "button:has-text('Save and Continue')",
            "input[value='Next']", "button:text-is('Next')", "a:text-is('Save and Continue')",
        ])
        if clicked:
            await self.wait_for_page_ready()
        return bool(clicked)

    async def fill_current_section(self) -> Dict[str, Any]:
        await self.upload_resume()
        return await self.form_filler.fill_all_fields()

    async def upload_resume(self) -> bool:
        from pathlib import Path
        path = str((self.resume_data.get("additional", {}) or {}).get("resume_file", "") or "")
        inp = self.page.locator("input[type='file']")
        if not path or not Path(path).exists() or not await inp.count():
            return False
        if await self.page.locator(f"text={Path(path).name}").count():
            return True
        await inp.first.set_input_files(path)
        await self.click_first(["input[value='Attach']", "button:has-text('Attach')", "input[value='Upload']"])
        await asyncio.sleep(3)
        return True

    async def is_review_page(self) -> bool:
        heading = await self.get_current_section()
        if re.search(r"review and submit|review & submit|^review$", heading, re.I):
            return True
        return await super().is_review_page()
