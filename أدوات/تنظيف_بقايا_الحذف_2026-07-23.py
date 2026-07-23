# -*- coding: utf-8 -*-
"""
تنظيف بقايا حذف ملفات الأدمن القديمة (2026-07-23) — قبل رفع حزمة iug_kb_v2.

يحذف نهائياً من قاعدة الإنتاج:
  ١. مفاتيح المتجهات اليتيمة `uploaded::*` في `embedding_index`
     (مجموعات مصدرها حُذفت؛ يُبقي `knowledge_base` — مفتاح القاعدة الرئيسية).
  ٢. سجلات `file_catalog` المؤرشفة + نسخها الكاملة في `managed_file_versions`
     (سد باب «استرجاع نسخة» يعيد البيانات القديمة المتعارضة).

آمن بالتصميم: يتحقق أولاً أن قاعدة الملفات المرفوعة فارغة فعلاً (0 مجموعة)،
وإلا يرفض التنفيذ كي لا يحذف فهرس ملف حي.
"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, r"C:\Users\ASUS\Desktop\باسم نهاية\Iug-Chat-Bot-Merged\Iug-Chat-Bot-Merged")

from dotenv import load_dotenv

load_dotenv(r"C:\Users\ASUS\Desktop\باسم نهاية\Iug-Chat-Bot-Merged\Iug-Chat-Bot-Merged\.env")

from app.db import _get_db, list_uploaded_collections  # noqa: E402

main = _get_db()

# ── حارس أمان: لا تنظيف والملفات الحية موجودة ────────────────────────────
live = list_uploaded_collections()
if live:
    print(f"⛔ رفض التنفيذ: ما زالت {len(live)} مجموعة ملفات حية موجودة — "
          "التنظيف مخصص لبقايا ما بعد الحذف الكامل فقط.")
    sys.exit(1)
print("✓ الحارس: قاعدة الملفات المرفوعة فارغة (0 مجموعة) — التنظيف آمن.")

# ── ١) المتجهات اليتيمة ───────────────────────────────────────────────────
idx = main["embedding_index"]
orphans = [d["_id"] for d in idx.find({}, {"_id": 1})
           if str(d["_id"]).startswith("uploaded::")]
print(f"\n١) مفاتيح متجهات يتيمة للحذف: {len(orphans)}")
result = idx.delete_many({"_id": {"$in": orphans}})
print(f"   حُذف: {result.deleted_count}")

# ── ٢) الكتالوج المؤرشف ونسخه ────────────────────────────────────────────
catalog = main["file_catalog"]
versions = main["managed_file_versions"]
archived = list(catalog.find({}, {"file_id": 1, "collection": 1, "status": 1}))
file_ids = [d.get("file_id") for d in archived if d.get("file_id")]
print(f"\n٢) سجلات كتالوج للحذف: {len(archived)} "
      f"(كلها بحالة: {sorted({d.get('status') for d in archived})})")
snap_count = versions.count_documents({}) if "managed_file_versions" in main.list_collection_names() else 0
print(f"   لقطات نسخ مخزنة: {snap_count}")
r1 = catalog.delete_many({})
r2 = versions.delete_many({}) if snap_count else type("R", (), {"deleted_count": 0})()
print(f"   حُذف: {r1.deleted_count} سجل كتالوج + {r2.deleted_count} لقطة نسخة")

# ── التحقق النهائي ────────────────────────────────────────────────────────
print("\n── التحقق النهائي ──")
left_idx = [str(d["_id"]) for d in idx.find({}, {"_id": 1})]
print(f"embedding_index المتبقي: {left_idx}")
print(f"file_catalog المتبقي: {catalog.count_documents({})}")
print(f"managed_file_versions المتبقي: {versions.count_documents({}) if 'managed_file_versions' in main.list_collection_names() else 0}")
print(f"مجموعات الملفات المرفوعة: {len(list_uploaded_collections())}")
ok = (
    not any(k.startswith("uploaded::") for k in left_idx)
    and catalog.count_documents({}) == 0
)
print("\n✅ القاعدة صفرية نظيفة — جاهزة لرفع حزمة iug_kb_v2." if ok
      else "⚠️ بقايا لم تُحذف — راجع أعلاه.")
