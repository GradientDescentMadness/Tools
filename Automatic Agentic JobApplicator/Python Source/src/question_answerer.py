"""
Question-and-answer resolution engine using pattern matching and location awareness.
"""

import re
from typing import Any, Dict, List, Optional
from rapidfuzz import fuzz


class QuestionAnswerer:
    """Answers standard ATS questionnaires based on pre-configured rules."""

    def __init__(self, qa_rules: Dict[str, Any], resume_data: Dict[str, Any], threshold: int = 75):
        self.qa_rules = qa_rules
        self.resume_data = resume_data
        self.threshold = threshold
        self.indexed_rules = self._build_pattern_index()

    def _build_pattern_index(self) -> List[tuple[str, Dict[str, Any]]]:
        index = []
        rules = self.qa_rules.get("rules", [])
        for rule in rules:
            for pattern in rule.get("patterns", []):
                index.append((pattern.lower().strip(), rule))
        return index

    def find_answer(self, question_text: str) -> Optional[Dict[str, Any]]:
        """Find the matching rule for a question using token sort ratio."""
        cleaned_question = re.sub(r"[^\w\s]", " ", question_text.lower()).strip()
        cleaned_question = re.sub(r"\s+", " ", cleaned_question)

        best_match = None
        best_score = 0

        for pattern, rule in self.indexed_rules:
            # Direct inclusion shortcut
            if pattern in cleaned_question:
                score = 95
            else:
                score = int(fuzz.token_sort_ratio(pattern, cleaned_question))

            if score > best_score:
                best_score = score
                best_match = rule

        if best_score >= self.threshold and best_match:
            resolved_answer = self._resolve_answer(best_match)
            return {
                "answer": resolved_answer,
                "answer_type": best_match.get("answer_type", "text"),
                "confidence": best_score,
                "notes": best_match.get("notes", ""),
            }
        return None

    def _resolve_answer(self, rule: Dict[str, Any]) -> str:
        answer = rule.get("answer", "")
        if answer != "":
            return answer

        notes = rule.get("notes", "")
        # Lookup resume_data if referenced in notes
        if "resume_data.personal.gender" in notes:
            return self.resume_data.get("personal", {}).get("gender", "")
        if "resume_data.personal.linkedin_url" in notes:
            return self.resume_data.get("personal", {}).get("linkedin_url", "")
        if "resume_data.additional.cover_letter" in notes:
            return self.resume_data.get("additional", {}).get("cover_letter", "")

        return ""

    def get_answer(self, question_text: str) -> Optional[str]:
        """Convenience method returning pure answer string or None."""
        match = self.find_answer(question_text)
        return match["answer"] if match else None


class LocationAwareAnswerer(QuestionAnswerer):
    """Adapts legal work authorization and visa sponsorship answers by job location."""

    def __init__(self, qa_rules: Dict[str, Any], resume_data: Dict[str, Any], job_location: str = ""):
        super().__init__(qa_rules, resume_data)
        self.job_location = job_location.lower().strip()
        self.candidate_country = resume_data.get("citizenship", {}).get("country", "India").lower()
        self.is_home_country = self._is_target_in_home_country()

    def _is_target_in_home_country(self) -> bool:
        if not self.job_location:
            return True
        home_aliases = {"india", "ind", "bengaluru", "bangalore", "mumbai", "delhi", "hyderabad", "pune", "chennai"}
        return any(alias in self.job_location for alias in home_aliases)

    def find_answer(self, question_text: str) -> Optional[Dict[str, Any]]:
        q_lower = question_text.lower()

        # Handle "Authorized to work in X"
        if any(k in q_lower for k in ["legally authorized", "authorized to work", "legal right to work"]):
            if "india" in q_lower or (self.is_home_country and "united states" not in q_lower and "us" not in q_lower):
                return {"answer": "Yes", "answer_type": "radio", "confidence": 98, "notes": "Location-aware rule: Authorized in home country"}
            else:
                return {"answer": "No", "answer_type": "radio", "confidence": 98, "notes": "Location-aware rule: Not authorized internationally without visa"}

        # Handle "Require visa / sponsorship"
        if any(k in q_lower for k in ["require sponsorship", "need visa", "need sponsorship", "immigration sponsorship"]):
            if self.is_home_country and "india" in self.job_location:
                return {"answer": "No", "answer_type": "radio", "confidence": 98, "notes": "Location-aware rule: Home country role requires no visa"}
            else:
                return {"answer": "Yes", "answer_type": "radio", "confidence": 98, "notes": "Location-aware rule: Foreign role requires sponsorship"}

        return super().find_answer(question_text)

