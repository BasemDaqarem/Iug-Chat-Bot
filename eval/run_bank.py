# -*- coding: utf-8 -*-
"""مشغّل بنك الأسئلة الشامل (eval/question_bank.json) — يسأل البوت كزائر
(جمهور الاستفسارات الحقيقي) ويفحص كل إجابة آلياً:

  ✅ PASS   استوفت must_any ولم تقع في must_not ولا الفحوص العامة
  ⚠️ FAIL   وقعت في must_not أو فحص عام (جامعة غزة / جداول |)
  👁️ WATCH  لم تُظهر أياً من must_any (تحتاج عيناً بشرية)

التشغيل:  PYTHONIOENCODING=utf-8 python eval/run_bank.py [--base URL]
يكتب:    eval/bank_results.json + eval/تقرير_بنك_الأسئلة.md
"""

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
BANK = HERE / "question_bank.json"
RESULTS = HERE / "bank_results.json"
REPORT = HERE / "تقرير_بنك_الأسئلة.md"


def ask_guest(base: str, question: str, retries: int = 2) -> tuple[str, float]:
    payload = json.dumps({"question": question}, ensure_ascii=False).encode("utf-8")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                base + "/api/chat/guest", data=payload,
                headers={"Content-Type": "application/json"})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r).get("answer", ""), round(time.time() - t0, 1)
        except Exception as exc:  # عابر (429/شبكة) — مهلة ثم إعادة
            if attempt == retries:
                return f"[خطأ اتصال: {exc}]", 0.0
            time.sleep(20)
    return "", 0.0


_DENIALS = ("ليس جامعة غزة", "وليس جامعة غزة", "لسنا جامعة غزة", "مش جامعة غزة",
            "ليست جامعة غزة")


def grade(answer: str, checks: dict, global_must_not: list) -> tuple[str, str]:
    for bad in global_must_not + checks.get("must_not", []):
        if bad in answer:
            # ذكر «جامعة غزة» في سياق النفي الصريح صحيحٌ لا مخالفة
            if bad == "جامعة غزة" and any(d in answer for d in _DENIALS):
                continue
            return "FAIL", f"ظهر الممنوع: «{bad}»"
    must_any = checks.get("must_any") or []
    if must_any and not any(k in answer for k in must_any):
        return "WATCH", "لم يظهر أي مؤشر متوقع"
    return "PASS", ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--category", default=None, help="تشغيل فئة واحدة فقط")
    args = ap.parse_args()

    bank = json.loads(BANK.read_text(encoding="utf-8"))
    gmn = bank["global_must_not"]
    results, counts = [], {"PASS": 0, "FAIL": 0, "WATCH": 0}
    total = sum(len(c["questions"]) for c in bank["categories"]
                if not args.category or c["name"] == args.category)
    i = 0
    for cat in bank["categories"]:
        if args.category and cat["name"] != args.category:
            continue
        for item in cat["questions"]:
            i += 1
            ans, dt = ask_guest(args.base, item["q"])
            verdict, why = grade(ans, item, gmn)
            counts[verdict] += 1
            results.append({"cat": cat["name"], "q": item["q"], "answer": ans,
                            "t": dt, "verdict": verdict, "why": why})
            mark = {"PASS": "✅", "FAIL": "⚠️", "WATCH": "👁️"}[verdict]
            print(f"{i:3d}/{total} {mark} ({dt}s) [{cat['name']}] {item['q'][:45]}"
                  + (f"  ← {why}" if why else ""))
            sys.stdout.flush()

    RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                       encoding="utf-8")

    lines = [
        "# تقرير بنك الأسئلة الشامل",
        "",
        f"**النتيجة: {counts['PASS']} ✅ / {counts['WATCH']} 👁️ / "
        f"{counts['FAIL']} ⚠️ — من {len(results)} سؤالاً (كزائر)**",
        "",
    ]
    for cat in {r["cat"] for r in results}:
        pass  # الترتيب حسب البنك أدناه
    for cat in bank["categories"]:
        rows = [r for r in results if r["cat"] == cat["name"]]
        if not rows:
            continue
        p = sum(r["verdict"] == "PASS" for r in rows)
        lines.append(f"## {cat['name']} — {p}/{len(rows)} ✅")
        for r in rows:
            mark = {"PASS": "✅", "FAIL": "⚠️", "WATCH": "👁️"}[r["verdict"]]
            a = r["answer"].replace("\n", " ")[:160]
            lines.append(f"- {mark} **{r['q']}**" + (f" — _{r['why']}_" if r["why"] else ""))
            lines.append(f"  - {a}…")
        lines.append("")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nالمجموع: {counts} → {RESULTS.name} + {REPORT.name}")


if __name__ == "__main__":
    main()
