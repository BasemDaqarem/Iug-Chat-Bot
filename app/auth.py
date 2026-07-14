"""
Unified authentication service for students, employees, and administrators.

Transport-agnostic (no FastAPI here): the API layer turns the results below
into HTTP responses. Passwords are NEVER stored or compared in plaintext —
only bcrypt hashes are persisted, and verification is constant-time via
bcrypt.checkpw.

The historical collection name is kept so existing student accounts continue
to work, but every new document carries a role, active flag, and token_version.
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


def ensure_indexes() -> None:
    """Create unique indexes on the account identifiers so two concurrent
    registrations for the same subject can't both succeed (the check-then-insert
    race — security report finding 9). The second insert then fails with a
    DuplicateKeyError, which the router turns into a clean 409.

    `student_id` is sparse because employee/admin accounts don't carry one.
    Idempotent: safe to call on every startup."""
    try:
        _col().create_index("user_id", unique=True, name="uq_user_id")
        _col().create_index("student_id", unique=True, sparse=True, name="uq_student_id")
    except Exception as exc:  # index build must never block boot
        log.warning("تعذّر إنشاء فهارس الحسابات الفريدة: %s", exc)


def find_account(student_id: str) -> Optional[dict]:
    identifier = str(student_id)
    account = _col().find_one({"student_id": identifier})
    if account is None:
        account = _col().find_one({"user_id": identifier})
    return account


def account_subject(account: dict) -> str:
    return str(account.get("user_id") or account.get("student_id") or "")


def public_account(account: dict) -> dict:
    """Return only fields that are safe to send to the authenticated owner."""
    return {
        "user_id": account_subject(account),
        "student_id": str(account.get("student_id") or account_subject(account)),
        "role": str(account.get("role") or "student"),
        "active": bool(account.get("active", True)),
        "must_change_password": bool(account.get("must_change_password", False)),
        "profile": dict(account.get("profile") or {}),
    }


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
    if not account.get("active", True):
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
        "user_id": str(student_id),
        "role": "student",
        "active": True,
        "token_version": 1,
        "must_change_password": False,
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


def create_employee(
    employee_id: str,
    temporary_password: str,
    name: str,
    department: str,
    job_title: str,
    *,
    salary: Optional[float] = None,
    access_groups: Optional[list[str]] = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "user_id": str(employee_id),
        "role": "employee",
        "active": True,
        "token_version": 1,
        "must_change_password": True,
        "password_hash": hash_password(temporary_password),
        "access_groups": list(access_groups or []),
        "profile": {
            "name": name,
            "department": department,
            "job_title": job_title,
            "salary": float(salary) if salary is not None else None,
            "data_source": "admin_managed",
            "updated_at": now,
        },
        "created_at": now,
    }
    _col().insert_one(doc)
    doc.pop("_id", None)
    return public_account(doc)


def list_employees() -> list[dict]:
    cursor = _col().find(
        {"role": "employee"},
        {"password_hash": 0, "refresh_tokens": 0},
    )
    return [public_account(doc) | {"access_groups": doc.get("access_groups", [])} for doc in cursor]


def update_employee(employee_id: str, changes: dict) -> Optional[dict]:
    allowed: dict = {}
    for key in ("active", "access_groups", "must_change_password"):
        if key in changes:
            allowed[key] = changes[key]
    for key in ("name", "department", "job_title", "salary"):
        if key in changes:
            allowed[f"profile.{key}"] = changes[key]
    if changes.get("temporary_password"):
        allowed["password_hash"] = hash_password(str(changes["temporary_password"]))
        allowed["must_change_password"] = True
    if changes.get("end_sessions") or changes.get("temporary_password") or changes.get("active") is False:
        current = find_account(employee_id)
        if current is None:
            return None
        allowed["token_version"] = int(current.get("token_version", 1)) + 1
    allowed["profile.updated_at"] = datetime.now(timezone.utc).isoformat()
    result = _col().update_one(
        {"user_id": str(employee_id), "role": "employee"},
        {"$set": allowed},
    )
    if not getattr(result, "matched_count", 0):
        return None
    account = find_account(employee_id)
    return (public_account(account) | {"access_groups": account.get("access_groups", [])}) if account else None


def list_students(query: str = "", limit: int = 50) -> list[dict]:
    """Academic directory projection; credentials and token fields never leave Mongo."""
    criteria: dict = {"$or": [{"role": "student"}, {"role": {"$exists": False}}]}
    text = str(query or "").strip()
    if text:
        import re

        safe = re.escape(text)
        criteria = {
            "$and": [
                criteria,
                {"$or": [
                    {"student_id": {"$regex": safe, "$options": "i"}},
                    {"profile.name": {"$regex": safe, "$options": "i"}},
                    {"profile.major": {"$regex": safe, "$options": "i"}},
                ]},
            ]
        }
    cursor = _col().find(
        criteria,
        {"password_hash": 0, "token_version": 0, "refresh_tokens": 0},
    ).limit(max(1, min(int(limit), 100)))
    return [
        {
            "student_id": str(doc.get("student_id") or doc.get("user_id") or ""),
            **{
                key: (doc.get("profile") or {}).get(key)
                for key in ("name", "major", "gpa", "rank", "academic_status", "updated_at")
            },
        }
        for doc in cursor
    ]


def ensure_bootstrap_admin(identifier: str, password: str, name: str = "مدير النظام") -> None:
    """Create the first admin only from protected environment configuration."""
    if not identifier or not password or find_account(identifier) is not None:
        return
    now = datetime.now(timezone.utc).isoformat()
    _col().insert_one({
        "user_id": str(identifier),
        "role": "admin",
        "active": True,
        "token_version": 1,
        "must_change_password": True,
        "password_hash": hash_password(password),
        "profile": {"name": name, "data_source": "secure_bootstrap", "updated_at": now},
        "created_at": now,
    })
    log.warning("تم إنشاء حساب الأدمن الأول من إعدادات البيئة؛ غيّر كلمة مروره عند أول دخول.")
