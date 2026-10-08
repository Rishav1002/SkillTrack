"""Evaluate SkillTrack ranking signals on reproducible synthetic labelled pairs."""

from __future__ import annotations

import csv
import json
import math
import random
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import ndcg_score
from sklearn.metrics.pairwise import cosine_similarity

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RESULTS = ROOT / "results"
SKILLS = [
    "python",
    "sql",
    "react",
    "docker",
    "aws",
    "pandas",
    "scikit-learn",
    "flask",
    "power bi",
    "java",
    "spring boot",
    "nlp",
    "tensorflow",
    "statistics",
    "tableau",
    "linux",
    "javascript",
    "node.js",
    "mysql",
]


def make_pairs() -> list[dict[str, object]]:
    rng = random.Random(42)
    rows = []
    for i in range(60):
        required = rng.sample(SKILLS, rng.randint(3, 7))
        overlap = rng.randint(0, len(required))
        resume_skills = required[:overlap] + rng.sample(
            [s for s in SKILLS if s not in required], rng.randint(0, 4)
        )
        noise = rng.choice(
            [
                "built tested workflows and documented outcomes",
                "worked with stakeholders and production debugging",
                "completed an academic project with measurable results",
            ]
        )
        rating = 1 + round(
            4 * (overlap / max(1, len(required))) + rng.uniform(-0.35, 0.35)
        )
        rating = int(max(1, min(5, rating)))
        rows.append(
            {
                "pair_id": i + 1,
                "resume_text": "Projects and skills: "
                + ", ".join(resume_skills)
                + ". "
                + noise,
                "jd_text": "Role requires "
                + ", ".join(required)
                + ". Build reliable production workflows and communicate outcomes.",
                "human_rating": rating,
                "synthetic": "true",
            }
        )
    DATA.mkdir(exist_ok=True)
    with (DATA / "eval_pairs.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return rows


def tfidf_scores(rows):
    corpus = [r["jd_text"] for r in rows] + [r["resume_text"] for r in rows]
    mat = TfidfVectorizer(stop_words="english", ngram_range=(1, 2)).fit_transform(
        corpus
    )
    n = len(rows)
    return np.array(
        [
            float(cosine_similarity(mat[i : i + 1], mat[i + n : i + n + 1])[0][0])
            for i in range(n)
        ]
    )


def skill_scores(rows):
    scores = []
    for row in rows:
        jd = {x for x in SKILLS if x in row["jd_text"].lower()}
        resume = {x for x in SKILLS if x in row["resume_text"].lower()}
        scores.append(len(jd & resume) / max(1, len(jd)))
    return np.asarray(scores)


def semantic_scores(rows):
    """Use sentence-transformers when installed; otherwise use a real char/word TF-IDF similarity."""
    texts = [f"{r['jd_text']}" for r in rows] + [f"{r['resume_text']}" for r in rows]
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer("all-MiniLM-L6-v2")
        vectors = model.encode(texts, normalize_embeddings=True)
        n = len(rows)
        scores = np.array([float(vectors[i] @ vectors[i + n]) for i in range(n)])
        return scores, "sentence-transformers/all-MiniLM-L6-v2"
    except Exception:
        mat = TfidfVectorizer(
            analyzer="char_wb", ngram_range=(3, 5), min_df=1
        ).fit_transform(texts)
        n = len(rows)
        scores = np.array(
            [
                float(cosine_similarity(mat[i : i + 1], mat[i + n : i + n + 1])[0][0])
                for i in range(n)
            ]
        )
        return scores, "TF-IDF character n-gram semantic fallback"


def ndcg(scores, ratings, k=5):
    return float(
        ndcg_score(
            np.asarray(ratings).reshape(1, -1), np.asarray(scores).reshape(1, -1), k=k
        )
    )


def evaluate(name, scores, ratings):
    rho = spearmanr(scores, ratings).statistic
    return {
        "model": name,
        "spearman": round(float(rho) if math.isfinite(float(rho)) else 0.0, 4),
        "ndcg_at_5": round(ndcg(scores, ratings), 4),
    }


def main():
    rows = make_pairs()
    ratings = np.asarray([r["human_rating"] for r in rows])
    tfidf = tfidf_scores(rows)
    skill = skill_scores(rows)
    semantic, semantic_method = semantic_scores(rows)
    hybrid = 0.45 * skill + 0.30 * semantic + 0.25 * tfidf
    results = [
        evaluate("TF-IDF baseline", tfidf, ratings),
        evaluate("Skill-only", skill, ratings),
        evaluate("Embeddings-only", semantic, ratings),
        evaluate("Hybrid", hybrid, ratings),
    ]
    payload = {
        "synthetic": True,
        "pairs": len(rows),
        "semantic_method": semantic_method,
        "results": results,
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "scoring_eval.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    lines = [
        "# Scoring evaluation (synthetic)",
        "",
        f"Semantic method: `{semantic_method}`",
        "",
        "| Model | Spearman | NDCG@5 |",
        "|---|---:|---:|",
    ] + [
        f"| {r['model']} | {r['spearman']:.4f} | {r['ndcg_at_5']:.4f} |"
        for r in results
    ]
    (RESULTS / "scoring_eval.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
