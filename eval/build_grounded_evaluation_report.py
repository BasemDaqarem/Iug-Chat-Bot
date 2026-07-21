# -*- coding: utf-8 -*-
"""Build the human-readable grounded evaluation report for the 440-question rerun."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "eval" / "retest_440_detailed_2026-07-18"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def dump_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def compact(text: Any, limit: int = 700) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def bullets(values: list[str], fallback: str = "لا يوجد") -> str:
    cleaned = [compact(value, 500) for value in values if compact(value)]
    return "\n".join(f"- {value}" for value in cleaned) if cleaned else fallback


def llm_usage(record: dict[str, Any]) -> dict[str, Any]:
    events = [
        event
        for event in record.get("http_events", [])
        if event.get("url_host") == "openrouter.ai"
    ]
    usage = Counter()
    for event in events:
        event_usage = event.get("usage") or {}
        usage["prompt_tokens"] += int(event_usage.get("prompt_tokens") or 0)
        usage["completion_tokens"] += int(event_usage.get("completion_tokens") or 0)
        usage["total_tokens"] += int(event_usage.get("total_tokens") or 0)
        usage["cost"] += float(event_usage.get("cost") or 0)
    return {
        "calls": len(events),
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost": round(usage["cost"], 9),
        "models": sorted(
            {
                event.get("response_model") or event.get("request_model")
                for event in events
                if event.get("response_model") or event.get("request_model")
            }
        ),
    }


def selected_reference_sources(evaluation_record: dict[str, Any]) -> list[str]:
    labels = evaluation_record["evaluation"].get("correct_evidence_sources") or []
    matches = evaluation_record.get("reference_matches") or []
    sources: list[str] = []
    for label in labels:
        match = re.search(r"(?:مرجع|reference)\s*(\d+)", label, re.I)
        if not match:
            continue
        index = int(match.group(1)) - 1
        if 0 <= index < len(matches):
            sources.append(matches[index]["source"])
    return list(dict.fromkeys(sources))


def reranker_tier(
    record: dict[str, Any],
    evaluation_record: dict[str, Any],
) -> tuple[str, str]:
    evaluation = evaluation_record["evaluation"]
    verdict = evaluation["verdict"]
    flag = evaluation["could_reranker_help"]
    retrieval = evaluation["retrieval_status"]
    if verdict == "correct":
        return "not_candidate", "الإجابة صحيحة؛ لا توجد فائدة نوعية متوقعة."
    if retrieval in {"data_absent", "data_conflicting"}:
        return (
            "data_fix_first",
            "الدليل غائب أو متعارض؛ إصلاح/استكمال الداتا مطلوب قبل إعادة الترتيب.",
        )
    if flag == "yes":
        return (
            "high",
            "الدليل الصحيح موجود في corpus لكنه لم يظهر بصورة كافية في المقاطع النهائية.",
        )
    if flag == "maybe":
        return (
            "review",
            "قد يكون ترتيب المقاطع عاملًا، لكن القرينة ليست حاسمة.",
        )
    return (
        "generation_or_policy",
        "الدليل كان كافيًا أو لم يكن الترتيب سبب الخطأ؛ المشكلة أقرب للتوليد/السياسة.",
    )


def old_to_new_transition(record: dict[str, Any], verdict: str) -> str:
    old = record.get("before_verdict") or "correct"
    if old == "incorrect" and verdict == "correct":
        return "improved"
    if old == "incorrect" and verdict != "correct":
        return "still_problematic"
    if old != "incorrect" and verdict in {"partial", "incorrect"}:
        return "regressed_or_new_issue"
    if old != "incorrect" and verdict == "unverifiable":
        return "needs_review"
    return "stayed_correct"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("--refined-evaluations", type=Path)
    args = parser.parse_args()

    results_dir = args.results_dir.resolve()
    raw = load_jsonl(results_dir / "after_results.jsonl")
    evaluated_path = results_dir / "grounded_evaluation_440.jsonl"
    evaluated = load_jsonl(evaluated_path)
    raw_by_qid = {item["qid"]: item for item in raw}
    eval_by_qid = {item["qid"]: item for item in evaluated if not item.get("error")}
    refined_path = (
        args.refined_evaluations
        or results_dir / "grounded_evaluation_full_corpus_flagged.jsonl"
    )
    refined: list[dict[str, Any]] = []
    if refined_path.exists():
        refined = load_jsonl(refined_path)
        eval_by_qid.update(
            {item["qid"]: item for item in refined if not item.get("error")}
        )
    refined_qids = {item["qid"] for item in refined if not item.get("error")}

    overrides_path = args.overrides or results_dir / "evaluation_overrides.json"
    overrides: dict[str, Any] = {}
    if overrides_path.exists():
        loaded = json.loads(overrides_path.read_text(encoding="utf-8"))
        overrides = loaded.get("overrides", loaded)

    if len(raw_by_qid) != 440 or len(eval_by_qid) != 440:
        raise SystemExit(
            f"Expected 440 raw and evaluated records, got "
            f"{len(raw_by_qid)} raw and {len(eval_by_qid)} evaluated."
        )

    combined: list[dict[str, Any]] = []
    for qid in sorted(raw_by_qid):
        record = raw_by_qid[qid]
        evaluation_record = eval_by_qid[qid]
        evaluation = dict(evaluation_record["evaluation"])
        if qid in overrides:
            evaluation.update(overrides[qid])
            evaluation["manually_overridden"] = True
        tier, tier_reason = reranker_tier(record, {"evaluation": evaluation})
        combined.append(
            {
                "qid": qid,
                "scenario_id": record["scenario_id"],
                "scenario_title": record["scenario_title"],
                "turn": record["turn"],
                "difficulty": record["difficulty"],
                "question": record["question"],
                "answer": record["after_answer"],
                "evaluation": evaluation,
                "reference_sources": selected_reference_sources(evaluation_record),
                "reference_matches": evaluation_record.get("reference_matches") or [],
                "top_chunk_sources": record.get("top_chunk_sources") or [],
                "after_source": record.get("after_source"),
                "after_latency_ms": record.get("after_latency_ms"),
                "usage": llm_usage(record),
                "old_verdict": record.get("before_verdict") or "correct",
                "old_findings": record.get("before_findings") or [],
                "transition": old_to_new_transition(record, evaluation["verdict"]),
                "reranker_tier": tier,
                "reranker_tier_reason": tier_reason,
                "rag_trace": record.get("rag_trace") or [],
                "evaluator_meta": evaluation_record.get("evaluator_meta") or {},
                "evaluation_pass": (
                    "full_live_corpus_refinement"
                    if qid in refined_qids
                    else "initial"
                ),
            }
        )

    verdict_counts = Counter(item["evaluation"]["verdict"] for item in combined)
    difficulty_counts: dict[str, Counter] = defaultdict(Counter)
    for item in combined:
        difficulty_counts[item["difficulty"]][item["evaluation"]["verdict"]] += 1
    transitions = Counter(item["transition"] for item in combined)
    retrieval_counts = Counter(
        item["evaluation"]["retrieval_status"] for item in combined
    )
    grounding_counts = Counter(item["evaluation"]["grounding"] for item in combined)
    error_counts = Counter(
        error
        for item in combined
        for error in item["evaluation"].get("error_categories", [])
    )
    tier_counts = Counter(item["reranker_tier"] for item in combined)
    ranker_comparison_path = results_dir / "ranker_retest_comparison.json"
    ranker_comparison = (
        json.loads(ranker_comparison_path.read_text(encoding="utf-8"))
        if ranker_comparison_path.exists()
        else None
    )

    reranker_candidates = [
        item
        for item in combined
        if item["reranker_tier"] in {"high", "review"}
        and item["evaluation"]["verdict"] != "correct"
    ]
    data_fix = [
        item for item in combined if item["reranker_tier"] == "data_fix_first"
    ]
    manual_review = [
        item
        for item in combined
        if item["evaluation"].get("needs_manual_review")
        or item["evaluation"]["verdict"] == "unverifiable"
        or item["reranker_tier"] == "review"
    ]

    evaluator_usage = Counter()
    for item in combined:
        usage = (item["evaluator_meta"].get("usage") or {})
        evaluator_usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        evaluator_usage["completion_tokens"] += int(
            usage.get("completion_tokens") or 0
        )
        evaluator_usage["total_tokens"] += int(usage.get("total_tokens") or 0)
        evaluator_usage["cost"] += float(usage.get("cost") or 0)
    all_pass_usage = Counter()
    for evaluation_record in evaluated + refined:
        usage = ((evaluation_record.get("evaluator_meta") or {}).get("usage") or {})
        all_pass_usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        all_pass_usage["completion_tokens"] += int(
            usage.get("completion_tokens") or 0
        )
        all_pass_usage["total_tokens"] += int(usage.get("total_tokens") or 0)
        all_pass_usage["cost"] += float(usage.get("cost") or 0)

    summary = {
        "questions": len(combined),
        "knowledge_snapshot": {
            "local_data_json_files": 12,
            "uploaded_zip_json_files": 18,
            "live_loaded_collections": 37,
            "live_public_collections": 36,
            "live_private_or_restricted_collections": 1,
            "live_public_chunks": 511,
            "combined_reference_records": 1207,
            "refined_question_count": len(refined_qids),
        },
        "verdict_counts": dict(verdict_counts),
        "average_score": round(
            mean(item["evaluation"]["score"] for item in combined), 3
        ),
        "difficulty_verdict_counts": {
            difficulty: dict(counts)
            for difficulty, counts in sorted(difficulty_counts.items())
        },
        "transition_counts": dict(transitions),
        "retrieval_status_counts": dict(retrieval_counts),
        "grounding_counts": dict(grounding_counts),
        "error_category_counts": dict(error_counts),
        "reranker_tier_counts": dict(tier_counts),
        "reranker_candidate_qids": [item["qid"] for item in reranker_candidates],
        "data_fix_first_qids": [item["qid"] for item in data_fix],
        "manual_review_qids": [item["qid"] for item in manual_review],
        "evaluator_usage": {
            "prompt_tokens": evaluator_usage["prompt_tokens"],
            "completion_tokens": evaluator_usage["completion_tokens"],
            "total_tokens": evaluator_usage["total_tokens"],
            "cost": round(evaluator_usage["cost"], 9),
        },
        "evaluator_usage_all_passes": {
            "prompt_tokens": all_pass_usage["prompt_tokens"],
            "completion_tokens": all_pass_usage["completion_tokens"],
            "total_tokens": all_pass_usage["total_tokens"],
            "cost": round(all_pass_usage["cost"], 9),
        },
        "cache_analysis": {
            "cache_enabled_during_rerun": False,
            "eligible_first_turns": 141,
            "possible_hits_in_single_pass": 0,
            "exact_duplicate_questions": {"اذكرهم": 3},
            "duplicate_is_contextual_and_not_cacheable": True,
            "quality_conclusion": (
                "لا توجد أسئلة يُتوقع تحسن صحة إجابتها بسبب الكاش؛ "
                "الكاش يعيد إجابة مطابقة محفوظة ولا يعيد الاسترجاع أو التوليد."
            ),
        },
        "ranker_experiment": (
            ranker_comparison["summary"] if ranker_comparison else None
        ),
    }

    dump_json(results_dir / "grounded_evaluation_summary.json", summary)
    dump_json(
        results_dir / "reranker_retest_candidates.json",
        {
            "candidate_count": len(reranker_candidates),
            "candidates": [
                {
                    key: item[key]
                    for key in [
                        "qid",
                        "scenario_id",
                        "turn",
                        "question",
                        "answer",
                        "evaluation",
                        "reference_sources",
                        "reference_matches",
                        "top_chunk_sources",
                        "reranker_tier",
                        "reranker_tier_reason",
                    ]
                }
                for item in reranker_candidates
            ],
        },
    )
    dump_json(
        results_dir / "grounded_evaluation_detailed.json",
        {"summary": summary, "questions": combined},
    )
    if ranker_comparison:
        recommended_qids = (
            ranker_comparison["summary"]["confirmed_improvement_qids"]
            + ranker_comparison["summary"]["possible_improvement_qids"]
        )
        dump_json(
            results_dir / "recommended_ranker_questions.json",
            {
                "confirmed": ranker_comparison["summary"][
                    "confirmed_improvement_qids"
                ],
                "possible": ranker_comparison["summary"][
                    "possible_improvement_qids"
                ],
                "questions": [
                    row
                    for row in ranker_comparison["questions"]
                    if row["qid"] in recommended_qids
                ],
            },
        )

    report: list[str] = [
        "# تقرير تقييم إجابات إعادة الاختبار — 440 سؤالًا",
        "",
        "## منهجية التقييم",
        "",
        (
            "قُيّمت الإجابة الجديدة اعتمادًا على ملفات JSON في `data/`، "
            "والسجلات المرفوعة في `iug_admission_pages_json.zip`، والمقاطع "
            "النهائية التي دخلت سياق البوت لكل سؤال. لم تُستخدم الإجابة القديمة "
            "مرجعًا للحقيقة؛ استُخدمت فقط لحساب اتجاه التحسن أو التراجع."
        ),
        "",
        (
            "يتكوّن snapshot المعرفة من 37 collection محمّلة: 36 عامة للزائر "
            "ومجموعة واحدة خاصة/مقيّدة، بإجمالي 511 مقطعًا عامًا فريدًا. دُمجت "
            "مع 12 ملفًا محليًا و18 ملفًا داخل ZIP؛ أصبح corpus التحققي 1207 "
            "سجلًا مرجعيًا. أُعيد فحص 131 حكمًا ضعيفًا على corpus الكامل."
        ),
        "",
        (
            "بدأ التقييم بحكم منظم يتضمن درجة وثقة وحالة grounding وحالة "
            "الاسترجاع ومصادر الدليل، ثم روجعت الحالات الحساسة وترشيحات الـranker "
            f"يدويًا وسُجلت {len(overrides)} تعديلات صريحة قابلة للتدقيق."
        ),
        "",
        "## النتيجة الإجمالية",
        "",
        f"- صحيح: {verdict_counts['correct']}",
        f"- صحيح جزئيًا: {verdict_counts['partial']}",
        f"- خاطئ: {verdict_counts['incorrect']}",
        f"- غير قابل للتحقق من الداتا: {verdict_counts['unverifiable']}",
        f"- متوسط الدرجة: {summary['average_score']}/100",
        "",
        "## المقارنة مع الاختبار الأول",
        "",
        f"- تحسن من خاطئ إلى صحيح: {transitions['improved']}",
        f"- بقي صحيحًا: {transitions['stayed_correct']}",
        f"- بقيت فيه مشكلة: {transitions['still_problematic']}",
        (
            "- كان بلا ملاحظة قديمة وظهر فيه خطأ/نقص جديد: "
            f"{transitions['regressed_or_new_issue']}"
        ),
        f"- يحتاج مراجعة مقارنة: {transitions['needs_review']}",
        "",
        (
            "ملاحظة: خانة «الاختبار الأول» مبنية على الملاحظات المسجلة في التقرير "
            "القديم؛ عدم وجود ملاحظة قديمة عومل كإجابة صحيحة عند حساب الاتجاه."
        ),
        "",
        "## تشخيص طبقة المشكلة",
        "",
        *[
            f"- `{status}`: {count}"
            for status, count in retrieval_counts.most_common()
        ],
        "",
        "أكثر فئات الأخطاء:",
        "",
        *[
            f"- `{category}`: {count}"
            for category, count in error_counts.most_common()
        ],
        "",
        "## ترشيح وإعادة الاختبار مع Ranker",
        "",
        (
            f"رشح فحص الأدلة {len(reranker_candidates)} سؤالًا بأولوية مرتفعة "
            f"بعد فصل مشكلات التوليد وغياب الداتا. توجد {len(data_fix)} حالات "
            "تحتاج إصلاح الداتا أولًا ولا يكفيها الـranker."
        ),
        "",
    ]

    if ranker_comparison:
        experiment = ranker_comparison["summary"]
        report.extend(
            [
                (
                    f"أُعيد فعليًا اختبار {experiment['tested']} أسئلة مع الكاش "
                    f"والـranker. شُغّل الـranker فعليًا في "
                    f"{experiment['ranker_applied']} منها؛ لم يحدث أي cache hit."
                ),
                "",
                (
                    "التحسن المؤكد: "
                    + (
                        "، ".join(experiment["confirmed_improvement_qids"])
                        if experiment["confirmed_improvement_qids"]
                        else "لا يوجد"
                    )
                    + ". التحسن المحدود/غير الحاسم: "
                    + (
                        "، ".join(experiment["possible_improvement_qids"])
                        if experiment["possible_improvement_qids"]
                        else "لا يوجد"
                    )
                    + "."
                ),
                "",
                (
                    f"متوسط زمن المرشحين ارتفع من "
                    f"{experiment['average_baseline_latency_ms']} ms إلى "
                    f"{experiment['average_ranker_total_latency_ms']} ms "
                    f"(+{experiment['average_latency_delta_ms']} ms)."
                ),
                "",
            ]
        )

    if ranker_comparison:
        recommended_rows = [
            row
            for row in ranker_comparison["questions"]
            if row["outcome"]
            in {"confirmed_improvement", "small_or_inconclusive_improvement"}
        ]
        for row in recommended_rows:
            report.extend(
                [
                    f"### توصية فعلية {row['qid']} — {compact(row['question'], 180)}",
                    "",
                    (
                        f"- النتيجة: `{row['outcome']}`؛ "
                        f"{row['baseline_verdict']} {row['baseline_score']} → "
                        f"{row['ranker_verdict']} {row['ranker_score']}"
                    ),
                    (
                        f"- شُغّل الـranker فعليًا: {row['ranker_applied']}؛ "
                        f"cache hit: {row['cache_hit']}"
                    ),
                    f"- سبب الحكم بعد الإعادة: {compact(row['evaluation']['reason_ar'], 800)}",
                    "",
                ]
            )
        report.extend(
            [
                (
                    "قائمة المرشحين الأولية الكاملة ونتائج من لم يتحسنوا محفوظة "
                    "في ملفات JSON وتقرير المقارنة المرافق."
                ),
                "",
            ]
        )
    elif reranker_candidates:
        for item in reranker_candidates:
            evaluation = item["evaluation"]
            report.extend(
                [
                    f"### مرشح أولي {item['qid']} — {compact(item['question'], 180)}",
                    "",
                    (
                        f"- الحكم: `{evaluation['verdict']}` "
                        f"({evaluation['score']}/100)"
                    ),
                    f"- أولوية الإعادة: `{item['reranker_tier']}`",
                    f"- حالة الاسترجاع: `{evaluation['retrieval_status']}`",
                    f"- السبب: {compact(evaluation['reranker_reason'], 600)}",
                    (
                        "- مصادر corpus المرجعية: "
                        + (
                            "، ".join(item["reference_sources"])
                            if item["reference_sources"]
                            else "راجع قائمة المطابقات في JSON"
                        )
                    ),
                    "",
                ]
            )
    else:
        report.extend(["لا توجد أسئلة مرشحة.", ""])

    report.extend(
        [
            "## أثر الكاش",
            "",
            (
                "الكاش لا يحسّن صحة الإجابة؛ يعيد الإجابة العامة المطابقة المحفوظة "
                "ولا يشغّل استرجاعًا أو توليدًا جديدًا. في مرور الأسئلة الـ440 لم "
                "يوجد أي cache hit ممكن. التكرار النصي الوحيد كان «اذكرهم» ثلاث "
                "مرات، لكنه سؤال سياقي غير مؤهل للكاش. لذلك لا توجد قائمة إعادة "
                "اختبار لتحسين الجودة بواسطة الكاش."
            ),
            "",
            "## تفاصيل كل سؤال",
            "",
        ]
    )

    for item in combined:
        evaluation = item["evaluation"]
        usage = item["usage"]
        report.extend(
            [
                f"### {item['qid']} — {item['scenario_id']} / الدور {item['turn']}",
                "",
                f"**السؤال:** {compact(item['question'], 1000)}",
                "",
                f"**الإجابة الجديدة:** {compact(item['answer'], 1800)}",
                "",
                (
                    f"**الحكم:** `{evaluation['verdict']}` — "
                    f"{evaluation['score']}/100 — الثقة "
                    f"{evaluation['confidence']}"
                ),
                "",
                f"**السبب:** {compact(evaluation['reason_ar'], 1100)}",
                "",
                f"**المشكلات:**\n\n{bullets(evaluation.get('key_issues', []))}",
                "",
                (
                    "**حالة الدليل والاسترجاع:** "
                    f"`{evaluation['grounding']}` / "
                    f"`{evaluation['retrieval_status']}`"
                ),
                "",
                (
                    f"**الـranker:** `{item['reranker_tier']}` — "
                    f"{compact(item['reranker_tier_reason'], 650)}"
                ),
                "",
                (
                    "**مصادر المقاطع النهائية:** "
                    + (
                        "، ".join(map(str, item["top_chunk_sources"]))
                        if item["top_chunk_sources"]
                        else "مسار مباشر بلا مقاطع"
                    )
                ),
                "",
                (
                    "**مصادر المرجع المحددة:** "
                    + (
                        "، ".join(item["reference_sources"])
                        if item["reference_sources"]
                        else "مذكورة بالترقيم داخل سجل التقييم"
                    )
                ),
                "",
                (
                    f"**الـmetadata:** زمن الإجابة {item['after_latency_ms']} ms؛ "
                    f"استدعاءات LLM={usage['calls']}؛ "
                    f"tokens={usage['total_tokens']}؛ cost=${usage['cost']:.9f}؛ "
                    f"المصدر={item['after_source']}."
                ),
                "",
                f"**تمرير التقييم:** `{item['evaluation_pass']}`.",
                "",
                (
                    f"**المقارنة القديمة:** `{item['transition']}`؛ "
                    f"الحكم القديم `{item['old_verdict']}`."
                ),
                "",
            ]
        )

    report_path = (
        results_dir / "تقرير_تقييم_الإجابات_440_المستند_للداتا.md"
    )
    report_path.write_text("\n".join(report).rstrip() + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
