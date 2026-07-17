# -*- coding: utf-8 -*-
"""صهر ثقبي الترابط المكتشفين بالسبر (data-integrity-forge — مرحلة FORGE).

١) 21 ملفاً متبنى (adopt_all) يحمل latest_version=1 بلا لقطة في
   managed_file_versions → الرجوع للنسخة 1 كان سيفشل. نبذر اللقطة من
   المحتوى الحي بنفس شكل create_draft (سياسة الوصول مع المحتوى).
٢) جلسات يتيمة: جلسات تجارب قديمة + جلسات زوار سابقة لإصلاح حراسة
   _is_guest — تُحذف (الزائر لا يُخزَّن بقرار أمني).
Idempotent: إعادة تشغيله لا تكرر شيئاً.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from app import db  # noqa: E402
from app.db import get_uploaded_collection, list_uploaded_collections  # noqa: E402

TEST_SESSION_IDS = {"rltest001", "demo_rl", "md_demo", "stream_demo"}


def backfill_versions() -> int:
    uploaded = set(list_uploaded_collections())
    cat = db.get_collection("file_catalog")
    ver = db.get_collection("managed_file_versions")
    now = datetime.now(timezone.utc).isoformat()
    fixed = 0
    for c in cat.find({"status": {"$ne": "archived"}}):
        latest = int(c.get("latest_version") or 0)
        if latest <= 0 or c["collection"] not in uploaded:
            continue
        if ver.find_one({"file_id": c["file_id"], "version": latest}):
            continue
        docs = [{k: v for k, v in d.items() if k != "_id"}
                for d in get_uploaded_collection(c["collection"]).find({})]
        ver.insert_one({
            "file_id": c["file_id"],
            "version": latest,
            "documents": docs,
            "classification": c.get("classification"),
            "allowed_roles": c.get("allowed_roles"),
            "owner_id": c.get("owner_id"),
            "status": c.get("status"),
            "created_at": now,
            "created_by": "integrity_backfill",
        })
        fixed += 1
        print(f"  + لقطة نسخة {latest} لـ «{c['collection']}» ({len(docs)} سجلاً)")
    return fixed


def purge_orphan_sessions() -> int:
    col = db.get_collection("chat_sessions")
    removed = 0
    removed += col.delete_many({"_id": {"$in": list(TEST_SESSION_IDS)}}).deleted_count
    removed += col.delete_many({"_id": {"$regex": "^guest:"}}).deleted_count
    return removed


if __name__ == "__main__":
    n = backfill_versions()
    m = purge_orphan_sessions()
    print(f"\nلقطات مبذورة: {n} | جلسات يتيمة حُذفت: {m}")
