"""
Core FormFiller engine for scanning, mapping, and populating ATS web inputs.

One in-page scan collects every visible field with its real label, its section headings
(so 'Company' in 'Work Experience 2' maps to the 2nd employer), options and current value.
Values come from FieldResolver; this module only deals with the browser.
"""

import asyncio
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from .field_resolver import Answer, FieldInfo, FieldResolver
from .matching import best_option, date_part_candidates, date_part_of, format_date, is_placeholder, norm, parse_date

SCAN_JS = r"""
() => {
  const vis = (e) => {
    if (!e) return false;
    const r = e.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return false;
    const s = getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').replace(/\s*\*\s*$/, '').replace(/^\*\s*/, '').replace(/\s*\*\s*$/, '').trim();
  const txt = (e) => clean(e ? (e.innerText || e.textContent || '') : '');
  const byIds = (ids) => clean((ids || '').split(/\s+/).map(id => txt(document.getElementById(id))).join(' '));
  const fieldsIn = (el) => [...el.querySelectorAll('input:not([type=hidden]), select, textarea')].filter(vis);

  function ownLabel(e) {
    const lb = byIds(e.getAttribute('aria-labelledby')); if (lb) return lb;
    if (e.id) { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l && txt(l)) return txt(l); }
    const wl = e.closest('label'); if (wl && txt(wl)) return txt(wl);
    const al = clean(e.getAttribute('aria-label')); if (al) return al;
    return '';
  }
  function contextLabel(e) {
    const td = e.closest('td');
    if (td && td.previousElementSibling && txt(td.previousElementSibling)) return txt(td.previousElementSibling);
    let p = e.parentElement;
    for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
      const fs = fieldsIn(p);
      // a split date (Day / Month / Year selects) shares its container label
      if (fs.length > 1 && !(fs.length <= 3 && fs.every(x => x.tagName === 'SELECT'))) break;
      const l = [...p.querySelectorAll('label, legend, [class*=label i]')].find(x => !x.contains(e) && txt(x) && txt(x).length < 200);
      if (l) return txt(l);
    }
    return clean(e.getAttribute('title') || e.getAttribute('placeholder') || '');
  }
  function groupLabel(e, members) {
    const fs = e.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg && txt(lg)) return txt(lg); }
    const rg = e.closest('[role=radiogroup],[role=group]');
    if (rg) {
      const t = byIds(rg.getAttribute('aria-labelledby')) || clean(rg.getAttribute('aria-label'));
      if (t) return t;
    }
    // smallest ancestor containing all members: its text minus the option labels
    let p = e.parentElement;
    while (p && !members.every(m => p.contains(m))) p = p.parentElement;
    for (let i = 0; i < 3 && p; i++, p = p.parentElement) {
      let t = txt(p);
      for (const m of members) { const ol = ownLabel(m); if (ol) t = t.replace(ol, ' '); }
      t = clean(t);
      if (t) return t.slice(0, 250);
    }
    return '';
  }

  // Outline of headings in document order, used to find each field's section and card.
  // bold text standing on its own (e.g. Taleo's 'Correspondence Address (If different...)') acts as a sub-heading
  const HSEL = 'h1,h2,h3,h4,h5,h6,legend,[role=heading],caption,button[aria-expanded],.rcmFormSectionTopBar,strong,b';
  const standalone = h => { const blk = h.closest('p, div, td, li'); return !blk || txt(blk).length <= txt(h).length + 2; };
  const heads = [...document.querySelectorAll(HSEL)].filter(h => vis(h) && txt(h) && txt(h).length < 120
      && !h.getAttribute('aria-haspopup') && !h.closest('header, nav, [role=listbox], label, a, button:not([aria-expanded])')
      && (!/^(STRONG|B)$/.test(h.tagName) || (standalone(h) && !h.closest('[class*=label i]')))).map(h => {
    let lvl = /^H\d$/.test(h.tagName) ? +h.tagName[1] : (h.tagName === 'LEGEND' ? 7 : h.tagName === 'BUTTON' ? 2
              : (h.tagName === 'STRONG' || h.tagName === 'B') ? 5 : +(h.getAttribute('aria-level') || 3));
    return {el: h, lvl, t: txt(h)};
  });
  function headings(e) {
    const out = []; let cur = 99;
    for (let i = heads.length - 1; i >= 0 && out.length < 4; i--) {
      const h = heads[i];
      if (h.el.contains(e)) continue;
      if (!(h.el.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
      if (h.lvl < cur) { out.push(h.t); cur = h.lvl; }
    }
    return out;
  }

  // A repeating row (one job / one degree) is the smallest block holding exactly one Remove/Delete control.
  const removers = [...document.querySelectorAll('button, a, [role=button]')].filter(b => vis(b) && txt(b).length < 30
      && /^[^a-z0-9]*(remove|delete)\b/i.test(txt(b) || b.getAttribute('aria-label') || b.getAttribute('title') || ''));
  function rowOf(e) {
    for (let p = e.parentElement, d = 0; p && d < 14 && removers.length; p = p.parentElement, d++) {
      const inside = removers.filter(r => p.contains(r));
      if (inside.length === 1) return removers.indexOf(inside[0]);
      if (inside.length > 1) return -1;
    }
    return -1;
  }

  document.querySelectorAll('[data-aaj]').forEach(e => e.removeAttribute('data-aaj'));
  const out = [];
  const seenGroups = new Set();
  let idx = 0;
  for (const e of document.querySelectorAll('input, select, textarea, [ui5-date-picker], ui5-date-picker')) {
    if (e.hasAttribute('ui5-date-picker') || e.tagName === 'UI5-DATE-PICKER') {
      if (!vis(e)) continue;
      const gid = idx++;
      e.setAttribute('data-aaj', String(gid));
      out.push({idx: gid, tag: 'ui5-date', type: 'ui5-date', id: e.id || '', name: '', field_type: 'date-widget',
        label: clean(e.getAttribute('accessible-name') || e.getAttribute('title')) || contextLabel(e), options: [],
        value: e.getAttribute('value') || '', placeholder: e.getAttribute('format-pattern') || e.getAttribute('placeholder') || '',
        title: '', required: e.hasAttribute('required'), disabled: e.hasAttribute('disabled') || e.hasAttribute('readonly'),
        combobox: false, listbox: '', headings: headings(e), row: rowOf(e)});
      continue;
    }
    const type = (e.getAttribute('type') || (e.tagName === 'SELECT' ? 'select' : e.tagName === 'TEXTAREA' ? 'textarea' : 'text')).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
    if (!vis(e) && type !== 'file' && type !== 'radio' && type !== 'checkbox') continue;
    if ((type === 'radio' || type === 'checkbox') && !vis(e) && !vis(e.closest('label') || e.parentElement)) continue;
    if (e.closest('header, nav, [role=navigation], [role=search]')) continue;
    // Bot traps: never touch honeypot fields or anything hidden from assistive tech.
    if (/honey|hpot|trap/i.test((e.id || '') + ' ' + (e.getAttribute('name') || '') + ' ' + (e.getAttribute('aria-label') || ''))
        || e.closest('[aria-hidden=true]')) continue;

    const base = {
      tag: e.tagName.toLowerCase(), type, id: e.id || '', name: e.getAttribute('name') || '',
      required: e.required || e.getAttribute('aria-required') === 'true',
      disabled: e.disabled || e.readOnly || e.getAttribute('aria-disabled') === 'true',
      placeholder: e.getAttribute('placeholder') || '', title: e.getAttribute('title') || '',
      combobox: e.getAttribute('role') === 'combobox' || !!e.getAttribute('aria-autocomplete') || !!e.getAttribute('list'),
      listbox: e.getAttribute('aria-owns') || e.getAttribute('aria-controls') || '',
      headings: headings(e), row: rowOf(e),
    };

    if (type === 'radio' || type === 'checkbox') {
      const scope = e.form || document;
      const members = type === 'radio' && base.name
        ? [...scope.querySelectorAll('input[type=radio][name="' + CSS.escape(base.name) + '"]')]
        : [e];
      if (type === 'radio') {
        const key = 'r:' + base.name + ':' + (e.form ? [...document.forms].indexOf(e.form) : -1);
        if (base.name && seenGroups.has(key)) continue;
        seenGroups.add(key);
      }
      const gid = idx++;
      members.forEach((m, j) => m.setAttribute('data-aaj', gid + '-' + j));
      const labels = members.map(m => ownLabel(m) || clean(m.value));
      const own = ownLabel(e);
      const group = members.length > 1 ? groupLabel(e, members) : '';
      out.push(Object.assign(base, {
        idx: gid, field_type: members.length > 1 ? 'radio' : type,
        label: members.length > 1 ? group : (own || contextLabel(e)),
        question: group || (type === 'checkbox' ? groupLabel(e, [e]) : ''),
        options: labels, value: members.filter(m => m.checked).map((m, j) => labels[members.indexOf(m)]).join(','),
        checked: e.checked,
      }));
      continue;
    }

    const gid = idx++;
    e.setAttribute('data-aaj', String(gid));
    let options = [], value = e.value || '';
    if (e.tagName === 'SELECT') {
      options = [...e.options].map(o => clean(o.text));
      value = e.selectedIndex >= 0 ? clean(e.options[e.selectedIndex].text) : '';
    }
    out.push(Object.assign(base, {
      idx: gid, field_type: e.tagName === 'SELECT' ? 'select' : e.tagName === 'TEXTAREA' ? 'textarea'
        : (['email', 'tel', 'number', 'date', 'file', 'month'].includes(type) ? type : 'text'),
      label: ownLabel(e) || contextLabel(e), ctx_label: contextLabel(e), options, value,
    }));
  }
  return out;
}
"""

EXPAND_JS = r"""
() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const skip = /menu|options|language|english|sign ?in|log ?in|account|profile|settings|filter|sort|share|search|cookie|help/i;
  let n = 0;
  const cands = document.querySelectorAll('[aria-expanded=false], details:not([open]) > summary');
  for (const el of cands) {
    if (!vis(el) || el.getAttribute('aria-haspopup') || /dropmenu|select/i.test(String(el.className || '')) ||
        el.closest('header, nav, [role=navigation], [role=menu], [role=listbox], .dropdown, select')) continue;
    if (el.tagName === 'INPUT' || el.getAttribute('role') === 'combobox') continue;
    const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
    if (!t || t.length > 80 || skip.test(t)) continue;
    if (el.hasAttribute('data-aaj-expanded')) continue;
    el.setAttribute('data-aaj-expanded', '1');
    el.click(); n++;
  }
  return n;
}
"""

OPTION_SELECTOR = (
    "[role='listbox'] [role='option'], [role='option'], [role='grid'] [role='gridcell'], ul.ui-autocomplete li, .ui-menu-item, "
    ".autocomplete-suggestions div, [class*='suggestion'] li"
)


@dataclass
class FormField:
    """Represents a discovered web form input (radio groups are one field)."""
    element: Any
    field_type: str  # text, select, radio, checkbox, textarea, date, email, tel, number, file
    label: str
    name: str = ""
    field_id: str = ""
    is_required: bool = False
    options: List[str] = field(default_factory=list)
    value: str = ""
    placeholder: str = ""
    title: str = ""
    question: str = ""
    headings: List[str] = field(default_factory=list)
    combobox: bool = False
    listbox: str = ""
    disabled: bool = False
    checked: bool = False
    ctx_label: str = ""
    date_part: str = ""   # 'day' / 'month' / 'year' for split-date dropdowns
    section: str = ""
    card: int = 0
    idx: int = 0
    row: int = -1         # repeating row (by its Remove/Delete control), -1 = not in one


SKIP_SECTION = re.compile(r"\b(employees? only|staff only|internal (candidates?|applicants?)( only)?|"
                          r"office use|for official use|referee|emergency contact)\b")
SKIP_IF_DIFFERENT = re.compile(r"\bif different\b|\bif not the same\b|\balternate address\b")
ADDRESSISH = re.compile(r"\b(address|street|line [12]|city|town|district|state|province|region|post ?code|postal|zip|pin ?code|country|phone|telephone)\b")

SECTION_PATTERNS = [
    ("experience", re.compile(r"\b(work experience|experience|employment|work history|employer|previous (job|position)s?|career history)\b")),
    ("education", re.compile(r"\b(education|qualifications?|academic|schools?|university|degrees?)\b")),
]


def section_from_headings(headings: List[str]) -> tuple:
    """('experience', 1) for headings like ['Work Experience 2', 'My Experience']."""
    for h in headings:
        hn = norm(h)
        for kind, pat in SECTION_PATTERNS:
            if pat.search(hn):
                m = re.search(r"(\d+)\s*$", hn) or re.search(r"(?:experience|education|employment|qualification)\D{0,12}(\d+)", hn)
                return kind, (int(m.group(1)) - 1 if m else None)
    return "", None


class FormFiller:
    """Scans and populates web inputs based on candidate data and fuzzy QA matching."""

    def __init__(self, page: Any, resume_data: Dict[str, Any], skill_matcher: Any, question_answerer: Any,
                 settings: Optional[Dict[str, Any]] = None):
        self.page = page
        self.resume_data = resume_data
        self.skill_matcher = skill_matcher
        self.qa = question_answerer
        self.settings = settings or {}
        app = self.settings.get("application", {}) or {}
        self.date_format = app.get("date_format", "dd/mm/yyyy")
        self.overwrite = bool(app.get("overwrite_existing", False))
        self.resolver = FieldResolver(resume_data, question_answerer, skill_matcher)
        self.auto_expand = True  # handlers that expand sections themselves switch this off

    async def expand_all_accordions(self, passes: int = 3) -> int:
        """Expand collapsed sections (aria-expanded=false / <details>), skipping menus, dropdowns and nav."""
        total = 0
        for _ in range(passes):
            try:
                n = await self.page.evaluate(EXPAND_JS)
            except Exception:
                n = 0
            total += n
            if not n:
                break
            await asyncio.sleep(1.0)
        return total

    async def scan_form_fields(self) -> List[FormField]:
        """Scan the page (or frame) for visible inputs with labels, headings, options and values."""
        raw = await self.page.evaluate(SCAN_JS)
        fields: List[FormField] = []
        for r in raw:
            label = r["label"] or ""
            if re.search(r"\s*\.?\s*\bRequired\s*$", label):
                label, r["required"] = re.sub(r"\s*\.?\s*\bRequired\s*$", "", label), True
                r["label"] = label
            grouped = r["type"] in ("radio", "checkbox")
            sel = f"[data-aaj^='{r['idx']}-']" if grouped else f"[data-aaj='{r['idx']}']"
            fields.append(FormField(
                element=self.page.locator(sel), field_type=r["field_type"], label=r["label"] or "",
                name=r["name"], field_id=r["id"], is_required=bool(r["required"]), options=r.get("options") or [],
                value=r.get("value") or "", placeholder=r.get("placeholder") or "", title=r.get("title") or "",
                question=r.get("question") or "", headings=r.get("headings") or [], combobox=bool(r.get("combobox")),
                listbox=r.get("listbox") or "",
                disabled=bool(r.get("disabled")), checked=bool(r.get("checked")), idx=r["idx"],
                ctx_label=r.get("ctx_label") or "", row=r.get("row", -1),
            ))
        self._group_split_dates(fields)
        self._assign_cards(fields)
        return fields

    @staticmethod
    def _group_split_dates(fields: List[FormField]) -> None:
        """Day / Month / Year dropdowns next to each other form one date; they share the group's label."""
        generic = re.compile(r"^(day|month|year|dd|mm|yyyy)?$", re.I)
        i = 0
        while i < len(fields) - 1:
            group = []
            for f in fields[i:i + 3]:
                part = date_part_of(f.options) if f.field_type == "select" else None
                if not part or part in [g.date_part for g in group]:
                    break
                f.date_part = part
                group.append(f)
            if len(group) >= 2:
                near = [re.sub(r"\s*\.?\s*Required\s*$", "", group[0].headings[0])] if group[0].headings else []
                label = next((x for g in group for x in (g.label, g.ctx_label) if x and not generic.match(x.strip())), "") \
                    or next((h for h in near if len(h) < 60), "")
                for g in group:
                    g.label = label or g.label
                i += len(group)
            else:
                for g in group:
                    g.date_part = ""
                i += 1

    @staticmethod
    def _assign_cards(fields: List[FormField]) -> None:
        """Give each field its section and card index (from numbered headings, else label repetition)."""
        sticky, last_heading = "", None
        seen: Dict[str, Dict[str, int]] = {"experience": {}, "education": {}}
        for f in fields:
            heading = f.headings[0] if f.headings else None
            if heading != last_heading:
                sticky, last_heading = "", heading
            kind, number = section_from_headings(f.headings)
            label_kind = FieldResolver.section_for_label(f.label)
            if not kind:
                kind = label_kind or sticky
            elif label_kind and label_kind != kind:
                kind = label_kind
            sticky = kind if (kind or label_kind) else sticky
            f.section = kind
            if kind in seen:
                key = norm(f.label)
                count = seen[kind].get(key, 0)
                seen[kind][key] = count + 1
                f.card = number if number is not None else count
                if number is not None:
                    f.row = -1  # a numbered heading ('Work Experience 2') is authoritative

        # Rows marked by their own Remove/Delete control beat label counting, which drifts as soon as one
        # row shows a field the others don't (pre-filled profile rows next to rows the tool just added).
        for kind in seen:
            in_kind = [f for f in fields if f.section == kind]
            rows = list(dict.fromkeys(f.row for f in in_kind if f.row >= 0))
            if len(rows) < 2:
                continue
            row_labels = {norm(f.label) for f in in_kind if f.row >= 0}
            rowless = Counter(norm(f.label) for f in in_kind if f.row < 0 and norm(f.label) in row_labels)
            offset = max(rowless.values(), default=0)  # leading rows without a Remove control (e.g. a required first row)
            for f in in_kind:
                if f.row >= 0:
                    f.card = offset + rows.index(f.row)

    def _misplaced(self, f: FormField, ans: Answer) -> bool:
        """
        A job/degree row value that is really another resume entry's or another field's value (an earlier
        run's mix-up such as the MBA school in 'State/City of School') - replaced rather than kept.
        Anything else already in the box (typed by the candidate) is kept.
        """
        entries = self.resume_data.get(f.section) if f.section in ("experience", "education") else None
        if not entries or not f.value.strip():
            return False
        values = [v for e in entries for val in (e or {}).values() for v in (val if isinstance(val, list) else [val])
                  if isinstance(v, str) and v.strip()]
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", f.value.strip())
        if ans.date and m:  # page dates may be MM/DD or DD/MM - compare what they mean
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            meant = {date(y, mo, d) for d, mo in ((a, b), (b, a)) if 1 <= mo <= 12 and 1 <= d <= 31}
            if ans.date in meant:
                return False
            return any(parse_date(v) in meant for v in values)
        have = norm(f.value)
        if have in {norm(c) for c in ans.candidates}:
            return False
        return any(norm(v) == have for v in values)

    def _has_value(self, f: FormField) -> bool:
        if f.field_type in ("radio",):
            return bool(f.value)
        if f.field_type == "checkbox":
            return f.checked
        if f.field_type == "select":
            return bool(f.value) and not is_placeholder(f.value)
        # Values left by resume parsing / defaults such as 'UNSPECIFIED' or 'N/A' count as empty.
        return bool(f.value.strip()) and norm(f.value) not in ("unspecified", "n a", "na", "none", "unknown", "no selection", "tbd")

    async def fill_all_fields(self) -> Dict[str, Any]:
        """
        Scan and populate all fields. A second pass re-scans the page: choosing one value often changes
        others (Pin Code -> City/State, Country -> State), so fields re-rendered by the first pass get filled
        on fresh element handles while values the page filled itself are kept.
        """
        first = await self._fill_pass()
        if not first["failed_count"] and not first.get("disabled_count"):
            return first  # nothing failed and nothing was waiting to be unlocked by an earlier answer
        second = await self._fill_pass()
        retried = {d["label"] for d in second["details"] if d["status"] in ("filled", "kept")}
        details = [d for d in first["details"] if not (d["status"] == "failed" and d["label"] in retried)]
        details += [d for d in second["details"] if d["status"] == "filled"]
        return {"filled_count": first["filled_count"] + second["filled_count"],
                "skipped_count": first["skipped_count"],
                "failed_count": len([d for d in details if d["status"] == "failed"]),
                "kept_count": first["kept_count"], "details": details}

    async def _fill_pass(self) -> Dict[str, Any]:
        if self.auto_expand:
            await self.expand_all_accordions()
        fields = await self.scan_form_fields()
        self.resolver.has_country_code_field = any(
            re.search(r"\b(country|region|itu|dial(l)?ing|calling|phone) (region )?code\b", norm(f.label)) for f in fields)

        filled = skipped = failed = kept = 0
        details: List[Dict[str, Any]] = []
        disabled = 0
        for f in fields:
            if f.disabled or f.field_type == "file":
                disabled += f.disabled
                continue
            heads = [norm(h) for h in f.headings[:2]]
            if any(SKIP_SECTION.search(h) for h in heads) or \
                    (any(SKIP_IF_DIFFERENT.search(h) for h in heads) and ADDRESSISH.search(norm(f.label))):
                skipped += 1  # e.g. 'For current employees only', address under 'Correspondence address (if different)'
                continue
            label = f.label if f.field_type != "checkbox" or not f.question else f"{f.question} {f.label}"
            # picklist comboboxes ('No Selection' inputs) answer like dropdowns, not like free text
            kind = "select" if f.combobox and f.field_type == "text" else f.field_type
            info = FieldInfo(label=label, field_type=kind, options=f.options, section=f.section,
                             card=f.card, placeholder=f.placeholder or f.title)
            ans = self.resolver.resolve(info)
            if ans is None or (not ans.value and ans.check is None and ans.date is None):
                skipped += 1
                if f.is_required and not self._has_value(f):
                    details.append({"label": f.label, "status": "unanswered-required"})
                continue
            if self._has_value(f) and not self.overwrite and f.field_type != "checkbox" and not self._misplaced(f, ans):
                kept += 1
                details.append({"label": f.label, "status": "kept", "value": f.value, "rule": ans.rule})
                continue
            try:
                result = await self.fill_field(f, ans)
            except Exception as e:
                result = f"error: {str(e).splitlines()[0][:120]}"
            if result is True:
                filled += 1
                details.append({"label": f.label, "status": "filled", "value": self._display(ans), "rule": ans.rule,
                                "section": f.section, "card": f.card})
            else:
                failed += 1
                details.append({"label": f.label, "status": "failed", "value": self._display(ans), "rule": ans.rule,
                                "reason": result if isinstance(result, str) else "no matching option"})
        return {"filled_count": filled, "skipped_count": skipped, "failed_count": failed, "kept_count": kept,
                "disabled_count": disabled, "details": details}

    @staticmethod
    def _display(ans: Answer) -> str:
        return ans.date.isoformat() if ans.date else (str(ans.check) if ans.check is not None and not ans.value else ans.value)[:60]

    def text_for(self, f: FormField, ans: Answer) -> str:
        if ans.date:
            if f.field_type == "date":
                return ans.date.isoformat()
            if f.field_type == "month":
                return ans.date.strftime("%Y-%m")
            return format_date(ans.date, f"{f.placeholder} {f.title} {f.label}", self.date_format)
        return str(ans.value).strip()

    async def fill_field(self, f: FormField, ans: Answer) -> Any:
        """Fill one field. Returns True, or a reason string / False when it could not."""
        el = f.element
        if f.field_type == "date-widget":
            if not ans.date:
                return "not a date"
            inner = el.locator("input").first  # Playwright CSS pierces the component's shadow DOM
            await inner.fill(format_date(ans.date, f.placeholder, self.date_format), timeout=5000)
            await inner.press("Enter")
            return True
        if f.combobox and f.field_type == "text" and not ans.date:
            return await self.choose_in_combobox(f, ans)
        if f.field_type in ("text", "textarea", "email", "tel", "number", "date", "month"):
            value = self.text_for(f, ans)
            if not value:
                return "empty value"
            await el.fill(value, timeout=5000)
            await el.evaluate("e => { e.dispatchEvent(new Event('input', {bubbles: true}));"
                              " e.dispatchEvent(new Event('change', {bubbles: true})); e.blur(); }")
            return True

        if f.field_type == "select":
            if f.date_part and ans.date:
                choice = best_option(f.options, date_part_candidates(ans.date, f.date_part))
                if not choice:
                    return f"no {f.date_part} option"
                await el.select_option(label=choice, timeout=3000)
                await el.evaluate("e => e.dispatchEvent(new Event('change', {bubbles: true}))")
                return True
            choice = best_option(f.options, ans.candidates, ans.fallbacks, proxies_before_partial=ans.proxies_first)
            if not choice:
                return f"no option for {ans.candidates[:3]}"
            await el.select_option(label=choice, timeout=3000)
            await el.evaluate("e => e.dispatchEvent(new Event('change', {bubbles: true}))")
            return True

        if f.field_type == "radio":
            choice = best_option(f.options, ans.candidates, ans.fallbacks)
            if not choice:
                return f"no option for {ans.candidates[:3]}"
            radio = el.nth(f.options.index(choice))
            try:
                await radio.check(timeout=3000)
            except Exception:
                await radio.evaluate("e => { e.click(); e.dispatchEvent(new Event('change', {bubbles: true})); }")
            return True

        if f.field_type == "checkbox":
            want = ans.check if ans.check is not None else norm(ans.value) in ("yes", "true", "1", "check")
            if f.checked != want:
                try:
                    await el.set_checked(want, timeout=3000)
                except Exception:
                    await el.evaluate("e => e.click()")
            return True
        return False

    async def _visible_options(self, listbox_id: str = "") -> List[tuple]:
        """(text, locator) for the options of an open listbox (the one an input owns, else any visible)."""
        scope = self.page.locator(f"[id='{listbox_id}']") if listbox_id else None
        owned = scope is not None and await scope.count() > 0
        opts = scope.locator("[role='option'], [role='gridcell']") if owned else self.page.locator(OPTION_SELECTOR)
        out = []
        for i in range(min(await opts.count(), 400)):
            o = opts.nth(i)
            try:
                text = re.sub(r"\s+", " ", (await o.inner_text()).strip())
                if text and (owned or await o.is_visible()):
                    out.append((text, o))
            except Exception:
                continue
        return out

    async def choose_in_combobox(self, f: FormField, ans: Answer) -> Any:
        """
        Combobox/picklist: open it and click the best option. If the list is empty or has no match,
        type progressively shorter search terms (typeahead widgets) and pick from the suggestions.
        """
        el = f.element
        options: List[tuple] = []
        for attempt in range(3):  # a previous picker can still be closing and swallow the first click
            await self.page.keyboard.press("Escape")
            await el.scroll_into_view_if_needed(timeout=5000)
            await el.click(timeout=5000)
            await asyncio.sleep(0.8 + attempt)
            options = await self._visible_options(f.listbox)
            if options:
                break
            await el.press("Escape")
        choice = best_option([t for t, _ in options], ans.candidates, ans.fallbacks,
                             proxies_before_partial=ans.proxies_first) if options else None
        typed = False
        if not choice:
            for term in self._search_terms(ans):
                await el.fill("", timeout=5000)
                await el.press_sequentially(term, delay=60)  # typeahead widgets search on key events
                typed = True
                await asyncio.sleep(2.0)
                options = await self._visible_options(f.listbox)
                choice = best_option([t for t, _ in options], ans.candidates, ans.fallbacks)
                if choice:
                    break
        if not choice:
            if typed:
                await el.fill("", timeout=5000)  # never leave a half-typed search term behind
            await el.press("Escape")
            return f"no option for {ans.candidates[:3]} among {[t for t, _ in options][:6]}"
        target = next(o for t, o in options if t == choice)
        await target.scroll_into_view_if_needed(timeout=5000)
        await target.click(timeout=5000)
        await asyncio.sleep(0.5)
        return True

    @staticmethod
    def _search_terms(ans: Answer) -> List[str]:
        terms: List[str] = []
        for c in ans.candidates + ans.fallbacks:
            words = re.sub(r"[^\w\s]", " ", re.sub(r"['’]s\b", "", c)).split()  # "Master's" -> "Master", not "Master s"
            for t in (" ".join(words[:3]), words[0] if words else ""):
                if len(t) >= 2 and t not in terms:
                    terms.append(t)
        return terms[:6]
