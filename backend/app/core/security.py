"""JWT and password helpers shared by the API and tests."""

from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any
from jose import JWTError, jwt
from passlib.context import CryptContext
from ..config import settings

ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, stored: str) -> tuple[bool, bool]:
    """Return (valid, legacy_hash). Legacy hashes are SHA-256 hex strings."""
    if (
        stored
        and len(stored) == 64
        and all(ch in "0123456789abcdef" for ch in stored.lower())
    ):
        import hashlib

        return hashlib.sha256(password.encode()).hexdigest() == stored, True
    try:
        return pwd_context.verify(password, stored), False
    except Exception:
        return False, False


def create_token(subject: int, role: str, token_type: str, expires: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "role": role,
        "type": token_type,
        "iat": now,
        "exp": now + expires,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def create_access_token(subject: int, role: str) -> str:
    return create_token(
        subject, role, "access", timedelta(minutes=settings.access_minutes)
    )


def create_refresh_token(subject: int, role: str) -> str:
    return create_token(subject, role, "refresh", timedelta(days=settings.refresh_days))


def decode_token(token: str, expected_type: str = "access") -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise ValueError("Invalid or expired token") from exc
    if (
        payload.get("type") != expected_type
        or not payload.get("sub")
        or not payload.get("role")
    ):
        raise ValueError("Malformed token")
    return payload
