"""
SAP SuccessFactors (incl. jobs2web career sites such as ANZ Careers) specialist handler.

Sign-in with settings credentials, fallback to Create Account with the same credentials,
data-privacy-statement acceptance (link -> dialog -> Accept), accordion expansion and
repeating Work Experience / Education rows.
"""

import asyncio
import re
from typing import Any, Dict

from .. import utils

from ..matching import best_option, norm
from .base_handler import BaseHandler

REGISTER_LINKS = ["a:has-text('Create an account')", "a:has-text('Create your account')", "a:has-text('Create account')"]
PRIVACY_TRIGGERS = [
    "#dataPrivacyId", "a[aria-haspopup='dialog']:has-text('privacy')", "a:has-text('Read and accept the data privacy')",
    "a:has-text('data privacy statement')", "button:has-text('data privacy statement')",
]
DIALOG_ACCEPT = [
    ".sfDialogBoxButtonWrapper button:has-text('Accept')", "[role='dialog'] button:has-text('Accept')",
    "[role='alertdialog'] button:has-text('Accept')", "button:text-is('Accept')", "button:text-is('I Accept')",
    "button:text-is('I Agree')",
]


class SuccessFactorsHandler(BaseHandler):
    """Specialist handler for SAP SuccessFactors / jobs2web career portals."""

    async def wait_for_page_ready(self, timeout: int = 25000) -> None:
        """Wait for SuccessFactors AJAX spinners and overlays to settle."""
        for state in ("domcontentloaded", "networkidle"):
            try:
                await self.page.wait_for_load_state(state, timeout=timeout if state == "domcontentloaded" else 8000)
            except Exception:
                pass
        try:
            await self.page.wait_for_selector(".busyIndicator:visible, .sapUiBlockOverlay:visible, .loading-indicator:visible",
                                              state="detached", timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(1.5)

    # ------------------------------------------------------------------ authentication
    async def is_combined_apply_page(self) -> bool:
        """
        'Simplified apply': one page holding both the account fields (email/password) and the whole
        application, submitted by its 'Apply' button. It is an application form, not a login wall.
        """
        if not await self.is_registration_page():
            return False
        apply_btn = self.page.locator("[id$='_applyBtn'], button:text-is('Apply'), input[value='Apply'], [role='button']:text-is('Apply')")
        return bool(await apply_btn.count()) and await apply_btn.first.is_visible()

    async def is_auth_page(self) -> bool:
        if await self.is_combined_apply_page():
            return not getattr(self, "combined_registration", False)
        for sel in ("#fbclc_userName", "#fbclc_pwd", "input#password", "input#username"):
            loc = self.page.locator(sel)
            if await loc.count() and await loc.first.is_visible():
                return True
        return await super().is_auth_page()

    async def is_registration_page(self) -> bool:
        loc = self.page.locator("#fbclc_userName, #fbclc_emailConf")
        return bool(await loc.count()) and await loc.first.is_visible()

    async def handle_authentication_if_present(self) -> str:
        if await self.is_passcode_page():
            return "signed_in" if await self.enter_passcode() else "failed"
        if await self.is_combined_apply_page():
            # Existing candidates must sign in (the form would otherwise try to create a duplicate account).
            if await self.click_first(["a:has-text('Please sign in')", "a:has-text('sign in')"]):
                await self.wait_for_page_ready()
                if await self.sign_in(self.auth.get("email", ""), self.auth.get("password", "")):
                    return "signed_in"
                if self.job_url:
                    print("  ↩️  No existing account - returning to the application page to register while applying.")
                    await self.restart_application()
            self.combined_registration = True
            print("  📝 Register-and-apply page: account fields are filled with the application (you press Apply).")
            return "none"
        return await super().handle_authentication_if_present()

    async def sign_in(self, email: str, password: str) -> bool:
        user = self.page.locator("input#username:visible, input[name='username']:visible").first
        if not await user.count():
            return await super().sign_in(email, password)
        print("  🔑 Sign-in page: signing in with settings credentials...")
        await user.fill(email)
        # The password and button live in the username's own form or sign-in dialog; the page may also hold a
        # registration form with its own password boxes, and hidden decoys named 'password' (e.g. #qapassword).
        scope = user.locator("xpath=ancestor::*[.//input[@type='password'] and .//button][1]")
        if not await scope.count():
            scope = self.page
        await scope.locator("input[type='password']:visible").first.fill(password)
        if await self.captcha_visible():
            if utils.AUTO_MODE:
                print("  [!] Sign-in has an 'I'm not a robot' check - that is left for the candidate.")
                return False
            utils.wait_for_proceed("This sign-in asks you to confirm you're not a robot - tick it in the browser "
                                   "(the tool never does), then press Enter here...")
        await self.click_first(["button:has-text('Sign In')", "input[type='submit'][value*='Sign']"], root=scope, allow_submit=True)
        await self.wait_for_page_ready()
        await asyncio.sleep(2)
        if await self.is_auth_page():
            errors = await self.validation_errors()
            print(f"  [!] Sign-in did not succeed{': ' + errors[0] if errors else ''}")
            return False
        await self.accept_privacy()
        return True

    async def open_registration(self) -> bool:
        """
        Open Create Account (keeping the job context). After a failed sign-in, SuccessFactors' in-page
        link carries a double-encoded session token and bounces back to Sign In - and registering from
        that broken session silently creates nothing - so restart from the job posting and use the
        fresh sign-in page's link instead.
        """
        print("  ➕ Opening 'Create an account' with the same credentials...")
        if await self._open_in_page_registration():
            return True
        link = self.page.locator(", ".join(REGISTER_LINKS))
        if (await link.count() and not await link.first.is_visible()
                and not re.search(r"job_req_id|jobreqid|/job/", self.job_url or "", re.I)):
            # e.g. HSBC's general Sign In page: new candidates can only register by applying to a job
            print("  ℹ️  This sign-in page offers no account creation - if you already have an account here, "
                  "check auth.password in settings.yaml.")
            return False
        if self.job_url:
            print("  ↩️  Sign-in session is stale; reopening the job posting for a clean registration...")
            await self.restart_application()
            if await self._open_in_page_registration():
                return True
        return False

    async def _open_in_page_registration(self) -> bool:
        if await self.click_first(REGISTER_LINKS):
            await self.wait_for_page_ready()
            return await self.is_registration_page()
        return False

    async def register(self) -> bool:
        """Fill the SuccessFactors Create Account form completely, accept privacy, submit registration."""
        a = self.auth
        p = self.resume_data.get("personal", {}) or {}
        email, password = a.get("email", ""), a.get("password", "")
        first = a.get("first_name") or p.get("first_name", "")
        last = a.get("last_name") or p.get("last_name", "")
        code = re.sub(r"\D", "", str(p.get("phone_country_code", "") or a.get("phone_country_code", "")))
        digits = re.sub(r"\D", "", str(a.get("phone") or p.get("phone", "")))
        national = digits[len(code):] if code and digits.startswith(code) and len(digits) > 10 else digits
        country = a.get("country") or (p.get("address", {}) or {}).get("country", "")

        if not await self.is_registration_page():
            return False
        print("  ⚡ Create Account page: filling registration fields...")
        for sel, value in (("#fbclc_userName", email), ("#fbclc_emailConf", email), ("#fbclc_pwd", password),
                           ("#fbclc_pwdConf", password), ("#fbclc_fName", first), ("#fbclc_lName", last),
                           ("#fbclc_phoneNumber", national)):
            loc = self.page.locator(sel)
            if await loc.count() and await loc.first.is_visible() and value:
                await loc.first.fill(value)
                await loc.first.evaluate("e => { e.dispatchEvent(new Event('change', {bubbles: true})); e.blur(); }")

        await self._select_best("#fbclc_ituCode", [a.get("phone_country_code", ""), f"{country} (+{code})", f"+{code}", country])
        await self._select_best("#fbclc_country", [country])
        notif = self.page.locator("#fbclc_emailEnabled")
        if await notif.count() and await notif.first.is_visible() and not await notif.first.is_checked():
            await notif.first.check()

        # Tenant-specific extra fields (salutation, pronouns, ...) go through the generic filler.
        await self.form_filler.fill_all_fields()
        await self.accept_privacy()

        if not await self.click_first(["#fbclc_createAccountButton", "button:has-text('Create Account')"], allow_submit=True):
            print("  [!] Create Account button not found.")
            return False
        await self.wait_for_page_ready()
        await asyncio.sleep(3)
        if await self.is_passcode_page() and not await self.enter_passcode():
            return False
        if await self.account_already_exists():
            return False
        if await self.is_auth_page():
            errors = await self.validation_errors()
            print(f"  [!] Create Account did not complete{': ' + '; '.join(errors[:3]) if errors else ''}")
            return False
        print("  ✓ Account created and signed in.")
        await self.accept_privacy()
        return True

    async def _passcode_box(self) -> Any:
        inputs = self.page.locator("input:not([type=hidden]):not([type=button]):not([type=submit]):not([type=checkbox])")
        for loc in (self.page.get_by_label(re.compile(r"passcode|one-time|verification code", re.I)).and_(inputs),
                    self.page.locator("input[id*='passcode' i], input[name*='passcode' i], input[autocomplete='one-time-code']")):
            try:
                if await loc.count() and await loc.first.is_visible():
                    return loc.first
            except Exception:
                continue
        return None

    async def is_passcode_page(self) -> bool:
        """Email check during Create Account / sign-in: 'We've sent a one-time password ... Passcode'."""
        try:
            text = await self.page.evaluate("() => document.body.innerText.slice(0, 3000)")
        except Exception:
            return False
        return bool(re.search(r"one-time (password|passcode|code)|\bpasscode\b", text, re.I)) and bool(await self._passcode_box())

    async def enter_passcode(self) -> bool:
        provider = getattr(self, "verification_code_provider", None)
        code = await provider() if provider else None
        if not code:
            code = utils.ask_user("The portal emailed you a one-time passcode. Enter it here (blank = type it in the browser yourself)")
        box = await self._passcode_box()
        if not code or not box:
            return False
        await box.fill(code)
        await self.click_first(["button:text-is('Continue')", "input[value='Continue']", "button:text-is('Verify')",
                                "button:text-is('Confirm')"], allow_submit=True)
        await self.wait_for_page_ready()
        await asyncio.sleep(2)
        if await self.is_passcode_page():
            errors = await self.validation_errors()
            print(f"  [!] Passcode not accepted{': ' + errors[0] if errors else ''}")
            return False
        print("  ✓ Email passcode accepted.")
        await self.accept_privacy()
        return True

    async def restart_application(self) -> None:
        """Return to the job posting and press Apply again."""
        from ..linkedin_scanner import LinkedInScanner
        scanner = LinkedInScanner(self.page)
        await scanner.navigate_to_job(self.job_url)
        await scanner.click_apply()
        if scanner.active_page:
            self.page = scanner.active_page
            self.form_filler.page = scanner.active_page
        await self.wait_for_page_ready()

    async def _select_best(self, selector: str, candidates: list) -> bool:
        loc = self.page.locator(selector)
        if not await loc.count() or not await loc.first.is_visible():
            return False
        options = await loc.first.evaluate("e => [...e.options].map(o => o.text.trim())")
        choice = best_option(options, [c for c in candidates if c])
        if choice:
            await loc.first.select_option(label=choice, timeout=3000)
            await loc.first.evaluate("e => e.dispatchEvent(new Event('change', {bubbles: true}))")
            return True
        return False

    async def accept_privacy(self) -> bool:
        """Open 'Read and accept the data privacy statement' and press Accept in its dialog."""
        accepted = False
        for sel in PRIVACY_TRIGGERS:
            trig = self.page.locator(sel)
            if not await trig.count() or not await trig.first.is_visible():
                continue
            try:
                await trig.first.click(timeout=5000)
                await asyncio.sleep(2)
                if await self.click_first(DIALOG_ACCEPT, allow_submit=True):
                    print("  ✓ Accepted the data privacy statement.")
                    accepted = True
                    await asyncio.sleep(1.5)
                break
            except Exception:
                continue
        if not accepted and await self.click_first(DIALOG_ACCEPT, allow_submit=True):
            accepted = True  # dialog already open
        return accepted

    # ------------------------------------------------------------------ application form
    async def fill_registration_fields(self) -> None:
        """Account fields embedded in a register-and-apply page (filled, never submitted here)."""
        a = self.auth
        for sel, value in (("#fbclc_userName", a.get("email", "")), ("#fbclc_emailConf", a.get("email", "")),
                           ("#fbclc_pwd", a.get("password", "")), ("#fbclc_pwdConf", a.get("password", ""))):
            loc = self.page.locator(sel)
            if value and await loc.count() and await loc.first.is_visible() and not await loc.first.input_value():
                await loc.first.fill(value)
                await loc.first.evaluate("e => { e.dispatchEvent(new Event('change', {bubbles: true})); e.blur(); }")

    async def fill_current_section(self) -> Dict[str, Any]:
        """Accept privacy, expand every section, upload the CV, add a row per job/degree, then fill."""
        if await self.is_combined_apply_page():
            await self.fill_registration_fields()
        await self.accept_privacy()
        await self.expand_sections()
        uploaded = await self.upload_resume()
        added = await self.add_repeating_rows()
        result = await self.form_filler.fill_all_fields()
        again = await self.add_repeating_rows()  # rows a late page refresh discarded
        if any(again.values()):
            print("  ↻ Some added rows disappeared after a page refresh - added them again and refilled.")
            second = await self.form_filler.fill_all_fields()
            result = {**second, "filled_count": result["filled_count"] + second["filled_count"],
                      "details": result["details"] + second["details"]}
            added = {k: added[k] + again[k] for k in added}
        result["rows_added"] = added
        result["resume_uploaded"] = uploaded
        self.filled_urls.add(self.page.url)
        return result

    async def expand_sections(self) -> None:
        """
        Use the page's own 'Expand all sections' (it is asynchronous - generic toggling right after it
        would collapse sections again), then open any section still closed.
        """
        if await self.click_first(["a:has-text('Expand all sections')"]):
            self.form_filler.auto_expand = False
            await asyncio.sleep(3)
            closed = self.page.locator(".rcmFormSectionTopBar[aria-expanded='false']")
            for i in range(await closed.count()):
                try:
                    await closed.nth(i).click(timeout=3000)
                    await asyncio.sleep(0.8)
                except Exception:
                    pass
        else:
            await self.form_filler.expand_all_accordions()

    async def start_from_job_description(self) -> bool:
        """
        SuccessFactors' own job page ('Let's begin! <title>' with Apply / Save Job / Return to List and no
        form fields): its Apply opens the sign-in, it submits nothing - so press it to start applying.
        """
        apply_btn = self.page.locator("#applyButton_top, #applyButton_bottom")
        if not await apply_btn.count() or not await apply_btn.first.is_visible():
            return False
        markers = self.page.locator("button:text-is('Save Job'), button:text-is('Return to List'), "
                                    "input[value='Save Job'], input[value='Return to List']")
        if not await markers.count():
            return False
        fields = await self.page.evaluate("""() => [...document.querySelectorAll(
            'input:not([type=hidden]):not([type=button]):not([type=submit]), select, textarea')]
            .filter(e => e.getBoundingClientRect().height > 0).length""")
        if fields:
            return False
        await apply_btn.first.click(timeout=5000)
        await self.wait_for_page_ready()
        return True

    @property
    def filled_urls(self) -> set:
        if not hasattr(self, "_filled_urls"):
            self._filled_urls = set()
        return self._filled_urls

    async def is_review_page(self) -> bool:
        """SuccessFactors applications are one long page ending in Submit: once filled, it's the review stage."""
        if self.page.url in self.filled_urls:
            return await self._submit_visible()
        return False

    async def _submit_visible(self) -> bool:
        btn = self.page.locator("[id$='_submitBtn'], [role='button']:text-is('Submit'), button:text-is('Submit'), "
                                "input[value='Submit'], a:text-is('Submit'), [id$='_applyBtn'], button:text-is('Apply'), "
                                "input[value='Apply'], [role='button']:text-is('Apply')")
        return bool(await btn.count()) and await btn.first.is_visible()

    async def go_to_next_section(self) -> bool:
        if await self._submit_visible() and self.page.url in self.filled_urls:
            return True  # single-page form: nothing to advance to; the review check takes over
        return await super().go_to_next_section()

    async def upload_resume(self) -> bool:
        """Attach additional.resume_file through the 'Upload a CV' dialog (or any CV file input)."""
        from pathlib import Path
        path = str((self.resume_data.get("additional", {}) or {}).get("resume_file", "") or "")
        if not path or not Path(path).exists():
            return False
        name = Path(path).name
        if await self._shows_file(name):
            return True  # this CV is already attached
        # SuccessFactors: the CV tile's '+' opens an 'Upload from Device' panel whose file input uploads on change.
        opener = self.page.locator(".attachActions .addAttachments, [id$='_attachIcon']")
        if await opener.count():
            await opener.first.click()
            await asyncio.sleep(2)
        inputs = self.page.locator("input[type='file']")
        if not await inputs.count():
            return False
        await inputs.first.set_input_files(path)
        await asyncio.sleep(4)
        await self.click_first(["[role='dialog'] button:has-text('Done')", "[role='dialog'] button:has-text('OK')"])
        ok = False
        for _ in range(10):  # the upload re-renders the page when it lands - rows added before that are lost
            if await self._shows_file(name):
                ok = True
                break
            await asyncio.sleep(2)
        await self.wait_for_page_ready()
        print(f"  📎 Resume upload {'done' if ok else 'attempted (not confirmed on page)'}: {name}")
        return ok

    async def _shows_file(self, name: str) -> bool:
        """The attachment tile wraps long names ('dummy_resume.' / 'pdf'), so compare without whitespace."""
        return await self.page.evaluate("(n) => document.body.innerText.replace(/\\s+/g, '').includes(n)",
                                        re.sub(r"\s+", "", name))

    async def add_repeating_rows(self) -> Dict[str, int]:
        """Press a section's 'Add' until there's one row per resume entry (jobs and degrees)."""
        app = self.settings.get("application", {}) or {}
        targets = {
            "experience": min(len(self.resume_data.get("experience", []) or []), app.get("max_experience_rows", 99)),
            "education": min(len(self.resume_data.get("education", []) or []), app.get("max_education_rows", 99)),
        }
        added = {"experience": 0, "education": 0}
        for kind, wanted in targets.items():
            count = await self._row_count(kind)
            for _ in range(wanted + 2):
                if count >= wanted or not await self._click_add(kind):
                    break
                new = count
                for _ in range(12):  # the row arrives after a server round trip; clicks meanwhile are ignored
                    await asyncio.sleep(1)
                    new = await self._row_count(kind)
                    if new > count:
                        break
                if new <= count:
                    print(f"  [!] 'Add' did not create a new {kind} row.")
                    break
                added[kind] += new - count
                count = new
        return added

    async def _row_count(self, kind: str) -> int:
        fields = await self.form_filler.scan_form_fields()
        key = r"company( name)?|employer( name)?" if kind == "experience" else r"institution|school|university|college"
        # labels may carry instructions: 'School (Please use the full name, for example, ...)'
        return len([f for f in fields if re.match(rf"({key})\b(?!\s*(location|country|city|state))", norm(f.label))])

    async def _click_add(self, kind: str) -> bool:
        """Click the section's add-row control (SuccessFactors labels it via a hidden aria-label)."""
        words = "experience|employment|work history" if kind == "experience" else "education|qualification"
        return await self.page.evaluate("""(words) => {
            const re = new RegExp(words, 'i');
            const vis = e => e.getBoundingClientRect().height > 0;
            const ctrls = [...document.querySelectorAll('.addRowButton, [title="Add new row"], a, button, [role=button]')]
                .filter(b => vis(b) && /^[^a-z0-9]*add\\b/i.test((b.innerText || b.value || b.getAttribute('title') || '').trim()));
            const heads = [...document.querySelectorAll('h1,h2,h3,h4,legend,[role=heading],.rcmFormSectionTopBar,button[aria-expanded]')]
                .filter(h => vis(h) && h.innerText.trim());
            // the section an Add belongs to: its own (hidden) aria-label, else the nearest section heading above it
            const sectionOf = b => {
                const hidden = [...b.querySelectorAll('[aria-label]')].map(x => x.getAttribute('aria-label')).join(' ');
                const own = (hidden + ' ' + (b.getAttribute('aria-label') || '')).trim();
                if (re.test(own)) return own;
                const above = heads.filter(h => h.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
                return above.length ? above[above.length - 1].innerText : own;
            };
            for (const b of ctrls) {
                if (re.test(sectionOf(b))) { b.click(); return true; }
            }
            return false;
        }""", words)
