"""
Base handler defining common ATS interactions, the ATS detection registry and the handler factory.
"""

import asyncio
import re
from typing import Any, Dict, List, Optional


# Single source of truth for ATS detection (URL patterns), used by the scanner and the factory.
ATS_URL_PATTERNS = [
    ("workday", r"myworkdayjobs\.com|myworkday\.com|workdayjobs\.com|wd\d+\.myworkdaysite"),
    ("successfactors", r"successfactors\.(com|eu)|jobs2web|sapsf|careers\.anz\.com"),
    ("oracle", r"oraclecloud\.com|/hcmui/candidateexperience"),
    ("taleo", r"taleo\.net|/careersection/"),
    ("greenhouse", r"greenhouse\.io"),
    ("lever", r"lever\.co"),
    ("icims", r"icims\.com"),
    ("smartrecruiters", r"smartrecruiters\.com"),
    ("linkedin", r"linkedin\.com"),
]

# Navigation must never press a final submission button.
SUBMIT_WORDS = re.compile(r"\b(submit|send application|finish application|complete application)\b|^\s*apply( now)?\s*$", re.I)


def detect_ats_from_url(url: str) -> str:
    u = (url or "").lower()
    for key, pattern in ATS_URL_PATTERNS:
        if re.search(pattern, u):
            return key
    return "generic"


async def detect_ats(page: Any) -> str:
    """URL first; then DOM fingerprints for ATSs hosted on company domains."""
    key = detect_ats_from_url(page.url)
    if key != "generic":
        return key
    fingerprints = [
        ("successfactors", ".applyOption, [data-careersite-propertyid], #fbclc_userName"),
        ("workday", "[data-automation-id='jobPostingHeader'], [data-automation-id='applyFlowPage']"),
        ("taleo", "#requisitionDescriptionInterface, form[name='ftlform']"),
    ]
    for k, sel in fingerprints:
        try:
            if await page.query_selector(sel):
                return k
        except Exception:
            pass
    return "generic"


class BaseHandler:
    """Base class for generic Applicant Tracking System (ATS) automation."""

    def __init__(self, page: Any, form_filler: Any, resume_data: Dict[str, Any], settings: Dict[str, Any]):
        self.page = page
        self.form_filler = form_filler
        self.resume_data = resume_data
        self.settings = settings
        self.auth = settings.get("auth", {}) or {}
        self.job_url = ""  # set by the orchestrator; lets handlers restart an application after registering
        self.restart_after_auth = False

    # ------------------------------------------------------------------ page state
    async def wait_for_page_ready(self, timeout: int = 15000) -> None:
        """Wait for network and DOM stabilization."""
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
            await self.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(1.0)

    async def get_current_section(self) -> str:
        """Identify the active wizard stage name."""
        for sel in ("[aria-current='step']", "[aria-current='page'] ", ".current-step", "[aria-selected='true']", "h1", "h2"):
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    txt = (await el.inner_text()).strip()
                    if txt:
                        return txt.splitlines()[-1][:80]
            except Exception:
                pass
        return "Current Section"

    async def page_signature(self) -> str:
        """Cheap fingerprint used to tell whether navigation actually moved the form forward."""
        try:
            return f"{self.page.url}|{await self.get_current_section()}"
        except Exception:
            return self.page.url

    async def validation_errors(self) -> List[str]:
        """Visible validation / error messages on the page."""
        try:
            return await self.page.evaluate("""() => [...document.querySelectorAll(
                '[role=alert], .error, .errorMessage, .error-message, .alert-danger, [class*=error i][class*=message i], [aria-invalid=true]')]
                .filter(e => e.getBoundingClientRect().height > 0)
                .map(e => (e.innerText || e.getAttribute('aria-label') || '').trim()).filter(t => t && t.length < 300).slice(0, 10)""")
        except Exception:
            return []

    # ------------------------------------------------------------------ filling & navigation
    async def fill_current_section(self) -> Dict[str, Any]:
        """Delegate input population to FormFiller engine."""
        return await self.form_filler.fill_all_fields()

    async def click_first(self, selectors: List[str], root: Any = None, allow_submit: bool = False) -> Optional[str]:
        """Click the first visible+enabled match; refuses anything that looks like final submission."""
        root = root or self.page
        for sel in selectors:
            try:
                loc = root.locator(sel)
                for i in range(min(await loc.count(), 5)):
                    btn = loc.nth(i)
                    if not (await btn.is_visible() and await btn.is_enabled()):
                        continue
                    text = ((await btn.inner_text()) or await btn.get_attribute("value") or "").strip()
                    if not allow_submit and SUBMIT_WORDS.search(text):
                        continue
                    await btn.click(timeout=5000)
                    return text or sel
            except Exception:
                continue
        return None

    async def go_to_next_section(self) -> bool:
        """Locate and click standard Next/Continue navigation buttons (never Submit)."""
        clicked = await self.click_first([
            "button:has-text('Save and Continue')", "button:has-text('Save & Continue')",
            "button:has-text('Continue')", "button:has-text('Next')",
            "input[type='submit'][value*='Next']", "input[type='button'][value*='Next']",
            "a[role='button']:has-text('Next')", "button[aria-label*='Next']",
        ])
        if clicked:
            await self.wait_for_page_ready()
        return bool(clicked)

    async def is_review_page(self) -> bool:
        """Final confirmation stage: a visible Submit-type button or a Review heading."""
        try:
            return await self.page.evaluate("""() => {
                const vis = e => e.getBoundingClientRect().height > 0;
                const btns = [...document.querySelectorAll('button, input[type=submit], input[type=button], [role=button]')].filter(vis);
                if (btns.some(b => /^\\s*(submit|submit application|send application)\\s*$/i.test(b.innerText || b.value || ''))) return true;
                const hs = [...document.querySelectorAll('h1,h2,h3,[aria-current=step]')].filter(vis);
                return hs.some(h => /^\\s*(review( and submit| & submit| your application)?|summary)\\s*$/i.test(h.innerText || ''));
            }""")
        except Exception:
            return False

    # ------------------------------------------------------------------ authentication
    async def is_auth_page(self) -> bool:
        try:
            pw = self.page.locator("input[type='password']")
            for i in range(await pw.count()):
                if await pw.nth(i).is_visible():
                    return True
        except Exception:
            pass
        return False

    async def handle_authentication_if_present(self) -> str:
        """
        Sign in with settings credentials; if that fails and auto_create_account is on, register
        with the same credentials. Returns 'none' (no login page), 'signed_in', 'created' or 'failed'.
        """
        if not await self.is_auth_page():
            return "none"
        email, password = self.auth.get("email", ""), self.auth.get("password", "")
        if not email or not password or not self.auth.get("auto_login", True):
            return "failed"

        if await self.sign_in(email, password):
            return "signed_in"
        if self.auth.get("auto_create_account", False) and await self.open_registration():
            return "created" if await self.register() else "failed"
        return "failed"

    async def is_registration_page(self) -> bool:
        pw = self.page.locator("input[type='password']")
        visible = [i for i in range(await pw.count()) if await pw.nth(i).is_visible()]
        return len(visible) >= 2

    async def sign_in(self, email: str, password: str) -> bool:
        user = self.page.locator("input[type='email'], input[autocomplete='username'], input[name*='user' i], "
                                 "input[id*='user' i], input[name*='email' i], input[id*='email' i]")
        filled = False
        for i in range(await user.count()):
            if await user.nth(i).is_visible():
                await user.nth(i).fill(email)
                filled = True
                break
        pw = self.page.locator("input[type='password']:visible").first
        if not filled or not await pw.count():
            return False
        await pw.fill(password)
        await self.click_first(["button:has-text('Sign In')", "button:has-text('Log In')", "button:has-text('Login')",
                                "input[type='submit'][value*='Sign' i]", "input[type='submit'][value*='Log' i]"], allow_submit=True)
        await self.wait_for_page_ready()
        await asyncio.sleep(2)
        return not await self.is_auth_page()

    async def open_registration(self) -> bool:
        clicked = await self.click_first(["a:has-text('Create an account')", "a:has-text('Create Account')",
                                          "button:has-text('Create Account')", "a:has-text('Register')",
                                          "a:has-text('Sign Up')", "a:has-text('New user')", "button:has-text('Sign Up')"])
        if clicked:
            await self.wait_for_page_ready()
        return bool(clicked)

    async def register(self) -> bool:
        """Generic registration: email/confirm-email, password/confirm, then profile fields and consent."""
        email, password = self.auth.get("email", ""), self.auth.get("password", "")
        await self.fill_auth_basics(email, password)
        await self.form_filler.fill_all_fields()
        clicked = await self.click_first(["button:has-text('Create Account')", "button:has-text('Register')",
                                          "button:has-text('Sign Up')", "input[type='submit'][value*='Create' i]",
                                          "input[type='submit'][value*='Register' i]"], allow_submit=True)
        await self.wait_for_page_ready()
        await asyncio.sleep(2)
        if await self.account_already_exists():
            return False
        return bool(clicked) and not await self.is_auth_page()

    async def account_already_exists(self) -> bool:
        """Registration refused because the email/phone is already registered -> the saved password is wrong."""
        try:
            text = await self.page.evaluate("() => document.body.innerText.slice(0, 4000)")
        except Exception:
            return False
        # prompts such as 'Already registered? Please sign in' are links on every registration page, not errors
        m = re.search(r"[^\n]*(account already exists|already (an|have an) account(?!\s*\?)|already registered(?!\s*\?)"
                      r"|email address is already in use)[^\n]*(\n[^\n]+)?", text, re.I)
        if m:
            print(f"  [!] {' '.join(m.group(0).split())[:200]}")
            print("  [!] An account already exists, but signing in with settings.yaml auth.email/auth.password failed."
                  " Update auth.password in settings.yaml (or reset it on the portal) and run again.")
            return True
        return False

    async def fill_auth_basics(self, email: str, password: str) -> None:
        for sel in ("input[type='password']",):
            loc = self.page.locator(sel)
            for i in range(await loc.count()):
                if await loc.nth(i).is_visible():
                    await loc.nth(i).fill(password)
        emails = self.page.locator("input[type='email'], input[name*='mail' i], input[id*='mail' i], input[id*='userName' i]")
        for i in range(await emails.count()):
            if await emails.nth(i).is_visible() and (await emails.nth(i).get_attribute("type") or "text") != "checkbox":
                await emails.nth(i).fill(email)

    async def captcha_visible(self) -> bool:
        """An 'I'm not a robot' check. The tool never solves these - the candidate does."""
        loc = self.page.locator("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[title*='captcha' i], "
                                ".g-recaptcha, .h-captcha, [class*='captcha' i]")
        for i in range(min(await loc.count(), 6)):
            try:
                if await loc.nth(i).is_visible():
                    return True
            except Exception:
                continue
        return False

    async def start_from_job_description(self) -> bool:
        """Portals with their own job page in front of the application press its Apply here (see SuccessFactors)."""
        return False

    async def prepare(self) -> None:
        """Dismiss cookie banners and similar overlays."""
        await self.click_first(["button:has-text('Accept Cookies')", "button:has-text('Accept All')",
                                "button:has-text('Accept all cookies')", "#onetrust-accept-btn-handler"])


def get_handler(page: Any, form_filler: Any, resume_data: Dict[str, Any], settings: Dict[str, Any],
                ats: Optional[str] = None) -> BaseHandler:
    """Factory creating specialized ATS handlers for a detected ATS key (or the page URL)."""
    from .oracle_handler import OracleHandler
    from .successfactors_handler import SuccessFactorsHandler
    from .taleo_handler import TaleoHandler
    from .workday_handler import WorkdayHandler

    registry = {"workday": WorkdayHandler, "oracle": OracleHandler, "successfactors": SuccessFactorsHandler,
                "taleo": TaleoHandler}
    key = ats or detect_ats_from_url(page.url)
    return registry.get(key, BaseHandler)(page, form_filler, resume_data, settings)
