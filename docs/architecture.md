# SkillTrack architecture

```mermaid
flowchart LR
  Browser[React + Vite] -->|JWT / JSON| API[FastAPI API]
  API --> DB[(SQLite default)]
  API --> Resume[PyMuPDF + taxonomy extractor]
  API --> Score[TF-IDF / optional embeddings / weighted hybrid]
  API --> ML[ml/results JSON + joblib]
  API --> Static[FastAPI static serving]
```

## Runtime

- `backend/app/main.py` is the runnable FastAPI entrypoint and preserves the one-command demo.
- `backend/app/core/security.py` owns JWT and bcrypt.
- `backend/app/services/` owns parsing, taxonomy extraction and score computation.
- `backend/app/database.py` exposes SQLAlchemy 2.x engine/session metadata for the migration path.
- `frontend/src/App.jsx` consumes relative `/api/v1` routes and automatically retries one request after refreshing a JWT.
- `ml/` produces reproducible evaluation and placement artifacts. The admin endpoint reads those files instead of duplicating numbers.
