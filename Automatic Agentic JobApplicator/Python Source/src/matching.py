"""
Shared text matching primitives: normalization, whole-word keyword tests,
dropdown option selection with aliases and nearest-proxy fallbacks, and date handling.
"""

import re
from datetime import date, timedelta
from typing import Iterable, List, Optional, Sequence

from rapidfuzz import fuzz

PLACEHOLDER_OPTIONS = {
    "", "select", "select one", "please select", "choose", "choose one", "none selected",
    "select an option", "please choose", "no selection", "select...", "--", "not specified", "not selected",
    "day", "month", "year", "dd", "mm", "yyyy",
}

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def date_part_of(options: Sequence[str]) -> Optional[str]:
    """'day' / 'month' / 'year' when a dropdown is one part of a split date, else None."""
    vals = [norm(o) for o in options if o and not is_placeholder(o)]
    if len(vals) < 5:
        return None
    if all(v.isdigit() for v in vals):
        nums = [int(v) for v in vals]
        if min(nums) > 1800:
            return "year"
        if max(nums) == 31 and len(nums) >= 28:
            return "day"
        if max(nums) == 12 and len(nums) == 12:
            return "month"
        return None
    if sum(1 for v in vals if v[:3] in MONTHS) >= 12:
        return "month"
    return None


def date_part_candidates(d: date, part: str) -> List[str]:
    if part == "day":
        return [str(d.day), f"{d.day:02d}"]
    if part == "month":
        return [d.strftime("%B"), d.strftime("%b"), str(d.month), f"{d.month:02d}"]
    return [str(d.year)]

# Nearest-proxy fields of study, used when an ATS list lacks the candidate's exact field.
FIELD_OF_STUDY_PROXIES = [
    (r"analytic|machine learning|artificial intelligence|\bai\b|\bml\b|data science|data analytics",
     ["Data Science", "Analytics", "Business Analytics", "Computer Science", "Statistics",
      "Information Technology", "Mathematics"]),
    (r"production engineering|manufacturing",
     ["Production Engineering", "Industrial Engineering", "Manufacturing Engineering",
      "Industrial and Production Engineering", "Mechanical Engineering", "Engineering"]),
    (r"business|management|\bmba\b|commerce",
     ["Business Administration", "Business Administration and Management", "Management",
      "Business", "Finance", "Commerce"]),
    (r"computer|software|information technology",
     ["Computer Science", "Computer Engineering", "Information Technology", "Software Engineering"]),
    (r"engineering|technology",
     ["Engineering", "Engineering Technology", "General Engineering", "Technology"]),
]

# Industry / 'Type of Business' picklists name the same sector many ways.
INDUSTRY_PROXIES = [
    (r"bank|financ",
     ["Banking", "Financial Services", "Banking & Financial Services", "Banking and Finance", "Finance",
      "Financial Institutions", "Retail Banking"]),
    (r"insurance", ["Insurance", "Financial Services"]),
    (r"information technology|software|\bit\b",
     ["Information Technology", "Information Technology & Services", "IT Services", "Technology",
      "Computer Software", "Software"]),
    (r"consult", ["Consulting", "Management Consulting", "Professional Services", "IT Consulting"]),
    (r"automo", ["Automotive", "Automobile", "Automobiles", "Motor Vehicles"]),
    (r"manufactur", ["Manufacturing", "Industrial Manufacturing", "Engineering"]),
]


# Nearest-equivalent degrees for ATS lists that don't carry the exact qualification.
DEGREE_PROXIES = [
    (r"\bb ?tech|bachelor of technology|\bb ?e\b|bachelor of engineering",
     ["Bachelor of Engineering", "B.Eng", "BE", "B.E.", "Bachelor of Technology", "B.Tech",
      "Bachelor of Science", "BS", "B.Sc", "Bachelor's Degree", "Bachelors", "Undergraduate Degree"]),
    (r"\bm ?s\b|\bmsc\b|master of science|\bm ?tech|\bm ?eng",
     ["Master of Science", "M.Sc", "MS", "M.S.", "Master's Degree", "Masters", "Postgraduate Degree",
      "M.Eng", "Master of Engineering", "M.Tech"]),
    (r"\bmba\b|business administration|\bpgdm\b",
     ["MBA", "Master of Business Administration", "Masters of Business Administration",
      "Master's Degree", "Masters", "Postgraduate Degree"]),
    (r"\bph ?d\b|doctor",
     ["PhD", "Ph.D.", "Doctorate", "Doctoral Degree", "Doctor of Philosophy"]),
]


def degree_fallbacks(degree: str, level: str = "") -> List[str]:
    """Equivalent / nearest degree names, most specific first, ending with the generic level."""
    out: List[str] = []
    for pattern, proxies in DEGREE_PROXIES:
        if re.search(pattern, norm(degree)):
            out.extend(p for p in proxies if p not in out)
    if level and level not in out:
        out.append(level)
    return out


def norm(text: str) -> str:
    """Lowercase, unify '&' and apostrophes, strip punctuation (keeping + and #), collapse spaces."""
    t = str(text or "").lower().replace("&", " and ").replace("’", "'")
    t = re.sub(r"'s\b", "s", t)
    t = re.sub(r"[^\w+#]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def compact(text: str) -> str:
    """Normalization without spaces, so 'M.S.' == 'MS' and 'B. Tech.' == 'BTech'."""
    return norm(text).replace(" ", "")


def has_word(text: str, *phrases: str) -> bool:
    """True if any phrase occurs in text on word boundaries (so 'city' does not match 'ethnicity')."""
    t = norm(text)
    for p in phrases:
        if re.search(rf"(?<![\w]){re.escape(norm(p))}(?![\w])", t):
            return True
    return False


def matches(text: str, pattern: str) -> bool:
    """Regex search against normalized text."""
    return re.search(pattern, norm(text)) is not None


def is_placeholder(option: str) -> bool:
    n = norm(option)
    return n in PLACEHOLDER_OPTIONS or n.startswith("select ") or n.startswith("please select")


def _close_match(opts: list, cands: list) -> tuple:
    """Exact (normalized/compacted) match, else the strongest whole-string fuzzy match and its score."""
    for c_norm, c_comp in cands:
        for o, o_norm, o_comp in opts:
            if c_norm == o_norm or c_comp == o_comp:
                return o, 100.0
    best, best_score = None, 0.0
    for rank, (c_norm, _) in enumerate(cands):
        if len(c_norm) <= 4 or re.fullmatch(r"[\d+ ]+", c_norm):
            continue  # codes and short values ('+91', 'MS', 'Yes') must match exactly, never 'closely' ('+291')
        for o, o_norm, _ in opts:
            score = fuzz.token_sort_ratio(c_norm, o_norm) - rank * 0.01
            if score > best_score:
                best, best_score = o, score
    return best, best_score


def _partial_match(opts: list, cands: list) -> Optional[str]:
    """
    Option starts with / contains a candidate as whole words ('India' -> 'India (+91)'). When several
    options qualify, the one matching the most candidate terms wins ('Asian - Indian' over 'Asian - Other'
    for ['Asian', 'Indian']), then the earliest candidate, then the shortest option.
    """
    scored = {}
    for rank, (c_norm, _) in enumerate(cands):
        for o, o_norm, _ in opts:
            hit = re.match(rf"{re.escape(c_norm)}\b", o_norm) or (
                (len(c_norm) >= 4 or " " in c_norm) and re.search(rf"\b{re.escape(c_norm)}\b", o_norm))
            if hit:
                count, first = scored.get(o, (0, rank))
                scored[o] = (count + 1, min(first, rank))
    if not scored:
        return None
    return min(scored, key=lambda o: (-scored[o][0], scored[o][1], len(o)))


def best_option(
    options: Sequence[str],
    candidates: Iterable[str],
    fallbacks: Iterable[str] = (),
    threshold: int = 75,
    proxies_before_partial: bool = False,
) -> Optional[str]:
    """
    Pick the option that best represents the candidate value.

    Candidates are tried most-specific first (value, then aliases), so a generic alias such as
    'Master' can never beat 'Master of Science'. Fallbacks (nearest proxies) are only consulted
    when no candidate matches. With proxies_before_partial, an exact proxy ('Computer Science')
    beats a loose partial match ('Artificial Intelligence and Robotics' for 'Artificial Intelligence').
    """
    opts = [(o, norm(o), compact(o)) for o in options if o and not is_placeholder(o)]
    if not opts:
        return None
    groups = [[(norm(c), compact(c)) for c in g if c and norm(c)] for g in (list(candidates), list(fallbacks))]

    if proxies_before_partial:
        for cands in groups:
            best, score = _close_match(opts, cands) if cands else (None, 0)
            if score >= 88:
                return best
        # partial matches may only add qualifiers ('(General)'), not another subject ('... and Robotics')
        minor = {"and", "of", "the", "in", "general", "studies", "other", "program", "programme", "degree"}
        for cands in groups:
            hit = _partial_match(opts, cands) if cands else None
            if hit:
                extra = set(norm(hit).split()) - set(" ".join(c for c, _ in cands).split()) - minor
                if not extra:
                    return hit
        return None

    for cands in groups:
        if not cands:
            continue
        best, score = _close_match(opts, cands)
        if score >= 88:
            return best
        hit = _partial_match(opts, cands)
        if hit:
            return hit
        if score >= threshold:
            return best
    return None


def field_of_study_fallbacks(field: str, configured: Optional[List[str]] = None) -> List[str]:
    """Configured fallbacks first, then built-in nearest proxies for the field."""
    result = list(configured or [])
    for pattern, proxies in FIELD_OF_STUDY_PROXIES:
        if re.search(pattern, norm(field)):
            result.extend(p for p in proxies if p not in result)
    return result


def industry_fallbacks(industry: str) -> List[str]:
    """Nearest picklist names for an industry such as 'Banking / Financial Services'."""
    result: List[str] = []
    for pattern, proxies in INDUSTRY_PROXIES:
        if re.search(pattern, norm(industry)):
            result.extend(p for p in proxies if p not in result)
    return result


def parse_date(value: str) -> Optional[date]:
    """Parse resume dates: DD/MM/YYYY, MM/YYYY, YYYY-MM(-DD) or YYYY. 'Present' -> None."""
    v = str(value or "").strip()
    if not v or v.lower() in ("present", "current", "now", "till date"):
        return None
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", v)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{4})", v)
    if m:
        return date(int(m.group(2)), int(m.group(1)), 1)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", v)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    m = re.fullmatch(r"(\d{4})", v)
    if m:
        return date(int(m.group(1)), 1, 1)
    return None


def format_date(d: date, fmt_hint: str = "", default_fmt: str = "dd/mm/yyyy") -> str:
    """Format a date following an input's placeholder/title hint (e.g. 'MM/YYYY', 'yyyy-mm-dd')."""
    hint = (fmt_hint or "").lower()
    fmt = default_fmt
    for candidate in ("yyyy-mm-dd", "dd/mm/yyyy", "mm/dd/yyyy", "dd.mm.yyyy", "dd-mm-yyyy",
                      "mm/yyyy", "mm-yyyy", "yyyy"):
        if candidate in hint.replace(" ", ""):
            fmt = candidate
            break
    else:
        if re.search(r"d{1,2}/m{1,2}/y{2,4}", hint):
            fmt = "dd/mm/yyyy"
        elif re.search(r"m{1,2}/d{1,2}/y{2,4}", hint):
            fmt = "mm/dd/yyyy"
    return (fmt.replace("yyyy", f"{d.year:04d}").replace("mm", f"{d.month:02d}").replace("dd", f"{d.day:02d}"))


def notice_days(text: str) -> Optional[int]:
    """'60 days' -> 60, '2 months' -> 60, '8 weeks' -> 56."""
    m = re.search(r"(\d+)\s*(day|week|month)", str(text or "").lower())
    if not m:
        return None
    n = int(m.group(1))
    return n * {"day": 1, "week": 7, "month": 30}[m.group(2)]


def notice_alternatives(text: str) -> List[str]:
    """Equivalent phrasings of a notice period, for matching dropdown options."""
    days = notice_days(text)
    if days is None:
        return [text]
    months, weeks = round(days / 30), round(days / 7)
    alts = [text, f"{days} days", f"{months} months", f"{months} month", f"{weeks} weeks"]
    if days > 90:
        alts += ["More than 3 months", "More than 90 days", "90+ days"]
    elif days > 60:
        alts += ["3 months", "90 days"]
    elif days > 30:
        alts += ["2 months", "60 days", "1-2 months"]
    else:
        alts += ["1 month", "30 days", "Immediate"]
    return list(dict.fromkeys(alts))


def available_start(notice: str, today: Optional[date] = None) -> date:
    """Earliest start date = today + notice period."""
    return (today or date.today()) + timedelta(days=notice_days(notice) or 0)
