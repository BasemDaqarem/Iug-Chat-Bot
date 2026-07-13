"""Minimal security audit trail with deliberate secret redaction."""

from datetime import datetime, timezone
from typing import Optional

from app import db


COLLECTION = "audit_log"
_FORBIDDEN_KEYS = {"password", "temporary_password", "password_hash", "token", "access_token"}


def _safe_details(details: Optional[dict]) -> dict:
    return {
        str(key): value
        for key, value in (details or {}).items()
        if str(key).lower() not in _FORBIDDEN_KEYS
    }


def record(actor_id: str, actor_role: str, action: str, target: str, details: Optional[dict] = None) -> None:
    db.get_collection(COLLECTION).insert_one({
        "actor_id": str(actor_id),
        "actor_role": str(actor_role),
        "action": str(action),
        "target": str(target),
        "details": _safe_details(details),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


def recent(limit: int = 100) -> list[dict]:
    cursor = db.get_collection(COLLECTION).find(
        {}, {"_id": 0, "password_hash": 0, "token": 0, "access_token": 0}
    ).sort("created_at", -1).limit(max(1, min(int(limit), 250)))
    return list(cursor)
