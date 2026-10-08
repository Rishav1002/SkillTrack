# SkillTrack

**Explainable placement intelligence for students, recruiters and campus teams.**

SkillTrack is a full-stack placement/ATS demonstration built around one idea: **make every shortlist defensible**. It combines resume parsing, a canonical skills taxonomy, explainable hybrid fit scoring, student recommendations, recruiter blind screening and placement-office analytics.

## Quick start

### 1. Zero-config demo

```bash
python run_demo.py
```

Open `http://localhost:8000`.

### 2. Development setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r backend/requirements.txt
cd frontend
npm install
npm run build
cd ..
python run_demo.py
```

### 3. Docker

```bash
docker compose up --build
```

The demo database is SQLite. `DATABASE_URL` and `CORS_ORIGINS` are configurable, and `JWT_SECRET` must be replaced for production.

## Demo accounts

| Role | Email | Password |
|---|---|---|
| Student — Aarav | `aarav.sharma@skilltrack.demo` | `Demo@123` |
| Student — Rohan | `rohan.verma@skilltrack.demo` | `Demo@123` |
| Recruiter — Priya | `priya@technova.demo` | `Demo@123` |
| Admin | `admin@skilltrack.demo` | `Admin@123` |

Additional seeded users include Neha, Simran, Karan, Ishita, Aditya, Pooja, Harpreet and Vikram, plus Rahul/DataWise, Anita/CloudCraft, Tara/PixelForge and a pending recruiter/company account for Vivek Digital Labs.

## Core workflow

1. Student uploads a PDF resume.
2. PyMuPDF extracts text and the taxonomy extractor returns canonical skills with evidence snippets.
3. Student confirms the extracted skills.
4. Jobs are ranked using the same explainable scoring service used by applications.
5. Application scoring combines:
   - 45% skill alignment
   - 30% semantic similarity
   - 15% eligibility compliance
   - 10% hands-on experience
6. Recruiters can inspect matched/missing evidence and use server-side blind screening.
7. Status changes write history rows and student notifications.
8. Admin dashboards query the database and the ML Insights page reads generated synthetic evaluation artifacts.

## ML methodology

### Explainable fit scoring

`backend/app/services/scoring.py` computes real values for:

- baseline TF-IDF cosine similarity
- semantic similarity using sentence-transformers when available, otherwise a truthful TF-IDF semantic fallback
- mandatory/optional weighted skill overlap
- CGPA + graduation-year eligibility
- project/certificate/internship evidence score

### Scoring evaluation

```bash
python ml/evaluate_scoring.py
```

Creates 60 labelled **synthetic** resume/JD pairs and reports Spearman correlation and NDCG@5 for TF-IDF, skill-only, semantic and hybrid ranking.

### Placement prediction

```bash
python ml/train_placement_model.py
```

Creates 500 reproducible **synthetic** rows and evaluates Logistic Regression and Random Forest. The selected Random Forest artifact is written to `ml/models/placement_model.joblib`.

## Tests

```bash
make seed
make ml
make test
```

The repository CI runs Python lint/tests and the frontend build.

## API

All application routes are under `/api/v1`. Protected routes require `Authorization: Bearer <access_token>`.

See `docs/api_overview.md` for the endpoint map.

## 10-minute demo

See `docs/demo_script.md`.

Recommended narrative:

**fragmented placement process → unified workspace → explainable score → real user journey → technical architecture → governance → ML evidence → roadmap**.

## Important limitations

- The current zero-config runtime uses SQLite/raw SQL. SQLAlchemy 2.x is included as the migration foundation, but the complete query layer has not yet been converted to ORM.
- PostgreSQL Docker wiring is a deployment scaffold and needs an integration test before production use.
- Synthetic ML data must not be presented as evidence of real hiring performance.
- Run `npm install && npm run build` before presenting the latest React source if the checked-in static bundle is stale.
