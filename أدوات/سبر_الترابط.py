# -*- coding: utf-8 -*-
"""سبّار ترابط بيانات شات بوت الجامعة (سكِل data-integrity-forge).

خريطة العلاقات (من الكود لا التخمين):
  file_catalog.collection            → مجموعة فعلية في قاعدة uploaded_files
  managed_file_versions.file_id      → file_catalog.file_id
  embedding_index.name «uploaded::X» → مجموعة X في uploaded_files
  ملفات .index_cache على القرص       → مجموعات موجودة
  chat_sessions._id                  → حساب في students_auth (أو زائر لا يُخزَّن أصلاً)
مشتقات: file_catalog.latest_version == أعلى نسخة في managed_file_versions.

القراءة فقط — لا يعدّل شيئاً. السبر التفجيري في سكربت الدورة المنفصل.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from app import db  # noqa: E402
from app.db import list_uploaded_collections  # noqa: E402


def main() -> int:
    uploaded = set(list_uploaded_collections())
    catalog = list(db.get_collection("file_catalog").find({}, {"_id": 0}))
    versions = list(db.get_collection("managed_file_versions").find(
        {}, {"file_id": 1, "version": 1, "_id": 0}))
    embeds = [d.get("name", "") for d in
              db.get_collection("embedding_index").find({}, {"name": 1, "_id": 0})]
    sessions = [d["_id"] for d in db.get_collection("chat_sessions").find({}, {"_id": 1})]
    accounts = set()
    for d in db.get_collection("students_auth").find(
            {}, {"student_id": 1, "user_id": 1, "_id": 0}):
        for k in ("student_id", "user_id"):
            if d.get(k):
                accounts.add(str(d[k]))

    holes = []

    # ١) كتالوج → مجموعات
    active = [c for c in catalog if c.get("status") != "archived"]
    for c in active:
        if c.get("status") == "published" and c["collection"] not in uploaded:
            holes.append(f"كتالوج يشير لمجموعة مفقودة: {c['collection']}")
    catalogued = {c["collection"] for c in active}
    uncatalogued = uploaded - catalogued
    if uncatalogued:
        holes.append(f"مجموعات بلا سجل كتالوج ({len(uncatalogued)}): "
                     + "، ".join(sorted(uncatalogued)))

    # ٢) نسخ → كتالوج + تطابق latest_version
    cat_ids = {c["file_id"] for c in catalog}
    orphan_versions = [v for v in versions if v["file_id"] not in cat_ids]
    if orphan_versions:
        holes.append(f"نسخ يتيمة بلا سجل كتالوج: {len(orphan_versions)}")
    from collections import defaultdict
    maxv = defaultdict(int)
    for v in versions:
        maxv[v["file_id"]] = max(maxv[v["file_id"]], int(v["version"]))
    for c in active:
        if c.get("latest_version") and maxv.get(c["file_id"], 0) != int(c["latest_version"]):
            holes.append(f"انحراف نسخ: {c['collection']} latest={c['latest_version']} "
                         f"مقابل أعلى نسخة مخزنة={maxv.get(c['file_id'], 0)}")

    # ٣) متجهات Mongo → مجموعات
    for name in embeds:
        if name.startswith("uploaded::") and name.split("::", 1)[1] not in uploaded:
            holes.append(f"متجهات يتيمة في Mongo: {name}")

    # ٤) متجهات القرص → مجموعات (الأسماء مجزأة SHA1 — نبني المجموعة المتوقعة
    # بدالة المسار الرسمية ونعتبر أي ملف خارجها يتيماً)
    from app import index_store
    expected = {Path(index_store._disk_paths(f"uploaded::{c}")[0]).name
                for c in uploaded}
    expected.add(Path(index_store._disk_paths("knowledge_base")[0]).name)
    cache_dir = ROOT / ".index_cache"
    disk_orphans = ([f.name for f in cache_dir.glob("*.npy") if f.name not in expected]
                    if cache_dir.exists() else [])
    if disk_orphans:
        holes.append(f"متجهات يتيمة على القرص ({len(disk_orphans)}): {disk_orphans[:5]}")

    # ٥) جلسات → حسابات
    orphan_sessions = [s for s in sessions if str(s) not in accounts]
    if orphan_sessions:
        holes.append(f"جلسات يتيمة بلا حساب ({len(orphan_sessions)}): "
                     + "، ".join(str(s)[:25] for s in orphan_sessions[:8]))

    print(f"مجموعات محتوى: {len(uploaded)} | كتالوج نشط: {len(active)} | "
          f"نسخ: {len(versions)} | متجهات: {len(embeds)} | جلسات: {len(sessions)} | "
          f"حسابات: {len(accounts)}")
    if holes:
        print(f"\n⛔ ثقوب ({len(holes)}):")
        for h in holes:
            print(" -", h)
        return 1
    print("\n✅ صفر أيتام عبر العلاقات الخمس + المشتقات متطابقة")
    return 0


if __name__ == "__main__":
    sys.exit(main())
