# -*- coding: utf-8 -*-
"""
مجموعة تقييم معيارية (regression baseline) لشات بوت الجامعة.

الفكرة: بعد كل تعديل (بروموت)، نعيد نفس الأسئلة ونقارن. لأن إجابات الـ LLM ليست
متطابقة حرفياً بين التشغيلات (حرارة 0.05 + عدم حتمية النموذج)، لا نقارن النص
حرفياً — بل نتحقق من **الحقائق المفتاحية** التي يجب أن تظهر (مثل: رسوم الطب 100).

التشغيل:
    PYTHONIOENCODING=utf-8 python eval/run_eval.py

كل سؤال يُطرح في جلسة مستقلة (بلا سجل سابق) ليكون مستقلاً عن غيره — وهو نفس مسار
الـ CLI (chat_with_all_files).
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.chatbot import IUGChatbot  # noqa: E402

# ── الحالات المعيارية ──────────────────────────────────────────────────────
# expect      : كل هذه النصوص يجب أن تظهر في الإجابة (حقائق ذهبية)
# expect_not  : أيٌّ من هذه يجب ألا يظهر
# watch       : بند مراقبة يدوية (جودة مشكوك فيها من الجلسة الأصلية) — لا يُرسِّب
CASES = [
    {"id": "طب/ساعة",          "q": "كم سعر ساعة الطب؟",                       "expect": ["100"]},
    {"id": "هندسة/وصف",        "q": "حدثني عن كلية الهندسة",                    "expect": ["30", "80"]},
    {"id": "هندسة-حاسوب/ساعة", "q": "كم سعر ساعة هندسة الحاسوب؟",              "expect": ["30"]},
    {"id": "هندسة/قبول",       "q": "ما معدل القبول في كلية الهندسة؟",          "expect": ["80"]},
    {"id": "تمريض/ساعة",       "q": "كم سعر ساعة التمريض؟",                     "expect": ["80"]},
    {"id": "تأجيل/فصل",        "q": "كيف يمكن أن أتجاوز تأخير فصل؟",            "expect": ["تأجيل"]},
    {"id": "خصوصية/ترتيب",     "q": "كم ترتيبي على الدفعة؟",
     "watch": "ثغرة: سؤال الترتيب عبر chat_with_all_files لا يُحجب بالكود — يعتمد على "
              "الـ LLM (غير مضمون). لا توجد بيانات ترتيب في الملفات، فالمطلوب أن يقول "
              "'لا تتوفر' لا أن يخترع رسوماً. راجِع يدوياً."},
    {"id": "خارج-النطاق/فجر",  "q": "متى يؤذن الفجر؟",
     "watch": "سؤال خارج نطاق الجامعة — يجب ألا يخترع وقتاً محدداً (راجِع يدوياً)."},
    {"id": "تحية",             "q": "كيف الحال؟",
     "watch": "سابقاً ردّ البوت بترديد السؤال نفسه — إجابة رديئة (راجِع يدوياً)."},
    {"id": "بكالوريوس/رسوم",   "q": "ما هي رسوم البكالوريوس؟",
     "watch": "سابقاً عمّم '20 دينار لجميع البرامج' وهذا غير دقيق (الرسوم تختلف بالكلية)."},
]


def evaluate(bot):
    results = []
    for i, case in enumerate(CASES):
        session = f"eval_{i}"           # جلسة مستقلة لكل سؤال
        bot.clear_history(session)
        t = time.time()
        try:
            res = bot.chat_with_all_files(case["q"], session)
            answer = res.get("answer", "")
            err = None
        except Exception as exc:
            answer, err = "", str(exc)
        dt = time.time() - t

        missing = [s for s in case.get("expect", []) if s not in answer]
        present_forbidden = [s for s in case.get("expect_not", []) if s in answer]
        is_watch = "watch" in case and not case.get("expect")

        if err:
            status = "خطأ"
        elif is_watch:
            status = "مراقبة"
        elif missing or present_forbidden:
            status = "فشل"
        else:
            status = "نجح"

        results.append({
            "id": case["id"], "q": case["q"], "answer": answer, "err": err,
            "missing": missing, "forbidden": present_forbidden,
            "status": status, "watch": case.get("watch"), "seconds": dt,
        })
    return results


def print_report(results):
    print("\n" + "═" * 72)
    print("تقرير التقييم المعياري")
    print("═" * 72)
    counts = {"نجح": 0, "فشل": 0, "مراقبة": 0, "خطأ": 0}
    for r in results:
        counts[r["status"]] += 1
        icon = {"نجح": "✅", "فشل": "❌", "مراقبة": "👁️", "خطأ": "💥"}[r["status"]]
        print(f"\n{icon} [{r['status']}] {r['id']} ({r['seconds']:.1f}s)")
        print(f"   🧑 {r['q']}")
        snippet = (r["answer"] or r["err"] or "").replace("\n", " ")[:160]
        print(f"   🤖 {snippet}")
        if r["missing"]:
            print(f"   ⚠️ حقائق ناقصة: {r['missing']}")
        if r["forbidden"]:
            print(f"   ⚠️ ظهر ما يجب ألا يظهر: {r['forbidden']}")
        if r["watch"]:
            print(f"   👁️ {r['watch']}")

    print("\n" + "═" * 72)
    print(f"النتيجة: ✅ {counts['نجح']} نجح | ❌ {counts['فشل']} فشل | "
          f"👁️ {counts['مراقبة']} مراقبة | 💥 {counts['خطأ']} خطأ  "
          f"(من {len(results)})")
    print("═" * 72)
    return counts


def main():
    print("🚀 تهيئة البوت للتقييم …")
    bot = IUGChatbot()
    bot.initialize()
    results = evaluate(bot)
    counts = print_report(results)
    # رمز خروج غير صفري عند وجود فشل/خطأ (مفيد لأي CI مستقبلاً)
    sys.exit(1 if (counts["فشل"] or counts["خطأ"]) else 0)


if __name__ == "__main__":
    main()
