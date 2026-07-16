# -*- coding: utf-8 -*-
"""تصحيح ملف «رسوم البكالوريوس ومعدلات القبول» حسب الصفحة الرسمية (16/7/2026).

المصدر السيادي: admission.iugaza.edu.ps/guide/الرسوم-الدراسية/رسوم-البكالوريوس/
(قُرئت مباشرة بمتصفح فعلي — الموقع يحجب الجلب الآلي) + إعلان مفتاح التنسيق
2025-2026 (ad898). التصحيحات:
  الهندسة كلها → 28 | الآداب كلها → 18 | الإنتاج النباتي → 25
  اقتصاد: العلوم السياسية والإعلام/التسويق والتجارة الإلكترونية/المحاسبة فرعي IT → 25
  التربية: تعليم العلوم → 20 | الطب: المفتاح 91% (2025-2026) بدل «تنافسية»
يُرفع التعديل نسخةً جديدة عبر لوحة الأدمن (draft→process→publish) فيُحدَّث
فهرس Render الحي فوراً، مع إمكانية rollback لأي نسخة سابقة.
"""
import secrets
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
from app import auth  # noqa: E402
from app.db import get_uploaded_collection  # noqa: E402

BASE = "https://iug-chat-bot.onrender.com/api"
COLLECTION = "رسوم البكالوريوس ومعدلات القبول"
TMP_ID = "tmp_admin_fee_fix"

ECON_25 = {"العلوم السياسية والإعلام", "التسويق والتجارة الإلكترونية",
           "المحاسبة فرعي تكنولوجيا المعلومات"}


def corrected_docs() -> tuple[list, list]:
    docs, changes = [], []
    for d in get_uploaded_collection(COLLECTION).find({}, {"_id": 0}):
        fac = d.get("faculty_name", "")
        prog = d.get("program_name", "")
        old_fee = d.get("credit_hour_fee")
        if fac == "الهندسة" and old_fee != 28:
            d["credit_hour_fee"] = 28
        elif fac == "الآداب" and old_fee != 18:
            d["credit_hour_fee"] = 18
        elif prog == "الإنتاج النباتي" and old_fee != 25:
            d["credit_hour_fee"] = 25
        elif fac == "الاقتصاد والعلوم الإدارية" and prog in ECON_25 and old_fee != 25:
            d["credit_hour_fee"] = 25
        elif fac == "التربية" and prog == "تعليم العلوم" and old_fee != 20:
            d["credit_hour_fee"] = 20
        if d.get("credit_hour_fee") != old_fee:
            changes.append(f"رسوم {fac}/{prog}: {old_fee} → {d['credit_hour_fee']}")
        if fac == "الطب":
            crit = d.get("admission_criteria") or {}
            if crit.get("min_high_school_percentage") != 91:
                crit["min_high_school_percentage"] = 91
                crit["ملاحظة"] = ("مفتاح العام الجامعي 2025-2026 — قبول الطب تنافسي "
                                  "ويُعلن مفتاحه رسمياً في بداية كل تسجيل")
                d["admission_criteria"] = crit
                changes.append(f"مفتاح الطب/{prog}: تنافسية → 91% (2025-2026)")
        docs.append(d)
    return docs, changes


def main() -> int:
    docs, changes = corrected_docs()
    print(f"سجلات: {len(docs)} — تغييرات: {len(changes)}")
    for c in changes:
        print("  •", c)
    if not changes:
        print("لا شيء ليُصحَّح.")
        return 0

    password = secrets.token_urlsafe(24)
    col = auth._col()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    col.delete_one({"user_id": TMP_ID})
    col.insert_one({
        "user_id": TMP_ID, "role": "admin", "active": True,
        "token_version": 1, "must_change_password": False,
        "password_hash": auth.hash_password(password),
        "profile": {"name": "تصحيح رسوم مؤقت", "updated_at": now},
        "created_at": now,
    })
    try:
        r = requests.post(f"{BASE}/auth/login", timeout=120,
                          json={"identifier": TMP_ID, "password": password})
        r.raise_for_status()
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        r = requests.post(f"{BASE}/admin/files", headers=headers, timeout=180, json={
            "collection": COLLECTION,
            "documents": docs,
            "classification": "university_public",
            "allowed_roles": ["guest", "student", "employee", "admin"],
        })
        if r.status_code != 201:
            print(f"✗ draft: {r.status_code} {r.text[:300]}")
            return 1
        item = r.json()
        print(f"نسخة جديدة رقم {item.get('latest_version')} (file_id={item['file_id']})")
        for step in ("process", "publish"):
            r = requests.post(f"{BASE}/admin/files/{item['file_id']}/{step}",
                              headers=headers, timeout=300)
            if not r.ok:
                print(f"✗ {step}: {r.status_code} {r.text[:300]}")
                return 1
        print("✓ نُشر التصحيح على البوت الحي.")
        return 0
    finally:
        col.delete_one({"user_id": TMP_ID})
        print("حُذف الحساب المؤقت.")


if __name__ == "__main__":
    sys.exit(main())
