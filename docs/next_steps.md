# Next steps

## Before presentation

- Run `npm ci && npm run build` so the FastAPI-served static bundle matches the updated React source.
- Run `make test`, `make ml` and `make lint` in the target environment.
- Capture one student, recruiter and admin screenshot each.
- Rehearse `docs/demo_script.md` twice.

## Production hardening

- Complete SQLAlchemy/Alembic migration for the runtime query layer.
- Add PostgreSQL integration tests and switch Docker to the PostgreSQL service only after those tests pass.
- Move rate limiting to Redis for multi-instance deployment.
- Add consented, privacy-safe real evaluation data if placement prediction is ever used beyond the demo.
- Add a formal fairness audit by relevant groups before using model scores in real hiring decisions.

## Stretch goals

- Live LLM provider integration through `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` with response caching and redaction.
- Assessment signals feeding experience/placement features.
- Email notifications and interview scheduling.
- Celery/RQ background workers for heavier scoring and parsing.
- Fine-tuned skill NER using consented resume data.
