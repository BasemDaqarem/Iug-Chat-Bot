# -*- coding: utf-8 -*-
"""
مشغّل اختبار «الاستخراج السهل» (بنك سول، 100 سؤال) محلياً عبر مسار الإنتاج
الحرفي للزائر: bot.chat_as_principal(question, guest, allowed_collections).

الاستعمال:
    python eval/run_easy_kb_test.py 0 5      # الحالات [0,5)
    python eval/run_easy_kb_test.py 5 100    # البقية

الضمانات:
  - جلسات بالذاكرة (لا كتابة على Mongo الحي)، فهرس من كاش القرص المحلي.
  - LLM بمفتاح .env المحلي (openai/gpt-oss-20b) — ليس مفتاح Render.
  - تهدئة بين الأسئلة لحدود المفتاح المجاني.
  - لكل حالة: هل مرّ السؤال بالـLLM؟ المسار (fast_rag/semantic)، الحكم الأولي
    الحتمي (تغطية أرقام الجواب المطلوب وكياناته)، والمخرجات تُلحق بـJSONL.
"""
import io
import json
import os
import re
import sys
import time

os.environ["SESSION_BACKEND"] = "memory"     # لا جلسات على Mongo الحي
os.environ["INDEX_BACKEND"] = "disk"         # كاش المتجهات محلي
os.environ["CACHE_ENABLED"] = "false"        # كل سؤال إجابة طازجة

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, ".env"))
os.environ["SESSION_BACKEND"] = "memory"
os.environ["INDEX_BACKEND"] = "disk"
os.environ["CACHE_ENABLED"] = "false"

CASES_PATH = r"C:\Users\ASUS\Desktop\باسم نهاية\iug_kb_v2_easy\iug_kb_v2_easy\cases.json"
OUT_PATH = os.path.join(ROOT, "eval", "easy_test_results.jsonl")
PAUSE_SECONDS = 4.0


def norm(t: str) -> str:
    t = re.sub(r"[أإآ]", "ا", str(t or ""))
    t = t.replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s%./]", " ", t)).strip()


_AR_STOP = set("""من في علي الي عن مع هو هي هذا هذه ذلك التي الذي ما لا نعم ثم قد كل بعد قبل بين حيث كما اذا أو او ان أن إن يتم تم غير عند حتي فقط ايضا وذلك لكل حسب وفق خلال بغزه الجامعه الاسلاميه جامعه كليه الطالب للطالب الطلبه يمكن يمكنك يجب عليك""".split())


def key_facts(required: str) -> tuple[list[str], list[str]]:
    """أرقام الجواب المطلوب + أبرز كلماته الدلالية (بعد التطبيع)."""
    numbers = re.findall(r"\d+(?:[.,/]\d+)*%?", required)
    tokens = [w for w in norm(required).split()
              if len(w) >= 4 and w not in _AR_STOP and not w.isdigit()]
    seen, content = set(), []
    for w in tokens:
        if w not in seen:
            seen.add(w)
            content.append(w)
    return list(dict.fromkeys(numbers)), content[:25]


def auto_judge(answer: str, required: str) -> dict:
    """السؤال «استخراج سهل» لحقيقة محددة — الجواب الموجز الصحيح ناجح.

    المعياران الجوهريان:
      ١. لا أرقام مهلوسة: كل رقم في جواب البوت يجب أن يوجد في نص السجل
         المطلوب (وإلا فهو اختلاق/خلط — فشل فوري).
      ٢. تغطية دنيا: رقم واحد على الأقل من أرقام السجل حاضر (إن كان للسجل
         أرقام)، أو تقاطع دلالي معقول للأسئلة النصية.
    """
    numbers, content = key_facts(required)
    na = norm(answer)
    ans_numbers = re.findall(r"\d+(?:[.,/]\d+)*%?", answer)
    req_norm = norm(required)
    hallucinated = [n for n in ans_numbers
                    if norm(n) not in req_norm and len(n.strip("%")) > 1]
    num_hits = [n for n in numbers if norm(n) in na]
    tok_hits = [w for w in content if w in na]
    num_cov = len(num_hits) / len(numbers) if numbers else None
    tok_cov = len(tok_hits) / len(content) if content else 0.0
    if hallucinated:
        verdict = "fail"
    elif numbers:
        verdict = ("pass" if num_hits and tok_cov >= 0.15
                   else "review" if num_hits or tok_cov >= 0.3 else "fail")
    else:
        verdict = "pass" if tok_cov >= 0.35 else ("review" if tok_cov >= 0.2 else "fail")
    return {"verdict": verdict, "num_cov": num_cov, "tok_cov": round(tok_cov, 2),
            "missing_numbers": [n for n in numbers if norm(n) not in na][:6],
            "hallucinated_numbers": hallucinated[:6]}


def main() -> None:
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    end = int(sys.argv[2]) if len(sys.argv) > 2 else start + 5
    with open(CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)
    cases = cases[start:end]
    print(f"تشغيل الحالات [{start}:{end}) = {len(cases)} سؤالاً…")

    from app.chatbot import IUGChatbot
    from app.sessions import SessionStore
    from app.rbac import Principal
    from app import file_catalog

    bot = IUGChatbot(sessions=SessionStore())
    print("تحميل الملفات المرفوعة من Mongo (قراءة فقط) + الفهارس من الكاش…")
    t0 = time.time()
    bot._uploaded.load_all()
    print(f"جاهز خلال {time.time()-t0:.0f}ث | ملفات: "
          f"{len(bot.get_uploaded_files_list())} | index_ready={bot._uploaded.index_ready}")

    available = {item["collection"] for item in bot.get_uploaded_files_list()}

    results = []
    for i, case in enumerate(cases):
        qid = case.get("qid", f"c{start+i}")
        question = case["question"]
        principal = Principal.guest(f"guest:easy-{qid}")
        allowed = file_catalog.allowed_collections(principal, available)
        t = time.time()
        try:
            res = bot.chat_as_principal(question, principal,
                                        allowed_collections=allowed)
            err = None
        except Exception as exc:
            res, err = {}, f"{type(exc).__name__}: {exc}"
        elapsed = round(time.time() - t, 1)
        answer = res.get("answer", "")
        meta = res.get("retrieval_metadata") or {}
        trace = meta.get("diagnostic_trace") or {}
        av = trace.get("answer_validation") or {}
        plan = trace.get("query_plan") or {}
        judged = auto_judge(answer, case["required_answer"]) if answer else {
            "verdict": "error", "num_cov": 0, "tok_cov": 0,
            "missing_numbers": [], "hallucinated_numbers": []}
        row = {
            "qid": qid, "idx": start + i, "question": question,
            "source_file": case["source_file"], "canonical_id": case["canonical_id"],
            "verdict": judged["verdict"], "num_cov": judged["num_cov"],
            "tok_cov": judged["tok_cov"], "missing_numbers": judged["missing_numbers"],
            "latency_s": elapsed, "error": err,
            "source": res.get("source"),
            "llm_calls": av.get("llm_call_count"),
            "route": plan.get("route"),
            "intent": plan.get("intent"),
            "generation_outcome": av.get("generation_outcome"),
            "turn_status": av.get("turn_status"),
            "top_chunks": (res.get("top_chunks") or [])[:4],
            "answer": answer,
            "required": case["required_answer"],
        }
        results.append(row)
        mark = {"pass": "✅", "review": "🟡", "fail": "❌", "error": "💥"}[judged["verdict"]]
        print(f"{mark} [{start+i}] {qid} | llm={row['llm_calls']} route={row['route']} "
              f"num={judged['num_cov']} tok={judged['tok_cov']} {elapsed}s"
              + (f" | {err}" if err else ""))
        time.sleep(PAUSE_SECONDS)

    with open(OUT_PATH, "a", encoding="utf-8") as f:
        for row in results:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    passes = sum(1 for r in results if r["verdict"] == "pass")
    print(f"\nالمحصلة: {passes}/{len(results)} ناجح آلياً | "
          f"مراجعة: {sum(1 for r in results if r['verdict']=='review')} | "
          f"فشل: {sum(1 for r in results if r['verdict']=='fail')} | "
          f"أخطاء: {sum(1 for r in results if r['verdict']=='error')}")
    no_llm = [r["qid"] for r in results if not r["llm_calls"]]
    if no_llm:
        print(f"⚠️ حالات لم تمر بالـLLM: {no_llm}")
    print(f"التفاصيل أُلحقت: {OUT_PATH}")


if __name__ == "__main__":
    main()
