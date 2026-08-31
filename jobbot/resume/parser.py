"""Extract text from a resume file and structure it into a Profile via Claude."""
from __future__ import annotations

import logging
from pathlib import Path

from jobbot.llm import call_tool
from jobbot.resume.schema import Profile

log = logging.getLogger(__name__)

_PROFILE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "email": {"type": "string"},
        "phone": {"type": "string"},
        "location": {"type": "string"},
        "links": {
            "type": "object",
            "properties": {
                "linkedin": {"type": "string"},
                "github": {"type": "string"},
                "portfolio": {"type": "string"},
            },
        },
        "summary": {"type": "string"},
        "skills": {"type": "array", "items": {"type": "string"}},
        "experience": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "highlights": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "education": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "school": {"type": "string"},
                    "degree": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                },
            },
        },
    },
    "required": ["name", "email"],
}

_SYSTEM = """You extract structured facts from a resume's raw text. Only report
facts that are explicitly present in the text. Never infer or invent a
company, title, date, or skill that isn't written there. If a field is not
present, omit it or leave it empty. Dates should be normalized to YYYY-MM
where possible, or "present" for current roles."""


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    if suffix in (".docx", ".doc"):
        import docx

        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    if suffix in (".txt", ".md"):
        return path.read_text()
    raise ValueError(f"Unsupported resume format: {suffix}")


def parse_resume(path: Path) -> Profile:
    text = extract_text(path)
    if not text.strip():
        raise ValueError(f"No extractable text found in {path}")

    result = call_tool(
        system=_SYSTEM,
        user_message=f"Resume text:\n\n{text}",
        tool_name="record_profile",
        tool_description="Record the structured facts extracted from the resume.",
        input_schema=_PROFILE_TOOL_SCHEMA,
        # 2048 was chosen to ease TPM pressure (see below), but a genuinely
        # dense resume — long skill lists, several detailed roles — can
        # need more than that to finish; confirmed live: several imports hit
        # Groq's 'tool_use_failed' 400 with generation visibly cut off
        # mid-array at the 2048 ceiling, burning a retry (itself more TPM
        # spend) rather than succeeding on the first attempt. 3072 covers
        # what was observed without reverting all the way to the old
        # default (4096), which was needlessly reserving most of a minute's
        # worth of Groq's free-tier TPM budget per call for an average-length
        # resume.
        max_tokens=3072,
    )
    return Profile.model_validate(result)
