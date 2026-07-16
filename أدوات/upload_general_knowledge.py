# -*- coding: utf-8 -*-
"""رفع ملفات المعرفة العامة الثمانية إلى البوت الحي على Render.

ينشئ حساب أدمن مؤقتاً في قاعدة Mongo المشتركة (نمط حساب التجربة المعتمد)،
يرفع كل ملف من data/ عبر لوحة الأدمن الرسمية (draft → process → publish)
فيبني Render فهارسها في عمليته الحية مباشرة، ثم يحذف الحساب المؤقت حتماً.
"""
import json
import secrets
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
from app import auth  # noqa: E402  (يحتاج .env محمّلاً)

BASE = "https://iug-chat-bot.onrender.com/api"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TMP_ID = "tmp_admin_kb_upload"

FILES = [
    "عن الجامعة الاسلامية - هوية وتاريخ وقيادة",
    "التقويم الاكاديمي 2025-2027",
    "الوضع الدراسي بعد الحرب والعودة الحضورية",
    "خطوات الالتحاق والبوابات الالكترونية والدفع",
    "التواصل والعناوين وقنوات الجامعة",
    "الخريجون والوثائق والتصديق",
    "التحويل والتجسير بين الجامعات والتخصصات",
    "العمادات والمراكز وشؤون الطلبة والتعليم المستمر",
]


def make_temp_admin() -> str:
    password = secrets.token_urlsafe(24)
    col = auth._col()
    col.delete_one({"user_id": TMP_ID})
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    col.insert_one({
        "user_id": TMP_ID, "role": "admin", "active": True,
        "token_version": 1, "must_change_password": False,
        "password_hash": auth.hash_password(password),
        "profile": {"name": "رفع معرفة مؤقت", "updated_at": now},
        "created_at": now,
    })
    return password


def drop_temp_admin() -> None:
    auth._col().delete_one({"user_id": TMP_ID})


def main() -> int:
    password = make_temp_admin()
    try:
        r = requests.post(f"{BASE}/auth/login", timeout=120,
                          json={"identifier": TMP_ID, "password": password})
        r.raise_for_status()
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        for name in FILES:
            docs = json.loads((DATA_DIR / f"{name}.json").read_text(encoding="utf-8"))
            r = requests.post(f"{BASE}/admin/files", headers=headers, timeout=180, json={
                "collection": name,
                "documents": docs,
                "classification": "university_public",
                "allowed_roles": ["guest", "student", "employee", "admin"],
            })
            if r.status_code != 201:
                print(f"✗ draft {name}: {r.status_code} {r.text[:200]}")
                return 1
            file_id = r.json()["file_id"]
            for step in ("process", "publish"):
                r = requests.post(f"{BASE}/admin/files/{file_id}/{step}",
                                  headers=headers, timeout=300)
                if not r.ok:
                    print(f"✗ {step} {name}: {r.status_code} {r.text[:200]}")
                    return 1
            print(f"✓ {name} ({len(docs)} عنصراً) — منشور")

        r = requests.get(f"{BASE.replace('/api', '')}/health", timeout=60)
        print("health:", r.json())
        return 0
    finally:
        drop_temp_admin()
        print("حُذف الحساب المؤقت.")


if __name__ == "__main__":
    sys.exit(main())
