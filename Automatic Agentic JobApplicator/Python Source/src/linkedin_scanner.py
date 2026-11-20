"""
LinkedIn and ATS job page scanner and application interaction controller using Playwright.
"""

import asyncio
import re
from typing import Any, Dict, Optional

from .site_handlers.base_handler import detect_ats

TITLE_SELECTORS = [
    "h1.job-details-jobs-unified-top-card__job-title", "h1.top-card-layout__title",
    "[data-automation-id='jobPostingHeader']", "[data-careersite-propertyid='title']", "span[itemprop='title']",
    "[id*='reqTitle']", ".titlepage",
    ".job-details__title", "h1[class*='jobTitle']", "h1[class*='title']", "h1",
]
COMPANY_SELECTORS = [
    ".job-details-jobs-unified-top-card__company-name a", ".topcard__flavor--black-link",
    "div.jobs-unified-top-card__company-name", "span.company-name", "[data-careersite-propertyid='company']",
]
LOCATION_SELECTORS = [
    ".job-details-jobs-unified-top-card__primary-description-container .tvm__text",
    ".job-details-jobs-unified-top-card__bullet", ".topcard__flavor--bullet",
    "[data-automation-id='locations']", "[data-careersite-propertyid='location']", "[data-careersite-propertyid='city']",
    ".jobGeoLocation", "span[itemprop='addressLocality']", ".job-details__subtitle", "span.job-location",
    "[class*='job-location']", "span[class*='location']", "div[class*='location']", "p[class*='location']",
]

# Visible text of an element that starts an application.
APPLY_TEXT = re.compile(r"^\s*(easy apply|apply|apply now|apply for this job|apply online|apply to job|i'm interested)\s*$", re.I)
# Choices offered after pressing Apply (dropdown menu or dialog), most preferred first.
CHOICE_PREFERENCE = [r"apply manually", r"^\s*apply now\s*$", r"^\s*apply\s*$", r"apply"]
CHOICE_EXCLUDE = re.compile(r"linkedin|indeed|seek|facebook|google|autofill|resume|last application|help|job alert|share", re.I)
# Cookie-consent layers sit over the page and swallow clicks. Decline/close is preferred over accept.
CONSENT_BUTTONS = [
    "#onetrust-reject-all-handler", "#onetrust-banner-sdk .onetrust-close-btn-handler", "#onetrust-accept-btn-handler",
    "button:text-is('Reject All Cookies')", "button:text-is('Reject all cookies')", "button:text-is('Reject All')",
    "button:text-is('Reject all')", "button:text-is('Decline')", "button:text-is('Necessary cookies only')",
    "button:text-is('Accept All Cookies')", "button:text-is('Accept all cookies')", "button:text-is('Accept Cookies')",
    "button:text-is('Accept All')", "button:text-is('Accept all')",
]


class LinkedInScanner:
    """Navigates job postings, parses details, and triggers application flows."""

    def __init__(self, page: Any):
        self.page = page
        self.active_page: Optional[Any] = None

    async def navigate_to_job(self, url: str) -> Dict[str, Any]:
        """Navigate to a job URL and extract metadata."""
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(1.5)
        return await self.extract_job_info()

    async def _first_text(self, selectors) -> str:
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                for i in range(min(await loc.count(), 3)):
                    if await loc.nth(i).is_visible():
                        t = re.sub(r"\s+", " ", (await loc.nth(i).inner_text()).strip())
                        t = re.sub(r"^(locations?|job location)\s*", "", t, flags=re.I)
                        if t:
                            return t[:160]
            except Exception:
                pass
        return ""

    async def extract_job_info(self) -> Dict[str, Any]:
        """Scrape job title, company, location and Easy Apply availability (LinkedIn or ATS page)."""
        info = {
            "title": await self._first_text(TITLE_SELECTORS) or "Unknown Title",
            "company": await self._first_text(COMPANY_SELECTORS) or "Unknown Company",
            "location": await self._first_text(LOCATION_SELECTORS),
            "url": self.page.url,
            "is_easy_apply": False,
        }
        try:
            easy = self.page.locator(".jobs-apply-button")
            if await easy.count() and "easy apply" in (await easy.first.inner_text()).lower():
                info["is_easy_apply"] = True
        except Exception:
            pass
        return info

    async def _choose_from_open_menu(self) -> Optional[str]:
        """If an apply menu/dialog is open, click the manual-apply choice. Returns the clicked text."""
        items = await self.page.evaluate("""() => {
            const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
            const scopes = [...document.querySelectorAll("[role=menu], .dropdown-menu, [role=dialog], [data-automation-id='wd-popup-frame']")].filter(vis);
            const out = [];
            scopes.forEach((s, si) => s.querySelectorAll("a, button, [role=menuitem], [role=button]").forEach((e, ei) => {
                if (!vis(e)) return;
                const t = (e.innerText || e.getAttribute('aria-label') || '').trim();
                if (t) { e.setAttribute('data-aaj-choice', si + '-' + ei); out.push({key: si + '-' + ei, text: t}); }
            }));
            return out;
        }""")
        for pref in CHOICE_PREFERENCE:
            for it in items:
                if re.search(pref, it["text"], re.I) and not CHOICE_EXCLUDE.search(it["text"]):
                    await self._click_maybe_popup(self.page.locator(f"[data-aaj-choice='{it['key']}']").first)
                    return it["text"]
        return None

    async def _click_maybe_popup(self, loc: Any) -> None:
        """Click, following a new tab if one opens."""
        try:
            async with self.page.context.expect_page(timeout=5000) as new_page_info:
                await loc.click(timeout=5000)
            new_page = await new_page_info.value
            await new_page.wait_for_load_state("domcontentloaded")
            self.active_page = new_page
        except Exception:
            pass
        await asyncio.sleep(2.5)

    async def dismiss_consent(self) -> bool:
        """Close a cookie-consent banner (e.g. OneTrust) so it can't swallow the Apply click."""
        for sel in CONSENT_BUTTONS:
            loc = self.page.locator(sel)
            try:
                if await loc.count() and await loc.first.is_visible():
                    await loc.first.click(timeout=3000)
                    await asyncio.sleep(1)
                    return True
            except Exception:
                continue
        return False

    async def click_apply(self) -> str:
        """
        Press Apply and follow through: dropdown menus ('Apply now' -> 'Apply Now' / 'with LinkedIn'),
        chooser dialogs (Workday 'Apply Manually'), new tabs and LinkedIn Easy Apply.
        Returns 'easy_apply', 'external:<url>', 'menu_unresolved' or 'not_found'.
        """
        start_url = self.page.url
        await self.dismiss_consent()
        if await self._choose_from_open_menu():
            return f"external:{(self.active_page or self.page).url}"
        if await self.is_application_form():
            # The page already is the application (e.g. SuccessFactors register-and-apply pages): its
            # 'Apply' button submits it, so it must never be pressed here.
            return "already_on_form"

        candidates = await self.page.evaluate("""(src) => {
            const re = new RegExp(src, 'i');
            const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
            const out = [];
            const fieldsIn = f => [...f.querySelectorAll('input:not([type=hidden]):not([type=search]), select, textarea')].filter(vis).length;
            document.querySelectorAll("button, a, [role=button], input[type=button], input[type=submit]").forEach((e, i) => {
                if (!vis(e) || e.closest('nav, footer')) return;
                const form = e.closest('form');
                if (form && fieldsIn(form) >= 3) return;  // a form's own submit button, not a 'start applying' link
                const t = (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().split('\\n')[0];
                if (re.test(t)) { e.setAttribute('data-aaj-apply', String(i)); out.push({key: String(i), text: t,
                    menu: e.getAttribute('aria-haspopup') === 'true' || e.classList.contains('dropdown-toggle') || e.getAttribute('data-toggle') === 'dropdown'}); }
            });
            return out;
        }""", APPLY_TEXT.pattern)
        if not candidates:
            return "not_found"

        # Prefer LinkedIn's own button, then plain 'Apply now'/'Apply', then the rest.
        candidates.sort(key=lambda c: (0 if "easy apply" in c["text"].lower() else 1 if re.fullmatch(r"apply( now)?", c["text"].strip(), re.I) else 2))
        btn = self.page.locator(f"[data-aaj-apply='{candidates[0]['key']}']").first
        await btn.scroll_into_view_if_needed()
        await self._click_maybe_popup(btn)

        # A menu or chooser dialog may have opened instead of navigating.
        choice = await self._choose_from_open_menu()
        if choice:
            print(f"  ⚡ Apply menu opened - chose '{choice}'.")
        elif candidates[0]["menu"] and self.page.url == start_url and not self.active_page:
            return "menu_unresolved"
        elif self.page.url == start_url and not self.active_page:
            href = await btn.get_attribute("href") or ""
            if href.startswith("http") and href.split("#")[0] != start_url.split("#")[0]:
                # something on the page swallowed the click - an Apply link can simply be followed
                print("  ↪️  Apply link didn't respond to the click - following it directly.")
                await self.page.goto(href, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(2.5)

        page = self.active_page or self.page
        if "linkedin.com/jobs" in page.url and await page.query_selector("div.jobs-easy-apply-modal, [data-test-modal-id='easy-apply-modal']"):
            return "easy_apply"
        return f"external:{page.url}"

    async def is_application_form(self) -> bool:
        """
        True when the page already holds an application/registration form (not a job description).
        A job page that also carries a sign-up form ('Join our talent community') still has an 'Apply'
        link leading elsewhere - that page is not the application.
        """
        return await self.page.evaluate("""() => {
            const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
            const applyLink = [...document.querySelectorAll('a[href]')].some(a => vis(a) && !a.closest('nav, footer, form')
                && /^\\s*apply( now| for this (job|position|role)| online)?\\s*$/i.test(a.innerText || '')
                && !/^\\s*(#|javascript:)/i.test(a.getAttribute('href')));
            if (applyLink) return false;
            const fields = [...document.querySelectorAll('input:not([type=hidden]):not([type=search]):not([type=submit]):not([type=button]), select, textarea')]
                .filter(e => vis(e) && !e.closest('header, nav, [role=search], footer'));
            return fields.some(e => e.type === 'password') || fields.length >= 5;
        }""")

    async def detect_external_site(self) -> str:
        """Identify the target Applicant Tracking System (ATS) for the current page."""
        return await detect_ats(self.page)

    async def is_login_required(self) -> bool:
        """Detect whether current page demands authentication."""
        try:
            pw = self.page.locator("input[type='password']")
            for i in range(await pw.count()):
                if await pw.nth(i).is_visible():
                    return True
        except Exception:
            pass
        return False
