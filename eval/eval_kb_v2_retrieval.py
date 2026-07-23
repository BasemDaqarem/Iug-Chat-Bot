# -*- coding: utf-8 -*-
"""
تقييم استرجاع RAG على حزمة iug_kb_v2 النهائية (قبل الرفع) — محلياً بالكامل.

- يبني UploadedFilesStore بمسار v2 الحقيقي (Jina embeddings + BM25 + RRF + دمج
  canonical_id) على الملفات الـ13 الجاهزة للرفع.
- بنك أسئلة ذهبي مؤلف بصياغات عامية/مغايرة عمداً عن example_queries حتى لا
  تتضخم النتائج، وكل سؤال مربوط بسجله الصحيح عبر كلمات مميزة تُحل إلى
  canonical_ids وقت التشغيل.
- المقاييس: Recall@1/@3/@5، MRR، nDCG@5، إصابة الملف الصحيح، وزمن الاسترجاع
  P50/P95 (كامل مع embed، والترتيب وحده).
- INDEX_BACKEND يُجبر على disk حتى لا يُكتب أي شيء في Mongo الإنتاجي.
"""
import io
import json
import math
import os
import statistics
import sys
import time

os.environ["INDEX_BACKEND"] = "disk"          # لا كتابة في Mongo الإنتاجي
os.environ.setdefault("SEMANTIC_RAG_ENABLED", "false")

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.text_norm import normalize_arabic  # noqa: E402
from app.uploaded_files import UploadedFilesStore  # noqa: E402

KB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))),
    "IUG_canonical_official_merged_iug_kb_v2", "canonical",
)
TOP_K = 5

# ─── بنك الأسئلة الذهبي ─────────────────────────────────────────────────────
# كل بند: (السؤال بصياغة طالب حقيقي، قيود تحديد السجلات الصحيحة)
# القيد: قائمة كلمات يجب أن تظهر كلها في (title+answer_text+data) للسجل.
GOLDEN = [
    # رسوم
    ("قديش الثوابت الفصلية للفصل التاني بكالوريوس؟", [["ثوابت", "ما عدا الأول"]], "رسوم"),
    ("كم بدفع ثوابت بأول فصل للماجستير؟", [["ثوابت", "فصل أول", "دراسات عليا"]], "رسوم"),
    ("سعر ساعة هندسة الحاسوب البكالوريوس كم؟", [["هندسة الحاسوب", "بكالوريوس"]], "رسوم"),
    ("كم رسوم الساعة لماجستير إدارة الأزمات والكوارث؟", [["إدارة الأزمات", "ماجستير"]], "رسوم"),
    ("بدي أعرف رسوم طلب الالتحاق للبكالوريوس", [["طلبات الالتحاق", "البكالوريوس"], ["طلب الالتحاق", "بكالوريوس"]], "رسوم"),
    # قبول ومعدلات
    ("شو معدل قبول الطب البشري عندكم؟", [["الطب البشري"]], "قبول"),
    ("أقل معدل بيقبلوا فيه تمريض؟", [["التمريض (عام)"]], "قبول"),
    ("معدل قبول العلاج الطبيعي قديش؟", [["العلاج الطبيعي"]], "قبول"),
    ("الأدبي بينفع يدخل إدارة أعمال؟", [["إدارة الأعمال", "العربية"]], "قبول"),
    ("كيف بحول من جامعة الأزهر عالجامعة الإسلامية؟", [["التحويل إلى الجامعة الإسلامية"]], "قبول"),
    ("أنا طالب جديد، شو خطوات التسجيل من أولها؟", [["خطوات قبول وتسجيل"]], "قبول"),
    ("شهادتي الثانوية من السعودية، شو أول خطوة؟", [["الخارجية"], ["خارج غزة"]], "قبول"),
    ("وين بلاقي رقم الجلوس تبعي؟", [["رقم الجلوس"]], "قبول"),
    ("كيف بجسر من الدبلوم للبكالوريوس؟", [["التجسير"]], "قبول"),
    # منح
    ("أنا حافظ للقرآن كامل، في إلي منحة؟", [["حفظة القرآن"]], "منح"),
    ("في خصم لأبناء الشهداء؟", [["ذوي الشهداء"]], "منح"),
    ("أنا وأخوي بالجامعة، بنستفيد من منحة؟", [["الأسرة", "الأخوين"]], "منح"),
    ("شو منح ذوي الإعاقة الحركية؟", [["الاحتياجات الخاصة"]], "منح"),
    ("منحة الامتياز للمتفوقين شو شروطها؟", [["منحة الامتياز"]], "منح"),
    ("في إعفاء للطلاب الجدد المقبولين عالصيفي؟", [["إعفاء", "الصيفي"], ["إعفاء", "القبول الصيفي"]], "منح"),
    # تقويم ووضع حالي
    ("إيمتى بيبلش الفصل الأول للسنة الجاي 2026/2027؟", [["الفصل الدراسي الأول 2026/2027"]], "تقويم"),
    ("الدوام هلق حضوري ولا عن بعد؟", [["حضورية"]], "تقويم"),
    ("التسجيل للدراسات العليا مفتوح هلق؟", [["الدراسات العليا 2026/2027"]], "تقويم"),
    ("وين بستقبلوا الطلاب لما أراجع الجامعة؟", [["استقبال"]], "تقويم"),
    ("شو صار بالجامعة وقت الحرب؟ كيف كملتوا؟", [["خلال الحرب"]], "تقويم"),
    # برامج وتخصصات
    ("شو تخصصات البكالوريوس بكلية العلوم؟", [["برامج البكالوريوس في كلية العلوم"]], "برامج"),
    ("في عندكم دكتوراه رياضيات؟", [["الرياضيات", "دكتوراه"]], "برامج"),
    ("شو برامج الماجستير بكلية التربية؟", [["برامج الماجستير في كلية التربية"]], "برامج"),
    ("في تخصص تدقيق لغوي؟ تبع مين؟", [["التدقيق اللغوي"]], "برامج"),
    ("بدي أدرس قبالة، هاد التخصص موجود؟", [["القبالة"]], "برامج"),
    # هوية وتواصل
    ("إيمتى تأسست الجامعة الإسلامية؟", [["تأسست"], ["التأسيس"], ["1978"]], "هوية"),
    ("كيف أحكي مع القبول والتسجيل؟ شو أرقامهم؟", [["القبول والتسجيل", "تواصل"], ["regist@iugaza"]], "هوية"),
    ("شو رؤية الجامعة ورسالتها؟", [["الرؤية"], ["رؤية الجامعة"]], "هوية"),
    # تعليم إلكتروني وخدمات
    ("كيف بفوت على مودل الجامعة؟", [["مودل"], ["Moodle"]], "خدمات"),
    ("بدي كشف علامات رسمي مصدق، كيف؟", [["كشف درجات"], ["كشف علامات"], ["كشف العلامات"]], "خدمات"),
    ("كيف بستلم شهادة التخرج بعد ما خلصت؟", [["شهادة التخرج"], ["الخريج"]], "خدمات"),
    # أنظمة أكاديمية
    ("شو أكثر عدد ساعات بقدر أسجله بالفصل؟", [["الحد الأقصى للساعات"], ["الحد الأعلى للساعات"]], "أنظمة"),
    ("قديش نسبة الغياب اللي بتنفصل بعدها من المساق؟", [["الغياب"]], "أنظمة"),
    ("شو يعني تحذير أكاديمي وإيمتى بصير؟", [["التحذير الأكاديمي"]], "أنظمة"),
    ("بدي أأجل الفصل الجاي، شو الإجراء؟", [["تأجيل"]], "أنظمة"),
    ("سحبت مساق بالغلط وخلصت فترة التسجيل، شو أعمل؟", [["سحبت مساق"], ["بالخطأ"]], "أنظمة"),
]


def load_store() -> tuple[UploadedFilesStore, dict]:
    store = UploadedFilesStore()
    all_records = {}
    files = [f for f in sorted(os.listdir(KB))
             if f.endswith(".json") and not f.startswith("all_")]
    for fn in files:
        name = fn[:-5]
        with open(os.path.join(KB, fn), encoding="utf-8") as f:
            docs = json.load(f)
        generation = store._build_generation(name, docs)
        store._publish_generation(name, docs, generation, rebuild_admissions=False)
        for rec in docs:
            all_records[rec["canonical_id"]] = rec
        print(f"  ✓ {name}: {len(generation['chunks'])} مقطعاً مفهرساً")
    store._rebuild_admissions()
    return store, all_records


def resolve_expected(all_records: dict, constraint_sets) -> set[str]:
    """يحل قيود الكلمات إلى canonical_ids للسجلات النشطة المطابقة."""
    expected = set()
    for constraints in constraint_sets:
        needles = [normalize_arabic(c) for c in constraints]
        for cid, rec in all_records.items():
            if rec.get("validity", {}).get("status") != "active":
                continue
            blob = normalize_arabic(json.dumps(
                {"t": rec.get("title"), "a": rec.get("answer_text"),
                 "d": rec.get("data"), "n": rec.get("notes")},
                ensure_ascii=False,
            ))
            if all(n in blob for n in needles):
                expected.add(cid)
    return expected


def ndcg_at_k(hit_ranks: list[int], k: int) -> float:
    """صيغة ثنائية الصلة: سجل واحد صحيح على الأقل."""
    dcg = sum(1.0 / math.log2(rank + 1) for rank in hit_ranks if rank <= k)
    ideal = 1.0  # أفضل حالة: أول نتيجة صحيحة
    return min(dcg, 1.0) / ideal


def main():
    print("═" * 60)
    print("بناء الفهرس الحقيقي (Jina + BM25) على حزمة الرفع النهائية…")
    t0 = time.time()
    store, all_records = load_store()
    print(f"اكتمل البناء في {time.time()-t0:.1f}ث\n")

    from app.embeddings import embed_query  # بعد ضبط البيئة

    rows = []
    unresolved = []
    for question, constraint_sets, category in GOLDEN:
        expected = resolve_expected(all_records, constraint_sets)
        if not expected:
            unresolved.append(question)
            continue
        t_start = time.time()
        results = store.search_all(question, top_k=TOP_K)
        t_total = (time.time() - t_start) * 1000
        t_rank_start = time.time()
        embed_query(question)          # محسوبة الآن في الكاش
        store.search_all(question, top_k=TOP_K)
        t_rank = (time.time() - t_rank_start) * 1000

        retrieved_ids = [store.canonical_id_of(c) for c in results]
        first_hit = next(
            (i + 1 for i, cid in enumerate(retrieved_ids) if cid in expected),
            None,
        )
        rows.append({
            "q": question, "cat": category,
            "expected": expected, "retrieved": retrieved_ids,
            "first_hit": first_hit,
            "t_total_ms": t_total, "t_rank_ms": t_rank,
        })

    n = len(rows)
    r_at = lambda k: sum(1 for r in rows if r["first_hit"] and r["first_hit"] <= k) / n
    mrr = sum(1.0 / r["first_hit"] for r in rows if r["first_hit"]) / n
    ndcg = statistics.mean(
        ndcg_at_k([r["first_hit"]] if r["first_hit"] else [], TOP_K) for r in rows
    )
    lat_total = sorted(r["t_total_ms"] for r in rows)
    lat_rank = sorted(r["t_rank_ms"] for r in rows)
    pct = lambda arr, p: arr[min(len(arr) - 1, int(round(p / 100 * len(arr))))]

    print("═" * 60)
    print(f"أسئلة مقيسة: {n} (غير محلولة الحقيقة الذهبية: {len(unresolved)})")
    print(f"Recall@1: {r_at(1)*100:.1f}%   Recall@3: {r_at(3)*100:.1f}%   "
          f"Recall@5: {r_at(5)*100:.1f}%")
    print(f"MRR: {mrr:.3f}   nDCG@5: {ndcg:.3f}")
    print(f"زمن الاسترجاع الكامل (مع embed): P50={pct(lat_total,50):.0f}ms "
          f"P95={pct(lat_total,95):.0f}ms")
    print(f"زمن الترتيب وحده (كاش المتجه): P50={pct(lat_rank,50):.0f}ms "
          f"P95={pct(lat_rank,95):.0f}ms")

    print("\n── حسب الفئة ──")
    cats = {}
    for r in rows:
        cats.setdefault(r["cat"], []).append(r)
    for cat, items in cats.items():
        hits = sum(1 for r in items if r["first_hit"] and r["first_hit"] <= TOP_K)
        print(f"  {cat}: {hits}/{len(items)} داخل top-{TOP_K}")

    misses = [r for r in rows if not r["first_hit"] or r["first_hit"] > TOP_K]
    print(f"\n── الإخفاقات ({len(misses)}) ──")
    for r in misses:
        got = []
        for cid in r["retrieved"][:3]:
            rec = all_records.get(cid or "")
            got.append(rec["title"][:45] if rec else str(cid))
        print(f"  ✗ {r['q']}")
        print(f"     المتوقع: {[all_records[c]['title'][:45] for c in list(r['expected'])[:2]]}")
        print(f"     المسترجَع: {got}")
    for q in unresolved:
        print(f"  ⚠ بلا حقيقة ذهبية: {q}")

    out = {
        "n": n, "recall@1": r_at(1), "recall@3": r_at(3), "recall@5": r_at(5),
        "mrr": mrr, "ndcg@5": ndcg,
        "latency_full_p50_ms": pct(lat_total, 50),
        "latency_full_p95_ms": pct(lat_total, 95),
        "latency_rank_p50_ms": pct(lat_rank, 50),
        "misses": [
            {"q": r["q"], "cat": r["cat"],
             "expected_titles": [all_records[c]["title"] for c in r["expected"]],
             "retrieved_titles": [
                 all_records[c]["title"] if c in all_records else str(c)
                 for c in r["retrieved"]
             ]}
            for r in misses
        ],
    }
    path = os.path.join(os.path.dirname(__file__), "kb_v2_retrieval_results.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\nالنتائج التفصيلية: {path}")

    # ── تجربة الريرانكر على الإخفاقات فقط ─────────────────────────────────
    # هل مرشحون أوسع (top-20) + Jina reranker يصطادون السجل الصحيح؟
    if misses and os.getenv("TRY_RERANK") == "1":
        from app import config as app_config, rerank as rerank_mod
        print("\n── تجربة الريرانكر (top-20 → rerank → top-5) على الإخفاقات ──")
        app_config.RERANK_ENABLED = True
        rescued = 0
        for r in misses:
            wide = store.search_all(r["q"], top_k=20, threshold=-10.0)
            ranked = rerank_mod.rerank(r["q"], wide, TOP_K)
            ids = [store.canonical_id_of(c) for c in ranked]
            hit = next((i + 1 for i, cid in enumerate(ids) if cid in r["expected"]), None)
            mark = f"✓ أنقذها بالمرتبة {hit}" if hit else "✗ ما زالت خارج top-5"
            if hit:
                rescued += 1
            print(f"  {mark} | {r['q']}")
        print(f"أنقذ الريرانكر {rescued}/{len(misses)} من الإخفاقات.")


if __name__ == "__main__":
    main()
