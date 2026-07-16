# -*- coding: utf-8 -*-
"""أدوات مشتركة لإدارة ملفات معرفة البوت الحي عبر لوحة الأدمن الرسمية.

النمط المعتمد: حساب أدمن مؤقت في Mongo المشترك (يُحذف حتماً بعد الانتهاء)،
ثم draft→process→publish عبر API الحي على Render — فيتحدّث فهرس العملية
الحية فوراً وتبقى كل نسخة قابلة للاسترجاع (rollback).
"""
import secrets
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
from app import auth  # noqa: E402

BASE = "https://iug-chat-bot.onrender.com/api"


@contextmanager
def temp_admin_session(user_id: str = "tmp_admin_kb"):
    """يوفّر ترويسات Bearer لأدمن مؤقت، ويضمن حذف حسابه في كل الأحوال."""
    password = secrets.token_urlsafe(24)
    col = auth._col()
    now = datetime.now(timezone.utc).isoformat()
    col.delete_one({"user_id": user_id})
    col.insert_one({
        "user_id": user_id, "role": "admin", "active": True,
        "token_version": 1, "must_change_password": False,
        "password_hash": auth.hash_password(password),
        "profile": {"name": "صيانة معرفة مؤقتة", "updated_at": now},
        "created_at": now,
    })
    try:
        r = requests.post(f"{BASE}/auth/login", timeout=120,
                          json={"identifier": user_id, "password": password})
        r.raise_for_status()
        yield {"Authorization": f"Bearer {r.json()['access_token']}"}
    finally:
        col.delete_one({"user_id": user_id})


def publish_collection(headers: dict, collection: str, documents: list) -> dict:
    """رفع (أو تحديث نسخة) ملف معرفة ونشره فوراً. يرمي RuntimeError عند الفشل."""
    r = requests.post(f"{BASE}/admin/files", headers=headers, timeout=180, json={
        "collection": collection,
        "documents": documents,
        "classification": "university_public",
        "allowed_roles": ["guest", "student", "employee", "admin"],
    })
    if r.status_code != 201:
        raise RuntimeError(f"draft {collection}: {r.status_code} {r.text[:300]}")
    item = r.json()
    for step in ("process", "publish"):
        r = requests.post(f"{BASE}/admin/files/{item['file_id']}/{step}",
                          headers=headers, timeout=300)
        if not r.ok:
            raise RuntimeError(f"{step} {collection}: {r.status_code} {r.text[:300]}")
    return item
