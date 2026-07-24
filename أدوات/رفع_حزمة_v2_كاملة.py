# -*- coding: utf-8 -*-
"""
رفع حزمة iug_kb_v2 النهائية (13 ملفاً) للبوت الحي — بعد نشرة طبقة v2.

المراحل:
  ١. انتظار جاهزية السيرفر بعد نشرة Render.
  ٢. رفع ملف التقويم كمسبار: 11 مقطعاً = الكود الجديد (v2)؛ 28 = القديم
     (هرمي) → ننتظر اكتمال النشرة ونعيد الفحص (النسخة الجديدة عند إقلاعها
     تعيد بناء المقاطع من Mongo بمسار v2 تلقائياً فيتصحح العدد وحده).
  ٣. رفع بقية الملفات تصاعدياً بالحجم مع تهدئة 70 ثانية بين الملفات
     (ميزانية Jina 100 ألف توكن/دقيقة مشتركة بين النداءات).
  ٤. تحقق نهائي: عدد الملفات والمقاطع في /health + عيّنة كتالوج القبول.

انقطاع HTTP أثناء نشر ملف كبير لا يعني فشلاً: السيرفر يكمل التنفيذ،
فنتحقق من الكتالوج بدل الاعتماد على رد الطلب.
"""
import io
import json
import os
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent))

import requests  # noqa: E402
from kb_admin import BASE, temp_admin_session  # noqa: E402

KB = Path(r"C:\Users\ASUS\Desktop\باسم نهاية\IUG_canonical_official_merged_iug_kb_v2\canonical")
HEALTH = "https://iug-chat-bot.onrender.com/health"

# الترتيب: المسبار أولاً ثم تصاعدياً بالحجم (الكبار آخراً بعد ثبوت السلامة)
FILES = [
    "academic_calendar_and_current_status",   # المسبار: 11 نشطاً
    "centers_and_deanships",                  # 5
    "university_profile",                     # 9
    "faculties",                              # 13
    "elearning",                              # 18
    "scholarships",                           # 24 نشطاً
    "student_services_and_documents",         # 39
    "official_program_and_faculty_materials", # 54
    "fees_and_payments",                      # 61 نشطاً
    "admissions_and_registration",            # 63
    "academic_regulations",                   # 72
    "contacts_and_staff",                     # 3 نشطة من 105 (drafts لا تُفهرس)
    "academic_programs",                      # 182 — الأكبر
]
# المقاطع المتوقعة = السجلات النشطة لكل ملف (سجل نشط = مقطع v2 واحد)
EXPECTED_ACTIVE = {}


def load_docs(name: str) -> list:
    with open(KB / f"{name}.json", encoding="utf-8") as f:
        docs = json.load(f)
    EXPECTED_ACTIVE[name] = sum(
        1 for r in docs
        if r.get("validity", {}).get("status") == "active"
        and r.get("governance", {}).get("is_canonical", True)
    )
    return docs


def health() -> dict:
    try:
        return requests.get(HEALTH, timeout=45).json()
    except Exception as exc:
        return {"error": str(exc)}


def wait_for_service(minutes: float = 20) -> bool:
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        h = health()
        if h.get("status") in ("ready", "starting") and "error" not in h:
            print(f"   السيرفر يستجيب: status={h.get('status')}")
            return True
        print(f"   بانتظار السيرفر… ({h.get('error', h.get('status'))})")
        time.sleep(40)
    return False


def publish_with_patience(headers: dict, name: str, docs: list) -> str:
    """نشر ملف مع تسامح مع انقطاع HTTP (السيرفر يكمل خلف الكواليس)."""
    try:
        r = requests.post(f"{BASE}/admin/files", headers=headers, timeout=180, json={
            "collection": name,
            "documents": docs,
            "classification": "university_public",
            "allowed_roles": ["guest", "student", "employee", "admin"],
        })
        if r.status_code != 201:
            return f"draft-fail {r.status_code}: {r.text[:180]}"
        file_id = r.json()["file_id"]
        for step in ("process", "publish"):
            try:
                r = requests.post(
                    f"{BASE}/admin/files/{file_id}/{step}",
                    headers=headers, timeout=600,
                )
                if not r.ok:
                    return f"{step}-fail {r.status_code}: {r.text[:180]}"
            except requests.exceptions.ReadTimeout:
                # الملفات الكبيرة مع كبح 429 تتجاوز مهلة الوكيل — نتحقق لاحقاً
                print(f"   ⏳ مهلة {step} انتهت للعميل — السيرفر يكمل؛ سنتحقق من الكتالوج.")
                time.sleep(90)
        return "ok"
    except Exception as exc:
        return f"error: {type(exc).__name__} {str(exc)[:150]}"


def catalog_status(headers: dict, name: str) -> str:
    try:
        r = requests.get(f"{BASE}/admin/files", headers=headers, timeout=60)
        for item in r.json():
            if item.get("collection") == name and item.get("status") == "published":
                return "published"
        return "not-published"
    except Exception as exc:
        return f"check-error: {exc}"


def main():
    print("═" * 62)
    print("١) انتظار السيرفر بعد نشرة Render…")
    if not wait_for_service():
        print("⛔ السيرفر لم يستجب خلال 20 دقيقة — أوقف وأبلغ.")
        return

    calendar_docs = load_docs(FILES[0])
    with temp_admin_session("tmp_admin_kb_upload") as headers:
        # ── ٢) المسبار ──
        print("\n٢) رفع المسبار (التقويم) وكشف نسخة الكود…")
        for probe_round in range(1, 9):
            result = publish_with_patience(headers, FILES[0], calendar_docs)
            print(f"   نشر التقويم: {result}")
            time.sleep(8)
            h = health()
            chunks = h.get("uploaded_chunks")
            print(f"   المقاطع الظاهرة: {chunks} (11=v2 الجديد، 28=القديم)")
            if chunks == EXPECTED_ACTIVE[FILES[0]]:
                print("   ✅ الكود الجديد (v2) حي — نكمل الرفع الكامل.")
                break
            print(f"   الكود القديم ما زال يخدم — انتظار النشرة (جولة {probe_round}/8)…")
            time.sleep(150)
        else:
            print("⛔ النشرة الجديدة لم تظهر خلال ~20 دقيقة — أوقف الرفع الكامل.")
            return

        # ── ٣) بقية الملفات ──
        print("\n٣) رفع بقية الملفات (تهدئة 70 ثانية بين الملفات)…")
        results = {FILES[0]: "ok"}
        for name in FILES[1:]:
            docs = load_docs(name)
            print(f"\n▶ {name} ({len(docs)} سجلاً / {EXPECTED_ACTIVE[name]} نشطاً)")
            outcome = publish_with_patience(headers, name, docs)
            if outcome != "ok":
                # تحقق الكتالوج: انقطاع العميل لا يعني فشل السيرفر
                time.sleep(30)
                if catalog_status(headers, name) == "published":
                    outcome = "ok (تأكد من الكتالوج بعد انقطاع)"
            if outcome.startswith("ok"):
                print(f"   ✅ {outcome}")
            else:
                print(f"   ❌ {outcome} — محاولة ثانية بعد 90 ثانية…")
                time.sleep(90)
                outcome = publish_with_patience(headers, name, docs)
                print(f"   المحاولة الثانية: {outcome}")
            results[name] = outcome
            time.sleep(70)   # نافذة توكنات جديدة قبل الملف التالي

        # ── ٤) التحقق النهائي ──
        print("\n٤) التحقق النهائي…")
        h = health()
        expected_total = sum(EXPECTED_ACTIVE.values())
        print(f"   /health: status={h.get('status')} ready={h.get('index_ready')} "
              f"ملفات={h.get('uploaded_files')} مقاطع={h.get('uploaded_chunks')}")
        print(f"   المتوقع: 13 ملفاً و{expected_total} مقطعاً (سجل نشط = مقطع)")
        failures = {k: v for k, v in results.items() if not str(v).startswith("ok")}
        if not failures and h.get("uploaded_files") == 13 \
                and h.get("uploaded_chunks") == expected_total \
                and h.get("index_ready"):
            print("\n🎉 اكتمل رفع الحزمة كاملةً وتطابقت الأعداد — البوت جاهز.")
        else:
            print(f"\n⚠️ راجع: إخفاقات={list(failures)} | "
                  f"فاشلة الفهرسة={h.get('failed_sources')}")


if __name__ == "__main__":
    main()
