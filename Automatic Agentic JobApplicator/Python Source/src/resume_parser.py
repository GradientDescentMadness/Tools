"""
Resume text parser extracting candidate information from PDF and DOCX files.
"""

from pathlib import Path
import re
from typing import Any, Dict, List
import docx
import pdfplumber


class ResumeParser:
    """Parses PDF or Word (.docx) resumes into structured profile sections."""

    def __init__(self, file_path: str):
        self.path = Path(file_path)
        if not self.path.exists():
            raise FileNotFoundError(f"Resume file not found: {file_path}")

    def parse(self) -> Dict[str, Any]:
        suffix = self.path.suffix.lower()
        if suffix == ".pdf":
            raw_text = self._parse_pdf()
        elif suffix in [".docx", ".doc"]:
            raw_text = self._parse_docx()
        else:
            raise ValueError(f"Unsupported file format: {suffix}")

        contact = self._extract_contact_info(raw_text)
        sections = self._extract_sections(raw_text)
        skills = self._extract_skills(sections.get("skills", ""))

        return {
            "contact_info": contact,
            "skills": skills,
            "sections": sections,
            "raw_text": raw_text[:2000],
        }

    def _parse_pdf(self) -> str:
        text_chunks = []
        with pdfplumber.open(self.path) as pdf:
            for page in pdf.pages:
                extracted = page.extract_text()
                if extracted:
                    text_chunks.append(extracted)
        return "\n".join(text_chunks)

    def _parse_docx(self) -> str:
        doc = docx.Document(self.path)
        return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])

    def _extract_contact_info(self, text: str) -> Dict[str, str]:
        email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", text)
        phone_match = re.search(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", text)
        linkedin_match = re.search(r"https?://(?:www\.)?linkedin\.com/in/[\w-]+", text)
        github_match = re.search(r"https?://(?:www\.)?github\.com/[\w-]+", text)

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        first_line_name = lines[0] if lines else "Candidate"

        return {
            "name": first_line_name,
            "email": email_match.group(0) if email_match else "",
            "phone": phone_match.group(0) if phone_match else "",
            "linkedin": linkedin_match.group(0) if linkedin_match else "",
            "github": github_match.group(0) if github_match else "",
        }

    def _extract_sections(self, text: str) -> Dict[str, str]:
        section_headers = {
            "experience": r"(?:work\s+experience|professional\s+experience|experience)",
            "education": r"(?:education|academic\s+background|qualifications)",
            "skills": r"(?:technical\s+skills|skills|core\s+competencies)",
            "projects": r"(?:projects|key\s+projects)",
            "certifications": r"(?:certifications|licenses)",
        }

        pattern = r"\n\s*(" + "|".join(section_headers.values()) + r")\s*[:\n]"
        matches = list(re.finditer(pattern, text, re.IGNORECASE))

        sections: Dict[str, str] = {}
        if not matches:
            return sections

        for i in range(len(matches)):
            curr_match = matches[i]
            matched_title = curr_match.group(1).lower()
            start_pos = curr_match.end()
            end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            content = text[start_pos:end_pos].strip()

            for key, reg in section_headers.items():
                if re.match(reg, matched_title, re.IGNORECASE):
                    sections[key] = content
                    break

        return sections

    def _extract_skills(self, text: str) -> List[str]:
        if not text:
            return []
        tokens = re.split(r"[,|\n•\u2022\-\;]", text)
        cleaned = [t.strip() for t in tokens if t.strip() and len(t.strip()) > 1]
        # Preserve order while deduplicating
        seen = set()
        unique = []
        for c in cleaned:
            if c.lower() not in seen:
                seen.add(c.lower())
                unique.append(c)
        return unique


def parse_resume_file(file_path: str) -> Dict[str, Any]:
    """Convenience functional wrapper for ResumeParser."""
    return ResumeParser(file_path).parse()
