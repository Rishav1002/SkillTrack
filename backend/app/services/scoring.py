"""Explainable hybrid scoring. All displayed values are computed from stored data."""

from __future__ import annotations

import json
import re
from typing import Any

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from ..config import settings


def _cosine(a: str, b: str, analyzer: str = "word") -> float:
    texts = [a or "", b or ""]
    if not any(t.strip() for t in texts):
        return 0.0
    vectorizer = TfidfVectorizer(
        analyzer=analyzer,
        stop_words="english" if analyzer == "word" else None,
        ngram_range=(1, 2) if analyzer == "word" else (3, 5),
    )
    try:
        matrix = vectorizer.fit_transform(texts)
    except ValueError:
        return 0.0
    return round(float(cosine_similarity(matrix[0:1], matrix[1:2])[0][0] * 100), 2)


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9+#.]", "", value.lower())


def _skill_map(c, student_id: int) -> dict[str, str]:
    rows = c.execute(
        "SELECT s.name, s.aliases FROM student_skills ss JOIN skills s ON s.id=ss.skill_id WHERE ss.student_id=?",
        (student_id,),
    ).fetchall()
    result = {}
    for row in rows:
        result[_normalise(row["name"])] = row["name"]
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except json.JSONDecodeError:
            aliases = []
        for alias in aliases:
            result[_normalise(alias)] = row["name"]
    return result


def score_application(c, student_id: int, job_id: int) -> dict[str, Any]:
    job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    profile = c.execute(
        "SELECT * FROM students WHERE user_id=?", (student_id,)
    ).fetchone()
    if not job or not profile:
        raise ValueError("Student or job not found")

    mandatory = json.loads(job["mandatory_skills"] or "[]")
    optional = json.loads(job["optional_skills"] or "[]")
    student_skills = _skill_map(c, student_id)

    matched: list[str] = []
    missing: list[str] = []
    mandatory_norm = {_normalise(skill): skill for skill in mandatory}
    optional_norm = {_normalise(skill): skill for skill in optional}
    for norm, canonical in mandatory_norm.items():
        if norm in student_skills:
            matched.append(canonical)
        else:
            missing.append(canonical)
    for norm, canonical in optional_norm.items():
        if norm in student_skills:
            matched.append(canonical)

    total_weight = len(mandatory) * 2 + len(optional)
    covered_weight = sum(
        2 for skill in mandatory if _normalise(skill) in student_skills
    )
    covered_weight += sum(
        1 for skill in optional if _normalise(skill) in student_skills
    )
    skill_score = (
        round((covered_weight / total_weight) * 100, 2) if total_weight else 0.0
    )

    resume_row = c.execute(
        "SELECT extracted_text FROM resumes WHERE student_id=? ORDER BY parsed_at DESC LIMIT 1",
        (student_id,),
    ).fetchone()
    resume_text = (
        resume_row["extracted_text"]
        if resume_row and resume_row["extracted_text"]
        else ""
    )
    profile_text = " ".join(student_skills.values()) + " " + (profile["bio"] or "")
    semantic_input = f"{resume_text} {profile_text}".strip()
    baseline = _cosine(job["description"], semantic_input)

    semantic_method = "TF-IDF semantic fallback"
    role_text = f"{job['title']} {job['description']} {' '.join(mandatory)} {' '.join(optional)}"
    # The fallback uses two independent text representations rather than a
    # hand-written constant or a transformed skill score.
    semantic_word = _cosine(role_text, semantic_input, analyzer="word")
    semantic_char = _cosine(role_text, semantic_input, analyzer="char")
    semantic = round((semantic_word + semantic_char) / 2, 2)
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer("all-MiniLM-L6-v2")
        embeddings = model.encode(
            [role_text, semantic_input], normalize_embeddings=True
        )
        semantic = round(float(embeddings[0] @ embeddings[1]) * 100, 2)
        semantic_method = "sentence-transformers/all-MiniLM-L6-v2"
    except Exception:
        pass

    cgpa = float(profile["cgpa"] or 0)
    min_cgpa = float(job["min_cgpa"] or 0)
    cgpa_passed = cgpa >= min_cgpa
    graduation_year = int(profile["graduation_year"] or 0)
    year_passed = graduation_year >= 2026
    eligibility = round(((int(cgpa_passed) + int(year_passed)) / 2) * 100, 2)
    eligibility_reasons = [
        f"CGPA {cgpa:.1f} vs minimum {min_cgpa:.1f}: {'passed' if cgpa_passed else 'flagged'}",
        f"Graduation year {graduation_year}: {'aligned' if year_passed else 'outside the demo role window'}",
    ]

    projects = c.execute(
        "SELECT title, description, tech_stack FROM projects WHERE student_id=?",
        (student_id,),
    ).fetchall()
    certificates = c.execute(
        "SELECT title, issuer FROM certificates WHERE student_id=?", (student_id,)
    ).fetchall()
    project_count_signal = min(35.0, len(projects) / 3 * 35.0)
    project_depth = min(
        25.0, sum(min(9.0, len((p["description"] or "").split()) / 3) for p in projects)
    )
    stack_depth = min(
        20.0,
        sum(
            min(
                7.0,
                len([x for x in (p["tech_stack"] or "").split(",") if x.strip()]) * 2.5,
            )
            for p in projects
        ),
    )
    certificate_depth = min(15.0, len(certificates) / 2 * 15.0)
    internship_signal = min(
        5.0,
        sum("intern" in f"{p['title']} {p['description']}".lower() for p in projects)
        * 5.0,
    )
    experience = round(
        min(
            100.0,
            project_count_signal
            + project_depth
            + stack_depth
            + certificate_depth
            + internship_signal,
        ),
        2,
    )

    final = round(
        settings.skill_weight * skill_score
        + settings.semantic_weight * semantic
        + settings.eligibility_weight * eligibility
        + settings.experience_weight * experience,
        2,
    )
    return {
        "score": final,
        "matched": list(dict.fromkeys(matched)),
        "missing": list(dict.fromkeys(missing)),
        "skill_score": skill_score,
        "semantic_score": semantic,
        "semantic_method": semantic_method,
        "eligibility": {
            "passed": cgpa_passed and year_passed,
            "reasons": eligibility_reasons,
        },
        "experience_score": experience,
        "baseline_tfidf": baseline,
        "weights": {
            "skill": settings.skill_weight,
            "semantic": settings.semantic_weight,
            "eligibility": settings.eligibility_weight,
            "experience": settings.experience_weight,
        },
        "notes": "Decision support only — review the full profile and evidence before making a decision.",
    }
