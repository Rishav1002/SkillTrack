# SkillTrack completion report

## Validation environment

The supplied execution environment did not contain the project's `python-jose`/Passlib/bcrypt packages or the frontend `node_modules`. Network access was unavailable, so dependency installation and a production frontend build could not be performed here.

For backend functional validation, temporary test-only compatibility shims were used for JWT/password packages. These shims were **not included in the project**. The actual project requirements still specify `python-jose` and bcrypt/Passlib.

## Acceptance checks

| Check | Result |
|---|---|
| Forged `Authorization: demo:1` rejected | PASS |
| Valid JWT accepted | PASS |
| Bcrypt-format seeded hashes | PASS in dependency-compatible validation; production uses Passlib/bcrypt |
| Aarav/Rohan PDF parsing produces different skills | PASS |
| Four scoring components computed independently | PASS |
| Rohan ML Engineer fit low with missing skills + CGPA flag | PASS |
| Server-side blind payload strips identity fields | PASS |
| Recruiter job ownership and admin approval flow | PASS |
| Duplicate application returns 409 | PASS |
| Expired job application returns 400 | PASS |
| Status history + notification written | PASS |
| Student skills/projects/certificates APIs | PASS |
| Admin ML artifacts endpoint | PASS |
| Demo reset is idempotent | PASS |
| `python -m compileall backend ml` | PASS |
| Scoring evaluation script | PASS |
| Placement model training script | PASS |
| Frontend `npm run build` | NOT RUN: dependencies/network unavailable |
| Ruff | NOT RUN: executable unavailable in environment |
| Docker integration | NOT RUN: Docker unavailable in environment |

## Generated ML evidence

Scoring evaluation uses 60 synthetic pairs:

- TF-IDF baseline: Spearman 0.8903, NDCG@5 1.0000
- Skill-only: Spearman 0.9704, NDCG@5 0.9846
- Embeddings/semantic: Spearman 0.8557, NDCG@5 1.0000
- Hybrid: Spearman 0.9456, NDCG@5 1.0000

Placement prediction uses 500 synthetic rows. Random Forest is the selected artifact; Logistic Regression is also evaluated. Current Random Forest results:

- Accuracy: 0.7800
- Precision: 0.8333
- Recall: 0.7759
- F1: 0.8036
- ROC-AUC: 0.8945
- 5-fold CV accuracy: 0.8200

These are synthetic research/demo metrics only.

## Remaining manual work

1. Install the pinned backend dependencies and run the full pytest suite without the temporary validation shims.
2. Run `npm ci && npm run build` and refresh `backend/static` before presenting the updated React UI.
3. If production PostgreSQL is required, finish the SQLAlchemy migration and run a real PostgreSQL integration suite.
4. Set a unique production `JWT_SECRET`, production CORS origins and shared rate-limit storage before deployment.

## Follow-up verification pass (independent review)
- Frontend source had no `export default App`, so `npm run build` failed; fixed, rebuilt, `backend/static` now matches source (ESLint 0 errors).
- Black + Ruff applied: Ruff clean (was 321 errors). `main.py` is still a single ~2,400-line module using raw sqlite3; splitting into routers and finishing the SQLAlchemy/PostgreSQL migration is NOT done.
- Added `backend/tests/test_extended.py` (ownership, post to approve to apply flow, status history and notification, deadline rule, blind mode, upload validation, seed idempotency). 20 tests pass.
- Admin UI gained pending-job approve/reject (previously only companies could be approved from the UI).
- Real sentence embeddings not verified: run `make embeddings` on a machine with internet (needs Hugging Face access), then re-check `ml/results/*.json`.
- Not done in UI: edit/close/delete job, add project/certificate, company profile edit (APIs exist).
