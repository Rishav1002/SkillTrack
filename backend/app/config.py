"""Central configuration for the SkillTrack demo and production deployment."""

from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{os.getenv('SKILLTRACK_DB', BACKEND_DIR / 'skilltrack.db')}",
    )
    jwt_secret: str = os.getenv("JWT_SECRET", "skilltrack-demo-secret-change-me")
    access_minutes: int = int(os.getenv("ACCESS_TOKEN_MINUTES", "30"))
    refresh_days: int = int(os.getenv("REFRESH_TOKEN_DAYS", "7"))
    cors_origins: tuple[str, ...] = tuple(
        x.strip()
        for x in os.getenv(
            "CORS_ORIGINS", "http://localhost:5173,http://localhost:8000"
        ).split(",")
        if x.strip()
    )
    environment: str = os.getenv("ENVIRONMENT", "demo")
    upload_limit: int = 5 * 1024 * 1024
    skill_weight: float = 0.45
    semantic_weight: float = 0.30
    eligibility_weight: float = 0.15
    experience_weight: float = 0.10


settings = Settings()
if (
    settings.environment == "production"
    and settings.jwt_secret == "skilltrack-demo-secret-change-me"
):
    raise RuntimeError("JWT_SECRET must be set to a non-default value in production")
