# API overview

All application routes are under `/api/v1`. Protected routes require `Authorization: Bearer <access_token>`.

| Group | Routes |
|---|---|
| Auth | `POST /auth/login`, `POST /auth/register`, `POST /auth/refresh`, `GET /auth/me` |
| Student | `GET /students/me`, dashboard, skills, projects, certificates, resume upload/confirm, recommendations |
| Jobs | list/detail/fit, recruiter create/update/delete/mine |
| Applications | create, student list, recruiter applicants/detail/history, status update |
| Company | `GET/PUT /companies/me` |
| Notifications | list, read one, read all |
| Admin | stats, users active flag, pending/approve/reject companies/jobs, placement insights, demo reset |
| AI | status, real resume feedback, interview questions, JD skill extraction |

Errors are JSON objects with `error.code` and `error.message` for unknown API paths and rate limits. List responses use `{items,total,page,page_size}` where pagination applies.
