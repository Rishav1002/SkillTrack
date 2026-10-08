# SkillTrack audit and completion record

## Completed in this build

| Area | Result | Evidence |
|---|---|---|
| Authentication | Real HS256 access/refresh JWTs, 30-minute access and 7-day refresh; forged `demo:<id>` tokens rejected | `backend/app/core/security.py`, `backend/app/main.py` |
| Passwords | Bcrypt through Passlib in the production dependency set; legacy SHA-256 hashes are rehashed on successful login | `backend/app/core/security.py` |
| Blind screening | Server-side anonymisation removes name/email/gender/college/avatar in blind payloads; identity is released only from Shortlisted onward | `backend/app/main.py` |
| API fallback | Unknown `/api/*` routes return JSON 404 instead of SPA HTML | `backend/app/main.py` |
| CORS/security | Configurable CORS plus security headers | `backend/app/config.py`, `backend/app/main.py` |
| Rate limiting | Login/register/AI endpoints use a per-process rate gate | `backend/app/main.py` |
| Resume parsing | PDF magic-byte/size validation, PyMuPDF extraction, section detection, taxonomy-backed evidence | `backend/app/services/resume_parser.py`, `skill_extractor.py` |
| Skill extraction | Canonical taxonomy with aliases, PhraseMatcher when available, deterministic regex fallback | `backend/app/services/skill_extractor.py`, `ml/data/skills.csv` |
| Scoring | Real TF-IDF, optional sentence-transformer semantic score, weighted skill overlap, eligibility and project/certificate experience | `backend/app/services/scoring.py` |
| Recommendations | Open approved jobs are ranked using the same scoring service and applied jobs are excluded | `backend/app/main.py` |
| Applications | Duplicate, deadline and closed-job rules; background scoring; history and notifications | `backend/app/main.py` |
| Recruiter workflow | Job create/edit/close/delete, approval flow, ownership checks, blind applicants and status changes | `backend/app/main.py`, `frontend/src/App.jsx` |
| Admin analytics | KPIs, demand/supply, applications over time and company placements are database queries | `backend/app/main.py` |
| ML evaluation | 60 synthetic scoring pairs; Spearman/NDCG for TF-IDF, skill-only, semantic and hybrid | `ml/evaluate_scoring.py` |
| Placement model | 500 reproducible synthetic rows; Logistic Regression + Random Forest metrics, CV, confusion matrix and feature importance | `ml/train_placement_model.py` |
| Frontend completion | Real resume upload/confirmation, application polling/kanban, recruiter job posting, admin user controls, profile editing | `frontend/src/App.jsx` |
| Tests | 12 API/security/ML/ownership/flow tests pass in the supplied validation environment | `backend/tests/test_api.py` |

## Known limitations

1. The zero-config runtime remains SQLite/raw-SQL for maximum demo reliability. `backend/app/database.py` exposes SQLAlchemy 2.x, but the complete application query layer has not yet been migrated to ORM/SQLAlchemy.
2. PostgreSQL is not the tested runtime in this environment. Docker remains a deployment scaffold and should be verified with a real PostgreSQL integration before production use.
3. Sentence-transformers is optional. When it is unavailable, the scoring service explicitly reports a TF-IDF semantic fallback rather than pretending it used embeddings.
4. The placement dataset and scoring evaluation are synthetic. Their metrics are suitable for demonstrating methodology, not for claiming hiring performance.
5. The rate limiter is process-local. A multi-instance deployment should move it to Redis or another shared store.
6. The generated React source is updated in this build, but the checked-in `backend/static` bundle can only be refreshed after `npm install && npm run build` in an environment with the frontend toolchain available.
