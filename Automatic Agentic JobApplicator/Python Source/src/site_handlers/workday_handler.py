"""
Workday ATS specialist handler (*.myworkdayjobs.com).

Workday forms are built from custom widgets keyed by data-automation-id: button dropdowns
(listbox), 'prompt' search fields (type, Enter, pick from results), segmented MM/YYYY dates,
and repeatable cards ('Work Experience 1..n', 'Education 1..n') created with an Add button.
"""

import asyncio
import re
from typing import Any, Dict, List, Optional

from ..field_resolver import Answer, FieldInfo
from ..matching import best_option, norm
from .base_handler import BaseHandler

FIELD_SCAN_JS = r"""
() => {
  const vis = e => { if (!e) return false; const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const clean = s => (s || '').replace(/\s+/g, ' ').replace(/\*/g, '').trim();
  document.querySelectorAll('[data-aaj-wd]').forEach(e => e.removeAttribute('data-aaj-wd'));
  const out = [];
  let idx = 0;
  // Every Workday question lives in a formField-* container; group headings are 'Work Experience 2' etc.
  for (const c of document.querySelectorAll("[data-automation-id^='formField-']")) {
    if (!vis(c)) continue;
    const aid = c.getAttribute('data-automation-id').replace('formField-', '');
    let label = '';
    const lab = c.querySelector('label, legend');
    if (lab) label = clean(lab.innerText);
    if (!label) { const lb = c.getAttribute('aria-labelledby'); if (lb) label = clean((document.getElementById(lb) || {}).innerText); }
    let widget = 'text', value = '', options = [], checked = false;
    const multi = c.querySelector("[data-automation-id='multiSelectContainer'], [data-uxi-widget-type='multiselect'], [data-automation-id='searchBox']");
    const ddBtn = c.querySelector("button[aria-haspopup='listbox']");
    const dateMonth = c.querySelector("[data-automation-id='dateSectionMonth-input']");
    const dateYear = c.querySelector("[data-automation-id='dateSectionYear-input']");
    const radios = [...c.querySelectorAll("input[type='radio']")];
    const cbs = [...c.querySelectorAll("input[type='checkbox']")];
    const area = c.querySelector('textarea');
    const inp = c.querySelector("input[type='text'], input:not([type]), input[type='email'], input[type='tel'], input[type='number']");
    if (multi) {
      widget = 'prompt';
      value = clean([...c.querySelectorAll("[data-automation-id='selectedItem'], [data-automation-id='promptSelectionLabel']")].map(x => x.innerText).join(', '));
    } else if (ddBtn) {
      widget = 'dropdown';
      value = clean(ddBtn.innerText);
      if (/^select one$/i.test(value)) value = '';
    } else if (dateMonth || dateYear) {
      widget = dateMonth ? 'date-my' : 'date-y';
      value = clean((dateYear || {}).value || '');
      if (/^yyyy$/i.test(value)) value = '';
    } else if (radios.length) {
      widget = 'radio';
      options = radios.map(r => clean((c.querySelector(`label[for='${r.id}']`) || {}).innerText || r.value));
      value = options[radios.findIndex(r => r.checked)] || '';
    } else if (cbs.length) {
      widget = 'checkbox';
      checked = cbs[0].checked;
    } else if (area) {
      widget = 'textarea'; value = area.value;
    } else if (inp) {
      widget = 'text'; value = inp.value;
    } else continue;
    // card heading: nearest preceding h3/h4 inside the enclosing group (e.g. 'Work Experience 2')
    const heads = [];
    let p = c.parentElement;
    while (p && heads.length < 3) {
      const h = [...p.querySelectorAll("h2, h3, h4, [role='heading']")].filter(x => vis(x) && (x.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING)).pop();
      if (h && !heads.includes(clean(h.innerText))) heads.push(clean(h.innerText));
      p = p.parentElement;
    }
    c.setAttribute('data-aaj-wd', String(idx));
    out.push({idx: idx++, aid, label, widget, value, options, checked, headings: heads,
              required: !!c.querySelector('[aria-required=true], abbr[title=required]') || /\*/.test(lab ? lab.innerText : '')});
  }
  return out;
}
"""

SECTION_KINDS = [("experience", re.compile(r"work experience|experience|employment")),
                 ("education", re.compile(r"education")),
                 ("website", re.compile(r"website|social")),
                 ("language", re.compile(r"language"))]


class WorkdayHandler(BaseHandler):
    """Handles Workday application portals."""

    NEXT = "[data-automation-id='pageFooterNextButton'], [data-automation-id='bottom-navigation-next-button']"

    async def wait_for_page_ready(self, timeout: int = 20000) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
            await self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        try:
            await self.page.wait_for_selector("[data-automation-id='loadingAnimation'], [data-automation-id='wd-LoadingPanel']",
                                              state="detached", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(1.5)
        # Workday's transient 'Something went wrong - please refresh the page' screen: refresh like a person would.
        for _ in range(2):
            err = self.page.locator("text=Something went wrong")
            if not (await err.count() and await err.first.is_visible()):
                break
            print("  ↻ Workday asked to refresh the page; reloading...")
            await self.page.reload(wait_until="domcontentloaded")
            await asyncio.sleep(4)

    async def prepare(self) -> None:
        await self.click_first(["[data-automation-id='legalNoticeAcceptButton']"])

    async def get_current_section(self) -> str:
        loc = self.page.locator("[data-automation-id='progressBarActiveStep']")
        if await loc.count():
            txt = re.sub(r"current step \d+ of \d+", "", (await loc.first.inner_text())).strip()
            return " ".join(txt.split()) or "Workday step"
        return await super().get_current_section()

    # ------------------------------------------------------------------ authentication
    async def is_auth_page(self) -> bool:
        pw = self.page.locator("[data-automation-id='password']")
        return bool(await pw.count()) and await pw.first.is_visible()

    async def is_registration_page(self) -> bool:
        vp = self.page.locator("[data-automation-id='verifyPassword']")
        return bool(await vp.count()) and await vp.first.is_visible()

    async def _press(self, aid: str, aria: str) -> bool:
        """Workday buttons sit under a 'click_filter' overlay that receives the click."""
        for sel in (f"[data-automation-id='click_filter'][aria-label='{aria}']", f"[data-automation-id='{aid}']",
                    f"button:has-text('{aria}')"):
            loc = self.page.locator(sel)
            if await loc.count() and await loc.first.is_visible():
                await loc.first.click(timeout=5000)
                return True
        return False

    async def sign_in(self, email: str, password: str) -> bool:
        if await self.is_registration_page():
            # The apply flow opens on Create Account; switch to Sign In first.
            if not await self.click_first(["[data-automation-id='signInLink']", "button:has-text('Sign In')"]):
                return False
            await asyncio.sleep(2)
        print("  🔑 Workday sign-in with settings credentials...")
        await self.page.locator("[data-automation-id='email']").first.fill(email)
        await self.page.locator("[data-automation-id='password']").first.fill(password)
        await self._press("signInSubmitButton", "Sign In")
        await self.wait_for_page_ready()
        await asyncio.sleep(2)
        if await self.is_auth_page():
            errors = await self.validation_errors()
            print(f"  [!] Sign-in did not succeed{': ' + errors[0][:120] if errors else ''}")
            return False
        return True

    async def open_registration(self) -> bool:
        if await self.is_registration_page():
            return True
        await self.click_first(["[data-automation-id='createAccountLink']", "button:has-text('Create Account')",
                                "[data-automation-id='click_filter'][aria-label='Create Account']"])
        await asyncio.sleep(2)
        return await self.is_registration_page()

    async def register(self) -> bool:
        print("  ⚡ Workday Create Account...")
        email, password = self.auth.get("email", ""), self.auth.get("password", "")
        await self.page.locator("[data-automation-id='email']").first.fill(email)
        await self.page.locator("[data-automation-id='password']").first.fill(password)
        await self.page.locator("[data-automation-id='verifyPassword']").first.fill(password)
        consent = self.page.locator("[data-automation-id='createAccountCheckbox']")
        if await consent.count() and not await consent.first.is_checked():
            await consent.first.check()
        await self._press("createAccountSubmitButton", "Create Account")
        await self.wait_for_page_ready()
        await asyncio.sleep(3)
        if await self.account_already_exists():
            return False
        if await self.needs_email_verification():
            if not await self.complete_email_verification():
                return False
        if await self.is_auth_page():
            errors = await self.validation_errors()
            print(f"  [!] Create Account did not complete{': ' + '; '.join(e[:100] for e in errors[:3]) if errors else ''}")
            return False
        print("  ✓ Workday account created and signed in.")
        return True

    async def needs_email_verification(self) -> bool:
        text = await self.page.evaluate("() => document.body.innerText.slice(0, 3000)")
        return bool(re.search(r"verify (your )?(email|account)|verification email|activate your account", text, re.I))

    async def complete_email_verification(self) -> bool:
        """Open the verification link (provider hook for tests) or wait for the candidate to do it."""
        provider = getattr(self, "verification_link_provider", None)
        link = await provider() if provider else None
        if link:
            print("  ✉️  Opening the account verification link...")
            await self.page.goto(link, wait_until="domcontentloaded")
            await self.wait_for_page_ready()
            if self.job_url:
                await self.page.goto(self.job_url, wait_until="domcontentloaded")
                await self.wait_for_page_ready()
                from ..linkedin_scanner import LinkedInScanner
                await LinkedInScanner(self.page).click_apply()
                await self.wait_for_page_ready()
            return True
        print("  ✉️  Workday sent a verification email. Verify it, then continue (you'll be asked to sign in).")
        return False

    # ------------------------------------------------------------------ filling
    async def scan(self) -> List[Dict[str, Any]]:
        return await self.page.evaluate(FIELD_SCAN_JS)

    @staticmethod
    def _kind_and_card(headings: List[str]) -> tuple:
        for h in headings:
            hn = norm(h)
            for kind, pat in SECTION_KINDS:
                if pat.search(hn):
                    m = re.search(r"(\d+)\s*$", hn)
                    return kind, (int(m.group(1)) - 1 if m else 0)
        return "", 0

    CARD_FIELD = {"Work Experience": "formField-jobTitle", "Education": "formField-school"}

    async def _card_count(self, section: str) -> int:
        return await self.page.evaluate(
            "(aid) => [...document.querySelectorAll(`[data-automation-id='${aid}']`)].filter(e => e.getBoundingClientRect().height > 0).length",
            self.CARD_FIELD[section])

    async def ensure_cards(self) -> Dict[str, int]:
        """Click 'Add'/'Add Another' in Work Experience and Education until there's one card per resume entry."""
        app = self.settings.get("application", {}) or {}
        wanted = {
            "Work Experience": min(len(self.resume_data.get("experience", []) or []), app.get("max_experience_rows", 99)),
            "Education": min(len(self.resume_data.get("education", []) or []), app.get("max_education_rows", 99)),
        }
        added = {}
        for section, n in wanted.items():
            added[section] = 0
            group = self.page.locator(f"[role='group'][aria-labelledby*='{section.replace(' ', '-')}']")
            if not await group.count():
                continue
            while await self._card_count(section) < n and added[section] < n:
                btn = group.first.locator("button[data-automation-id='add-button'], button:has-text('Add Another'), button:text-is('Add')")
                if not await btn.count():
                    break
                await btn.last.click(timeout=5000)
                added[section] += 1
                await asyncio.sleep(1.5)
        return added

    async def wait_until_stable(self, rounds: int = 6) -> None:
        """Workday renders sections lazily: wait until the number of form fields stops changing."""
        last = -1
        for _ in range(rounds):
            n = await self.page.locator("[data-automation-id^='formField-']").count()
            if n == last and n > 0:
                return
            last = n
            await asyncio.sleep(1.5)

    async def fill_current_section(self) -> Dict[str, Any]:
        await self.wait_until_stable()
        cards = await self.ensure_cards()
        uploaded = await self.upload_resume()
        fields = await self.scan()
        filled = skipped = failed = kept = 0
        details = []
        resolver = self.form_filler.resolver
        resolver.has_country_code_field = any(re.search(r"country phone code|phone code", norm(f["label"])) for f in fields)
        seen: Dict[str, int] = {}
        for f in fields:
            kind, card = self._kind_and_card(f["headings"])
            if kind:  # headings carry no card number here: the n-th 'jobTitle' belongs to card n
                key = f"{kind}:{f['aid']}"
                card = seen.get(key, 0)
                seen[key] = card + 1
            ftype = {"dropdown": "select", "prompt": "select", "radio": "radio", "checkbox": "checkbox",
                     "textarea": "textarea", "date-my": "date", "date-y": "date"}.get(f["widget"], "text")
            info = FieldInfo(label=f["label"], field_type=ftype, options=f["options"],
                             section=kind if kind in ("experience", "education") else "", card=card)
            ans = self._workday_specific(f, kind, card) or resolver.resolve(info)
            if ans is None or (not ans.value and ans.check is None and ans.date is None):
                skipped += 1
                current_job_end = kind == "experience" and f["aid"] == "endDate" and \
                    card < len(self.resume_data.get("experience", []) or []) and self.resume_data["experience"][card].get("is_current")
                if f["required"] and not current_job_end and not (f["value"] or f["checked"]):
                    details.append({"label": f["label"], "status": "unanswered-required"})
                continue
            wrong_card_value = kind in ("experience", "education") and f["widget"] in ("prompt", "dropdown") and f["value"] \
                and not best_option([f["value"]], ans.candidates, ans.fallbacks)
            if (f["value"] or f["checked"]) and not self.form_filler.overwrite and not wrong_card_value:
                kept += 1
                details.append({"label": f["label"], "status": "kept", "value": f["value"]})
                continue
            try:
                result = await self.set_widget(f, ans)
            except Exception as e:
                result = f"error: {str(e).splitlines()[0][:120]}"
            if result is True:
                filled += 1
                details.append({"label": f["label"], "status": "filled", "value": ans.date.isoformat() if ans.date else ans.value[:60],
                                "rule": ans.rule, "section": kind, "card": card})
            else:
                failed += 1
                details.append({"label": f["label"], "status": "failed", "value": ans.value[:60], "rule": ans.rule,
                                "reason": result if isinstance(result, str) else "not set"})
        return {"filled_count": filled, "skipped_count": skipped, "failed_count": failed, "kept_count": kept,
                "details": details, "cards_added": cards, "resume_uploaded": uploaded}

    def _workday_specific(self, f: Dict[str, Any], kind: str, card: int) -> Optional[Answer]:
        """Workday fields keyed by automation id where the label alone is ambiguous."""
        aid = f["aid"]
        personal = self.resume_data.get("personal", {}) or {}
        if aid in ("source", "sourcePrompt") or re.search(r"how did you hear", norm(f["label"])):
            return Answer("LinkedIn", alternatives=["Job Board", "Social Media", "Website"], fallbacks=["Other"], rule="wd.source")
        if aid in ("candidateIsPreviousWorker", "previousWorker"):
            return Answer("No", rule="wd.previous_worker")
        if kind == "website" and personal.get("linkedin_url"):
            return Answer(personal["linkedin_url"], rule="wd.website")
        if kind == "language":
            langs = self.resume_data.get("languages", []) or []
            if card < len(langs):
                lang = langs[card]
                if re.search(r"\blanguage\b", norm(f["label"])):
                    return Answer(lang.get("language", ""), rule="wd.language")
                if re.search(r"fluent|native", norm(f["label"])):
                    return Answer("Yes" if norm(lang.get("proficiency", "")) == "native" else "No",
                                  check=norm(lang.get("proficiency", "")) == "native", rule="wd.native")
        if aid == "gpa":
            return None
        return None

    async def set_widget(self, f: Dict[str, Any], ans: Answer) -> Any:
        box = self.page.locator(f"[data-aaj-wd='{f['idx']}']")
        w = f["widget"]
        await self._close_popups()
        if w == "dropdown":
            btn = box.locator("button[aria-haspopup='listbox']").first
            try:
                await btn.click(timeout=4000)
            except Exception:
                await btn.focus()
                await btn.press("Enter")
            await asyncio.sleep(1.0)
            texts = await self._popup_options()
            choice = best_option(texts, ans.candidates, ans.fallbacks, proxies_before_partial=ans.proxies_first)
            if not choice:
                await self.page.keyboard.press("Escape")
                return f"no option for {ans.candidates[:3]} among {texts[:8]}"
            await self._click_popup_option(choice)
            await asyncio.sleep(0.5)
            return True
        if w == "prompt":
            return await self._fill_prompt(box, ans)
        if w in ("date-my", "date-y"):
            if not ans.date:
                return "not a date"
            first = "dateSectionMonth-input" if w == "date-my" else "dateSectionYear-input"
            await box.locator(f"[data-automation-id='{first}']").first.focus()  # inputs sit under display overlays
            digits = (f"{ans.date.month:02d}" if w == "date-my" else "") + f"{ans.date.year:04d}"
            await self.page.keyboard.type(digits, delay=80)
            await self.page.keyboard.press("Tab")
            return True
        if w == "radio":
            choice = best_option(f["options"], ans.candidates, ans.fallbacks)
            if not choice:
                return f"no option for {ans.candidates[:3]}"
            await box.locator("input[type='radio']").nth(f["options"].index(choice)).check(force=True)
            return True
        if w == "checkbox":
            want = ans.check if ans.check is not None else norm(ans.value) in ("yes", "true")
            cb = box.locator("input[type='checkbox']").first
            if await cb.is_checked() != want:
                await cb.click(force=True)
            return True
        target = box.locator("textarea").first if w == "textarea" else box.locator("input").first
        await target.fill(ans.date.strftime("%d/%m/%Y") if ans.date else ans.value)
        await target.press("Tab")
        return True

    # The open popup list (not selections rendered inside fields). Lists are virtualized, so collecting
    # all options means scrolling the list; 'step' scrolls one page and reports whether it moved.
    POPUP_STEP_JS = """(step) => {
        const lists = [...document.querySelectorAll('[role=listbox]')].filter(l => l.getBoundingClientRect().height > 0
            && !l.closest("[data-automation-id^='formField-']"));
        const list = lists[lists.length - 1];
        if (!list) return {texts: [], moved: false};
        const texts = [...list.querySelectorAll('[role=option]')].map(o => o.innerText.trim()).filter(Boolean);
        let sc = list; while (sc && sc.scrollHeight <= sc.clientHeight + 2) sc = sc.parentElement;
        let moved = false;
        if (step && sc) { const before = sc.scrollTop; sc.scrollTop += sc.clientHeight * 0.8; moved = sc.scrollTop > before; }
        return {texts, moved};
    }"""

    async def _popup_options(self, full: bool = True) -> List[str]:
        seen: List[str] = []
        for _ in range(40 if full else 1):
            r = await self.page.evaluate(self.POPUP_STEP_JS, full)
            for t in r["texts"]:
                if t not in seen:
                    seen.append(t)
            if not r["moved"]:
                break
            await asyncio.sleep(0.15)
        return seen

    async def _click_popup_option(self, text: str) -> None:
        """Scroll the (virtualized) list from the top until the option is rendered, then click it."""
        found = await self.page.evaluate("""async (text) => {
            const lists = [...document.querySelectorAll('[role=listbox]')].filter(l => l.getBoundingClientRect().height > 0
                && !l.closest("[data-automation-id^='formField-']"));
            const list = lists[lists.length - 1];
            if (!list) return false;
            let sc = list; while (sc && sc.scrollHeight <= sc.clientHeight + 2) sc = sc.parentElement;
            if (sc) sc.scrollTop = 0;
            for (let i = 0; i < 60; i++) {
                const o = [...list.querySelectorAll('[role=option]')].find(x => x.innerText.trim() === text);
                if (o) { o.scrollIntoView({block: 'center'}); o.click(); return true; }
                if (!sc) break;
                const before = sc.scrollTop; sc.scrollTop += sc.clientHeight * 0.8;
                await new Promise(r => setTimeout(r, 120));
                if (sc.scrollTop === before) break;
            }
            return false;
        }""", text)
        if not found:
            raise RuntimeError(f"option '{text}' not found in list")

    async def _close_popups(self) -> None:
        """Workday multi-select prompts stay open after a pick; click blank page margin to close them."""
        await self.page.keyboard.press("Escape")
        try:
            await self.page.mouse.click(8, 400)
        except Exception:
            pass
        await asyncio.sleep(0.3)

    async def _prompt_selection(self, box: Any) -> List[str]:
        sel = box.locator("[data-automation-id='selectedItem'] [data-automation-id='promptOption']")
        return [((await sel.nth(i).get_attribute("data-automation-label")) or (await sel.nth(i).inner_text())).strip()
                for i in range(await sel.count())]

    async def _clear_prompt(self, box: Any) -> None:
        charms = box.locator("[data-automation-id='DELETE_charm']")
        for _ in range(await charms.count()):
            await charms.first.click(force=True, timeout=3000)
            await asyncio.sleep(0.6)

    def _acceptable(self, value: str, ans: Answer) -> bool:
        return bool(best_option([value], ans.candidates, ans.fallbacks, proxies_before_partial=ans.proxies_first))

    async def _fill_prompt(self, box: Any, ans: Answer) -> Any:
        """
        Workday 'prompt' (search) widget: type a few letters, press Enter, choose the best result.
        Tries the value and each alias, then the nearest proxies (e.g. Computer Science for Analytics).
        Workday auto-selects when a search has a single result, so every selection is verified and
        anything that doesn't match the candidate is cleared again.
        """
        inp = box.locator("input").first
        current = await self._prompt_selection(box)
        if current and all(self._acceptable(v, ans) for v in current):
            return True
        if current:
            print(f"  ✗ Clearing mismatched selection {current} (wanted {ans.value})")
            await self._clear_prompt(box)
        tried: List[str] = []
        for term in self._prompt_terms(ans):
            await self._close_popups()
            await inp.fill("")  # fill/press need no click, so an open popup elsewhere can't block them
            await inp.press_sequentially(term, delay=40)
            await inp.press("Enter")
            await asyncio.sleep(2.0)
            auto = await self._prompt_selection(box)
            if auto:  # single search hit was auto-selected
                if all(self._acceptable(v, ans) for v in auto):
                    await self._close_popups()
                    return True
                tried.append(f"{term}->auto:{auto[0][:30]}(rejected)")
                await self._clear_prompt(box)
                continue
            texts = await self._popup_options(full=False)
            tried.append(f"{term}->{len(texts)}")
            choice = best_option(texts, ans.candidates, ans.fallbacks, proxies_before_partial=ans.proxies_first) if texts else None
            if choice:
                await self._click_popup_option(choice)
                await asyncio.sleep(1.0)
                await self._close_popups()
                if await self._prompt_selection(box):
                    return True
                tried.append(f"{choice[:30]}(did not stick)")
        await self._close_popups()
        return f"no search result matched (tried {', '.join(tried[:5])})"

    COMMON_WORDS = {"of", "and", "the", "in", "for", "at", "university", "institute", "college", "school", "technology",
                    "science", "sciences", "management", "engineering", "studies", "main", "campus", "national", "state",
                    "agriculture", "agricultural", "business", "medical", "research", "international", "global", "indian",
                    "american", "graduate", "academy", "polytechnic", "applied", "arts", "commerce", "education"}

    @classmethod
    def _prompt_terms(cls, ans: Answer) -> List[str]:
        """Search terms a person would type: full name, its most distinctive word, aliases - then the proxies."""
        def terms_for(values: List[str], limit: int) -> List[str]:
            out: List[str] = []
            for c in values:
                words = re.sub(r"[^\w\s]", " ", c).split()
                distinctive = sorted((w for w in words if w.lower() not in cls.COMMON_WORDS and len(w) > 2), key=len, reverse=True)
                for t in (" ".join(words), distinctive[0] if distinctive else "", " ".join(words[:2]) if len(words) > 2 else ""):
                    if len(t) >= 2 and t.lower() not in [x.lower() for x in out]:
                        out.append(t)
            return out[:limit]
        primary = terms_for(ans.candidates, 9)
        proxies = [t for t in terms_for(ans.fallbacks, 5) if t.lower() not in [x.lower() for x in primary]]
        return primary + proxies

    async def upload_resume(self) -> bool:
        from pathlib import Path
        path = str((self.resume_data.get("additional", {}) or {}).get("resume_file", "") or "")
        inp = self.page.locator("input[data-automation-id='file-upload-input-ref'], input[type='file']")
        if not path or not Path(path).exists() or not await inp.count():
            return False
        if await self.page.locator(f"text={Path(path).name}").count():
            return True
        await inp.first.set_input_files(path)
        await asyncio.sleep(4)
        return bool(await self.page.locator(f"text={Path(path).name}").count())

    # ------------------------------------------------------------------ navigation
    async def go_to_next_section(self) -> bool:
        btn = self.page.locator(self.NEXT)
        if not await btn.count() or not await btn.first.is_visible():
            return False
        text = (await btn.first.inner_text()).strip()
        if re.search(r"submit", text, re.I):
            return False  # final page: never submit
        await btn.first.click()
        await self.wait_for_page_ready()
        return True

    async def is_review_page(self) -> bool:
        active = (await self.get_current_section()).lower()
        if active.startswith("review"):
            return True
        btn = self.page.locator(self.NEXT)
        if await btn.count() and re.search(r"submit", (await btn.first.inner_text()), re.I):
            return True
        return False

    async def validation_errors(self) -> List[str]:
        errs = await self.page.evaluate("""() => [...document.querySelectorAll("[data-automation-id='errorMessage'], [data-automation-id='inputAlert'], [role='alert']")]
            .filter(e => e.getBoundingClientRect().height > 0).map(e => e.innerText.trim()).filter(Boolean).slice(0, 10)""")
        return errs or await super().validation_errors()
