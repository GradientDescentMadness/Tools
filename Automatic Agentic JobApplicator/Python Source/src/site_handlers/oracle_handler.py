"""
Oracle Recruiting Cloud (Candidate Experience, *.oraclecloud.com/hcmUI/CandidateExperience) handler.

Flow: email (+ one-time PIN for returning candidates) -> one long application page with
Oracle JET comboboxes (dependent Country -> Pin Code -> City/State), pill-button choices
(Title, Yes/No questions), resume upload and an e-signature -> SUBMIT (never pressed).
"""

import asyncio
import re
from typing import Any, Dict, List

from ..field_resolver import FieldInfo
from ..matching import best_option
from .base_handler import BaseHandler

PILL_GROUPS_JS = r"""
() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const clean = s => (s || '').replace(/\s+/g, ' ').replace(/\*/g, '').trim();
  document.querySelectorAll('[data-aaj-pill]').forEach(e => e.removeAttribute('data-aaj-pill'));
  const groups = new Map();
  for (const b of document.querySelectorAll('button[aria-pressed], [role=radio][aria-checked], button.cx-select-pill-section')) {
    if (!vis(b)) continue;
    const g = b.closest('[role=radiogroup], [role=group], fieldset, .cx-select-pills-container, .input-row') || b.parentElement;
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(b);
  }
  const out = [];
  let gi = 0;
  for (const [g, btns] of groups) {
    if (btns.length < 2) continue;
    let label = '';
    const lb = g.getAttribute('aria-labelledby');
    if (lb) label = clean(lb.split(/\s+/).map(id => (document.getElementById(id) || {}).innerText || '').join(' '));
    if (!label) label = clean((g.querySelector('legend, label') || {}).innerText);
    if (!label) { let p = g; for (let i = 0; i < 3 && !label && p; i++) { p = p.parentElement; const l = p && p.querySelector('label, legend, .input-row__label'); if (l && !g.contains(l)) label = clean(l.innerText); } }
    const opts = btns.map(b => clean(b.innerText || b.getAttribute('aria-label')));
    const pressed = btns.map(b => b.getAttribute('aria-pressed') === 'true' || b.getAttribute('aria-checked') === 'true');
    btns.forEach((b, j) => b.setAttribute('data-aaj-pill', gi + '-' + j));
    out.push({gi: gi++, label, options: opts, selected: opts[pressed.indexOf(true)] || ''});
  }
  return out;
}
"""


class OracleHandler(BaseHandler):
    """Handles Oracle Recruiting Cloud (and legacy Oracle-hosted) candidate experience flows."""

    async def wait_for_page_ready(self, timeout: int = 25000) -> None:
        for state in ("domcontentloaded", "networkidle"):
            try:
                await self.page.wait_for_load_state(state, timeout=timeout if state == "domcontentloaded" else 10000)
            except Exception:
                pass
        await asyncio.sleep(2.0)

    def _frame(self) -> Any:
        """Some Oracle-hosted sites embed the form in a recruiting iframe."""
        for frame in self.page.frames:
            if frame != self.page.main_frame and re.search(r"recruit|candidate|apply", frame.url, re.I):
                return frame
        return self.page

    # ------------------------------------------------------------------ authentication (email + PIN)
    async def is_auth_page(self) -> bool:
        if re.search(r"/apply/email", self.page.url):
            return True
        pin = self.page.locator("input[autocomplete='one-time-code'], input[id*='pin' i], input[name*='pin' i]")
        return bool(await pin.count()) and await pin.first.is_visible()

    async def page_text(self) -> str:
        try:
            return await self.page.evaluate("() => document.body.innerText.slice(0, 4000)")
        except Exception:
            return ""

    async def handle_authentication_if_present(self) -> str:
        if not await self.is_auth_page():
            return "none"
        email = self.auth.get("email") or (self.resume_data.get("personal", {}) or {}).get("email", "")
        box = self.page.locator("#primary-email-0, input[type='email']").first
        if await box.count() and await box.is_visible():
            await box.fill(email)
            for cb in await self.page.locator("input[type='checkbox']").all():
                if await cb.is_visible() and not await cb.is_checked():
                    await cb.check(force=True)  # terms / privacy consent on the email step
            await self.click_first(["button:has-text('Next')"], allow_submit=True)
            await self.wait_for_page_ready()
        text = await self.page_text()
        if re.search(r"too many attempts", text, re.I):
            print("  [!] Oracle: 'Too many attempts' for this email - the portal blocks it for ~30 minutes.")
            return "failed"
        if await self._pin_visible():
            return "signed_in" if await self.enter_pin() else "failed"
        return "failed" if await self.is_auth_page() else "signed_in"

    async def _pin_visible(self) -> bool:
        pin = self.page.locator("input[autocomplete='one-time-code'], input[id*='pin' i], input[name*='pin' i], "
                                "input[aria-label*='code' i]")
        return bool(await pin.count()) and await pin.first.is_visible()

    async def enter_pin(self) -> bool:
        """Returning candidates get a one-time code by email: from the test hook, else asked from the user."""
        provider = getattr(self, "verification_code_provider", None)
        code = await provider() if provider else None
        if not code and provider and await self.click_first(["a:has-text('Send New Code')", "button:has-text('Send New Code')"]):
            print("  ✉️  No code arrived - requested a new one.")
            code = await provider()
        if not code:
            from ..utils import ask_user
            code = ask_user("Oracle emailed you a verification code. Enter it here (blank = do it in the browser)")
        if not code:
            return False
        boxes = self.page.locator("input[autocomplete='one-time-code'], input[id*='pin' i], input[name*='pin' i], input[aria-label*='code' i]")
        n = await boxes.count()
        if n >= len(code):
            for i, ch in enumerate(code):
                await boxes.nth(i).fill(ch)
        else:
            await boxes.first.fill(code)
        await self.click_first(["button:has-text('Verify')", "button:has-text('Next')", "button:has-text('Continue')"], allow_submit=True)
        await self.wait_for_page_ready()
        return not await self.is_auth_page()

    # ------------------------------------------------------------------ filling
    async def fill_current_section(self) -> Dict[str, Any]:
        target = self._frame()
        original = self.form_filler.page
        self.form_filler.page = target
        try:
            uploaded = await self.upload_resume(target)
            result = await self.form_filler.fill_all_fields()
            pills = await self.fill_pill_groups(target)
        finally:
            self.form_filler.page = original
        result["filled_count"] += pills["filled"]
        result["failed_count"] += pills["failed"]
        result["details"] += pills["details"]
        result["resume_uploaded"] = uploaded
        self.filled_once = True
        return result

    async def fill_pill_groups(self, target: Any) -> Dict[str, Any]:
        """Pill/toggle button choices (Title: Mr./Ms., Yes/No questions)."""
        filled = failed = 0
        details: List[Dict[str, Any]] = []
        for g in await target.evaluate(PILL_GROUPS_JS):
            if g["selected"] or not g["label"]:
                continue
            ans = self.form_filler.resolver.resolve(FieldInfo(label=g["label"], field_type="radio", options=g["options"]))
            if not ans:
                details.append({"label": g["label"], "status": "skipped"})
                continue
            choice = best_option(g["options"], ans.candidates, ans.fallbacks)
            if not choice:
                failed += 1
                details.append({"label": g["label"], "status": "failed", "value": ans.value, "reason": f"options {g['options']}"})
                continue
            await target.locator(f"[data-aaj-pill='{g['gi']}-{g['options'].index(choice)}']").click(timeout=5000)
            filled += 1
            details.append({"label": g["label"], "status": "filled", "value": choice, "rule": ans.rule})
            await asyncio.sleep(0.4)
        return {"filled": filled, "failed": failed, "details": details}

    async def upload_resume(self, target: Any) -> bool:
        from pathlib import Path
        path = str((self.resume_data.get("additional", {}) or {}).get("resume_file", "") or "")
        if not path or not Path(path).exists():
            return False
        if await target.locator(f"text={Path(path).name}").count():
            return True
        inputs = target.locator("input[type='file']")
        if not await inputs.count():
            return False
        await inputs.first.set_input_files(path)
        await asyncio.sleep(4)
        return bool(await target.locator(f"text={Path(path).name}").count())

    # ------------------------------------------------------------------ navigation / review
    async def is_review_page(self) -> bool:
        """The application is one page ending in SUBMIT: once filled, hand over for review."""
        if not getattr(self, "filled_once", False):
            return False
        btn = self._frame().locator("button:text-is('Submit'), button:text-is('SUBMIT'), button:has-text('Submit')")
        return bool(await btn.count()) and await btn.first.is_visible()

    async def go_to_next_section(self) -> bool:
        if await self.is_review_page():
            return True
        target = self._frame()
        return bool(await self.click_first(["button:has-text('Next')", "button:has-text('Continue')",
                                            "button:has-text('Save and Continue')"], root=target)) or \
            await super().go_to_next_section()
