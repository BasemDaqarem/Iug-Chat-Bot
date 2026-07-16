# -*- coding: utf-8 -*-
"""تنظيف بيانات المعرفة حسب قرارات باسم (16/7/2026) + الفورمات الذهبي.

1. الطب: مفتاح تنافسي يتغير سنوياً (2025-2026 كان 91%، وعادة أعلى ~96%) —
   بدل الرقم الجازم 91.
2. رفع ملف «تخصصات البكالوريوس لكل كلية» الجديد (يسد ثغرة «4 تخصصات فقط»).
3. إعادة نشر «خطوات الالتحاق» برسوم الالتحاق 20 ديناراً المعفاة 100%.
4. حذف سجلات السياسات (runtime_policy/assistant_policy/conversation_examples)
   من ملفَي «دليل الكليات» و«دليل تسجيل الطلبة في الخارج» — الفورمات الذهبي
   يمنعها: المحرك لا ينفذها وتلوث الاسترجاع.
5. فحص تعارضات: مقارنة كل نسب القبول والرسوم عبر الملفات بالملف القانوني.
"""
import json
import sys
from pathlib import Path

from kb_admin import ROOT, publish_collection, temp_admin_session

sys.path.insert(0, str(ROOT))
from app.db import get_uploaded_collection, list_uploaded_collections  # noqa: E402

DATA = ROOT / "data"
POLICY_TYPES = {"runtime_policy", "assistant_policy", "conversation_examples"}
CANON = "رسوم البكالوريوس ومعدلات القبول"

TIBB_KEY = ("تنافسي يتغير كل عام — في 2025-2026 كان 91%، وفي أعوام سابقة كان "
            "أعلى (نحو 96%)؛ يُعلن رسمياً في بداية كل تسجيل ويختلف بين العادي والموازي")


def fixed_canon_docs() -> tuple[list, list]:
    docs, changes = [], []
    for d in get_uploaded_collection(CANON).find({}, {"_id": 0}):
        if d.get("faculty_name") == "الطب":
            crit = d.get("admission_criteria") or {}
            if crit.get("min_high_school_percentage") != TIBB_KEY:
                crit["min_high_school_percentage"] = TIBB_KEY
                crit.pop("ملاحظة", None)
                d["admission_criteria"] = crit
                changes.append("مفتاح الطب → تنافسي متغير سنوياً (قرار باسم)")
        docs.append(d)
    return docs, changes


def stripped_policy_docs(collection: str) -> tuple[list, list]:
    docs, removed = [], []
    for d in get_uploaded_collection(collection).find({}, {"_id": 0}):
        if d.get("document_type") in POLICY_TYPES:
            removed.append(d.get("title") or d.get("document_type"))
        else:
            docs.append(d)
    return docs, removed


def conflict_scan() -> None:
    """تقرير فقط: أين تُذكر نسب قبول أو رسوم خارج الملف القانوني؟"""
    import re
    print("\n── فحص التعارضات (للاطلاع) ──")
    for name in sorted(list_uploaded_collections()):
        if name == CANON:
            continue
        hits = []
        for d in get_uploaded_collection(name).find({}, {"_id": 0}):
            s = json.dumps(d, ensure_ascii=False)
            for m in re.finditer(r"min_high_school_percentage[\"']?\s*:\s*[\"']?(\d+)", s):
                hits.append(f"مفتاح {m.group(1)}%")
            for m in re.finditer(r"credit_hour_fee[\"']?\s*:\s*(\d+)", s):
                hits.append(f"ساعة {m.group(1)}د")
        if hits:
            from collections import Counter
            top = "، ".join(f"{k}×{v}" for k, v in Counter(hits).most_common(8))
            print(f"  {name}: {top}")


def main() -> int:
    canon_docs, canon_changes = fixed_canon_docs()
    guide_docs, guide_removed = stripped_policy_docs("دليل الكليات في الجامعة الاسلامية")
    ext_docs, ext_removed = stripped_policy_docs("دليل تسجيل الطلبة في الخارج ومفاهيم رقم الجلوس")

    print("الطب:", canon_changes or "لا تغيير")
    print("سياسات دليل الكليات المحذوفة:", guide_removed or "لا شيء")
    print("سياسات دليل الخارج المحذوفة:", ext_removed or "لا شيء")

    uploads = []
    if canon_changes:
        uploads.append((CANON, canon_docs))
    if guide_removed:
        uploads.append(("دليل الكليات في الجامعة الاسلامية", guide_docs))
    if ext_removed:
        uploads.append(("دليل تسجيل الطلبة في الخارج ومفاهيم رقم الجلوس", ext_docs))
    for fname in ["تخصصات البكالوريوس لكل كلية",
                  "خطوات الالتحاق والبوابات الالكترونية والدفع"]:
        docs = json.loads((DATA / f"{fname}.json").read_text(encoding="utf-8"))
        uploads.append((fname, docs))

    with temp_admin_session("tmp_admin_cleanup") as headers:
        for collection, docs in uploads:
            item = publish_collection(headers, collection, docs)
            print(f"✓ {collection} ({len(docs)} سجلاً) — نسخة {item.get('latest_version')}")

    conflict_scan()
    return 0


if __name__ == "__main__":
    sys.exit(main())
