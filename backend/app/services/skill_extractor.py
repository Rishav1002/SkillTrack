"""Taxonomy-backed skill extraction with canonical names and evidence."""

from __future__ import annotations

import csv
import re
from pathlib import Path

TAXONOMY_PATH = Path(__file__).resolve().parents[3] / "ml" / "data" / "skills.csv"


def load_taxonomy() -> list[dict[str, str]]:
    if not TAXONOMY_PATH.exists():
        return []
    with TAXONOMY_PATH.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term.strip().lower())
    # Keep punctuation such as C++, C#, .NET and Power BI usable while avoiding
    # substring matches inside longer words.
    return re.compile(rf"(?<![a-z0-9+#.]){escaped}(?![a-z0-9+#.])", re.I)


def extract_skills(
    text: str, taxonomy: list[dict[str, str]] | None = None
) -> list[dict[str, str]]:
    """Return canonical skills with the exact evidence snippet and section.

    spaCy PhraseMatcher is used when available. A regex matcher is retained as
    a deterministic fallback so the demo works without a downloaded language model.
    """
    taxonomy = taxonomy or load_taxonomy()
    text = text or ""
    lower = text.lower()
    found: list[dict[str, str]] = []

    matcher = None
    nlp = None
    try:
        import spacy
        from spacy.matcher import PhraseMatcher

        nlp = spacy.blank("en")
        matcher = PhraseMatcher(nlp.vocab, attr="LOWER")
        for i, row in enumerate(taxonomy):
            terms = [row["name"]] + [
                a.strip() for a in row.get("aliases", "").split("|") if a.strip()
            ]
            patterns = [nlp.make_doc(term) for term in terms if term]
            if patterns:
                matcher.add(f"SKILL_{i}", patterns)
    except Exception:
        matcher = None

    hits: dict[str, tuple[int, int, str]] = {}
    if matcher is not None and nlp is not None:
        doc = nlp(text)
        for match_id, start, end in matcher(doc):
            key = nlp.vocab.strings[match_id]
            idx = int(key.split("_")[-1])
            row = taxonomy[idx]
            span = doc[start:end]
            hits.setdefault(
                row["name"].lower(), (span.start_char, span.end_char, span.text)
            )

    for row in taxonomy:
        canonical = row["name"]
        if canonical.lower() in hits:
            start, end, evidence = hits[canonical.lower()]
        else:
            evidence_match = None
            aliases = [canonical] + [
                a.strip() for a in row.get("aliases", "").split("|") if a.strip()
            ]
            for alias in sorted(set(aliases), key=len, reverse=True):
                match = _pattern(alias).search(lower)
                if match:
                    evidence_match = match
                    break
            if not evidence_match:
                continue
            start, end = evidence_match.span()

        snippet = (
            text[max(0, start - 60) : min(len(text), end + 90)]
            .replace("\n", " ")
            .strip()
        )
        section = _section_for_position(text, start)
        found.append({"name": canonical, "evidence": snippet, "section": section})

    return found


def _section_for_position(text: str, position: int) -> str:
    prefix = text[:position]
    lower = prefix.lower()
    candidates = [
        (lower.rfind(section), section.title())
        for section in (
            "summary",
            "skills",
            "experience",
            "projects",
            "education",
            "certifications",
        )
    ]
    index, section = max(candidates, default=(-1, "Profile"))
    return section if index >= 0 else "Profile"
