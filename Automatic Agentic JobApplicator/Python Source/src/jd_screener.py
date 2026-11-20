"""
Job-description screening: years of experience asked for, whether the posting still accepts
applications, and the profile fit LinkedIn shows a signed-in member.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
         "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
         "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "twenty five": 25}
NUM = r"(\d{1,2}|" + "|".join(sorted(WORDS, key=len, reverse=True)) + r")"
YEARS = r"(?:\+\s*)?(?:years?|yrs?)"
EXPERIENCE_NEAR = re.compile(r"experience|exp\b|background|track record|working in|work(?:ing)? with|leadership|managing", re.I)

CLOSED = re.compile(r"no longer accepting applications|applications? (are|is) closed|this job (has )?expired|"
                    r"job is no longer available|position has been filled|no longer available", re.I)
ALREADY_APPLIED = re.compile(r"\bapplied\s+(on|\d+\s+\w+\s+ago)\b|you applied|application submitted", re.I)


@dataclass
class YearsRequirement:
    years: Optional[int] = None      # the highest number of years the JD asks for
    open_ended: bool = False         # '15+ years', 'more than 15', 'over 15'
    evidence: List[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.years is None:
            return "not stated"
        return f"{self.years}{'+' if self.open_ended else ''} years"

    def more_than(self, threshold: int) -> Optional[bool]:
        """True if the JD asks for more than `threshold` years ('12+' counts as more than 12); None if unstated."""
        if self.years is None:
            return None
        return self.years > threshold or (self.years == threshold and self.open_ended)


def _num(token: str) -> int:
    token = token.lower().strip()
    return int(token) if token.isdigit() else WORDS.get(token, 0)


def required_years(text: str) -> YearsRequirement:
    """
    Years of experience a job description asks for. Only numbers tied to experience count
    ('15+ years of experience', 'minimum 12 years in banking', '10-15 yrs exp'), not '5 year plan'.
    The highest requirement wins (a range '10-15' counts as 15).
    """
    req = YearsRequirement()
    t = re.sub(r"\s+", " ", (text or "").replace("–", "-").replace("—", "-"))
    patterns = [
        (rf"{NUM}\s*(?:-|to)\s*{NUM}\s*{YEARS}", "range"),
        (rf"(?:more than|over|in excess of|upwards of|above)\s+{NUM}\s*{YEARS}", "more"),
        (rf"(?:minimum|min\.?|at least|atleast|no less than)\s*(?:of\s*)?{NUM}\s*{YEARS}", "min"),
        (rf"{NUM}\s*\+\s*{YEARS}|{NUM}\s*{YEARS}\s*\+|{NUM}\s*(?:years?|yrs?)\s*(?:or more|and above|plus)", "plus"),
        (rf"{NUM}\s*{YEARS}", "plain"),
    ]
    taken: List[range] = []
    for pattern, kind in patterns:
        for m in re.finditer(pattern, t, re.I):
            if any(m.start() in r for r in taken):
                continue
            context = t[max(0, m.start() - 60): m.end() + 80]
            if not EXPERIENCE_NEAR.search(context):
                continue
            nums = [_num(g) for g in m.groups() if g]
            if not nums or max(nums) == 0 or max(nums) > 40:
                continue
            years = max(nums)
            open_ended = kind in ("more", "plus") or (kind == "range" and "+" in m.group(0))
            taken.append(range(m.start(), m.end()))
            req.evidence.append(m.group(0).strip())
            if req.years is None or years > req.years or (years == req.years and open_ended):
                req.years, req.open_ended = years, open_ended
    return req


JD_SELECTORS = ["#job-details", ".jobs-description__content", ".jobs-description-content__text",
                ".show-more-less-html__markup", ".description__text", "[class*='job-description']",
                "[data-automation-id='jobPostingDescription']", "main"]

FIT_SELECTORS = [".job-details-fit-level-card", ".job-details-fit-level-preferences", "[class*='fit-level']",
                 ".jobs-premium-applicant-insights", "[class*='match-details']", "[class*='how-you-match']",
                 ".job-details-jobs-unified-top-card__job-insight", ".jobs-unified-top-card__job-insight"]
FIT_TEXT = [r"\d+\s+of\s+\d+\s+skills?\s+(?:on your profile\s+)?match(?:es)?[^.\n]*",
            r"\b(?:high|medium|low|strong|good|great)\s+(?:job\s+)?(?:match|fit)\b",
            r"(?:job match|match)\s*:?\s*(?:high|medium|low)",
            r"you(?:'re| are)\s+(?:a\s+)?(?:top|strong|good|great)\s+(?:applicant|candidate|fit)[^.\n]*",
            r"your profile (?:matches|fits)[^.\n]*"]


async def read_job_page(page: Any) -> Dict[str, Any]:
    """JD text, open/closed status and 'already applied' marker for the job page currently loaded."""
    for sel in ("button.jobs-description__footer-button", "button:has-text('See more')", "button:has-text('Show more')"):
        try:
            btn = page.locator(sel).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=3000)
                break
        except Exception:
            pass
    jd = ""
    for sel in JD_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                jd = (await loc.inner_text()).strip()
                if len(jd) > 200:
                    break
        except Exception:
            continue
    body = await page.evaluate("() => document.body.innerText.slice(0, 20000)")
    top = body[:4000]
    return {"description": jd or body, "accepting": not CLOSED.search(top + " " + body[:12000]),
            "already_applied": bool(ALREADY_APPLIED.search(top)),
            "signed_out": bool(re.search(r"\b(sign in|join now)\b", top[:600], re.I)) and "Me" not in top[:300]}


async def linkedin_fit(page: Any) -> str:
    """The match/fit LinkedIn shows a signed-in member for this job ('' if none is shown)."""
    for sel in ("button:has-text('Show match details')", "button:has-text('See how you match')",
                "button:has-text('How you match')", "button:has-text('match details')"):
        try:
            btn = page.locator(sel).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=3000)
                await page.wait_for_timeout(2000)
                break
        except Exception:
            pass
    parts: List[str] = []
    for sel in FIT_SELECTORS + ["[role='dialog']"]:
        try:
            loc = page.locator(sel)
            for i in range(min(await loc.count(), 4)):
                if await loc.nth(i).is_visible():
                    txt = re.sub(r"\s+", " ", (await loc.nth(i).inner_text())).strip()
                    if txt and re.search(r"match|fit|skill|qualif", txt, re.I) and txt not in parts:
                        parts.append(txt[:400])
        except Exception:
            continue
    body = await page.evaluate("() => document.body.innerText.slice(0, 15000)")
    for pattern in FIT_TEXT:
        for m in re.finditer(pattern, body, re.I):
            s = re.sub(r"\s+", " ", m.group(0)).strip()
            if s and not any(s.lower() in p.lower() for p in parts):
                parts.append(s)
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    return " | ".join(parts[:5])
