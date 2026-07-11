"""
Authentication service — verifies student credentials against the
`students_auth` collection using bcrypt password hashes.

Transport-agnostic (no FastAPI here): the API layer turns the results below
into HTTP responses. Passwords are NEVER stored or compared in plaintext —
only bcrypt hashes are persisted, and verification is constant-time via
bcrypt.checkpw.

NOTE (deferred hardening): issuing a signed session token (JWT) and using it
to authorize the chat endpoints is the next auth task. Today login only proves
identity and returns the student's own profile.
"""

from datetime import datetime, timezone
from typing import Optional

import bcrypt

from app import db
from app.log import get_logger

log = get_logger("auth")

COLLECTION = "students_auth"


def _col():
    return db.get_collection(COLLECTION)


def find_account(student_id: str) -> Optional[dict]:
    return _col().find_one({"student_id": str(student_id)})


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def authenticate(student_id: str, password: str) -> Optional[dict]:
    """Return the account document on success, else None (caller renders a
    single generic 401 — never reveal whether the id or the password was the
    wrong one)."""
    account = find_account(student_id)
    if account is None:
        return None
    if not verify_password(password, account.get("password_hash", "")):
        return None
    return account


def create_account(
    student_id: str,
    password: str,
    name: str,
    major: str,
    gpa: float,
    rank: int,
    academic_status: str,
) -> dict:
    """Create a demo account with a self-reported academic profile.

    The source marker keeps these prototype values distinguishable from the
    authoritative university integration that will replace them later.
    Caller must ensure the student id is free (409 otherwise).
    """
    doc = {
        "student_id": str(student_id),
        "password_hash": hash_password(password),
        "profile": {
            "name": name,
            "major": major,
            "gpa": float(gpa),
            "rank": int(rank),
            "academic_status": academic_status,
            "data_source": "self_reported_demo",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    _col().insert_one(doc)
    log.info("تم إنشاء حساب جديد للطالب %s", student_id)
    doc.pop("_id", None)
    return doc
