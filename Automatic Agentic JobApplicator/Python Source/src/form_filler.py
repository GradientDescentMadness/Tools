"""
Core FormFiller engine for scanning, mapping, and populating ATS web inputs.
"""

import asyncio
from dataclasses import dataclass, field
import re
from typing import Any, Dict, List, Optional
from rapidfuzz import fuzz


@dataclass
class FormField:
    """Represents a discovered web form input."""
    element: Any
    field_type: str  # text, select, radio, checkbox, textarea, file
    label: str
    name: str = ""
    field_id: str = ""
    is_required: bool = False
    options: List[str] = field(default_factory=list)


class FormFiller:
    """Scans and populates web inputs based on candidate data and fuzzy QA matching."""

    def __init__(self, page: Any, resume_data: Dict[str, Any], skill_matcher: Any, question_answerer: Any):
        self.page = page
        self.resume_data = resume_data
        self.skill_matcher = skill_matcher
        self.qa = question_answerer

    async def scan_form_fields(self) -> List[FormField]:
        """Scan active viewport DOM for inputs, selects, textareas, and radio buttons."""
        fields: List[FormField] = []
        raw_elements = await self.page.query_selector_all("input, select, textarea")

        for el in raw_elements:
            try:
                if not await el.is_visible():
                    continue

                tag_name = await el.evaluate("e => e.tagName.toLowerCase()")
                input_type = (await el.get_attribute("type") or "text").lower()
                name_attr = await el.get_attribute("name") or ""
                id_attr = await el.get_attribute("id") or ""
                req_attr = await el.get_attribute("required") or await el.get_attribute("aria-required")
                is_required = req_attr in ["true", "required", ""]

                # Determine effective field type
                if tag_name == "select":
                    f_type = "select"
                elif tag_name == "textarea":
                    f_type = "textarea"
                elif input_type in ["radio", "checkbox", "file", "email", "tel", "number", "date"]:
                    f_type = input_type
                else:
                    f_type = "text"

                # Extract label
                label_text = await self._find_label(el, id_attr)

                # For selects, collect options
                options: List[str] = []
                if f_type == "select":
                    opt_elements = await el.query_selector_all("option")
                    for o in opt_elements:
                        t = await o.inner_text()
                        if t.strip():
                            options.append(t.strip())

                fields.append(
                    FormField(
                        element=el,
                        field_type=f_type,
                        label=label_text,
                        name=name_attr,
                        field_id=id_attr,
                        is_required=is_required,
                        options=options,
                    )
                )
            except Exception:
                continue

        return fields

    async def _find_label(self, el: Any, element_id: str) -> str:
        """Heuristically discover user-facing label text for a field."""
        # 1. aria-label
        aria_label = await el.get_attribute("aria-label")
        if aria_label and aria_label.strip():
            return aria_label.strip()

        # 2. placeholder
        placeholder = await el.get_attribute("placeholder")
        if placeholder and placeholder.strip():
            return placeholder.strip()

        # 3. <label for="...">
        if element_id:
            try:
                lbl = await self.page.query_selector(f"label[for='{element_id}']")
                if lbl:
                    txt = await lbl.inner_text()
                    if txt.strip():
                        return txt.strip()
            except Exception:
                pass

        # 4. Closest parent label or container text
        try:
            parent_text = await el.evaluate("""
                e => {
                    let parent = e.closest('label') || e.closest('.form-group') || e.closest('div');
                    return parent ? parent.innerText : '';
                }
            """)
            if parent_text:
                first_line = parent_text.strip().splitlines()[0]
                if len(first_line) < 100:
                    return first_line
        except Exception:
            pass

        return ""

    async def fill_all_fields(self) -> Dict[str, Any]:
        """Scan and intelligently populate all fields on the current page."""
        fields = await self.scan_form_fields()
        filled = 0
        skipped = 0
        failed = 0
        details = []

        personal = self.resume_data.get("personal", {})
        address = personal.get("address", {})
        additional = self.resume_data.get("additional", {})

        for f in fields:
            lbl = f.label.lower()
            val_to_fill: Optional[str] = None

            # --- Check specific standard identity fields ---
            if any(k in lbl for k in ["first name", "given name", "first_name"]):
                val_to_fill = personal.get("first_name", "")
            elif any(k in lbl for k in ["last name", "surname", "family name", "last_name"]):
                val_to_fill = personal.get("last_name", "")
            elif any(k in lbl for k in ["full name", "applicant name"]) and not ("first" in lbl or "last" in lbl):
                val_to_fill = personal.get("full_name", "")
            elif "email" in lbl:
                val_to_fill = personal.get("email", "")
            elif any(k in lbl for k in ["phone", "mobile", "cell", "contact number"]):
                val_to_fill = personal.get("phone", "")
            elif "street" in lbl or "address line 1" in lbl:
                val_to_fill = address.get("street", "")
            elif "city" in lbl or "town" in lbl:
                val_to_fill = address.get("city", "")
            elif "state" in lbl or "province" in lbl or "region" in lbl:
                val_to_fill = address.get("state", "")
            elif any(k in lbl for k in ["zip", "postal", "pincode"]):
                val_to_fill = address.get("zip_code", "")
            elif "country" in lbl:
                val_to_fill = address.get("country", "")
            elif "linkedin" in lbl:
                val_to_fill = personal.get("linkedin_url", "")
            elif "github" in lbl or "portfolio" in lbl:
                val_to_fill = personal.get("github_url", "")
            elif "notice" in lbl or "start date" in lbl:
                val_to_fill = additional.get("notice_period", "")
            elif "salary" in lbl or "compensation" in lbl:
                val_to_fill = additional.get("expected_salary", "")

            # --- Screening Questions & Dropdowns ---
            if val_to_fill is None and self.qa:
                val_to_fill = self.qa.get_answer(f.label)

            if val_to_fill is None and f.field_type == "select" and self.skill_matcher and "skill" in lbl:
                # Fuzzy skill matcher
                matching_skills = self.skill_matcher.get_skills_to_select(f.options)
                if matching_skills:
                    val_to_fill = matching_skills[0]

            # Execute filling
            if val_to_fill:
                try:
                    success = await self.fill_field(f, val_to_fill)
                    if success:
                        filled += 1
                        details.append({"label": f.label, "filled": val_to_fill, "status": "success"})
                    else:
                        failed += 1
                        details.append({"label": f.label, "status": "failed"})
                except Exception as e:
                    failed += 1
                    details.append({"label": f.label, "error": str(e), "status": "error"})
            else:
                skipped += 1

        return {"filled_count": filled, "skipped_count": skipped, "failed_count": failed, "details": details}

    async def fill_field(self, field: FormField, value: str) -> bool:
        """Dispatch value filling according to input element type."""
        try:
            if field.field_type in ["text", "textarea", "email", "tel", "number"]:
                await field.element.click()
                await field.element.fill("")
                # Human typing simulation (30ms per char)
                await field.element.type(value, delay=30)
                return True

            elif field.field_type == "select":
                # Find closest option
                best_opt = None
                highest = 0
                for opt in field.options:
                    score = fuzz.token_sort_ratio(opt.lower(), value.lower())
                    if score > highest:
                        highest = score
                        best_opt = opt

                target = best_opt if highest >= 65 else value
                try:
                    await field.element.select_option(label=target)
                    return True
                except Exception:
                    await field.element.select_option(value=target)
                    return True

            elif field.field_type == "radio":
                if value.lower() in ["yes", "true", "1"]:
                    await field.element.check()
                return True

            return False
        except Exception:
            return False

