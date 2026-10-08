"""Train placement classifiers on explicitly synthetic data."""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path

import numpy as np
from joblib import dump
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
MODELS = ROOT / "models"
RESULTS = ROOT / "results"
FEATURES = [
    "cgpa",
    "skill_count",
    "projects",
    "certificates",
    "assessment_score",
    "internship_months",
]


def make_data(n: int = 500):
    rng = random.Random(42)
    rows = []
    for _ in range(n):
        cgpa = round(rng.uniform(5.5, 10), 2)
        skills = rng.randint(1, 15)
        projects = rng.randint(0, 5)
        certs = rng.randint(0, 4)
        assessment = rng.randint(35, 100)
        internship = rng.randint(0, 18)
        signal = (
            0.28 * cgpa
            + 0.12 * skills
            + 0.22 * projects
            + 0.10 * certs
            + 0.18 * (assessment / 10)
            + 0.10 * internship
        )
        label = int(signal + rng.uniform(-0.8, 0.8) > 5.7)
        rows.append([cgpa, skills, projects, certs, assessment, internship, label])
    return rows


def evaluate_model(model, X_train, X_test, y_train, y_test):
    model.fit(X_train, y_train)
    pred = model.predict(X_test)
    proba = model.predict_proba(X_test)[:, 1]
    return {
        "accuracy": round(float(accuracy_score(y_test, pred)), 4),
        "precision": round(float(precision_score(y_test, pred, zero_division=0)), 4),
        "recall": round(float(recall_score(y_test, pred, zero_division=0)), 4),
        "f1": round(float(f1_score(y_test, pred, zero_division=0)), 4),
        "roc_auc": round(float(roc_auc_score(y_test, proba)), 4),
        "confusion_matrix": confusion_matrix(y_test, pred).tolist(),
    }


def main():
    rows = make_data()
    DATA.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    MODELS.mkdir(exist_ok=True)
    with (DATA / "placement_synthetic.csv").open(
        "w", newline="", encoding="utf-8"
    ) as f:
        writer = csv.writer(f)
        writer.writerow(FEATURES + ["placed", "synthetic"])
        writer.writerows([r + ["true"] for r in rows])

    arr = np.asarray(rows, dtype=float)
    X, y = arr[:, :-1], arr[:, -1].astype(int)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=42
    )
    models = {
        "logistic_regression": LogisticRegression(max_iter=1000, random_state=42),
        "random_forest": RandomForestClassifier(
            n_estimators=160, max_depth=6, random_state=42, class_weight="balanced"
        ),
    }
    evaluations = {}
    for name, model in models.items():
        metrics = evaluate_model(model, X_train, X_test, y_train, y_test)
        cv = cross_val_score(
            model,
            X,
            y,
            cv=StratifiedKFold(5, shuffle=True, random_state=42),
            scoring="accuracy",
        )
        metrics["cross_validation_accuracy"] = round(float(cv.mean()), 4)
        evaluations[name] = metrics

    selected = models["random_forest"]
    selected.fit(X_train, y_train)
    dump(selected, MODELS / "placement_model.joblib")
    payload = {
        "synthetic": True,
        "rows": len(rows),
        "selected_model": "random_forest",
        "models": evaluations,
        "accuracy": evaluations["random_forest"]["accuracy"],
        "precision": evaluations["random_forest"]["precision"],
        "recall": evaluations["random_forest"]["recall"],
        "f1": evaluations["random_forest"]["f1"],
        "roc_auc": evaluations["random_forest"]["roc_auc"],
        "confusion_matrix": evaluations["random_forest"]["confusion_matrix"],
        "feature_importance": [
            {"name": name, "importance": round(float(value), 4)}
            for name, value in zip(FEATURES, selected.feature_importances_)
        ],
        "cross_validation_accuracy": evaluations["random_forest"][
            "cross_validation_accuracy"
        ],
    }
    (RESULTS / "placement_metrics.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
