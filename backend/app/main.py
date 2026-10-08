from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from backend.app.config import settings
from backend.app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from backend.app.services.resume_parser import parse_pdf
from backend.app.services.scoring import score_application
from backend.app.services.skill_extractor import extract_skills, load_taxonomy

ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]
_database_url = settings.database_url
if _database_url.startswith("sqlite:///"):
    _sqlite_path = _database_url.removeprefix("sqlite:///")
    DB_PATH = Path(_sqlite_path)
    if not DB_PATH.is_absolute():
        DB_PATH = ROOT / DB_PATH
elif _database_url.startswith(("postgresql://", "postgresql+psycopg2://")):
    raise RuntimeError(
        "PostgreSQL URL detected, but the current zero-config runtime is SQLite. Complete the SQLAlchemy migration before enabling PostgreSQL."
    )
else:
    raise RuntimeError(
        "Unsupported DATABASE_URL. Use a SQLite URL for the demo runtime."
    )
UPLOAD_DIR = BACKEND_DIR / "uploads"
SAMPLE_DIR = BACKEND_DIR / "sample_resumes"
STATIC_DIR = BACKEND_DIR / "static"
UPLOAD_DIR.mkdir(exist_ok=True)
SAMPLE_DIR.mkdir(exist_ok=True)

app = FastAPI(
    title="SkillTrack API", version="1.0.0", docs_url="/docs", redoc_url="/redoc"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
_rate_buckets: dict[str, list[float]] = {}


def _rate_gate(request: Request, bucket: str, limit: int):
    key = f"{bucket}:{request.client.host if request.client else 'unknown'}"
    now = time.monotonic()
    recent = [stamp for stamp in _rate_buckets.get(key, []) if now - stamp < 60]
    if len(recent) >= limit:
        raise HTTPException(429, "Too many requests. Please try again shortly.")
    recent.append(now)
    _rate_buckets[key] = recent


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL, is_active INTEGER DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS students (user_id INTEGER PRIMARY KEY, full_name TEXT, phone TEXT, college TEXT, course TEXT, semester INTEGER, cgpa REAL, graduation_year INTEGER, bio TEXT, github_url TEXT, linkedin_url TEXT, gender TEXT, avatar_color TEXT);
CREATE TABLE IF NOT EXISTS companies (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, website TEXT, location TEXT, industry TEXT, description TEXT, is_approved INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS skills (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, category TEXT, aliases TEXT);
CREATE TABLE IF NOT EXISTS student_skills (student_id INTEGER, skill_id INTEGER, proficiency INTEGER DEFAULT 3, source TEXT DEFAULT 'manual', PRIMARY KEY(student_id, skill_id));
CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER, title TEXT, description TEXT, tech_stack TEXT, link TEXT);
CREATE TABLE IF NOT EXISTS certificates (id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER, title TEXT, issuer TEXT, year INTEGER);
CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, company_id INTEGER, title TEXT, description TEXT, location TEXT, job_type TEXT, salary_min REAL, salary_max REAL, min_cgpa REAL, deadline TEXT, is_approved INTEGER DEFAULT 1, status TEXT DEFAULT 'Open', created_at TEXT, mandatory_skills TEXT, optional_skills TEXT);
CREATE TABLE IF NOT EXISTS resumes (id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER, original_name TEXT, file_path TEXT, extracted_text TEXT, parsed_at TEXT);
CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, student_id INTEGER, resume_id INTEGER, ats_score REAL, score_breakdown TEXT, status TEXT DEFAULT 'Applied', applied_at TEXT, UNIQUE(job_id, student_id));
CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, application_id INTEGER, old_status TEXT, new_status TEXT, changed_by INTEGER, note TEXT, changed_at TEXT);
CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, message TEXT, link TEXT, is_read INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS ai_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, cache_key TEXT UNIQUE NOT NULL, provider TEXT, response_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_app_job_score ON applications(job_id, ats_score);
CREATE INDEX IF NOT EXISTS idx_job_deadline ON jobs(deadline);
"""


class LoginBody(BaseModel):
    email: str
    password: str


class ProfileBody(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    college: Optional[str] = None
    course: Optional[str] = None
    semester: Optional[int] = None
    cgpa: Optional[float] = None
    graduation_year: Optional[int] = None
    bio: Optional[str] = None
    github_url: Optional[str] = None
    linkedin_url: Optional[str] = None


class ApplyBody(BaseModel):
    job_id: int
    resume_id: Optional[int] = None


class StatusBody(BaseModel):
    status: str
    note: Optional[str] = ""


class RegisterBody(BaseModel):
    email: str
    password: str = Field(min_length=6)
    role: str = "student"
    full_name: str = "New Student"


class RefreshBody(BaseModel):
    refresh_token: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def execute(sql: str, params: tuple = (), fetch: bool = False):
    c = conn()
    try:
        cur = c.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        c.commit()
        return rows
    finally:
        c.close()


def row_dict(row):
    return dict(row) if row else None


def seed_data(reset: bool = False):
    """Create an idempotent, realistic demo dataset and score applications from live services."""
    if reset:
        _rate_buckets.clear()
    c = conn()
    c.executescript(SCHEMA)
    columns = {
        row["name"] for row in c.execute("PRAGMA table_info(student_skills)").fetchall()
    }
    if "source" not in columns:
        c.execute("ALTER TABLE student_skills ADD COLUMN source TEXT DEFAULT 'manual'")

    existing = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    admin_hash = c.execute(
        "SELECT password_hash FROM users WHERE email=?", ("admin@skilltrack.demo",)
    ).fetchone()
    if (
        existing
        and not reset
        and admin_hash
        and admin_hash["password_hash"].startswith("$2b$")
    ):
        c.close()
        return

    if reset:
        for table in [
            "notifications",
            "ai_cache",
            "history",
            "applications",
            "resumes",
            "projects",
            "certificates",
            "student_skills",
            "jobs",
            "companies",
            "students",
            "skills",
            "users",
        ]:
            c.execute(f"DELETE FROM {table}")
    elif existing:
        reset = True
        for table in [
            "notifications",
            "ai_cache",
            "history",
            "applications",
            "resumes",
            "projects",
            "certificates",
            "student_skills",
            "jobs",
            "companies",
            "students",
            "skills",
            "users",
        ]:
            c.execute(f"DELETE FROM {table}")

    stamp = now_iso()
    for row in load_taxonomy():
        aliases = [x.strip() for x in row.get("aliases", "").split("|") if x.strip()]
        c.execute(
            "INSERT OR IGNORE INTO skills(name,category,aliases) VALUES(?,?,?)",
            (row["name"], row["category"], json.dumps(aliases)),
        )

    def add_user(email: str, role: str, name: str = "") -> int:
        password = "Admin@123" if role == "admin" else "Demo@123"
        uid = c.execute(
            "INSERT INTO users(email,password_hash,role,is_active,created_at) VALUES(?,?,?,?,?)",
            (email, hash_password(password), role, 1, stamp),
        ).lastrowid
        if role == "student":
            slug = name.lower().replace(" ", "-")
            c.execute(
                "INSERT INTO students(user_id,full_name,phone,college,course,semester,cgpa,graduation_year,bio,github_url,linkedin_url,gender,avatar_color) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    uid,
                    name,
                    "+91 98765 43210",
                    "Indian Institute of Information Technology",
                    "MCA Data Science",
                    4,
                    8.0,
                    2026,
                    "Curious builder who turns messy data into useful decisions.",
                    f"https://github.com/{slug}",
                    f"https://linkedin.com/in/{slug}",
                    "Prefer not to say",
                    "#4F46E5",
                ),
            )
        return uid

    add_user("admin@skilltrack.demo", "admin", "Placement Admin")

    recruiter_specs = [
        ("priya@technova.demo", "TechNova Solutions", "Bengaluru", "Software / AI", 1),
        ("rahul@datawise.demo", "DataWise Analytics", "Pune", "Data and BI", 1),
        (
            "anita@cloudcraft.demo",
            "CloudCraft Systems",
            "Hyderabad",
            "Cloud / DevOps",
            1,
        ),
        ("tara@pixelforge.demo", "PixelForge Studios", "Chandigarh", "Web / UI", 1),
        (
            "vivek@pixelforge.demo",
            "Vivek Digital Labs",
            "Chandigarh",
            "Digital Products",
            0,
        ),
    ]
    recruiter_ids: list[int] = []
    for email, company, location, industry, approved in recruiter_specs:
        uid = add_user(email, "recruiter", company)
        recruiter_ids.append(uid)
        c.execute(
            "INSERT INTO companies(user_id,name,website,location,industry,description,is_approved) VALUES(?,?,?,?,?,?,?)",
            (
                uid,
                company,
                "https://example.com",
                location,
                industry,
                f"{company} builds practical digital products with cross-functional engineering, data and product teams.",
                approved,
            ),
        )

    student_specs = [
        {
            "name": "Aarav Sharma",
            "cgpa": 8.6,
            "skills": [
                "Python",
                "SQL",
                "Pandas",
                "scikit-learn",
                "Flask",
                "React",
                "Git",
                "Power BI",
            ],
            "projects": [
                (
                    "Customer Churn Predictor",
                    "Built a supervised learning pipeline to identify high-risk customers, compare classification models and publish interpretable retention insights.",
                    "Python, Pandas, scikit-learn, SQL",
                ),
                (
                    "Sales Performance Dashboard",
                    "Designed a Power BI dashboard with SQL-backed metrics for revenue, conversion and regional performance tracking.",
                    "SQL, Power BI, Python",
                ),
                (
                    "Campus Placement API",
                    "Created a Flask REST API for student-job matching with validation, search and role-based endpoints.",
                    "Python, Flask, SQL, Git",
                ),
            ],
            "certs": [
                ("Machine Learning Specialization", "Coursera", 2026),
                ("Power BI Data Analyst", "Microsoft Learn", 2025),
            ],
        },
        {
            "name": "Simran Kaur",
            "cgpa": 7.9,
            "skills": [
                "React",
                "JavaScript",
                "HTML",
                "CSS",
                "Node.js",
                "MongoDB",
                "REST APIs",
            ],
            "projects": [
                (
                    "Campus Events Hub",
                    "Built a responsive event discovery app with search, registration and REST API integration.",
                    "React, JavaScript, Node.js",
                ),
                (
                    "Placement Portal UI",
                    "Implemented accessible student workflows and reusable interface components.",
                    "React, CSS, REST APIs",
                ),
            ],
            "certs": [("Meta Front-End Developer", "Meta", 2025)],
        },
        {
            "name": "Rohan Verma",
            "cgpa": 7.2,
            "skills": ["Python", "HTML", "CSS", "SQL"],
            "projects": [
                (
                    "Student Expense Tracker",
                    "Created a lightweight expense tracker with SQL persistence and monthly summaries.",
                    "Python, SQL, HTML, CSS",
                ),
                (
                    "Library Search Tool",
                    "Built a searchable catalogue with filters and simple data validation.",
                    "Python, SQL",
                ),
            ],
            "certs": [("Python Essentials", "Cisco", 2025)],
        },
        {
            "name": "Neha Gupta",
            "cgpa": 9.0,
            "skills": [
                "Python",
                "SQL",
                "scikit-learn",
                "TensorFlow",
                "NLP",
                "Tableau",
                "Statistics",
            ],
            "projects": [
                (
                    "Customer Sentiment Analyzer",
                    "Developed an NLP pipeline for review classification with preprocessing, evaluation and error analysis.",
                    "Python, NLP, scikit-learn",
                ),
                (
                    "Demand Forecasting Study",
                    "Compared time-series baselines and visualized demand trends for planning decisions.",
                    "Python, Statistics, Tableau",
                ),
            ],
            "certs": [
                ("TensorFlow Developer", "Google", 2025),
                ("Applied Machine Learning", "IBM", 2025),
            ],
        },
        {
            "name": "Karan Singh",
            "cgpa": 8.1,
            "skills": ["Java", "Spring Boot", "MySQL", "Docker", "AWS", "Git"],
            "projects": [
                (
                    "Inventory Management API",
                    "Built a Spring Boot service for stock movements, validation and relational persistence.",
                    "Java, Spring Boot, MySQL",
                ),
                (
                    "Containerized Order Service",
                    "Packaged a REST service with Docker and documented AWS deployment steps.",
                    "Java, Docker, AWS",
                ),
            ],
            "certs": [("AWS Cloud Practitioner", "Amazon Web Services", 2025)],
        },
        {
            "name": "Ishita Malhotra",
            "cgpa": 8.4,
            "skills": ["Python", "Django", "PostgreSQL", "React", "Docker", "Linux"],
            "projects": [
                (
                    "Research Workflow Portal",
                    "Built a Django application for experiment tracking, file metadata and review workflows.",
                    "Python, Django, PostgreSQL",
                ),
                (
                    "Containerized Analytics App",
                    "Created a Dockerized analytics frontend and API with Linux deployment notes.",
                    "React, Docker, Linux",
                ),
            ],
            "certs": [("Django Web Development", "Udemy", 2025)],
        },
        {
            "name": "Aditya Mehta",
            "cgpa": 6.8,
            "skills": ["C++", "Python", "SQL"],
            "projects": [
                (
                    "Campus Records Utility",
                    "Implemented CRUD utilities for student records and SQL-backed reports.",
                    "Python, SQL, C++",
                ),
                (
                    "Algorithm Practice Suite",
                    "Built small command-line programs covering data structures and search problems.",
                    "C++, Python",
                ),
            ],
            "certs": [("SQL Fundamentals", "DataCamp", 2025)],
        },
        {
            "name": "Pooja Yadav",
            "cgpa": 8.8,
            "skills": ["Power BI", "Excel", "SQL", "Python", "Statistics", "Tableau"],
            "projects": [
                (
                    "Retail KPI Dashboard",
                    "Built a multi-page BI dashboard with SQL transformations and operational KPIs.",
                    "Power BI, SQL, Excel",
                ),
                (
                    "Sales Variance Analysis",
                    "Used Python and statistics to investigate monthly variance and outliers.",
                    "Python, Statistics, Tableau",
                ),
            ],
            "certs": [
                ("PL-300 Power BI", "Microsoft", 2025),
                ("Business Analytics", "Google", 2025),
            ],
        },
        {
            "name": "Harpreet Gill",
            "cgpa": 7.6,
            "skills": ["JavaScript", "React", "TypeScript", "Figma", "CSS"],
            "projects": [
                (
                    "Design System Starter",
                    "Created reusable React components and interaction patterns from Figma specifications.",
                    "React, TypeScript, Figma",
                ),
                (
                    "Accessible Student Portal",
                    "Implemented keyboard-friendly layouts and responsive workflows.",
                    "React, CSS, JavaScript",
                ),
            ],
            "certs": [("UX Design Foundations", "Google", 2025)],
        },
        {
            "name": "Vikram Joshi",
            "cgpa": 8.0,
            "skills": ["Python", "FastAPI", "SQL", "Docker", "Git", "Machine Learning"],
            "projects": [
                (
                    "ML Inference API",
                    "Served a trained classifier through a FastAPI endpoint with validation and structured responses.",
                    "Python, FastAPI, Machine Learning",
                ),
                (
                    "Data Pipeline Runner",
                    "Built a Dockerized batch pipeline for SQL extraction and feature preparation.",
                    "Python, SQL, Docker",
                ),
            ],
            "certs": [("FastAPI Backend Development", "Udemy", 2025)],
        },
    ]

    student_ids: dict[str, int] = {}
    for spec in student_specs:
        uid = add_user(
            spec["name"].lower().replace(" ", ".") + "@skilltrack.demo",
            "student",
            spec["name"],
        )
        student_ids[spec["name"]] = uid
        c.execute("UPDATE students SET cgpa=? WHERE user_id=?", (spec["cgpa"], uid))
        for idx, skill_name in enumerate(spec["skills"]):
            row = c.execute(
                "SELECT id FROM skills WHERE lower(name)=lower(?)", (skill_name,)
            ).fetchone()
            if not row:
                row_id = c.execute(
                    "INSERT INTO skills(name,category,aliases) VALUES(?,?,?)",
                    (skill_name, "Programming", json.dumps([skill_name.lower()])),
                ).lastrowid
            else:
                row_id = row["id"]
            c.execute(
                "INSERT OR REPLACE INTO student_skills(student_id,skill_id,proficiency,source) VALUES(?,?,?,?)",
                (uid, row_id, min(5, 3 + idx % 3), "manual"),
            )
        for title, description, stack in spec["projects"]:
            c.execute(
                "INSERT INTO projects(student_id,title,description,tech_stack,link) VALUES(?,?,?,?,?)",
                (
                    uid,
                    title,
                    description,
                    stack,
                    f"https://github.com/{spec['name'].lower().replace(' ', '-')}/{title.lower().replace(' ', '-') }",
                ),
            )
        for title, issuer, year in spec["certs"]:
            c.execute(
                "INSERT INTO certificates(student_id,title,issuer,year) VALUES(?,?,?,?)",
                (uid, title, issuer, year),
            )

        # Generate a text-based PDF from exactly the same profile data used by the database.
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas

            pdf_path = (
                SAMPLE_DIR / f"{uid}-{spec['name'].lower().replace(' ', '-')}.pdf"
            )
            pdf = canvas.Canvas(str(pdf_path), pagesize=A4)
            y = 800
            pdf.setFont("Helvetica-Bold", 18)
            pdf.drawString(48, y, spec["name"])
            y -= 24
            pdf.setFont("Helvetica", 9)
            pdf.drawString(
                48,
                y,
                "MCA Data Science · Indian Institute of Information Technology · Graduation 2026",
            )
            y -= 24
            sections = [
                (
                    "SUMMARY",
                    "Curious builder who turns messy data into useful decisions.",
                ),
                ("SKILLS", ", ".join(spec["skills"])),
                (
                    "PROJECTS",
                    " | ".join(
                        f"{x[0]}: {x[1]} Tech: {x[2]}" for x in spec["projects"]
                    ),
                ),
                ("EDUCATION", f"MCA Data Science · CGPA {spec['cgpa']:.1f}"),
                (
                    "CERTIFICATIONS",
                    " | ".join(f"{x[0]} — {x[1]} ({x[2]})" for x in spec["certs"]),
                ),
            ]
            for heading, body in sections:
                pdf.setFont("Helvetica-Bold", 10)
                pdf.drawString(48, y, heading)
                y -= 14
                pdf.setFont("Helvetica", 8.5)
                words = body.split()
                line = ""
                for word in words:
                    if len(line) + len(word) + 1 > 100:
                        pdf.drawString(48, y, line)
                        y -= 12
                        line = word
                    else:
                        line = f"{line} {word}".strip()
                if line:
                    pdf.drawString(48, y, line)
                    y -= 20
                if y < 70:
                    pdf.showPage()
                    y = 800
            pdf.save()
            parsed = parse_pdf(pdf_path)
            c.execute(
                "INSERT INTO resumes(student_id,original_name,file_path,extracted_text,parsed_at) VALUES(?,?,?,?,?)",
                (uid, pdf_path.name, str(pdf_path), parsed["text"], stamp),
            )
        except Exception:
            # ReportLab is in the requirements; the database still remains usable if PDF generation fails.
            pass

    role_text = {
        "Data Science Intern": "Work with Python, SQL and pandas to prepare datasets, validate analytical assumptions and build supervised learning prototypes with scikit-learn. You will support exploratory analysis, feature preparation, model evaluation and clear stakeholder reporting. The internship includes hands-on work with real product questions, code reviews and documented experiments. We value candidates who can explain why a metric matters, compare alternatives and communicate uncertainty rather than only producing a model output.",
        "ML Engineer (Fresher)": "Build reproducible machine-learning services using Python and scikit-learn. You will package inference workflows, write tests, work with Docker containers and learn cloud deployment patterns on AWS. The team expects disciplined feature pipelines, model evaluation, logging and clear documentation. Candidates should be comfortable debugging data issues, reviewing model behaviour and collaborating with backend engineers on production interfaces.",
        "Business Intelligence Analyst": "Translate business questions into SQL analysis, Power BI dashboards and concise recommendations. You will clean data, define reusable metrics, investigate variance and partner with operations teams to improve reporting. The role values statistical reasoning, spreadsheet fluency and the ability to explain a dashboard to non-technical stakeholders. You will also document metric definitions and validate numbers before publishing reports.",
        "Python Backend Developer": "Develop maintainable Python APIs using Flask, SQL and practical testing patterns. You will design endpoints, validate input, work with relational data and collaborate with frontend and product teammates. The role includes debugging, code review, API documentation and incremental delivery. Candidates who can explain trade-offs around data validation, error handling and service boundaries will thrive in this environment.",
        "Full-Stack Developer (React + Node)": "Build user-facing workflows with React and JavaScript and connect them to Node.js services and REST APIs. You will implement responsive interfaces, handle asynchronous data states, test key interactions and work with MongoDB-backed features. The team values reusable components, clear API contracts, accessibility and thoughtful product details. Experience turning a design into a reliable end-to-end flow is especially useful.",
        "Frontend Developer (React)": "Create responsive React interfaces from product requirements and Figma designs. You will build reusable components, manage client-side state, test keyboard and responsive behaviour and collaborate closely with designers. The role values clean CSS, JavaScript fundamentals and attention to accessibility. Candidates should be able to turn a visual design into a polished workflow without sacrificing maintainability.",
        "DevOps Engineer Trainee": "Support deployment workflows using Docker, Linux and AWS. You will learn to package services, inspect logs, automate repeatable setup and document operational runbooks. The team values curiosity, careful incident analysis and a security-minded approach to configuration. Python and Git are useful for small automation tasks and infrastructure tooling.",
        "Cloud Support Associate": "Help troubleshoot cloud-hosted services on AWS and Linux environments. You will investigate logs, validate network and service configuration, document recurring issues and automate simple support tasks with Python where appropriate. The role rewards structured debugging, careful communication and a willingness to learn cloud concepts through real incidents and runbooks.",
        "Data Analyst": "Use SQL, Excel and Python to answer operational questions and produce reliable business analysis. You will clean data, define metrics, investigate anomalies and build clear visual summaries in Tableau when useful. The role values statistical thinking, reproducibility and the ability to turn analysis into a practical recommendation for a business stakeholder.",
        "NLP Research Intern": "Explore natural-language processing workflows using Python and machine-learning techniques. You will prepare text datasets, compare modelling approaches, evaluate errors and document experiments. Familiarity with NLP, machine learning and TensorFlow is useful. The internship emphasizes reproducible experiments, careful evaluation and communicating what a model can and cannot infer from text.",
        "Software Engineer (Java)": "Develop backend services with Java, Spring Boot and MySQL. You will design REST endpoints, model relational data, write tests and participate in code reviews. Docker and Git are useful for local development and deployment. The team values clean abstractions, defensive validation and the ability to explain how a feature behaves across API, service and database layers.",
        "UI Developer": "Translate Figma designs into responsive HTML and CSS interfaces with attention to accessibility and visual consistency. React and TypeScript are useful for richer application flows. You will collaborate with designers, test layouts across screen sizes and keep components understandable. This opening is currently pending placement-office approval and is not visible to students until approved.",
    }
    job_specs = [
        (
            1,
            "Data Science Intern",
            "TechNova Solutions",
            8.0,
            ["Python", "SQL", "Pandas", "scikit-learn"],
            ["Power BI", "Git"],
            "Bengaluru",
            "Internship",
            25000,
            35000,
            15,
            1,
        ),
        (
            2,
            "ML Engineer (Fresher)",
            "TechNova Solutions",
            8.2,
            ["Python", "scikit-learn", "Docker", "AWS"],
            ["SQL", "Git"],
            "Bengaluru",
            "Full-time",
            700000,
            1000000,
            30,
            1,
        ),
        (
            3,
            "Business Intelligence Analyst",
            "DataWise Analytics",
            7.5,
            ["SQL", "Power BI", "Statistics"],
            ["Excel", "Python"],
            "Pune",
            "Full-time",
            600000,
            900000,
            25,
            1,
        ),
        (
            4,
            "Python Backend Developer",
            "TechNova Solutions",
            7.5,
            ["Python", "Flask", "SQL"],
            ["Docker", "Git"],
            "Bengaluru",
            "Full-time",
            700000,
            1100000,
            20,
            1,
        ),
        (
            5,
            "Full-Stack Developer (React + Node)",
            "TechNova Solutions",
            7.0,
            ["React", "Node.js", "JavaScript"],
            ["MongoDB", "REST APIs"],
            "Remote",
            "Full-time",
            600000,
            1000000,
            32,
            1,
        ),
        (
            6,
            "Frontend Developer (React)",
            "PixelForge Studios",
            7.2,
            ["React", "JavaScript", "CSS"],
            ["TypeScript", "Figma"],
            "Chandigarh",
            "Full-time",
            550000,
            850000,
            28,
            1,
        ),
        (
            7,
            "DevOps Engineer Trainee",
            "CloudCraft Systems",
            7.0,
            ["Docker", "Linux", "AWS"],
            ["Git", "Python"],
            "Hyderabad",
            "Full-time",
            500000,
            800000,
            35,
            1,
        ),
        (
            8,
            "Cloud Support Associate",
            "CloudCraft Systems",
            7.0,
            ["AWS", "Linux"],
            ["Python", "Docker"],
            "Hyderabad",
            "Full-time",
            480000,
            750000,
            40,
            1,
        ),
        (
            9,
            "Data Analyst",
            "DataWise Analytics",
            7.0,
            ["SQL", "Excel", "Python"],
            ["Tableau", "Statistics"],
            "Pune",
            "Full-time",
            550000,
            800000,
            21,
            1,
        ),
        (
            10,
            "NLP Research Intern",
            "TechNova Solutions",
            8.3,
            ["Python", "NLP", "Machine Learning"],
            ["TensorFlow", "SQL"],
            "Bengaluru",
            "Internship",
            30000,
            40000,
            45,
            1,
        ),
        (
            11,
            "Software Engineer (Java)",
            "DataWise Analytics",
            7.8,
            ["Java", "Spring Boot", "MySQL"],
            ["Docker", "Git"],
            "Pune",
            "Full-time",
            700000,
            1200000,
            -3,
            1,
        ),
        (
            12,
            "UI Developer",
            "PixelForge Studios",
            7.0,
            ["HTML", "CSS", "Figma"],
            ["React", "TypeScript"],
            "Chandigarh",
            "Full-time",
            500000,
            800000,
            14,
            0,
        ),
    ]
    company_rows = {
        r["name"]: r["id"]
        for r in c.execute("SELECT id,name FROM companies").fetchall()
    }
    for (
        jid,
        title,
        company,
        min_cgpa,
        mandatory,
        optional,
        location,
        job_type,
        salary_min,
        salary_max,
        days,
        approved,
    ) in job_specs:
        deadline = datetime.now(timezone.utc) + timedelta(days=days)
        description = role_text[title]
        c.execute(
            "INSERT INTO jobs(id,company_id,title,description,location,job_type,salary_min,salary_max,min_cgpa,deadline,is_approved,status,created_at,mandatory_skills,optional_skills) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                jid,
                company_rows[company],
                title,
                description,
                location,
                job_type,
                salary_min,
                salary_max,
                min_cgpa,
                deadline.isoformat(),
                approved,
                "Open" if days >= 0 else "Closed",
                stamp,
                json.dumps(mandatory),
                json.dumps(optional),
            ),
        )

    # Ensure a useful 30-application demo with the required narratives.
    app_plan = [
        (1, "Aarav Sharma", "Shortlisted"),
        (4, "Aarav Sharma", "Under Review"),
        (1, "Neha Gupta", "Selected"),
        (3, "Neha Gupta", "Selected"),
        (1, "Rohan Verma", "Applied"),
        (1, "Simran Kaur", "Interview"),
        (1, "Ishita Malhotra", "Under Review"),
        (1, "Pooja Yadav", "Shortlisted"),
        (1, "Aditya Mehta", "Under Review"),
        (1, "Vikram Joshi", "Shortlisted"),
        (1, "Karan Singh", "Applied"),
        (1, "Harpreet Gill", "Applied"),
        (4, "Rohan Verma", "Applied"),
        (4, "Vikram Joshi", "Interview"),
        (4, "Ishita Malhotra", "Shortlisted"),
        (4, "Karan Singh", "Applied"),
        (2, "Aarav Sharma", "Under Review"),
        (2, "Neha Gupta", "Interview"),
        (2, "Vikram Joshi", "Shortlisted"),
        (2, "Karan Singh", "Applied"),
        (3, "Pooja Yadav", "Interview"),
        (3, "Aditya Mehta", "Rejected"),
        (9, "Harpreet Gill", "Applied"),
        (5, "Simran Kaur", "Interview"),
        (5, "Harpreet Gill", "Shortlisted"),
        (6, "Harpreet Gill", "Selected"),
        (7, "Karan Singh", "Shortlisted"),
        (7, "Vikram Joshi", "Interview"),
        (9, "Pooja Yadav", "Selected"),
        (10, "Neha Gupta", "Shortlisted"),
    ]
    # Add a couple more applications while keeping the Data Science Intern at 7 applicants.
    app_plan += [(8, "Karan Singh", "Under Review"), (11, "Karan Singh", "Applied")]
    for index, (job_id, student_name, final_status) in enumerate(app_plan):
        sid = student_ids[student_name]
        breakdown = build_breakdown(c, sid, job_id)
        applied_at = (
            datetime.now(timezone.utc) - timedelta(days=(index % 12))
        ).isoformat()
        aid = c.execute(
            "INSERT INTO applications(job_id,student_id,ats_score,score_breakdown,status,applied_at) VALUES(?,?,?,?,?,?)",
            (
                job_id,
                sid,
                breakdown["score"],
                json.dumps(breakdown),
                final_status,
                applied_at,
            ),
        ).lastrowid
        if final_status != "Applied":
            c.execute(
                "INSERT INTO history(application_id,old_status,new_status,changed_by,note,changed_at) VALUES(?,?,?,?,?,?)",
                (
                    aid,
                    "Applied",
                    final_status,
                    recruiter_ids[0],
                    "Seeded status generated after real scoring",
                    applied_at,
                ),
            )
        c.execute(
            "INSERT INTO notifications(user_id,message,link,is_read,created_at) VALUES(?,?,?,?,?)",
            (
                sid,
                f"Your application for {c.execute('SELECT title FROM jobs WHERE id=?', (job_id,)).fetchone()['title']} is {final_status.lower()}.",
                "/student/applications",
                0,
                applied_at,
            ),
        )

    # A few additional realistic status-history transitions.
    rows = c.execute(
        "SELECT id,student_id,job_id,status,applied_at FROM applications WHERE status IN ('Interview','Selected') LIMIT 8"
    ).fetchall()
    for row in rows:
        c.execute(
            "INSERT INTO history(application_id,old_status,new_status,changed_by,note,changed_at) VALUES(?,?,?,?,?,?)",
            (
                row["id"],
                "Shortlisted",
                row["status"],
                recruiter_ids[0],
                "Interview/selection milestone recorded in demo history",
                row["applied_at"],
            ),
        )

    c.commit()
    c.close()


def build_breakdown(
    c, student_id: int, job_id: int, score: Optional[float] = None
) -> dict[str, Any]:
    """Compatibility wrapper used by the existing routes and seed; score is intentionally ignored."""
    return score_application(c, student_id, job_id)


def user_by_token(token: Optional[str]):
    if not token or not token.lower().startswith("bearer "):
        raise HTTPException(401, "Authentication required")
    try:
        claims = decode_token(token.split(" ", 1)[1])
        user_id = int(claims["sub"])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(401, "Invalid or expired session")
    rows = execute("SELECT * FROM users WHERE id=?", (user_id,), True)
    if not rows or rows[0]["role"] != claims["role"] or not rows[0]["is_active"]:
        raise HTTPException(401, "Invalid or inactive session")
    return rows[0]


def current_user(authorization: Optional[str] = Header(default=None)):
    return user_by_token(authorization)


def require_role(user, *roles):
    if user["role"] not in roles:
        raise HTTPException(403, "You do not have access to this area")


def public_job(c, job, user_id=None):
    company = c.execute(
        "SELECT name,location,industry,is_approved FROM companies WHERE id=?",
        (job["company_id"],),
    ).fetchone()
    app_row = (
        c.execute(
            "SELECT id,status,ats_score,score_breakdown FROM applications WHERE job_id=? AND student_id=?",
            (job["id"], user_id),
        ).fetchone()
        if user_id
        else None
    )
    mandatory = json.loads(job["mandatory_skills"])
    optional = json.loads(job["optional_skills"])
    fit = build_breakdown(c, user_id, job["id"]) if user_id else None
    return {
        "id": job["id"],
        "title": job["title"],
        "description": job["description"],
        "location": job["location"],
        "job_type": job["job_type"],
        "salary_min": job["salary_min"],
        "salary_max": job["salary_max"],
        "min_cgpa": job["min_cgpa"],
        "deadline": job["deadline"],
        "status": job["status"],
        "is_approved": bool(job["is_approved"] and company["is_approved"]),
        "company": company["name"],
        "industry": company["industry"],
        "mandatory_skills": mandatory,
        "optional_skills": optional,
        "application": row_dict(app_row),
        "fit": fit,
    }


@app.on_event("startup")
def startup():
    seed_data()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "skilltrack",
        "ai_mode": (
            "live"
            if (os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))
            else "offline demo"
        ),
    }


@app.post("/api/v1/auth/login")
def login(request: Request, body: LoginBody):
    _rate_gate(request, "login", 10)
    rows = execute(
        "SELECT * FROM users WHERE lower(email)=lower(?)", (body.email,), True
    )
    if not rows:
        raise HTTPException(401, "Email or password is incorrect")
    user = rows[0]
    valid, legacy = verify_password(body.password, user["password_hash"])
    if not valid:
        raise HTTPException(401, "Email or password is incorrect")
    if legacy:
        execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (hash_password(body.password), user["id"]),
        )
    if not user["is_active"]:
        raise HTTPException(403, "This account is inactive")
    profile = execute(
        "SELECT full_name FROM students WHERE user_id=?", (user["id"],), True
    )
    return {
        "access_token": create_access_token(user["id"], user["role"]),
        "refresh_token": create_refresh_token(user["id"], user["role"]),
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
            "name": (
                profile[0]["full_name"]
                if profile
                else user["email"].split("@")[0].title()
            ),
        },
    }


@app.post("/api/v1/auth/register")
def register(request: Request, body: RegisterBody):
    _rate_gate(request, "register", 5)
    if body.role not in ("student", "recruiter"):
        raise HTTPException(400, "Only student or recruiter registration is available")
    try:
        c = conn()
        uid = c.execute(
            "INSERT INTO users(email,password_hash,role,is_active,created_at) VALUES(?,?,?,?,?)",
            (body.email, hash_password(body.password), body.role, 1, now_iso()),
        ).lastrowid
        if body.role == "student":
            c.execute(
                "INSERT INTO students(user_id,full_name,cgpa,graduation_year,course,college,bio,avatar_color) VALUES(?,?,?,?,?,?,?,?)",
                (
                    uid,
                    body.full_name,
                    0,
                    2026,
                    "MCA Data Science",
                    "Your college",
                    "Add a short bio to stand out.",
                    "#4F46E5",
                ),
            )
        c.commit()
        c.close()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "An account with this email already exists")
    return {
        "message": "Account created",
        "access_token": create_access_token(uid, body.role),
        "refresh_token": create_refresh_token(uid, body.role),
        "user": {
            "id": uid,
            "email": body.email,
            "role": body.role,
            "name": body.full_name,
        },
    }


@app.post("/api/v1/auth/refresh")
def refresh(body: RefreshBody):
    try:
        claims = decode_token(body.refresh_token, "refresh")
        uid = int(claims["sub"])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(401, "Invalid or expired refresh token")
    rows = execute("SELECT * FROM users WHERE id=? AND is_active=1", (uid,), True)
    if not rows or rows[0]["role"] != claims["role"]:
        raise HTTPException(401, "Invalid refresh token")
    return {
        "access_token": create_access_token(uid, rows[0]["role"]),
        "refresh_token": create_refresh_token(uid, rows[0]["role"]),
        "token_type": "bearer",
    }


@app.get("/api/v1/auth/me")
def me(user=Depends(current_user)):
    return {"id": user["id"], "email": user["email"], "role": user["role"]}


@app.get("/api/v1/stats/public")
def public_stats():
    c = conn()
    average = c.execute(
        "SELECT COALESCE(AVG(ats_score),0) FROM applications WHERE ats_score IS NOT NULL"
    ).fetchone()[0]
    result = {
        "students": c.execute(
            "SELECT COUNT(*) FROM users WHERE role='student'"
        ).fetchone()[0],
        "jobs": c.execute("SELECT COUNT(*) FROM jobs WHERE is_approved=1").fetchone()[
            0
        ],
        "applications": c.execute("SELECT COUNT(*) FROM applications").fetchone()[0],
        "placements": c.execute(
            "SELECT COUNT(*) FROM applications WHERE status='Selected'"
        ).fetchone()[0],
        "avg_match": round(average),
    }
    c.close()
    return result


@app.get("/api/v1/students/me/dashboard")
def student_dashboard(user=Depends(current_user)):
    require_role(user, "student")
    c = conn()
    uid = user["id"]
    profile = c.execute("SELECT * FROM students WHERE user_id=?", (uid,)).fetchone()
    apps = c.execute(
        "SELECT a.*,j.title,j.location,co.name company FROM applications a JOIN jobs j ON j.id=a.job_id JOIN companies co ON co.id=j.company_id WHERE a.student_id=? ORDER BY a.applied_at DESC",
        (uid,),
    ).fetchall()
    applied = {r["job_id"] for r in apps}
    open_jobs = c.execute(
        "SELECT * FROM jobs WHERE is_approved=1 AND status='Open'"
    ).fetchall()
    ranked = []
    for job in open_jobs:
        if job["id"] in applied:
            continue
        fit = build_breakdown(c, uid, job["id"])
        ranked.append((fit["score"], job))
    ranked.sort(key=lambda item: item[0], reverse=True)
    recommendations = [public_job(c, job, uid) for _, job in ranked[:4]]
    own_skill_names = {
        r["name"]
        for r in c.execute(
            "SELECT s.name FROM student_skills ss JOIN skills s ON s.id=ss.skill_id WHERE ss.student_id=?",
            (uid,),
        ).fetchall()
    }
    demand = [
        r["name"]
        for r in c.execute(
            "SELECT mandatory_skills FROM jobs WHERE is_approved=1 AND status='Open'"
        ).fetchall()
        for r in json.loads(r["mandatory_skills"])
    ]
    skill_gaps = list(
        dict.fromkeys([skill for skill in demand if skill not in own_skill_names])
    )[:5]
    average = round(sum((a["ats_score"] or 0) for a in apps) / max(1, len(apps)))
    result = {
        "profile": row_dict(profile),
        "stats": {
            "applications": len(apps),
            "shortlisted": sum(
                a["status"] in ("Shortlisted", "Interview") for a in apps
            ),
            "interviews": sum(a["status"] == "Interview" for a in apps),
            "avg_match": average,
            "profile_completion": round(
                sum(
                    bool(profile[k])
                    for k in (
                        "full_name",
                        "college",
                        "course",
                        "cgpa",
                        "graduation_year",
                        "bio",
                    )
                )
                / 6
                * 100
            ),
        },
        "applications": [row_dict(a) for a in apps[:5]],
        "recommendations": recommendations,
        "skill_gaps": skill_gaps,
    }
    c.close()
    return result


@app.get("/api/v1/students/me")
def student_profile(user=Depends(current_user)):
    require_role(user, "student")
    c = conn()
    profile = row_dict(
        c.execute("SELECT * FROM students WHERE user_id=?", (user["id"],)).fetchone()
    )
    skills = [
        row_dict(r)
        for r in c.execute(
            "SELECT s.name,ss.proficiency,s.category FROM student_skills ss JOIN skills s ON s.id=ss.skill_id WHERE ss.student_id=?",
            (user["id"],),
        ).fetchall()
    ]
    projects = [
        row_dict(r)
        for r in c.execute(
            "SELECT * FROM projects WHERE student_id=?", (user["id"],)
        ).fetchall()
    ]
    certs = [
        row_dict(r)
        for r in c.execute(
            "SELECT * FROM certificates WHERE student_id=?", (user["id"],)
        ).fetchall()
    ]
    c.close()
    return {
        "profile": profile,
        "skills": skills,
        "projects": projects,
        "certificates": certs,
    }


@app.put("/api/v1/students/me")
def update_profile(body: ProfileBody, user=Depends(current_user)):
    require_role(user, "student")
    values = body.model_dump(exclude_unset=True)
    c = conn()
    if values:
        c.execute(
            f"UPDATE students SET {','.join(f'{k}=?' for k in values)} WHERE user_id=?",
            (*values.values(), user["id"]),
        )
        c.commit()
    profile = row_dict(
        c.execute("SELECT * FROM students WHERE user_id=?", (user["id"],)).fetchone()
    )
    c.close()
    return profile


@app.get("/api/v1/students/me/sample-resume")
def sample_resume(user=Depends(current_user)):
    require_role(user, "student")
    files = list(SAMPLE_DIR.glob(f"{user['id']}-*.pdf"))
    if files:
        return FileResponse(
            files[0], media_type="application/pdf", filename="sample_resume.pdf"
        )
    raise HTTPException(404, "Sample resume is not available")


@app.post("/api/v1/students/me/resume")
async def upload_resume(file: UploadFile = File(...), user=Depends(current_user)):
    require_role(user, "student")
    data = await file.read()
    if (
        not file.filename
        or not file.filename.lower().endswith(".pdf")
        or not data.startswith(b"%PDF")
    ):
        raise HTTPException(400, "Please upload a valid PDF resume")
    if len(data) > settings.upload_limit:
        raise HTTPException(413, "Resume must be under 5 MB")
    safe = f"{uuid.uuid4()}.pdf"
    path = UPLOAD_DIR / safe
    path.write_bytes(data)
    try:
        parsed = parse_pdf(path)
    except ValueError as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc))
    extracted = extract_skills(parsed["text"])
    c = conn()
    rid = c.execute(
        "INSERT INTO resumes(student_id,original_name,file_path,extracted_text,parsed_at) VALUES(?,?,?,?,?)",
        (user["id"], file.filename, str(path), parsed["text"], now_iso()),
    ).lastrowid
    c.commit()
    c.close()
    return {
        "resume_id": rid,
        "status": "parsed",
        "sections": parsed["sections"],
        "skills": extracted,
    }


@app.post("/api/v1/students/me/resume/confirm-skills")
def confirm_resume_skills(skills: list[str], user=Depends(current_user)):
    require_role(user, "student")
    c = conn()
    c.execute(
        "DELETE FROM student_skills WHERE student_id=? AND source='resume'",
        (user["id"],),
    )
    confirmed = []
    for skill in skills:
        row = c.execute(
            "SELECT id,name FROM skills WHERE lower(name)=lower(?) OR lower(aliases) LIKE ?",
            (skill, f"%{skill.lower()}%"),
        ).fetchone()
        if row:
            c.execute(
                "INSERT OR REPLACE INTO student_skills(student_id,skill_id,proficiency,source) VALUES(?,?,?,?)",
                (user["id"], row["id"], 3, "resume"),
            )
            confirmed.append(row["name"])
    c.commit()
    c.close()
    return {"ok": True, "skills": confirmed}


@app.get("/api/v1/jobs")
def jobs(
    q: str = "",
    location: str = "",
    job_type: str = "",
    skills: str = "",
    salary_min: float = 0,
    sort: str = "latest",
    page: int = 1,
    page_size: int = 20,
    user=Depends(current_user),
):
    c = conn()
    params = [now_iso()]
    clauses = [
        "j.is_approved=1",
        "co.is_approved=1",
        "j.status='Open'",
        "j.deadline >= ?",
    ]
    if q:
        clauses.append(
            "(lower(j.title) like ? or lower(j.description) like ? or lower(co.name) like ?)"
        )
        params += [f"%{q.lower()}%"] * 3
    if location:
        clauses.append("lower(j.location) like ?")
        params.append(f"%{location.lower()}%")
    if job_type:
        clauses.append("j.job_type=?")
        params.append(job_type)
    if salary_min:
        clauses.append("j.salary_max>=?")
        params.append(salary_min)
    for skill in [s.strip().lower() for s in skills.split(",") if s.strip()]:
        clauses.append("lower(j.mandatory_skills || j.optional_skills) like ?")
        params.append(f"%{skill}%")
    total = c.execute(
        f"SELECT COUNT(*) AS n FROM jobs j JOIN companies co ON co.id=j.company_id WHERE {' AND '.join(clauses)}",
        tuple(params),
    ).fetchone()["n"]
    if sort == "match" and user["role"] == "student":
        rows = c.execute(
            f"SELECT j.* FROM jobs j JOIN companies co ON co.id=j.company_id WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchall()
        ranked = []
        for row in rows:
            item = public_job(c, row, user["id"])
            ranked.append((float((item.get("fit") or {}).get("score", 0)), item))
        ranked.sort(key=lambda x: x[0], reverse=True)
        start = max(0, page - 1) * page_size
        items = [item for _, item in ranked[start : start + page_size]]
    else:
        order = "j.salary_max DESC" if sort == "salary" else "j.created_at DESC"
        rows = c.execute(
            f"SELECT j.* FROM jobs j JOIN companies co ON co.id=j.company_id WHERE {' AND '.join(clauses)} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, max(1, page_size), max(0, page - 1) * page_size),
        ).fetchall()
        items = [
            public_job(c, r, user["id"] if user["role"] == "student" else None)
            for r in rows
        ]
    c.close()
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/api/v1/jobs/mine")
def my_jobs(user=Depends(current_user)):
    require_role(user, "recruiter")
    c = conn()
    company = c.execute(
        "SELECT id FROM companies WHERE user_id=?", (user["id"],)
    ).fetchone()
    rows = c.execute(
        "SELECT j.*,COUNT(a.id) applicant_count FROM jobs j LEFT JOIN applications a ON a.job_id=j.id WHERE j.company_id=? GROUP BY j.id ORDER BY j.created_at DESC",
        (company["id"],),
    ).fetchall()
    c.close()
    return {"items": [row_dict(r) for r in rows], "total": len(rows)}


@app.get("/api/v1/jobs/{job_id}")
def job_detail(job_id: int, user=Depends(current_user)):
    c = conn()
    job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise HTTPException(404, "Job not found")
    company_ok = c.execute(
        "SELECT is_approved FROM companies WHERE id=?", (job["company_id"],)
    ).fetchone()
    if user["role"] == "student" and (
        not job["is_approved"] or not company_ok or not company_ok["is_approved"]
    ):
        raise HTTPException(404, "Job not found")
    result = public_job(c, job, user["id"] if user["role"] == "student" else None)
    c.close()
    return result


@app.get("/api/v1/jobs/{job_id}/fit")
def job_fit(job_id: int, user=Depends(current_user)):
    require_role(user, "student")
    c = conn()
    if not c.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone():
        c.close()
        raise HTTPException(404, "Job not found")
    fit = build_breakdown(c, user["id"], job_id)
    c.close()
    return fit


@app.post("/api/v1/applications")
def apply(body: ApplyBody, background: BackgroundTasks, user=Depends(current_user)):
    require_role(user, "student")
    c = conn()
    job = c.execute("SELECT * FROM jobs WHERE id=?", (body.job_id,)).fetchone()
    if not job or not job["is_approved"]:
        raise HTTPException(404, "This job is not available")
    company = c.execute(
        "SELECT is_approved FROM companies WHERE id=?", (job["company_id"],)
    ).fetchone()
    if not company or not company["is_approved"]:
        raise HTTPException(404, "This job is not available")
    if job["status"] == "Closed" or datetime.fromisoformat(
        job["deadline"]
    ) < datetime.now(timezone.utc):
        raise HTTPException(400, "This job is closed or past its deadline")
    if c.execute(
        "SELECT id FROM applications WHERE job_id=? AND student_id=?",
        (body.job_id, user["id"]),
    ).fetchone():
        raise HTTPException(409, "You have already applied to this job")
    aid = c.execute(
        "INSERT INTO applications(job_id,student_id,resume_id,status,applied_at) VALUES(?,?,?,?,?)",
        (body.job_id, user["id"], body.resume_id, "Applied", now_iso()),
    ).lastrowid
    c.commit()
    c.close()
    background.add_task(finish_scoring, aid)
    return {
        "id": aid,
        "status": "Scoring...",
        "message": "Application submitted. SkillTrack is calculating your fit.",
    }


def finish_scoring(application_id: int):
    c = conn()
    app_row = c.execute(
        "SELECT student_id,job_id FROM applications WHERE id=?", (application_id,)
    ).fetchone()
    if app_row:
        breakdown = build_breakdown(c, app_row["student_id"], app_row["job_id"])
        c.execute(
            "UPDATE applications SET ats_score=?,score_breakdown=? WHERE id=?",
            (breakdown["score"], json.dumps(breakdown), application_id),
        )
        c.commit()
    c.close()


@app.get("/api/v1/applications/me")
def my_applications(user=Depends(current_user)):
    require_role(user, "student")
    c = conn()
    rows = c.execute(
        "SELECT a.*,j.title,j.location,j.deadline,co.name company FROM applications a JOIN jobs j ON j.id=a.job_id JOIN companies co ON co.id=j.company_id WHERE a.student_id=? ORDER BY a.applied_at DESC",
        (user["id"],),
    ).fetchall()
    result = []
    for r in rows:
        item = row_dict(r)
        item["score_breakdown"] = (
            json.loads(item["score_breakdown"]) if item["score_breakdown"] else None
        )
        item["history"] = [
            row_dict(h)
            for h in c.execute(
                "SELECT * FROM history WHERE application_id=? ORDER BY changed_at",
                (r["id"],),
            ).fetchall()
        ]
        result.append(item)
    c.close()
    return {"items": result, "total": len(result)}


@app.get("/api/v1/jobs/{job_id}/applicants")
def applicants(
    job_id: int,
    blind: bool = False,
    status: str = "",
    min_score: float = 0,
    user=Depends(current_user),
):
    require_role(user, "recruiter", "admin")
    c = conn()
    company = (
        c.execute("SELECT id FROM companies WHERE user_id=?", (user["id"],)).fetchone()
        if user["role"] == "recruiter"
        else None
    )
    job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job or (company and job["company_id"] != company["id"]):
        raise HTTPException(403, "You can only view applicants for your own jobs")
    params = (
        [job_id] + ([status] if status else []) + ([min_score] if min_score else [])
    )
    extra = (" AND a.status=?" if status else "") + (
        " AND a.ats_score>=?" if min_score else ""
    )
    rows = c.execute(
        f"SELECT a.*,s.full_name,s.cgpa,s.gender,s.college,u.email FROM applications a JOIN students s ON s.user_id=a.student_id JOIN users u ON u.id=a.student_id WHERE a.job_id=?{extra} ORDER BY a.ats_score DESC",
        tuple(params),
    ).fetchall()
    result = []
    for r in rows:
        item = {
            "id": r["id"],
            "label": f"Candidate #{chr(65 + ((r['id'] - 1) % 26))}{100 + r['id']}",
            "score": r["ats_score"],
            "status": r["status"],
            "cgpa": r["cgpa"],
            "breakdown": (
                json.loads(r["score_breakdown"]) if r["score_breakdown"] else None
            ),
        }
        identity_allowed = (not blind) and r["status"] in (
            "Shortlisted",
            "Interview",
            "Selected",
        )
        if identity_allowed:
            item.update(
                {
                    "name": r["full_name"],
                    "email": r["email"],
                    "gender": r["gender"],
                    "college": r["college"],
                    "avatar_color": "#4F46E5",
                }
            )
        result.append(item)
    c.close()
    return {"items": result, "blind": blind, "total": len(result)}


@app.patch("/api/v1/applications/{application_id}/status")
def update_status(application_id: int, body: StatusBody, user=Depends(current_user)):
    require_role(user, "recruiter", "admin")
    allowed = {
        "Applied",
        "Under Review",
        "Shortlisted",
        "Interview",
        "Selected",
        "Rejected",
    }
    if body.status not in allowed:
        raise HTTPException(400, "Unsupported status")
    c = conn()
    app_row = c.execute(
        "SELECT a.*,j.company_id,j.title FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.id=?",
        (application_id,),
    ).fetchone()
    if not app_row:
        raise HTTPException(404, "Application not found")
    if (
        user["role"] == "recruiter"
        and not c.execute(
            "SELECT id FROM companies WHERE id=? AND user_id=?",
            (app_row["company_id"], user["id"]),
        ).fetchone()
    ):
        raise HTTPException(403, "Not your company")
    old = app_row["status"]
    c.execute(
        "UPDATE applications SET status=? WHERE id=?", (body.status, application_id)
    )
    c.execute(
        "INSERT INTO history(application_id,old_status,new_status,changed_by,note,changed_at) VALUES(?,?,?,?,?,?)",
        (
            application_id,
            old,
            body.status,
            user["id"],
            body.note or "Status updated by recruiter",
            now_iso(),
        ),
    )
    c.execute(
        "INSERT INTO notifications(user_id,message,link,is_read,created_at) VALUES(?,?,?,?,?)",
        (
            app_row["student_id"],
            f"Your application for {app_row['title']} moved to {body.status}.",
            "/student/applications",
            0,
            now_iso(),
        ),
    )
    c.commit()
    c.close()
    return {"message": "Status updated", "status": body.status}


@app.get("/api/v1/notifications")
def notifications(user=Depends(current_user)):
    c = conn()
    rows = [
        row_dict(r)
        for r in c.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
    ]
    c.close()
    return {"items": rows, "unread": sum(not r["is_read"] for r in rows)}


@app.patch("/api/v1/notifications/{notification_id}/read")
def read_notification(notification_id: int, user=Depends(current_user)):
    execute(
        "UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?",
        (notification_id, user["id"]),
    )
    return {"ok": True}


@app.post("/api/v1/notifications/read-all")
def read_all(user=Depends(current_user)):
    execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
    return {"ok": True}


@app.get("/api/v1/recruiter/dashboard")
def recruiter_dashboard(user=Depends(current_user)):
    require_role(user, "recruiter")
    c = conn()
    company = c.execute(
        "SELECT * FROM companies WHERE user_id=?", (user["id"],)
    ).fetchone()
    jobs_rows = c.execute(
        "SELECT j.*,COUNT(a.id) applicant_count FROM jobs j LEFT JOIN applications a ON a.job_id=j.id WHERE j.company_id=? GROUP BY j.id ORDER BY j.id",
        (company["id"],),
    ).fetchall()
    statuses = {
        s: c.execute(
            "SELECT COUNT(*) FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.company_id=? AND a.status=?",
            (company["id"], s),
        ).fetchone()[0]
        for s in [
            "Applied",
            "Under Review",
            "Shortlisted",
            "Interview",
            "Selected",
            "Rejected",
        ]
    }
    open_count = c.execute(
        "SELECT COUNT(*) FROM jobs WHERE company_id=? AND is_approved=1 AND status='Open' AND deadline>=?",
        (company["id"], now_iso()),
    ).fetchone()[0]
    c.close()
    return {
        "company": row_dict(company),
        "jobs": [row_dict(r) for r in jobs_rows],
        "status_funnel": statuses,
        "stats": {
            "jobs": open_count,
            "applications": sum(r["applicant_count"] for r in jobs_rows),
            "shortlisted": statuses["Shortlisted"] + statuses["Interview"],
            "placements": statuses["Selected"],
        },
    }


@app.get("/api/v1/admin/stats")
def admin_stats(user=Depends(current_user)):
    require_role(user, "admin")
    c = conn()
    statuses = {
        s: c.execute(
            "SELECT COUNT(*) AS n FROM applications WHERE status=?", (s,)
        ).fetchone()["n"]
        for s in [
            "Applied",
            "Under Review",
            "Shortlisted",
            "Interview",
            "Selected",
            "Rejected",
        ]
    }
    companies = [
        row_dict(r)
        for r in c.execute(
            "SELECT co.name,COUNT(a.id) applications,COALESCE(SUM(CASE WHEN a.status='Selected' THEN 1 ELSE 0 END),0) placements FROM companies co LEFT JOIN jobs j ON j.company_id=co.id LEFT JOIN applications a ON a.job_id=j.id GROUP BY co.id ORDER BY applications DESC"
        ).fetchall()
    ]
    demand_counts = {}
    for row in c.execute(
        "SELECT mandatory_skills,optional_skills FROM jobs WHERE is_approved=1 AND status='Open' AND deadline >= ?",
        (now_iso(),),
    ).fetchall():
        for skill in json.loads(row["mandatory_skills"]) + json.loads(
            row["optional_skills"]
        ):
            demand_counts[skill] = demand_counts.get(skill, 0) + 1
    supply_counts = {
        row["name"]: row["n"]
        for row in c.execute(
            "SELECT s.name,COUNT(DISTINCT ss.student_id) n FROM student_skills ss JOIN skills s ON s.id=ss.skill_id GROUP BY s.name"
        ).fetchall()
    }
    over_time = [
        row_dict(r)
        for r in c.execute(
            "SELECT substr(applied_at,1,7) month,COUNT(*) applications FROM applications GROUP BY month ORDER BY month"
        ).fetchall()
    ]
    c.close()
    return {
        "kpis": {
            "students": execute(
                "SELECT COUNT(*) n FROM users WHERE role='student'", (), True
            )[0]["n"],
            "jobs": execute(
                "SELECT COUNT(*) n FROM jobs WHERE is_approved=1", (), True
            )[0]["n"],
            "applications": execute("SELECT COUNT(*) n FROM applications", (), True)[0][
                "n"
            ],
            "placements": statuses["Selected"],
        },
        "statuses": statuses,
        "companies": companies,
        "demand": [
            {"skill": s, "demand": d, "supply": supply_counts.get(s, 0)}
            for s, d in sorted(
                demand_counts.items(), key=lambda item: item[1], reverse=True
            )[:10]
        ],
        "over_time": over_time,
        "skill_supply_demand": [
            {"skill": s, "demand": d, "supply": supply_counts.get(s, 0)}
            for s, d in sorted(
                demand_counts.items(), key=lambda item: item[1], reverse=True
            )[:10]
        ],
    }


@app.get("/api/v1/admin/companies/pending")
def pending_companies(user=Depends(current_user)):
    require_role(user, "admin")
    return {
        "items": [
            row_dict(r)
            for r in execute("SELECT * FROM companies WHERE is_approved=0", (), True)
        ]
    }


@app.patch("/api/v1/admin/companies/{company_id}/approve")
def approve_company(company_id: int, user=Depends(current_user)):
    require_role(user, "admin")
    execute("UPDATE companies SET is_approved=1 WHERE id=?", (company_id,))
    return {"ok": True}


@app.get("/api/v1/admin/jobs/pending")
def pending_jobs(user=Depends(current_user)):
    require_role(user, "admin")
    return {
        "items": [
            row_dict(r)
            for r in execute(
                "SELECT j.*,co.name company FROM jobs j JOIN companies co ON co.id=j.company_id WHERE j.is_approved=0",
                (),
                True,
            )
        ]
    }


@app.patch("/api/v1/admin/jobs/{job_id}/approve")
def approve_job(job_id: int, user=Depends(current_user)):
    require_role(user, "admin")
    execute("UPDATE jobs SET is_approved=1 WHERE id=?", (job_id,))
    return {"ok": True}


@app.get("/api/v1/admin/placement-insights")
def placement_insights(user=Depends(current_user)):
    require_role(user, "admin")
    results_dir = ROOT / "ml" / "results"
    try:
        placement = json.loads(
            (results_dir / "placement_metrics.json").read_text(encoding="utf-8")
        )
        evaluation = json.loads(
            (results_dir / "scoring_eval.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        raise HTTPException(503, "ML artifacts have not been generated. Run make ml.")
    return {
        "synthetic": True,
        "metrics": {
            k: placement[k]
            for k in ("accuracy", "precision", "recall", "f1", "roc_auc")
        },
        "features": placement["feature_importance"],
        "confusion": placement["confusion_matrix"],
        "evaluation": [
            {
                "model": row["model"],
                "spearman": row["spearman"],
                "ndcg": row["ndcg_at_5"],
            }
            for row in evaluation["results"]
        ],
        "semantic_method": evaluation.get("semantic_method"),
        "cross_validation_accuracy": placement["cross_validation_accuracy"],
        "models": placement.get("models", {}),
    }


@app.post("/api/v1/demo/reset")
def demo_reset(user=Depends(current_user)):
    require_role(user, "admin")
    seed_data(reset=True)
    return {"message": "Demo data restored"}


@app.get("/api/v1/ai/status")
def ai_status():
    return {
        "mode": (
            "live"
            if (os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))
            else "offline demo"
        ),
        "provider": (
            "configured provider"
            if (os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))
            else "Rule-based fallback"
        ),
    }


@app.post("/api/v1/ai/resume-feedback")
def resume_feedback(request: Request, user=Depends(current_user)):
    _rate_gate(request, "ai", 10)
    require_role(user, "student")
    c = conn()
    profile = c.execute(
        "SELECT bio FROM students WHERE user_id=?", (user["id"],)
    ).fetchone()
    skills = [
        r["name"]
        for r in c.execute(
            "SELECT s.name FROM student_skills ss JOIN skills s ON s.id=ss.skill_id WHERE ss.student_id=?",
            (user["id"],),
        ).fetchall()
    ]
    projects = [
        row_dict(r)
        for r in c.execute(
            "SELECT * FROM projects WHERE student_id=?", (user["id"],)
        ).fetchall()
    ]
    resume = c.execute(
        "SELECT extracted_text FROM resumes WHERE student_id=? ORDER BY parsed_at DESC LIMIT 1",
        (user["id"],),
    ).fetchone()
    c.close()
    text = resume["extracted_text"] if resume else ""
    gaps = [
        skill
        for skill in ["Docker", "AWS", "scikit-learn", "SQL", "React"]
        if skill not in skills
    ]
    strengths = [
        f"{len(skills)} canonical skills confirmed",
        f"{len(projects)} hands-on projects listed",
        f"{len(text.split())} resume words extracted",
    ]
    return {
        "strengths": strengths,
        "gaps": gaps[:3] or ["Add quantified outcomes to project bullets"],
        "bullets": [
            "Add impact: state the baseline and the improvement in one line.",
            "Lead with the action and the tool, then close with the result.",
            "Keep each project to 2–3 evidence-rich bullets.",
        ],
        "rewrite": f"{(profile['bio'] if profile else 'Built practical software projects')} with {', '.join(skills[:4])}.",
    }


@app.post("/api/v1/ai/interview-questions")
def interview_questions(request: Request, job_id: int, user=Depends(current_user)):
    _rate_gate(request, "ai", 10)
    require_role(user, "student")
    c = conn()
    job = c.execute(
        "SELECT title,mandatory_skills FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if not job:
        c.close()
        raise HTTPException(404, "Job not found")
    skills = json.loads(job["mandatory_skills"])
    student_skills = [
        r["name"]
        for r in c.execute(
            "SELECT s.name FROM student_skills ss JOIN skills s ON s.id=ss.skill_id WHERE ss.student_id=?",
            (user["id"],),
        ).fetchall()
    ]
    c.close()
    focus = (
        [s for s in skills if s in student_skills] or student_skills[:2] or skills[:2]
    )
    gap = [s for s in skills if s not in student_skills]
    questions = [
        f"Walk us through a project where you used {focus[0] if focus else skills[0]}.",
        (
            f"How would you close the gap on {gap[0]}?"
            if gap
            else f"How would you test a production workflow built with {skills[0]}?"
        ),
        "Tell us about a trade-off you made under a deadline.",
        "How do you explain a technical result to a non-technical stakeholder?",
        "What would you monitor after shipping this solution?",
        "Describe a time feedback changed your implementation.",
        "What would you learn in your first 30 days here?",
        "What makes this role a strong next step for you?",
    ]
    return {"questions": questions}


class JobBody(BaseModel):
    title: str
    description: str
    location: str = "Remote"
    job_type: str = "Full-time"
    salary_min: float = 0
    salary_max: float = 0
    min_cgpa: float = 0
    deadline: str
    mandatory_skills: list[str] = []
    optional_skills: list[str] = []


class ProjectBody(BaseModel):
    title: str
    description: str = ""
    tech_stack: str = ""
    link: str = ""


class CertificateBody(BaseModel):
    title: str
    issuer: str = ""
    year: int = 2025


class CompanyBody(BaseModel):
    name: str
    website: str = ""
    location: str = ""
    industry: str = ""
    description: str = ""


class JDBody(BaseModel):
    description: str


@app.get("/api/v1/students/me/recommendations")
def recommendations(user=Depends(current_user)):
    require_role(user, "student")
    c = conn()
    applied = {
        r["job_id"]
        for r in c.execute(
            "SELECT job_id FROM applications WHERE student_id=?", (user["id"],)
        ).fetchall()
    }
    ranked = []
    for job in c.execute(
        "SELECT j.* FROM jobs j JOIN companies co ON co.id=j.company_id WHERE j.is_approved=1 AND co.is_approved=1 AND j.status='Open'"
    ).fetchall():
        if job["id"] in applied:
            continue
        fit = build_breakdown(c, user["id"], job["id"])
        ranked.append((fit["score"], job, fit))
    ranked.sort(key=lambda x: x[0], reverse=True)
    items = []
    for score, job, fit in ranked[:10]:
        item = public_job(c, job, user["id"])
        item["why_recommended"] = fit["matched"]
        item["recommendation_score"] = score
        items.append(item)
    c.close()
    return {"items": items, "total": len(items)}


@app.post("/api/v1/jobs")
def create_job(body: JobBody, user=Depends(current_user)):
    require_role(user, "recruiter")
    c = conn()
    company = c.execute(
        "SELECT id FROM companies WHERE user_id=?", (user["id"],)
    ).fetchone()
    if not company:
        raise HTTPException(404, "Company profile not found")
    jid = c.execute("SELECT COALESCE(MAX(id),0)+1 FROM jobs").fetchone()[0]
    c.execute(
        "INSERT INTO jobs(id,company_id,title,description,location,job_type,salary_min,salary_max,min_cgpa,deadline,is_approved,status,created_at,mandatory_skills,optional_skills) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            jid,
            company["id"],
            body.title,
            body.description,
            body.location,
            body.job_type,
            body.salary_min,
            body.salary_max,
            body.min_cgpa,
            body.deadline,
            0,
            "Open",
            now_iso(),
            json.dumps(body.mandatory_skills),
            json.dumps(body.optional_skills),
        ),
    )
    c.commit()
    c.close()
    return {
        "id": jid,
        "is_approved": False,
        "message": "Job submitted for admin approval",
    }


@app.put("/api/v1/jobs/{job_id}")
def edit_job(job_id: int, body: JobBody, user=Depends(current_user)):
    require_role(user, "recruiter")
    c = conn()
    own = c.execute(
        "SELECT j.id FROM jobs j JOIN companies co ON co.id=j.company_id WHERE j.id=? AND co.user_id=?",
        (job_id, user["id"]),
    ).fetchone()
    if not own:
        raise HTTPException(403, "You can only edit your own jobs")
    c.execute(
        "UPDATE jobs SET title=?,description=?,location=?,job_type=?,salary_min=?,salary_max=?,min_cgpa=?,deadline=?,mandatory_skills=?,optional_skills=? WHERE id=?",
        (
            body.title,
            body.description,
            body.location,
            body.job_type,
            body.salary_min,
            body.salary_max,
            body.min_cgpa,
            body.deadline,
            json.dumps(body.mandatory_skills),
            json.dumps(body.optional_skills),
            job_id,
        ),
    )
    c.commit()
    c.close()
    return {"ok": True}


@app.delete("/api/v1/jobs/{job_id}")
def delete_job(job_id: int, user=Depends(current_user)):
    require_role(user, "recruiter")
    c = conn()
    own = c.execute(
        "SELECT j.id FROM jobs j JOIN companies co ON co.id=j.company_id WHERE j.id=? AND co.user_id=?",
        (job_id, user["id"]),
    ).fetchone()
    if not own:
        raise HTTPException(403, "You can only delete your own jobs")
    c.execute("UPDATE jobs SET status='Closed' WHERE id=?", (job_id,))
    c.commit()
    c.close()
    return {"ok": True}


@app.get("/api/v1/students/me/skills")
def my_skills(user=Depends(current_user)):
    require_role(user, "student")
    c = conn()
    rows = c.execute(
        "SELECT s.name,s.category,ss.proficiency,ss.source FROM student_skills ss JOIN skills s ON s.id=ss.skill_id WHERE ss.student_id=?",
        (user["id"],),
    ).fetchall()
    c.close()
    return {"items": [row_dict(r) for r in rows]}


@app.post("/api/v1/students/me/skills")
def add_skill(
    name: str, proficiency: int = Query(3, ge=1, le=5), user=Depends(current_user)
):
    require_role(user, "student")
    c = conn()
    row = c.execute(
        "SELECT id FROM skills WHERE lower(name)=lower(?)", (name,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Skill is not in the taxonomy")
    c.execute(
        "INSERT OR REPLACE INTO student_skills(student_id,skill_id,proficiency,source) VALUES(?,?,?,?)",
        (user["id"], row["id"], proficiency, "manual"),
    )
    c.commit()
    c.close()
    return {"ok": True}


@app.delete("/api/v1/students/me/skills/{skill_name}")
def remove_skill(skill_name: str, user=Depends(current_user)):
    require_role(user, "student")
    execute(
        "DELETE FROM student_skills WHERE student_id=? AND skill_id IN (SELECT id FROM skills WHERE lower(name)=lower(?))",
        (user["id"], skill_name),
    )
    return {"ok": True}


@app.get("/api/v1/students/me/projects")
def my_projects(user=Depends(current_user)):
    require_role(user, "student")
    return {
        "items": [
            row_dict(r)
            for r in execute(
                "SELECT * FROM projects WHERE student_id=? ORDER BY id DESC",
                (user["id"],),
                True,
            )
        ]
    }


@app.post("/api/v1/students/me/projects")
def add_project(body: ProjectBody, user=Depends(current_user)):
    require_role(user, "student")
    c = conn()
    pid = c.execute(
        "INSERT INTO projects(student_id,title,description,tech_stack,link) VALUES(?,?,?,?,?)",
        (user["id"], body.title, body.description, body.tech_stack, body.link),
    ).lastrowid
    c.commit()
    c.close()
    return {"id": pid}


@app.delete("/api/v1/students/me/projects/{project_id}")
def delete_project(project_id: int, user=Depends(current_user)):
    require_role(user, "student")
    execute(
        "DELETE FROM projects WHERE id=? AND student_id=?", (project_id, user["id"])
    )
    return {"ok": True}


@app.get("/api/v1/students/me/certificates")
def my_certificates(user=Depends(current_user)):
    require_role(user, "student")
    return {
        "items": [
            row_dict(r)
            for r in execute(
                "SELECT * FROM certificates WHERE student_id=? ORDER BY year DESC",
                (user["id"],),
                True,
            )
        ]
    }


@app.post("/api/v1/students/me/certificates")
def add_certificate(body: CertificateBody, user=Depends(current_user)):
    require_role(user, "student")
    c = conn()
    cid = c.execute(
        "INSERT INTO certificates(student_id,title,issuer,year) VALUES(?,?,?,?)",
        (user["id"], body.title, body.issuer, body.year),
    ).lastrowid
    c.commit()
    c.close()
    return {"id": cid}


@app.delete("/api/v1/students/me/certificates/{certificate_id}")
def delete_certificate(certificate_id: int, user=Depends(current_user)):
    require_role(user, "student")
    execute(
        "DELETE FROM certificates WHERE id=? AND student_id=?",
        (certificate_id, user["id"]),
    )
    return {"ok": True}


@app.get("/api/v1/companies/me")
def my_company(user=Depends(current_user)):
    require_role(user, "recruiter")
    row = execute("SELECT * FROM companies WHERE user_id=?", (user["id"],), True)
    return row_dict(row[0]) if row else None


@app.put("/api/v1/companies/me")
def update_company(body: CompanyBody, user=Depends(current_user)):
    require_role(user, "recruiter")
    execute(
        "UPDATE companies SET name=?,website=?,location=?,industry=?,description=? WHERE user_id=?",
        (
            body.name,
            body.website,
            body.location,
            body.industry,
            body.description,
            user["id"],
        ),
    )
    return my_company(user)


@app.get("/api/v1/applications/{application_id}")
def application_detail(application_id: int, user=Depends(current_user)):
    require_role(user, "recruiter", "admin")
    c = conn()
    row = c.execute(
        "SELECT a.*,j.title,j.company_id,s.full_name,s.cgpa,s.gender,s.college,r.extracted_text FROM applications a JOIN jobs j ON j.id=a.job_id JOIN students s ON s.user_id=a.student_id LEFT JOIN resumes r ON r.id=a.resume_id WHERE a.id=?",
        (application_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Application not found")
    if (
        user["role"] == "recruiter"
        and not c.execute(
            "SELECT 1 FROM companies WHERE id=? AND user_id=?",
            (row["company_id"], user["id"]),
        ).fetchone()
    ):
        raise HTTPException(403, "Not your company")
    item = row_dict(row)
    item["score_breakdown"] = (
        json.loads(item["score_breakdown"]) if item["score_breakdown"] else None
    )
    item["history"] = [
        row_dict(h)
        for h in c.execute(
            "SELECT * FROM history WHERE application_id=? ORDER BY changed_at",
            (application_id,),
        ).fetchall()
    ]
    c.close()
    return item


@app.get("/api/v1/applications/{application_id}/history")
def application_history(application_id: int, user=Depends(current_user)):
    require_role(user, "student", "recruiter", "admin")
    c = conn()
    row = c.execute(
        "SELECT student_id,job_id FROM applications WHERE id=?", (application_id,)
    ).fetchone()
    if not row or (user["role"] == "student" and row["student_id"] != user["id"]):
        raise HTTPException(404, "Application not found")
    if (
        user["role"] == "recruiter"
        and not c.execute(
            "SELECT 1 FROM jobs j JOIN companies co ON co.id=j.company_id WHERE j.id=? AND co.user_id=?",
            (row["job_id"], user["id"]),
        ).fetchone()
    ):
        raise HTTPException(403, "Not your application")
    items = [
        row_dict(h)
        for h in c.execute(
            "SELECT * FROM history WHERE application_id=? ORDER BY changed_at",
            (application_id,),
        ).fetchall()
    ]
    c.close()
    return {"items": items, "total": len(items)}


@app.get("/api/v1/admin/users")
def admin_users(
    q: str = "",
    role: str = "",
    page: int = 1,
    page_size: int = 20,
    user=Depends(current_user),
):
    require_role(user, "admin")
    clauses = []
    params = []
    if q:
        clauses.append("lower(email) LIKE ?")
        params.append(f"%{q.lower()}%")
    if role:
        clauses.append("role=?")
        params.append(role)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    total = execute(f"SELECT COUNT(*) AS n FROM users{where}", tuple(params), True)[0][
        "n"
    ]
    rows = execute(
        f"SELECT id,email,role,is_active,created_at FROM users{where} ORDER BY id LIMIT ? OFFSET ?",
        tuple(params) + (page_size, (page - 1) * page_size),
        True,
    )
    return {
        "items": [row_dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/api/v1/admin/users/{user_id}/active")
def admin_user_active(user_id: int, active: bool, user=Depends(current_user)):
    require_role(user, "admin")
    execute("UPDATE users SET is_active=? WHERE id=?", (int(active), user_id))
    return {"ok": True, "active": active}


@app.patch("/api/v1/admin/companies/{company_id}/reject")
def reject_company(company_id: int, user=Depends(current_user)):
    require_role(user, "admin")
    execute("UPDATE companies SET is_approved=-1 WHERE id=?", (company_id,))
    return {"ok": True}


@app.patch("/api/v1/admin/jobs/{job_id}/reject")
def reject_job(job_id: int, user=Depends(current_user)):
    require_role(user, "admin")
    execute("UPDATE jobs SET is_approved=-1,status='Closed' WHERE id=?", (job_id,))
    return {"ok": True}


@app.post("/api/v1/ai/extract-jd-skills")
def extract_jd_skills(request: Request, body: JDBody, user=Depends(current_user)):
    _rate_gate(request, "ai", 20)
    require_role(user, "recruiter", "admin")
    return {"skills": extract_skills(body.description)}


@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    if full_path.startswith("api/"):
        return JSONResponse(
            {"error": {"code": "NOT_FOUND", "message": "API route not found"}},
            status_code=404,
        )
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse(
        {
            "message": "SkillTrack frontend is not built yet. Run npm run build in frontend."
        },
        status_code=200,
    )


if __name__ == "__main__":
    import uvicorn

    seed_data()
    uvicorn.run(
        "app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False
    )
