#!/usr/bin/env python3
"""SkillTrack zero-config demo launcher."""
from __future__ import annotations
import os
from pathlib import Path
from backend.app.main import seed_data

ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    import uvicorn
    seed_data()
    port = int(os.getenv("PORT", "8000"))
    print("SkillTrack demo is ready")
    print(f"  Local app: http://localhost:{port}")
    print(f"  Docs:      http://localhost:{port}/docs")
    print("  Demo users: Aarav (student), Priya (recruiter), Admin")
    uvicorn.run("backend.app.main:app", host="0.0.0.0", port=port, reload=False)
