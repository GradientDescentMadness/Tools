"""
Maps a discovered form field (label + type + options + card context) to the candidate's answer.

Kept free of browser code so every rule can be unit-tested. Rules use whole-word regexes and
are ordered most-specific first; experience/education rules are scoped to the card the field
belongs to, so the n-th 'Company' field gets the n-th employer.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from .matching import (
    available_start,
    degree_fallbacks,
    field_of_study_fallbacks,
    industry_fallbacks,
    norm,
    notice_alternatives,
    parse_date,
)
from .question_answerer import country_alternatives, find_countries, is_country_name

SALUTATIONS = {"mr", "mrs", "ms", "miss", "mx", "dr", "prof"}

LEVEL_SYNONYMS = {
    "master": ["Master's Degree", "Masters Degree", "Masters", "Master", "Postgraduate Degree",
               "Postgraduate", "Post Graduate", "Graduate Degree", "Graduate"],
    "bachelor": ["Bachelor's Degree", "Bachelors Degree", "Bachelors", "Bachelor", "Undergraduate Degree",
                 "Undergraduate", "Graduate"],
    "doctor": ["Doctorate", "PhD", "Doctoral Degree"],
    "diploma": ["Diploma", "Advanced Diploma"],
}


@dataclass
class Answer:
    """A resolved value plus the alternative phrasings and nearest proxies used for dropdowns."""
    value: str = ""
    alternatives: List[str] = field(default_factory=list)
    fallbacks: List[str] = field(default_factory=list)
    date: Optional[date] = None
    check: Optional[bool] = None
    rule: str = ""
    proxies_first: bool = False  # prefer exact proxies over loose partial matches (fields of study)

    @property
    def candidates(self) -> List[str]:
        return [v for v in dict.fromkeys([self.value] + self.alternatives) if v]


@dataclass
class FieldInfo:
    """Browser-independent description of a field, as consumed by the resolver."""
    label: str
    field_type: str = "text"  # text, textarea, select, radio, checkbox, date, email, tel, number, file
    options: List[str] = field(default_factory=list)
    section: str = ""         # 'experience', 'education' or ''
    card: int = 0
    placeholder: str = ""


def _yes_no(flag: Any) -> str:
    return "Yes" if flag else "No"


class FieldResolver:
    """Resolves candidate answers for form fields from resume_data, QA rules and skills."""

    def __init__(self, resume_data: Dict[str, Any], qa: Any = None, skill_matcher: Any = None):
        self.r = resume_data or {}
        self.qa = qa
        self.skill_matcher = skill_matcher
        self.personal = self.r.get("personal", {}) or {}
        self.address = self.personal.get("address", {}) or {}
        self.additional = self.r.get("additional", {}) or {}
        self.eeoc = self.r.get("eeoc_and_diversity", {}) or {}
        self.citizenship = self.r.get("citizenship", {}) or {}
        self.education = self.r.get("education", []) or []
        self.experience = self.r.get("experience", []) or []
        self.has_country_code_field = False  # set per page by the form filler

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def section_for_label(label: str) -> str:
        """Infer which card type a label belongs to when headings don't say (questions never count)."""
        if FieldResolver._is_question(label):
            return ""
        l = norm(label)
        if re.search(r"\b(school|university|college|degree|field of study|major|qualification|gpa|alma mater|commencement|educational institution)\b", l):
            return "education"
        if re.search(r"\b(company|employer|current role|currently work|responsibilities|duties)\b", l) \
                and not re.search(r"\bcurrent (job )?title\b", l):
            return "experience"
        return ""

    def _phone(self) -> Dict[str, str]:
        raw = str(self.personal.get("phone", ""))
        code = re.sub(r"\D", "", str(self.personal.get("phone_country_code", "") or "")) or ""
        digits = re.sub(r"\D", "", raw)
        national = digits[len(code):] if code and digits.startswith(code) and len(digits) > 10 else digits
        return {"code": f"+{code}" if code else "", "national": national,
                "full": f"+{code} {national}" if code else raw}

    def _country_code_alternatives(self) -> List[str]:
        code = self._phone()["code"]
        country = self.address.get("country", "")
        return [f"{country} ({code})", code, code.lstrip("+"), country, f"{country} {code}"]

    @staticmethod
    def _country_answer(entry: Dict[str, Any], rule: str) -> Optional[Answer]:
        """Country of a job/degree: an explicit 'country', else the first place named in 'location'."""
        country = str(entry.get("country", "") or "")
        if not country:
            found = find_countries(str(entry.get("location", "")).split("/")[0])
            country = found[0] if found else ""
        return Answer(country, alternatives=country_alternatives(country), rule=rule) if country else None

    def _place_answer(self, entry: Dict[str, Any], l: str, prefix: str) -> Optional[Answer]:
        """
        Where a job/degree was: 'Country of School', 'State/City/Region of School', 'Company Location'.
        Checked before the name rules, which would otherwise take 'School'/'Employer' in such labels.
        """
        if len(l.split()) > 6:
            return None
        if re.search(r"\b(country|nation)\b", l) and not re.search(r"\bcode\b", l):
            return self._country_answer(entry, f"{prefix}.country")
        place = str(entry.get("location", "")).split("/")[0].strip()
        if re.search(r"\blocation\b", l):
            return Answer(place, rule=f"{prefix}.location") if place else None
        if re.search(r"\b(city|state|region|province|town|county)\b", l):
            parts = [p.strip() for p in place.split(",") if p.strip()]
            if len(parts) > 1 and is_country_name(parts[-1]):
                parts = parts[:-1]  # 'Atlanta, GA, US' -> 'Atlanta, GA'
            return Answer(", ".join(parts), rule=f"{prefix}.city") if parts else None
        return None

    def _highest_education(self) -> int:
        """Index of the highest degree (by level, the most recent one on a tie)."""
        ranks = (("doctor", 4), ("phd", 4), ("master", 3), ("bachelor", 2), ("diploma", 1))

        def key(i: int) -> tuple:
            e = self.education[i]
            level = norm(str(e.get("qualification_level", "")))
            done = parse_date(e.get("completion_date") or e.get("graduation_date") or "") or date.min
            return next((r for k, r in ranks if k in level), 0), done
        return max(range(len(self.education)), key=key) if self.education else -1

    def _date_answer(self, raw: str, rule: str) -> Optional[Answer]:
        d = parse_date(raw)
        return Answer(value=raw, date=d, rule=rule) if d else None

    @staticmethod
    def _is_question(label: str) -> bool:
        l = norm(label)
        return label.strip().endswith("?") or bool(re.match(
            r"(do|does|did|have|has|are|is|were|was|will|would|can|could|why|how|what|which|please|describe|tell)\b", l))

    # ------------------------------------------------------------------ main entry
    def resolve(self, f: FieldInfo) -> Optional[Answer]:
        l = norm(f.label)
        if not l or f.field_type == "file":
            return None
        if re.match(r"if (yes|no|so|other|applicable|you (have|are|were|answered|selected|chose))\b", l):
            # conditional follow-ups ('If yes, please provide...') are left for the candidate - except the
            # instructed 'Otherwise, enter N/A' after a relationship question, which the QA rules answer 'No'
            if re.search(r"\botherwise\W+(please )?(enter|input|type|write|put) (n a|na|not applicable)\b", l) \
                    and re.search(r"\b(relationship|relatives?|related|family)\b", l):
                return Answer("N/A", rule="instructed_na")
            return None
        rules = (self._consent, self._experience, self._education, self._personal,
                 self._contact, self._address, self._work_prefs, self._eeo, self._qa, self._skills)
        if len(l.split()) > 20:
            rules = (self._consent, self._qa, self._eeo)  # long question text: words like 'available' or 'postal' mean nothing
        for rule in rules:
            ans = rule(f, l)
            if ans is not None:
                if self.is_date_field(f) and ans.date is None and parse_date(ans.value) is None:
                    return None  # a date input only ever receives a date (never '60 days')
                return ans
        return None

    @staticmethod
    def is_date_field(f: FieldInfo) -> bool:
        return f.field_type in ("date", "month", "date-widget") or bool(
            re.search(r"\b(dd|mm)\W?(mm|yyyy|yy)\b|\bmm\W?yyyy\b", (f.placeholder or "").lower()))

    # ------------------------------------------------------------------ rules
    def _consent(self, f: FieldInfo, l: str) -> Optional[Answer]:
        if f.field_type == "checkbox" and re.search(
                r"\b(privacy|terms|consent|i agree|agree|acknowledge|declaration|declare|hereby|certify|accept|i have read|confirm)\b", l):
            return Answer("Yes", check=True, rule="consent")
        return None

    def _experience(self, f: FieldInfo, l: str) -> Optional[Answer]:
        if f.section != "experience" or not self.experience:
            return None
        if f.card >= len(self.experience):
            return None
        exp = self.experience[f.card]

        if re.search(r"\b(currently work|current(ly)? (employed|working|role|job|position|employer)|present (role|job|employer)|is this your current|i currently|still work)", l):
            return Answer(_yes_no(exp.get("is_current")), check=bool(exp.get("is_current")), rule="exp.current")
        if re.search(r"\bnotice\b", l):
            return None
        place = self._place_answer(exp, l, "exp")
        if place:
            return place
        if re.search(r"\b(job title|position title|role title|designation|position|job role)\b|^(title|role|job)$", l):
            return Answer(exp.get("title", ""), rule="exp.title")
        if re.search(r"\b(company|employer|organi[sz]ation|business name)\b", l) and not self._is_question(f.label):
            return Answer(exp.get("company", ""), rule="exp.company")
        if re.search(r"\b(description|responsibilit\w*|duties|achievements?|summary)\b", l):
            return Answer(str(exp.get("description", "")).strip(), rule="exp.description")
        if re.search(r"\b(industry|sector|(type|line|nature|area) of business|business (type|sector|line|area))\b", l):
            industry = str(exp.get("industry", "") or "")
            if not industry:
                return None
            parts = [p.strip() for p in re.split(r"[/,]", industry) if p.strip()]
            return Answer(industry, alternatives=list(exp.get("industry_aliases", []) or []) + parts,
                          fallbacks=industry_fallbacks(industry) + ["Other"], rule="exp.industry")
        if re.search(r"\b(location|city)\b", l):
            return Answer(str(exp.get("location", "")).split("/")[0].strip(), rule="exp.location")
        if re.search(r"\b(employment|job|work) type\b", l):
            return Answer("Full-time", alternatives=["Full time", "Permanent", "Regular"], rule="exp.type")
        if len(l.split()) <= 4 or f.field_type == "date":
            if re.search(r"\b(start|from|commence\w*|began|begin|joined)\b", l):
                return self._date_answer(exp.get("start_date", ""), "exp.start")
            if re.search(r"\b(end|to|until|finish\w*|left|ended)\b", l):
                if exp.get("is_current"):
                    return None
                return self._date_answer(exp.get("end_date", ""), "exp.end")
        return None

    def _education(self, f: FieldInfo, l: str) -> Optional[Answer]:
        if f.section != "education" or not self.education:
            return None
        if f.card >= len(self.education):
            return None
        edu = self.education[f.card]
        level = str(edu.get("qualification_level", ""))
        level_alts = next((v for k, v in LEVEL_SYNONYMS.items() if k in norm(level)), [])

        place = self._place_answer(edu, l, "edu")
        if place:
            return place
        if re.search(r"\bhighest\b", l):
            # per degree row: a level list on some portals, 'is this your highest?' Yes/No on others (Moody's)
            highest = _yes_no(f.card == self._highest_education())
            if {norm(o) for o in f.options} >= {"yes", "no"}:
                return Answer(highest, rule="edu.highest")
            return Answer(level, alternatives=level_alts, fallbacks=[highest], rule="edu.highest")
        if re.search(r"\b(type of (education|qualification|degree|study)|level of (education|qualification|study)|(qualification|education|degree) (level|type)|level)\b", l):
            return Answer(level, alternatives=level_alts, rule="edu.level")
        if re.search(r"\bstatus\b", l):
            return Answer(edu.get("qualification_status", "Completed"),
                          alternatives=["Completed", "Graduated", "Complete", "Awarded", "Conferred"], rule="edu.status")
        if re.search(r"\b(school|university|institution|college|institute|alma mater)\b", l):
            return Answer(edu.get("university", ""), alternatives=list(edu.get("university_aliases", []) or []),
                          fallbacks=["Other"], rule="edu.school")
        if re.search(r"\b(field of study|major|speciali[sz]ation|discipline|subject|area of study|stream|concentration)\b", l):
            fos = edu.get("field_of_study", "")
            return Answer(fos, alternatives=list(edu.get("field_of_study_aliases", []) or []),
                          fallbacks=field_of_study_fallbacks(fos, edu.get("field_of_study_fallbacks")) + ["Other"],
                          rule="edu.field", proxies_first=True)
        if re.search(r"\b(degree|qualification|course|programme|program|award)\b", l):
            return Answer(edu.get("degree", ""), alternatives=list(edu.get("degree_aliases", []) or []),
                          fallbacks=degree_fallbacks(edu.get("degree", ""), level) + level_alts, rule="edu.degree")
        if re.search(r"\b(gpa|cgpa|grade|result|score|percentage|marks)\b", l):
            return Answer(str(edu.get("gpa", "")), rule="edu.gpa") if edu.get("gpa") else None
        if re.search(r"\b(location|city|country)\b", l):
            return Answer(str(edu.get("location", "")), rule="edu.location")
        if len(l.split()) <= 5 or f.field_type == "date":
            if re.search(r"\b(start|from|commence\w*|begin|first year)\b", l):
                return self._date_answer(edu.get("commencement_date", ""), "edu.start")
            if re.search(r"\b(end|to|complet\w*|graduat\w*|finish\w*|last year)\b", l):
                return self._date_answer(edu.get("completion_date", "") or edu.get("graduation_date", ""), "edu.end")
        return None

    def _personal(self, f: FieldInfo, l: str) -> Optional[Answer]:
        p = self.personal
        if re.search(r"\bin (chinese|arabic|japanese|korean|thai|hindi|local language|native (script|language))\b", l):
            # name in another script: honour 'if no ... please input NA' instructions, else leave for the candidate
            return Answer("NA", rule="name_other_script") if re.search(r"\b(input|enter|put|type|write) (na|n a)\b", l) else None
        option_words = {norm(o).rstrip(".") for o in f.options}
        looks_like_salutation = len(option_words & SALUTATIONS) >= 2
        if re.search(r"\b(salutation|prefix|honorific)\b", l) or (re.fullmatch(r"(name )?title", l) and (looks_like_salutation or f.field_type != "select")):
            if re.fullmatch(r"(name )?title", l) and not looks_like_salutation and f.section:
                return None
            sal = p.get("salutation", "")
            return Answer(sal, alternatives=[sal.rstrip("."), sal.rstrip(".") + "."], rule="salutation")
        if re.search(r"\bmiddle (name|initial)\b", l):
            return None
        if re.search(r"\b(first name|given name|forename|preferred (first )?name|known as|nickname)\b", l):
            return Answer(p.get("first_name", ""), rule="first_name")
        if re.search(r"\b(last name|surname|family name)\b", l):
            return Answer(p.get("last_name", ""), rule="last_name")
        if re.search(r"\b(full name|legal name|applicant name|your name|candidate name|signature)\b|^name$", l):
            return Answer(p.get("full_name", "") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(), rule="full_name")
        if re.search(r"\bpronouns?\b", l):
            pr = p.get("pronouns", "")
            return Answer(pr, alternatives=[f"{pr}/His", pr.replace("/", " / ")], rule="pronouns")
        if re.search(r"\b(date of birth|birth ?date|dob)\b", l):
            return self._date_answer(p.get("date_of_birth", ""), "dob")
        if re.search(r"\b(gender|sex)\b", l) and not re.search(r"\b(identity|orientation)\b", l):
            g = p.get("gender", "")
            return Answer(g, alternatives={"male": ["Man", "M"], "female": ["Woman", "F"]}.get(norm(g), []), rule="gender")
        if re.search(r"\b(nationality|citizenship)\b", l) and not self._is_question(f.label):
            c = self.citizenship.get("country", "")
            return Answer(c, alternatives=["Indian"] if norm(c) == "india" else [], rule="nationality")
        return None

    def _contact(self, f: FieldInfo, l: str) -> Optional[Answer]:
        p = self.personal
        if re.search(r"\b(alternate|alternative|secondary|other) e ?mail\b", l):
            return Answer(p.get("secondary_email", ""), rule="email2")
        if re.search(r"\be ?mail\b", l):
            return Answer(p.get("email", ""), rule="email")
        if re.search(r"\b(extension|ext)\b", l):
            return None
        if re.search(r"\b(device|phone|number) type\b", l):
            return Answer("Mobile", alternatives=["Mobile Phone", "Personal Mobile", "Cell", "Cell Phone", "Mobile/Cell"], rule="phone_type")
        if re.search(r"\b(country|region|itu|dial(l)?ing|calling|phone) (region )?code\b|\bcountry region code\b|\bitu code\b", l):
            ph = self._phone()
            return Answer(ph["code"], alternatives=self._country_code_alternatives(), rule="phone_code")
        if re.search(r"\b(phone|mobile|cell|telephone|contact number)\b", l):
            ph = self._phone()
            return Answer(ph["national"] if self.has_country_code_field else ph["full"], rule="phone")
        if re.search(r"\blinked ?in\b", l):
            return Answer(p.get("linkedin_url", ""), rule="linkedin")
        if re.search(r"\bgit ?hub\b", l):
            return Answer(p.get("github_url", ""), rule="github")
        if re.search(r"\b(portfolio|website|personal url|blog|web site)\b", l):
            return Answer(p.get("portfolio_url", "") or p.get("github_url", ""), rule="website")
        return None

    def _address(self, f: FieldInfo, l: str) -> Optional[Answer]:
        a = self.address
        if f.section or self._is_question(f.label) or len(l.split()) > 6:
            return None  # address words inside sentences ('Please state the source...') are not address fields
        if re.search(r"\bdistrict\b", l):
            return Answer(a.get("city", ""), alternatives=[a.get("state", "")],
                          fallbacks=["Outside Hong Kong", "Overseas", "Outside of Hong Kong", "Others", "Other", "Not Applicable"],
                          rule="district")
        if re.search(r"\baddress (line )?2\b|\bline 2\b", l):
            return None
        if re.search(r"\b(street|address line 1|address line|address 1|address)\b", l) and not re.search(r"\be ?mail\b", l):
            return Answer(a.get("street", ""), rule="street")
        if re.search(r"\b(city|town|suburb)\b", l):
            return Answer(a.get("city", ""), alternatives=["Bengaluru"] if norm(a.get("city", "")) == "bangalore" else [], rule="city")
        if re.search(r"\b(state|province|region|county|territory)\b", l) and not re.search(r"\bcountry\b", l):
            return Answer(a.get("state", ""), rule="state")
        if re.search(r"\b(zip|postal|post code|postcode|pin ?code)\b", l):
            return Answer(str(a.get("zip_code", "")), rule="zip")
        if re.search(r"\bcountry\b", l) and not re.search(r"\b(code|citizen\w*|authori[sz]\w*|eligib\w*|visa)\b", l) and not self._is_question(f.label):
            return Answer(a.get("country", ""), rule="country")
        return None

    def _work_prefs(self, f: FieldInfo, l: str) -> Optional[Answer]:
        add = self.additional
        notice = str(add.get("notice_period", ""))
        is_date_field = f.field_type == "date" or re.search(r"\bdate\b", l) or re.search(r"[dmy]{2}.?[dmy]{2}", f.placeholder.lower())
        if re.search(r"\b(available|availability|earliest|when can you|start date|date available|joining date|able to start)\b", l) \
                and not re.search(r"\bnotice\b", l):
            if is_date_field:
                d = available_start(notice)
                return Answer(d.strftime("%d/%m/%Y"), date=d, rule="available_start")
            return Answer(notice, alternatives=notice_alternatives(notice), rule="available_text")
        if re.search(r"\bnotice\b", l):
            if is_date_field:
                return None  # never put '60 days' into a date
            return Answer(notice, alternatives=notice_alternatives(notice), rule="notice")
        if re.search(r"\b(number of )?months?\W*s? pay\b|\bmonths pay per year\b", l):
            return Answer(str(add.get("months_pay_per_year", "12")), rule="months_pay")
        if re.search(r"\b(present|current)\b.*\b(office|employer|company|organi[sz]ation|institution|workplace)\b", l) and self.experience:
            return Answer(self.experience[0].get("company", ""), rule="current_employer")
        if re.search(r"\b(salary|compensation|ctc|remuneration|pay)\b", l):
            if re.search(r"\bcurrency\b", l):
                return Answer(add.get("salary_currency", ""), rule="currency")
            if re.search(r"\b(current|present|existing|last)\b", l):
                return Answer(str(add.get("current_salary", "")), rule="salary_current")
            return Answer(str(add.get("expected_salary", "")), rule="salary_expected")
        if re.search(r"\byears? of (total |professional |work |overall )?experience$|\btotal (years of )?experience\b", l):
            yrs = re.sub(r"\D", "", str(add.get("years_of_experience", "")))
            return Answer(yrs, alternatives=[f"{yrs}+", f"More than {yrs}", "15+", "10+"], rule="years")
        if re.search(r"\bhighest (level of )?(education|qualification|degree)\b", l):
            hi = str(add.get("highest_education", ""))
            alts = next((v for k, v in LEVEL_SYNONYMS.items() if k in norm(hi)), [])
            return Answer(hi, alternatives=alts, rule="highest_edu")
        if re.search(r"\b(relocat\w*)\b", l):
            return Answer(_yes_no(add.get("willing_to_relocate")), rule="relocate")
        if re.search(r"\bcurrent (employer|company)\b", l) and self.experience:
            return Answer(self.experience[0].get("company", ""), rule="current_employer")
        if re.search(r"\bcurrent (job )?title\b", l) and self.experience:
            return Answer(self.experience[0].get("title", ""), rule="current_title")
        if re.search(r"\b(industry|sector)\b", l) and self.experience and not self._is_question(f.label):
            industry = self.experience[0].get("industry", "")
            return Answer(industry, alternatives=[p.strip() for p in re.split(r"[/,]", industry)], rule="industry")
        return None

    def _eeo(self, f: FieldInfo, l: str) -> Optional[Answer]:
        e = self.eeoc
        if re.search(r"\b(veteran|military)\b", l):
            return Answer(e.get("veteran_status", ""), alternatives=["I am not a protected veteran", "I am not a veteran",
                          "Not a veteran", "No"], rule="veteran")
        if re.search(r"\bdisabilit\w*\b", l):
            return Answer(e.get("disability_status", ""), alternatives=["I do not wish to answer", "I don't wish to answer",
                          "Prefer not to say", "Decline to self identify", "I do not want to answer"], rule="disability")
        if re.search(r"\b(hispanic|latino|latina|latinx)\b", l):
            return Answer("No", rule="hispanic")
        if re.search(r"\b(ethnicity|ethnic|race|racial)\b", l):
            return Answer(e.get("ethnicity", ""), alternatives=["Asian", "Asian (Not Hispanic or Latino)", "Asian Indian",
                          "South Asian", "Indian"], rule="ethnicity")
        if re.search(r"\bgender identity\b", l):
            g = self.personal.get("gender", "")
            return Answer(g, alternatives=["Man", "Male", e.get("gender_identity", "")], rule="gender_identity")
        if re.search(r"\bsexual orientation\b", l):
            return Answer(e.get("sexual_orientation", ""), alternatives=["Heterosexual", "Straight"], rule="orientation")
        if re.search(r"\btransgender\b", l):
            return Answer(e.get("is_transgender", ""), rule="transgender")
        if re.search(r"\b(age range|age group|age bracket)\b|^age$", l):
            return Answer(e.get("age_range", ""), rule="age")
        return None

    def _qa(self, f: FieldInfo, l: str) -> Optional[Answer]:
        if not self.qa:
            return None
        found = self.qa.find_answer(f.label)
        ans = found["answer"] if found else None
        if not ans:
            return None
        alts = list(found.get("alternatives") or [])
        fallbacks = list(found.get("fallbacks") or [])
        if norm(ans) in ("yes", "no") and f.field_type in ("text", "textarea") and not self._is_question(f.label):
            return None  # e.g. 'External Auditor Job Title' is a detail field, not the yes/no question
        if norm(ans) in ("yes", "no"):
            alts += ["Yes, I am", "Yes, I do", "Yes, I have"] if norm(ans) == "yes" else ["No, I am not", "No, I do not", "No, I don't", "No, I have not"]
        return Answer(ans, alternatives=alts, fallbacks=fallbacks,
                      check=(norm(ans) == "yes") if f.field_type == "checkbox" else None, rule="qa")

    def _skills(self, f: FieldInfo, l: str) -> Optional[Answer]:
        if f.field_type == "select" and self.skill_matcher and re.search(r"\bskills?\b", l):
            picks = self.skill_matcher.get_skills_to_select(f.options)
            if picks:
                return Answer(picks[0], rule="skill")
        return None
