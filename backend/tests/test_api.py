from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app.main import DB_PATH, SAMPLE_DIR, app, seed_data


@pytest.fixture()
def client():
    seed_data(reset=True)
    with TestClient(app) as test_client:
        yield test_client


def token(client, email, password):
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_jwt_and_forged_demo_token_rejected(client):
    assert (
        client.get("/api/v1/auth/me", headers={"Authorization": "demo:1"}).status_code
        == 401
    )
    admin = token(client, "admin@skilltrack.demo", "Admin@123")
    assert client.get("/api/v1/auth/me", headers=auth(admin)).status_code == 200
    assert len(admin.split(".")) == 3


def test_bcrypt_seed_and_admin_kpis_are_queries(client):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    assert (
        conn.execute("select password_hash from users limit 1")
        .fetchone()["password_hash"]
        .startswith("$2b$")
    )
    direct = {
        "students": conn.execute(
            "select count(*) from users where role='student'"
        ).fetchone()[0],
        "jobs": conn.execute(
            "select count(*) from jobs where is_approved=1"
        ).fetchone()[0],
        "applications": conn.execute("select count(*) from applications").fetchone()[0],
    }
    result = client.get(
        "/api/v1/admin/stats",
        headers=auth(token(client, "admin@skilltrack.demo", "Admin@123")),
    )
    assert result.status_code == 200
    assert all(result.json()["kpis"][key] == value for key, value in direct.items())


def test_resume_pdfs_extract_different_skills(client):
    aarav = token(client, "aarav.sharma@skilltrack.demo", "Demo@123")
    rohan = token(client, "rohan.verma@skilltrack.demo", "Demo@123")
    a = next(SAMPLE_DIR.glob("*-aarav-sharma.pdf"))
    r = next(SAMPLE_DIR.glob("*-rohan-verma.pdf"))
    ra = client.post(
        "/api/v1/students/me/resume",
        headers=auth(aarav),
        files={"file": ("aarav.pdf", a.read_bytes(), "application/pdf")},
    )
    rr = client.post(
        "/api/v1/students/me/resume",
        headers=auth(rohan),
        files={"file": ("rohan.pdf", r.read_bytes(), "application/pdf")},
    )
    assert ra.status_code == rr.status_code == 200
    assert {x["name"] for x in ra.json()["skills"]} != {
        x["name"] for x in rr.json()["skills"]
    }
    assert ra.json()["skills"]


def test_upload_validation(client):
    student = token(client, "aarav.sharma@skilltrack.demo", "Demo@123")
    assert (
        client.post(
            "/api/v1/students/me/resume",
            headers=auth(student),
            files={"file": ("bad.txt", b"hello", "text/plain")},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/students/me/resume",
            headers=auth(student),
            files={"file": ("fake.pdf", b"not a pdf", "application/pdf")},
        ).status_code
        == 400
    )


def test_real_fit_breakdown_and_recommendations(client):
    aarav = token(client, "aarav.sharma@skilltrack.demo", "Demo@123")
    rohan = token(client, "rohan.verma@skilltrack.demo", "Demo@123")
    fit = client.get("/api/v1/jobs/1/fit", headers=auth(aarav)).json()
    assert {
        "baseline_tfidf",
        "semantic_score",
        "skill_score",
        "experience_score",
        "semantic_method",
    } <= fit.keys()
    assert (
        len(
            {
                fit["baseline_tfidf"],
                fit["semantic_score"],
                fit["skill_score"],
                fit["experience_score"],
            }
        )
        == 4
    )
    assert fit["score"] > 70
    weak = client.get("/api/v1/jobs/2/fit", headers=auth(rohan)).json()
    assert weak["score"] < 50
    assert {"scikit-learn", "Docker", "AWS"}.issubset(set(weak["missing"]))
    assert weak["eligibility"]["passed"] is False
    recommendations = client.get(
        "/api/v1/students/me/recommendations", headers=auth(aarav)
    ).json()
    assert recommendations["items"]
    assert all(item["application"] is None for item in recommendations["items"])


def test_blind_mode_server_side_and_unknown_api_json(client):
    recruiter = token(client, "priya@technova.demo", "Demo@123")
    data = client.get(
        "/api/v1/jobs/1/applicants?blind=true", headers=auth(recruiter)
    ).json()
    assert data["items"]
    assert not {"name", "email", "gender", "college", "avatar_color"} & set(
        data["items"][0]
    )
    missing = client.get("/api/v1/does-not-exist")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"


def test_role_access_and_duplicate_apply(client):
    student = token(client, "aarav.sharma@skilltrack.demo", "Demo@123")
    admin = token(client, "admin@skilltrack.demo", "Admin@123")
    assert client.get("/api/v1/admin/users", headers=auth(student)).status_code == 403
    assert client.get("/api/v1/admin/users", headers=auth(admin)).status_code == 200
    assert (
        client.post(
            "/api/v1/applications", headers=auth(student), json={"job_id": 3}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/applications", headers=auth(student), json={"job_id": 3}
        ).status_code
        == 409
    )


def test_expired_and_closed_job_rules(client):
    student = token(client, "aarav.sharma@skilltrack.demo", "Demo@123")
    response = client.post(
        "/api/v1/applications", headers=auth(student), json={"job_id": 11}
    )
    assert response.status_code == 400
    assert "past its deadline" in response.json()["detail"]


def test_recruiter_job_ownership_and_approval_flow(client):
    priya = token(client, "priya@technova.demo", "Demo@123")
    rahul = token(client, "rahul@datawise.demo", "Demo@123")
    admin = token(client, "admin@skilltrack.demo", "Admin@123")
    body = {
        "title": "Platform Engineering Intern",
        "description": "Python SQL Docker internship building tested services and deployment automation.",
        "location": "Remote",
        "job_type": "Internship",
        "salary_min": 20000,
        "salary_max": 30000,
        "min_cgpa": 7,
        "deadline": "2026-12-31",
        "mandatory_skills": ["Python", "SQL"],
        "optional_skills": ["Docker"],
    }
    created = client.post("/api/v1/jobs", headers=auth(priya), json=body)
    assert created.status_code == 200
    job_id = created.json()["id"]
    assert (
        client.put(f"/api/v1/jobs/{job_id}", headers=auth(rahul), json=body).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/jobs",
            headers=auth(token(client, "aarav.sharma@skilltrack.demo", "Demo@123")),
        ).json()["total"]
        == 10
    )
    assert (
        client.patch(
            f"/api/v1/admin/jobs/{job_id}/approve", headers=auth(admin)
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/jobs",
            headers=auth(token(client, "aarav.sharma@skilltrack.demo", "Demo@123")),
        ).json()["total"]
        == 11
    )


def test_status_change_history_notification_and_blind_release(client):
    recruiter = token(client, "priya@technova.demo", "Demo@123")
    student = token(client, "aarav.sharma@skilltrack.demo", "Demo@123")
    applications = client.get(
        "/api/v1/jobs/1/applicants?blind=false", headers=auth(recruiter)
    ).json()["items"]
    target = next(item for item in applications if item["status"] == "Applied")
    changed = client.patch(
        f"/api/v1/applications/{target['id']}/status",
        headers=auth(recruiter),
        json={"status": "Shortlisted", "note": "Evidence reviewed"},
    )
    assert changed.status_code == 200
    detail = client.get(
        f"/api/v1/applications/{target['id']}", headers=auth(recruiter)
    ).json()
    assert any(h["new_status"] == "Shortlisted" for h in detail["history"])
    assert client.get("/api/v1/notifications", headers=auth(student)).status_code == 200


def test_student_profile_items_and_resume_confirmation(client):
    student = token(client, "aarav.sharma@skilltrack.demo", "Demo@123")
    assert (
        client.post(
            "/api/v1/students/me/skills?name=Python&proficiency=5",
            headers=auth(student),
        ).status_code
        == 200
    )
    project = client.post(
        "/api/v1/students/me/projects",
        headers=auth(student),
        json={
            "title": "Test Project",
            "description": "A useful test project",
            "tech_stack": "Python",
        },
    )
    assert project.status_code == 200
    certificate = client.post(
        "/api/v1/students/me/certificates",
        headers=auth(student),
        json={"title": "Test Certificate", "issuer": "Test Issuer", "year": 2026},
    )
    assert certificate.status_code == 200
    profile = client.get("/api/v1/students/me", headers=auth(student)).json()
    assert any(p["title"] == "Test Project" for p in profile["projects"])
    assert any(c["title"] == "Test Certificate" for c in profile["certificates"])


def test_admin_ml_artifacts_and_reset(client):
    admin = token(client, "admin@skilltrack.demo", "Admin@123")
    result = client.get("/api/v1/admin/placement-insights", headers=auth(admin))
    assert result.status_code == 200
    body = result.json()
    assert body["synthetic"] is True
    assert body["evaluation"]
    assert body["models"]
    before = client.get("/api/v1/admin/stats", headers=auth(admin)).json()["kpis"][
        "applications"
    ]
    assert client.post("/api/v1/demo/reset", headers=auth(admin)).status_code == 200
    after = client.get("/api/v1/admin/stats", headers=auth(admin)).json()["kpis"][
        "applications"
    ]
    assert before == after
