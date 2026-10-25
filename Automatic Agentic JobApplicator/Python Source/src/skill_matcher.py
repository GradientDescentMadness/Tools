"""
Skill matching engine using RapidFuzz and technology abbreviation mapping.
"""

import re
from typing import Dict, List, Tuple
from rapidfuzz import fuzz


class AbbreviationMap:
    """Dictionary of tech synonyms, variations, and common abbreviations."""

    SYNONYM_GROUPS = [
        {"js", "javascript"},
        {"ts", "typescript"},
        {"py", "python"},
        {"react", "reactjs", "react.js"},
        {"vue", "vuejs", "vue.js"},
        {"node", "nodejs", "node.js"},
        {"next", "nextjs", "next.js"},
        {"postgres", "postgresql", "pgsql"},
        {"mongo", "mongodb"},
        {"k8s", "kubernetes"},
        {"aws", "amazon web services"},
        {"gcp", "google cloud", "google cloud platform"},
        {"azure", "microsoft azure"},
        {"ml", "machine learning"},
        {"ai", "artificial intelligence"},
        {"dl", "deep learning"},
        {"nlp", "natural language processing"},
        {"cv", "computer vision"},
        {"ci/cd", "cicd", "continuous integration", "continuous delivery"},
        {"devops", "dev ops"},
        {"golang", "go"},
        {"cpp", "c++"},
        {"csharp", "c#", "dotnet", ".net"},
        {"rest", "rest api", "restful"},
        {"graphql", "gql"},
        {"docker", "containerization"},
        {"linux", "unix"},
    ]

    @classmethod
    def expand(cls, text: str) -> List[str]:
        """Expand term into all associated synonyms and aliases."""
        clean = text.lower().strip()
        variants = {clean}
        for group in cls.SYNONYM_GROUPS:
            if clean in group:
                variants.update(group)
        return list(variants)


class SkillMatcher:
    """Fuzzy matching engine comparing user skills with ATS dropdown options."""

    def __init__(self, user_skills: List[str], threshold: int = 70):
        self.user_skills = user_skills
        self.threshold = threshold
        self.normalized_skills = [self._normalize(s) for s in user_skills]

    @staticmethod
    def _normalize(text: str) -> str:
        """Strip non-alphanumeric noise while preserving key programming tokens (+, #)."""
        t = text.lower().strip()
        t = re.sub(r"[._/\\-]", " ", t)
        return re.sub(r"\s+", " ", t).strip()

    def find_best_match(self, dropdown_option: str) -> Tuple[str | None, int]:
        """Find the user skill that best matches a specific dropdown choice."""
        normalized_option = self._normalize(dropdown_option)
        expanded_options = AbbreviationMap.expand(normalized_option)

        best_skill = None
        highest_score = 0

        for original_skill, norm_skill in zip(self.user_skills, self.normalized_skills):
            expanded_skills = AbbreviationMap.expand(norm_skill)

            for opt_variant in expanded_options:
                for skill_variant in expanded_skills:
                    if opt_variant == skill_variant:
                        return original_skill, 100

                    token_score = fuzz.token_sort_ratio(opt_variant, skill_variant)
                    partial_score = fuzz.partial_ratio(opt_variant, skill_variant)
                    composite = int(token_score * 0.65 + partial_score * 0.35)

                    if composite > highest_score:
                        highest_score = composite
                        best_skill = original_skill

        if highest_score >= self.threshold:
            return best_skill, highest_score
        return None, highest_score

    def match_dropdown_options(self, dropdown_options: List[str]) -> List[Dict]:
        """Process a list of dropdown options, flagging matching choices."""
        results = []
        for opt in dropdown_options:
            matched, score = self.find_best_match(opt)
            results.append({
                "option": opt,
                "matched_skill": matched,
                "score": score,
                "should_select": matched is not None and score >= self.threshold,
            })
        return results

    def get_skills_to_select(self, dropdown_options: List[str]) -> List[str]:
        """Convenience method returning exact dropdown options to click/select."""
        matches = self.match_dropdown_options(dropdown_options)
        return [m["option"] for m in matches if m["should_select"]]
