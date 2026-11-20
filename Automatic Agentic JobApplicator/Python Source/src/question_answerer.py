"""
Question-and-answer resolution engine using whole-word pattern matching and location awareness.
"""

import re
from typing import Any, Callable, Dict, List, Optional

from rapidfuzz import fuzz

from .matching import norm

# Places that identify a job's country. Checked on word boundaries against location text and URLs.
COUNTRY_ALIASES: Dict[str, List[str]] = {
    "India": ["india", "indian", "bengaluru", "bangalore", "mumbai", "pune", "hyderabad", "chennai", "delhi",
              "new delhi", "gurgaon", "gurugram", "noida", "kolkata", "ahmedabad", "kochi", "jaipur",
              "karnataka", "maharashtra", "telangana", "tamil nadu", "haryana", "uttar pradesh",
              "west bengal", "gujarat", "kerala"],
    "United States": ["united states", "usa", "u s a", "america", "american", "new york", "nyc",
                      "san francisco", "seattle", "chicago", "boston", "wilmington", "jersey city",
                      "tampa", "dallas", "charlotte", "columbus", "plano", "atlanta", "texas",
                      "california", "delaware", "florida"],
    "United Kingdom": ["united kingdom", "uk", "u k", "britain", "british", "great britain", "england",
                       "london", "scotland", "edinburgh", "belfast", "glasgow"],
    "Canada": ["canada", "canadian", "toronto", "vancouver", "montreal"],
    "Australia": ["australia", "australian", "sydney", "melbourne", "brisbane", "perth"],
    "New Zealand": ["new zealand", "auckland", "wellington"],
    "Singapore": ["singapore", "singaporean"],
    "United Arab Emirates": ["uae", "united arab emirates", "dubai", "abu dhabi"],
    "Hong Kong": ["hong kong"],
    "Ireland": ["ireland", "dublin"],
    "Germany": ["germany", "frankfurt", "berlin", "munich"],
    "Netherlands": ["netherlands", "amsterdam"],
    "Philippines": ["philippines", "manila"],
    "Japan": ["japan", "tokyo"],
    "Poland": ["poland", "warsaw", "krakow"],
    "Mexico": ["mexico"],
    "European Union": ["european union", "eea"],
}

# Official spellings country picklists use instead of the short name.
COUNTRY_OPTION_NAMES: Dict[str, List[str]] = {
    "United States": ["United States of America", "USA", "U.S.A.", "US"],
    "United Kingdom": ["United Kingdom of Great Britain and Northern Ireland", "UK", "Great Britain"],
    "United Arab Emirates": ["UAE"],
    "Hong Kong": ["Hong Kong SAR", "Hong Kong SAR, China", "Hong Kong, China"],
}


def country_alternatives(country: str) -> List[str]:
    return list(COUNTRY_OPTION_NAMES.get(country, []))


def is_country_name(text: str) -> bool:
    """'US', 'India', 'United Kingdom' - a country itself, not a city or state that implies one."""
    names = {norm(c) for c in COUNTRY_ALIASES} | {norm(a) for alts in COUNTRY_OPTION_NAMES.values() for a in alts}
    return norm(text) in names | {"usa", "u s a", "uk", "u k", "america", "britain", "great britain", "england"}

DEMONYMS = {"India": "Indian", "United States": "US", "United Kingdom": "British", "Australia": "Australian",
            "Canada": "Canadian", "Singapore": "Singaporean", "New Zealand": "New Zealand", "Ireland": "Irish",
            "Germany": "German", "Philippines": "Filipino", "Japan": "Japanese", "Mexico": "Mexican"}

US_STATE_CODE = re.compile(
    r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|"
    r"NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b"
)

WORK_AUTH = re.compile(
    r"\b(authori[sz]ed|authori[sz]ation|eligible|eligibility|entitled|permitted|legal right|right) to (work|be employed)\b"
    r"|\bwork (authori[sz]ation|rights|permit|eligibility)\b"
)
CITIZENSHIP = re.compile(r"\b(citizen|citizenship|nationality|national of|passport holder)\b")
SPONSORSHIP = re.compile(r"\bsponsor(ship|ed)?\b|\b(require|need|needs|requires)\b.{0,40}\bvisa\b")


def find_countries(*texts: str) -> List[str]:
    """Return countries mentioned in the given texts (location strings, URLs, questions)."""
    found: List[str] = []
    for raw in texts:
        if not raw:
            continue
        raw = re.sub(r"\b[a-z]{2}[-_][A-Z]{2}\b", " ", raw)  # locale codes such as en-US / en_GB
        # "us" is too ambiguous in lowercase ("work for us"), so only uppercase US / U.S. counts.
        if US_STATE_CODE.search(raw) or re.search(r"\bU\.?S\.?(?!\w)", raw):
            found.append("United States")
        t = norm(re.sub(r"[-_/]", " ", raw))
        for country, aliases in COUNTRY_ALIASES.items():
            for alias in aliases:
                if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", t):
                    found.append(country)
                    break
    return list(dict.fromkeys(found))


class QuestionAnswerer:
    """Answers standard ATS questionnaires based on pre-configured rules."""

    def __init__(self, qa_rules: Dict[str, Any], resume_data: Dict[str, Any], threshold: int = 85):
        self.qa_rules = qa_rules or {}
        self.resume_data = resume_data or {}
        self.threshold = threshold
        self.indexed_rules = self._build_pattern_index()

    def _build_pattern_index(self) -> List[tuple]:
        index = []
        for rule in self.qa_rules.get("rules", []) or []:
            for pattern in rule.get("patterns", []) or []:
                index.append((norm(pattern), rule))
        return index

    def find_answer(self, question_text: str) -> Optional[Dict[str, Any]]:
        """Find the matching rule: whole-word containment first (longest pattern wins), then fuzzy."""
        q = norm(question_text)
        if not q:
            return None

        best_match, best_score = None, 0.0
        for pattern, rule in self.indexed_rules:
            if not pattern:
                continue
            if re.search(rf"(?<!\w){re.escape(pattern)}(?:s|es)?(?!\w)", q):
                score = 90 + min(len(pattern), 40) / 8  # longer, more specific patterns win
            elif len(pattern.split()) >= 3 and len(q.split()) <= len(pattern.split()) * 2:
                score = fuzz.token_sort_ratio(pattern, q)
            else:
                continue
            if score > best_score:
                best_score, best_match = score, rule

        if best_match and best_score >= self.threshold:
            answer = self._resolve_answer(best_match)
            if answer == "":
                return None
            return {
                "answer": answer,
                "answer_type": best_match.get("answer_type", "text"),
                "confidence": int(best_score),
                "notes": best_match.get("notes", ""),
                "fallbacks": list(best_match.get("fallbacks", []) or []),
            }
        return None

    def _lookup(self, dotted: str) -> str:
        node: Any = self.resume_data
        for part in dotted.replace("resume_data.", "").split("."):
            if not isinstance(node, dict):
                return ""
            node = node.get(part)
        if isinstance(node, bool):
            return "Yes" if node else "No"
        return "" if node is None else str(node)

    def _resolve_answer(self, rule: Dict[str, Any]) -> str:
        if rule.get("answer_from"):
            return self._lookup(rule["answer_from"])
        answer = rule.get("answer", "")
        if answer != "":
            return str(answer)
        m = re.search(r"resume_data\.[\w.]+", rule.get("notes", ""))
        return self._lookup(m.group(0)) if m else ""

    def get_answer(self, question_text: str) -> Optional[str]:
        """Convenience method returning pure answer string or None."""
        match = self.find_answer(question_text)
        return match["answer"] if match else None


class LocationAwareAnswerer(QuestionAnswerer):
    """
    Answers work-authorization and sponsorship questions from the candidate's citizenship config
    and the job's country. When the job's country cannot be determined it asks the user once
    (via `ask`) instead of guessing; with no answer the question is left for manual review.
    """

    def __init__(
        self,
        qa_rules: Dict[str, Any],
        resume_data: Dict[str, Any],
        job_location: str = "",
        job_url: str = "",
        ask: Optional[Callable[[str], Optional[str]]] = None,
    ):
        super().__init__(qa_rules, resume_data)
        cit = (resume_data or {}).get("citizenship", {}) or {}
        authorized = cit.get("work_authorized_countries") or []
        if isinstance(authorized, str):
            authorized = re.split(r"[,;/]", authorized)
        self.authorized = {c for a in authorized + [cit.get("country", "")] for c in find_countries(a)}
        self.citizen_of = (find_countries(cit.get("country", "")) or [None])[0]
        self.job_location = job_location or ""
        located = find_countries(job_location) or find_countries(job_url)
        self.job_country: Optional[str] = located[0] if located else None
        self._ask = ask
        self._asked = False

    def resolve_job_country(self) -> Optional[str]:
        if self.job_country is None and self._ask and not self._asked:
            self._asked = True
            reply = self._ask(f"Couldn't tell which country this job is in (location: '{self.job_location or 'unknown'}').")
            found = find_countries(reply or "")
            self.job_country = found[0] if found else None
        return self.job_country

    def _country_for(self, question_text: str) -> Optional[str]:
        mentioned = find_countries(question_text)
        return mentioned[0] if mentioned else self.resolve_job_country()

    def find_answer(self, question_text: str) -> Optional[Dict[str, Any]]:
        q = norm(question_text)
        is_sponsorship = bool(SPONSORSHIP.search(q))
        is_work_auth = bool(WORK_AUTH.search(q)) and not is_sponsorship

        if is_work_auth or is_sponsorship:
            country = self._country_for(question_text)
            if country is None:
                return None  # unknown jurisdiction: leave for the candidate to answer
            authorized = country in self.authorized
            alternatives: List[str] = []
            if is_sponsorship:
                answer = "No" if authorized else "Yes"
            else:
                answer = "Yes" if authorized else "No"
                if authorized and country == self.citizen_of:
                    dem = DEMONYMS.get(country, country)
                    alternatives = [f"{dem} Citizen", f"Citizen of {country}", f"{country} Citizen", "Citizen"]
            return {"answer": answer, "answer_type": "radio", "confidence": 98, "alternatives": alternatives,
                    "notes": f"Location-aware: job/question country = {country}, authorized = {authorized}"}

        if CITIZENSHIP.search(q):
            named = find_countries(question_text)
            if len(named) == 1 and self.citizen_of:
                answer = "Yes" if named[0] == self.citizen_of else "No"
                return {"answer": answer, "answer_type": "radio", "confidence": 97, "alternatives": [], "fallbacks": [],
                        "notes": f"Citizenship: candidate is a citizen of {self.citizen_of}"}

        return super().find_answer(question_text)
