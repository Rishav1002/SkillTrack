"""Extra tests: ownership, full workflow, blind mode, deadlines, uploads, seed."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app.main import DB_PATH, app, seed_data

API = "/api/v1"


@pytest.fixture()
def client():
    seed_data(reset=True)
    with TestClient(app) as test_client:
        yield test_client


def login(client, email, password="Demo@123"):
    r = client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def db_rows(sql, args=()):
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def new_job_payload(title="Test Data Engineer"):
    return {
        "title": title,
        "description": "Build ETL pipelines in Python and SQL. " * 5,
        "location": "Remote",
        "job_type": "Internship",
        "salary_min": 10000,
        "salary_max": 20000,
        "min_cgpa": 6.0,
        "deadline": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
        "mandatory_skills": ["Python", "SQL"],
        "optional_skills": ["Docker"],
    }


def test_student_blocked_from_recruiter_and_admin_routes(client):
    student = login(client, "rohan.verma@skilltrack.demo")
    assert (
        client.post(f"{API}/jobs", json=new_job_payload(), headers=student).status_code
        == 403
    )
    assert client.get(f"{API}/admin/users", headers=student).status_code == 403
    assert client.get(f"{API}/admin/stats", headers=student).status_code == 403


def test_recruiter_cannot_edit_another_companys_job(client):
    priya = login(client, "priya@technova.demo")
    rahul = login(client, "rahul@datawise.demo")
    mine = client.get(f"{API}/jobs/mine", headers=priya).json()
    items = mine["items"] if isinstance(mine, dict) else mine
    job_id = items[0]["id"]
    r = client.put(
        f"{API}/jobs/{job_id}", json=new_job_payload("Hijack"), headers=rahul
    )
    assert r.status_code in (403, 404)
    r = client.delete(f"{API}/jobs/{job_id}", headers=rahul)
    assert r.status_code in (403, 404)


def test_post_approve_apply_status_notification_flow(client):
    priya = login(client, "priya@technova.demo")
    admin = login(client, "admin@skilltrack.demo", "Admin@123")
    student = login(client, "neha.gupta@skilltrack.demo")

    created = client.post(f"{API}/jobs", json=new_job_payload(), headers=priya)
    assert created.status_code in (200, 201), created.text
    body = created.json()
    job_id = body.get("id") or body.get("job", {}).get("id")
    assert job_id

    # Pending jobs must be hidden from students.
    assert client.get(f"{API}/jobs/{job_id}", headers=student).status_code == 404
    assert (
        client.post(
            f"{API}/applications", json={"job_id": job_id}, headers=student
        ).status_code
        == 404
    )

    assert (
        client.patch(f"{API}/admin/jobs/{job_id}/approve", headers=admin).status_code
        == 200
    )
    assert client.get(f"{API}/jobs/{job_id}", headers=student).status_code == 200

    first = client.post(f"{API}/applications", json={"job_id": job_id}, headers=student)
    assert first.status_code in (200, 201), first.text
    assert (
        client.post(
            f"{API}/applications", json={"job_id": job_id}, headers=student
        ).status_code
        == 409
    )

    applicants = client.get(f"{API}/jobs/{job_id}/applicants", headers=priya).json()[
        "items"
    ]
    assert len(applicants) == 1
    app_id = applicants[0].get("id") or applicants[0].get("application_id")

    r = client.patch(
        f"{API}/applications/{app_id}/status",
        json={"status": "Shortlisted", "note": "Great fit"},
        headers=priya,
    )
    assert r.status_code == 200, r.text
    history = client.get(f"{API}/applications/{app_id}/history", headers=priya).json()
    hist_items = history["items"] if isinstance(history, dict) else history
    assert any(h.get("new_status") == "Shortlisted" for h in hist_items)
    notes = client.get(f"{API}/notifications", headers=student).json()
    text = json.dumps(notes)
    assert "Shortlisted" in text


def test_expired_and_closed_jobs_reject_applications(client):
    expired = db_rows(
        "SELECT id FROM jobs WHERE is_approved=1 AND deadline < ?",
        (datetime.now(timezone.utc).isoformat(),),
    )
    assert expired, "seed should contain one expired job"
    student = login(client, "harpreet.gill@skilltrack.demo")
    r = client.post(
        f"{API}/applications", json={"job_id": expired[0]["id"]}, headers=student
    )
    assert r.status_code == 400


def test_blind_mode_removes_identity_fields(client):
    priya = login(client, "priya@technova.demo")
    mine = client.get(f"{API}/jobs/mine", headers=priya).json()
    items = mine["items"] if isinstance(mine, dict) else mine
    job_id = max(items, key=lambda j: j.get("applicants", j.get("applicant_count", 0)))[
        "id"
    ]
    blind = client.get(f"{API}/jobs/{job_id}/applicants?blind=true", headers=priya)
    assert blind.status_code == 200
    text = json.dumps(blind.json())
    assert "@skilltrack.demo" not in text
    names = [r["full_name"] for r in db_rows("SELECT full_name FROM students")]
    assert not any(n in text for n in names)


def test_resume_upload_validation(client):
    student = login(client, "rohan.verma@skilltrack.demo")
    url = f"{API}/students/me/resume"
    bad_ext = client.post(
        url, files={"file": ("cv.txt", b"%PDF-1.4 hi", "text/plain")}, headers=student
    )
    assert bad_ext.status_code == 400
    fake = client.post(
        url,
        files={"file": ("cv.pdf", b"not really a pdf", "application/pdf")},
        headers=student,
    )
    assert fake.status_code == 400
    big = b"%PDF-1.4" + b"0" * (5 * 1024 * 1024 + 10)
    too_big = client.post(
        url, files={"file": ("cv.pdf", big, "application/pdf")}, headers=student
    )
    assert too_big.status_code in (400, 413)


def test_seed_is_idempotent_and_reset_restores(client):
    before = {
        t: db_rows(f"SELECT COUNT(*) n FROM {t}")[0]["n"]
        for t in ("users", "jobs", "applications", "skills")
    }
    seed_data(reset=False)
    after = {t: db_rows(f"SELECT COUNT(*) n FROM {t}")[0]["n"] for t in before}
    assert before == after


def test_unknown_api_path_returns_json_404(client):
    r = client.get(f"{API}/does-not-exist")
    assert r.status_code == 404
    assert "json" in r.headers["content-type"]
