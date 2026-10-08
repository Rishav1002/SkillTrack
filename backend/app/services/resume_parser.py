"""Secure PDF parsing and lightweight section detection."""

from __future__ import annotations
import re
from pathlib import Path
import fitz

SECTIONS = (
    "summary",
    "skills",
    "projects",
    "education",
    "certifications",
    "experience",
)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\x00", " ")).strip()


def parse_pdf(path: str | Path) -> dict:
    doc = fitz.open(str(path))
    text = "\n".join(page.get_text("text") for page in doc)
    cleaned = clean_text(text)
    if not cleaned:
        raise ValueError(
            "We could not find selectable text in this PDF. Please upload a text-based resume."
        )
    lower = cleaned.lower()
    sections = [section.title() for section in SECTIONS if section in lower]
    return {
        "text": cleaned,
        "sections": sections,
        "page_count": len(doc),
        "word_count": len(cleaned.split()),
    }
