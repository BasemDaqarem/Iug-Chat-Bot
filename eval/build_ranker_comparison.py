# -*- coding: utf-8 -*-
"""Compare the isolated cache+reranker replay with the baseline answers."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "retest_440_detailed_2026-07-18"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    baseline_payload = json.loads(
        (RESULTS / "grounded_evaluation_detailed.json").read_text(encoding="utf-8")
    )
    baseline = {item["qid"]: item for item in baseline_payload["questions"]}
    rerun = {
        item["qid"]: item
        for item in load_jsonl(RESULTS / "ranker_retest_results.jsonl")
    }
    rerun_eval = {
        item["qid"]: item
        for item in load_jsonl(RESULTS / "ranker_retest_evaluation.jsonl")
    }
    overrides_path = RESULTS / "ranker_evaluation_overrides.json"
    overrides = json.loads(overrides_path.read_text(encoding="utf-8"))["overrides"]

    rows = []
    for qid in sorted(rerun):
        result = rerun[qid]
        old = baseline[qid]["evaluation"]
        new = dict(rerun_eval[qid]["evaluation"])
        if qid in overrides:
            new.update(overrides[qid])
            new["manually_overridden"] = True
        ranker_event = next(
            (
                event
                for event in result["http_events"]
                if event.get("request_model")
                == "jina-reranker-v2-base-multilingual"
            ),
            None,
        )
        llm_events = [
            event
            for event in result["http_events"]
            if event.get("url_host") == "openrouter.ai"
        ]
        cache_hit = not llm_events
        delta = int(new["score"]) - int(old["score"])
        verdict_order = {
            "incorrect": 0,
            "unverifiable": 0,
            "partial": 1,
            "correct": 2,
        }
        verdict_improved = (
            verdict_order[new["verdict"]] > verdict_order[old["verdict"]]
        )
        if not ranker_event:
            outcome = "ranker_not_applied"
        elif verdict_improved and delta >= 10:
            outcome = "confirmed_improvement"
        elif delta > 0 and new["verdict"] != "incorrect":
            outcome = "small_or_inconclusive_improvement"
        elif delta >= 0:
            outcome = "no_improvement"
        else:
            outcome = "worse"
        rows.append(
            {
                "qid": qid,
                "question": result["question"],
                "baseline_answer": result["baseline_answer"],
                "ranker_answer": result["after_answer"],
                "baseline_verdict": old["verdict"],
                "baseline_score": old["score"],
                "ranker_verdict": new["verdict"],
                "ranker_score": new["score"],
                "score_delta": delta,
                "outcome": outcome,
                "ranker_applied": bool(ranker_event),
                "ranker_latency_ms": (
                    ranker_event.get("latency_ms") if ranker_event else None
                ),
                "cache_hit": cache_hit,
                "baseline_latency_ms": result["baseline_latency_ms"],
                "ranker_total_latency_ms": result["after_latency_ms"],
                "latency_delta_ms": (
                    result["after_latency_ms"] - result["baseline_latency_ms"]
                ),
                "baseline_top_chunk_sources": result[
                    "baseline_top_chunk_sources"
                ],
                "ranker_top_chunk_sources": result["top_chunk_sources"],
                "evaluation": new,
            }
        )

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    summary = {
        "tested": len(rows),
        "cache_hits": sum(row["cache_hit"] for row in rows),
        "ranker_applied": sum(row["ranker_applied"] for row in rows),
        "outcome_counts": counts,
        "confirmed_improvement_qids": [
            row["qid"]
            for row in rows
            if row["outcome"] == "confirmed_improvement"
        ],
        "possible_improvement_qids": [
            row["qid"]
            for row in rows
            if row["outcome"] == "small_or_inconclusive_improvement"
        ],
        "average_baseline_latency_ms": round(
            mean(row["baseline_latency_ms"] for row in rows), 1
        ),
        "average_ranker_total_latency_ms": round(
            mean(row["ranker_total_latency_ms"] for row in rows), 1
        ),
        "average_latency_delta_ms": round(
            mean(row["latency_delta_ms"] for row in rows), 1
        ),
    }
    payload = {"summary": summary, "questions": rows}
    (RESULTS / "ranker_retest_comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report = [
        "# مقارنة إعادة اختبار المرشحين مع Cache + Ranker",
        "",
        f"- الأسئلة المختبرة: {summary['tested']}",
        f"- شُغّل الـranker فعليًا: {summary['ranker_applied']}",
        f"- Cache hits: {summary['cache_hits']}",
        (
            "- تحسن مؤكد: "
            + (
                "، ".join(summary["confirmed_improvement_qids"])
                if summary["confirmed_improvement_qids"]
                else "لا يوجد"
            )
        ),
        (
            "- تحسن محدود/غير حاسم: "
            + (
                "، ".join(summary["possible_improvement_qids"])
                if summary["possible_improvement_qids"]
                else "لا يوجد"
            )
        ),
        (
            f"- متوسط الزمن: {summary['average_baseline_latency_ms']} ms "
            f"قبل، {summary['average_ranker_total_latency_ms']} ms بعد"
        ),
        "",
        "## التفاصيل",
        "",
    ]
    for row in rows:
        report.extend(
            [
                f"### {row['qid']} — {row['question']}",
                "",
                (
                    f"- النتيجة: `{row['outcome']}`؛ "
                    f"{row['baseline_verdict']} {row['baseline_score']} → "
                    f"{row['ranker_verdict']} {row['ranker_score']}"
                ),
                (
                    f"- الـranker شُغّل: {row['ranker_applied']}؛ "
                    f"cache hit: {row['cache_hit']}"
                ),
                (
                    f"- الزمن: {row['baseline_latency_ms']} → "
                    f"{row['ranker_total_latency_ms']} ms"
                ),
                f"- السبب: {row['evaluation']['reason_ar']}",
                "",
                f"**إجابة قبل:** {row['baseline_answer']}",
                "",
                f"**إجابة بعد:** {row['ranker_answer']}",
                "",
            ]
        )
    (RESULTS / "تقرير_مقارنة_المرشحين_مع_ranker_والكاش.md").write_text(
        "\n".join(report).rstrip() + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
