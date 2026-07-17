# -*- coding: utf-8 -*-
"""السبر التفجيري (data-integrity-forge مرحلة ٥) — على الخادم المحلي حصراً.

١) حارس جلسات الزائر: سؤال زائر (مسار حقيقة موثوقة — بلا LLM) يجب ألا
   يترك أي جلسة في chat_sessions.
٢) دورة ملف كاملة: إنشاء «سبر_تفجيري_مؤقت» بمستند فاسد ضمن مستنداته →
   نشر (يثبت صمود التقطيع/الفهرسة للفاسد) → تحقق من كل الآثار (مجموعة،
   متجهات قرص، كتالوج، لقطة نسخة) → حذف → تحقق من صفر بقايا.
يستعيد كل ما أنشأه حتى عند الفشل (finally).
"""
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import os  # noqa: E402
os.chdir(ROOT)  # مسارات الفهرس نسبية (.index_cache) — يجب أن نقف حيث يقف الخادم
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from kb_admin import temp_admin_session  # noqa: E402  (سنمرر BASE محلياً)
import kb_admin  # noqa: E402
kb_admin.BASE = "http://127.0.0.1:8000/api"  # ⚠️ محلي فقط — لا Render أبداً

from app import db  # noqa: E402
from app.db import list_uploaded_collections  # noqa: E402

TMP = "سبر_تفجيري_مؤقت"
DOCS = [
    {"الموضوع": "حقيقة سبر", "الإجابة": "قيمة تجريبية للسبر 12345"},
    {"الموضوع": "مستند فاسد", "قيمة": None, "رقم_كنص": "١٢٣",
     "متشعب": {"عميق": [None, "", {"أعمق": ["🌊" * 50]}]}, "فارغ": ""},
    {"الموضوع": "حقيقة ثالثة", "الإجابة": "سطر عادي"},
]
ok = lambda b: "✅" if b else "⛔"


def artifacts() -> dict:
    return {
        "المجموعة": TMP in set(list_uploaded_collections()),
        "الكتالوج النشط": db.get_collection("file_catalog").find_one(
            {"collection": TMP, "status": {"$ne": "archived"}}) is not None,
        "لقطة نسخة": db.get_collection("managed_file_versions").count_documents(
            {"file_id": {"$in": [c["file_id"] for c in
             db.get_collection("file_catalog").find({"collection": TMP})]}}),
        # أسماء ملفات القرص مجزأة SHA1 — نسأل دالة المسار الرسمية نفسها
        "متجهات قرص": Path(__import__("app.index_store", fromlist=["x"])
                           ._disk_paths(f"uploaded::{TMP}")[0]).exists(),
        "متجهات Mongo": db.get_collection("embedding_index").find_one(
            {"name": f"uploaded::{TMP}"}) is not None,
    }


def main() -> int:
    # ١) حارس الزائر — قبل/بعد
    sess = db.get_collection("chat_sessions")
    before = sess.count_documents({})
    r = requests.post("http://127.0.0.1:8000/api/chat/guest", timeout=120,
                      json={"question": "ما هي عاصمة فلسطين؟"})
    guard_ok = r.ok and sess.count_documents({}) == before
    print(f"{ok(guard_ok)} حارس الزائر: سؤال زائر ({r.status_code}) لم يخزّن جلسة "
          f"({before}→{sess.count_documents({})})")

    failures = 0 if guard_ok else 1
    with temp_admin_session("tmp_admin_probe") as headers:
        file_id = None
        try:
            r = requests.post(f"{kb_admin.BASE}/admin/files", headers=headers,
                              timeout=180, json={
                                  "collection": TMP, "documents": DOCS,
                                  "classification": "university_public",
                                  "allowed_roles": ["guest", "student", "employee", "admin"]})
            assert r.status_code == 201, r.text[:200]
            file_id = r.json()["file_id"]
            for step in ("process", "publish"):
                r = requests.post(f"{kb_admin.BASE}/admin/files/{file_id}/{step}",
                                  headers=headers, timeout=300)
                assert r.ok, f"{step}: {r.text[:200]}"
            art = artifacts()
            all_there = all(bool(v) for k, v in art.items() if k != "متجهات Mongo")
            print(f"{ok(all_there)} بعد النشر (بمستند فاسد ضمنه): {art}")
            failures += 0 if all_there else 1
        finally:
            if file_id:
                r = requests.delete(f"{kb_admin.BASE}/admin/files/{file_id}",
                                    headers=headers, timeout=180)
                print(f"   حذف: {r.status_code}")
    art = artifacts()
    exploded_clean = (not art["المجموعة"] and not art["الكتالوج النشط"]
                      and art["لقطة نسخة"] == 0 and not art["متجهات قرص"]
                      and not art["متجهات Mongo"])
    print(f"{ok(exploded_clean)} بعد التفجير — صفر بقايا: {art}")
    failures += 0 if exploded_clean else 1
    print("\n" + ("✅ السبر التفجيري كله أخضر" if failures == 0
                  else f"⛔ {failures} فشل"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
