# -*- coding: utf-8 -*-
"""Build a neutral, no-judgment report for manual review of all 440 answers."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        # The benchmark is resumable: a transient failed attempt is retained
        # and a later successful retry is appended for the same QID.  Review
        # the final state of every question, not the obsolete attempt.
        latest_by_qid: dict[str, dict[str, Any]] = {}
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            latest_by_qid[record["qid"]] = record
    return [latest_by_qid[qid] for qid in sorted(latest_by_qid)]


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 1)


def latency_stats(values: list[float]) -> dict[str, float | None]:
    return {
        "mean": round(statistics.mean(values), 1) if values else None,
        "median": round(statistics.median(values), 1) if values else None,
        "p95": percentile(values, 0.95),
        "max": round(max(values), 1) if values else None,
        "min": round(min(values), 1) if values else None,
    }


def fmt_s(ms: float | int | None) -> str:
    return "—" if ms is None else f"{float(ms) / 1000:.3f}"


def md_cell(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "／").replace("\n", " ")


def pre(text: str) -> str:
    return f'<pre dir="rtl">{html.escape(text)}</pre>'


def chunk_source(chunk: str) -> str:
    if chunk.startswith("[ملف: ") and "]" in chunk:
        return chunk[6:chunk.find("]")]
    return "<structured-or-unknown>"


def event_usage(event: dict[str, Any]) -> str:
    usage = event.get("usage") or {}
    fields = []
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cost",
    ):
        if usage.get(key) is not None:
            fields.append(f"{key}={usage[key]}")
    return ", ".join(fields) or "—"


def record_cost(record: dict[str, Any]) -> float:
    return sum(
        float((event.get("usage") or {}).get("cost") or 0)
        for event in record.get("http_events", [])
        if event.get("url_host") == "openrouter.ai"
    )


def record_token_totals(record: dict[str, Any]) -> tuple[int, int, int]:
    prompt = completion = jina = 0
    for event in record.get("http_events", []):
        usage = event.get("usage") or {}
        if event.get("url_host") == "openrouter.ai":
            prompt += int(usage.get("prompt_tokens") or 0)
            completion += int(usage.get("completion_tokens") or 0)
        elif event.get("url_host") == "api.jina.ai":
            jina += int(usage.get("total_tokens") or 0)
    return prompt, completion, jina


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    before_latency = [float(record["before_latency_ms"]) for record in records]
    after_latency = [float(record["after_latency_ms"]) for record in records]
    http_statuses = Counter()
    models = Counter()
    sources = Counter(record.get("after_source") or "<empty>" for record in records)
    top_files = Counter()
    old_findings = Counter()
    selected_channels = Counter()
    retrieval_candidate_counts = []
    rerank_statuses = Counter()
    rerank_question_ids: dict[str, list[str]] = {}
    cache_hits = 0
    cache_hit_question_ids: list[str] = []
    answer_check_retries = 0
    answer_check_fallbacks = 0
    source_metadata_extractions = 0
    breakdown_values: dict[str, list[float]] = {
        "openrouter_generation_ms": [],
        "jina_reranker_ms": [],
        "jina_embeddings_ms": [],
        "local_pipeline_ms_estimate": [],
    }
    prompt_tokens = completion_tokens = jina_tokens = 0
    provider_cost = 0.0
    multi_llm_questions = 0
    for record in records:
        metadata = record.get("retrieval_metadata") or {}
        rerank_status = str(metadata.get("rerank_status") or "not_recorded")
        rerank_statuses[rerank_status] += 1
        rerank_question_ids.setdefault(rerank_status, []).append(record["qid"])
        cache_hit = bool(metadata.get("cache_hit"))
        cache_hits += cache_hit
        if cache_hit:
            cache_hit_question_ids.append(record["qid"])
        answer_check_retries += bool(metadata.get("answer_check_retry"))
        answer_check_fallbacks += bool(
            metadata.get("answer_check_safety_fallback")
        )
        source_metadata_extractions += bool(
            metadata.get("source_metadata_extracted")
        )
        breakdown = record.get("latency_breakdown") or {}
        for key in breakdown_values:
            if breakdown.get(key) is not None:
                breakdown_values[key].append(float(breakdown[key]))
        if int(record.get("llm_call_count") or 0) > 1:
            multi_llm_questions += 1
        p, c, j = record_token_totals(record)
        prompt_tokens += p
        completion_tokens += c
        jina_tokens += j
        provider_cost += record_cost(record)
        for finding in record.get("before_findings") or []:
            old_findings[finding["category"]] += 1
        for name in record.get("top_chunk_sources") or []:
            top_files[name] += 1
        for event in record.get("http_events") or []:
            http_statuses[str(event.get("status_code"))] += 1
            if event.get("response_model"):
                models[event["response_model"]] += 1
        for trace in record.get("rag_trace") or []:
            retrieval_candidate_counts.append(int(trace.get("candidate_count") or 0))
            for candidate in trace.get("candidates") or []:
                if candidate.get("selected_by_ranker"):
                    channels = candidate.get("selection_channels") or []
                    selected_channels["+".join(channels) if channels else "fallback"] += 1

    faster = sum(
        record["after_latency_ms"] < record["before_latency_ms"] for record in records
    )
    slower = sum(
        record["after_latency_ms"] > record["before_latency_ms"] for record in records
    )
    equal = len(records) - faster - slower
    return {
        "report_mode": "neutral_manual_review_no_judgment",
        "manual_acceptance_guideline_ar": (
            "تُعد الإجابة مقبولة إذا أجابت السؤال بصورة جيدة ولم تتضمن خطأً "
            "ماديًا، حتى لو لم تكن مثالية أو شاملة لكل تفصيل."
        ),
        "questions": len(records),
        "unique_qids": len({record["qid"] for record in records}),
        "scenarios": len({record["scenario_id"] for record in records}),
        "successful_responses": sum(not record.get("error") for record in records),
        "errors": sum(bool(record.get("error")) for record in records),
        "empty_answers": sum(not record.get("after_answer", "").strip() for record in records),
        "historical_review_metadata": {
            "finding_entries": sum(
                len(record.get("before_findings") or []) for record in records
            ),
            "unique_questions_with_findings": sum(
                bool(record.get("before_findings")) for record in records
            ),
            "categories": dict(old_findings),
            "note": (
                "These are copied historical findings only. "
                "No verdict was assigned to any new answer."
            ),
        },
        "answer_text_comparison": {
            "exactly_unchanged": sum(
                record.get("before_answer", "") == record.get("after_answer", "")
                for record in records
            ),
            "changed": sum(
                record.get("before_answer", "") != record.get("after_answer", "")
                for record in records
            ),
            "note": (
                "Textual comparison only; a changed or unchanged answer is not "
                "a correctness verdict."
            ),
        },
        "latency_ms": {
            "before": latency_stats(before_latency),
            "after": latency_stats(after_latency),
            "paired": {
                "faster_questions": faster,
                "slower_questions": slower,
                "equal_questions": equal,
                "mean_delta": round(
                    statistics.mean(
                        record["after_latency_ms"] - record["before_latency_ms"]
                        for record in records
                    ),
                    1,
                ),
                "mean_speedup_ratio": round(
                    statistics.mean(before_latency)
                    / max(statistics.mean(after_latency), 0.001),
                    3,
                ),
            },
        },
        "generation": {
            "requested_model": records[0]["benchmark"]["model"],
            "response_models": dict(models),
            "openrouter_prompt_tokens": prompt_tokens,
            "openrouter_completion_tokens": completion_tokens,
            "openrouter_total_tokens": prompt_tokens + completion_tokens,
            "openrouter_reported_cost_usd": round(provider_cost, 8),
            "jina_reported_tokens": jina_tokens,
            "http_statuses": dict(http_statuses),
            "questions_with_multiple_llm_requests": multi_llm_questions,
        },
        "pipeline_diagnostics": {
            "cache_hits": cache_hits,
            "cache_hit_question_ids": cache_hit_question_ids,
            "rerank_status_counts": dict(rerank_statuses),
            "rerank_question_ids": rerank_question_ids,
            "answer_check_retries": answer_check_retries,
            "answer_check_safety_fallbacks": answer_check_fallbacks,
            "source_metadata_extractions": source_metadata_extractions,
            "latency_breakdown_ms": {
                key: latency_stats(values)
                for key, values in breakdown_values.items()
            },
        },
        "benchmark_flags": records[0]["benchmark"],
        "rag": {
            "records_with_trace": sum(bool(record.get("rag_trace")) for record in records),
            "records_without_trace_instant_path": sum(
                not record.get("rag_trace") for record in records
            ),
            "retrieval_calls": len(retrieval_candidate_counts),
            "mean_candidates_per_call": round(
                statistics.mean(retrieval_candidate_counts), 2
            )
            if retrieval_candidate_counts
            else None,
            "selected_channel_counts": dict(selected_channels),
            "answer_source_counts": dict(sources),
            "most_frequent_final_chunk_files": top_files.most_common(25),
        },
    }


def build_csv(records: list[dict[str, Any]], path: Path) -> None:
    fields = [
        "qid",
        "scenario_id",
        "scenario_title",
        "difficulty",
        "turn",
        "question",
        "before_answer",
        "after_answer",
        "before_source",
        "after_source",
        "before_latency_ms",
        "after_latency_ms",
        "latency_delta_ms",
        "top_chunk_count",
        "top_chunk_sources",
        "cache_hit",
        "rerank_status",
        "latency_breakdown",
        "retrieval_metadata",
        "client_history_snapshot",
        "rag_query_count",
        "rag_candidate_counts",
        "llm_call_count",
        "http_call_count",
        "openrouter_prompt_tokens",
        "openrouter_completion_tokens",
        "jina_tokens",
        "openrouter_cost_usd",
        "http_statuses",
        "response_models",
        "http_request_shapes",
        "historical_findings",
        "pipeline_code_sha256",
        "started_at",
        "finished_at",
        "raw_record_sha256",
        "manual_verdict",
        "manual_notes",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            prompt, completion, jina = record_token_totals(record)
            raw = json.dumps(record, ensure_ascii=False, sort_keys=True)
            writer.writerow(
                {
                    "qid": record["qid"],
                    "scenario_id": record["scenario_id"],
                    "scenario_title": record["scenario_title"],
                    "difficulty": record["difficulty"],
                    "turn": record["turn"],
                    "question": record["question"],
                    "before_answer": record["before_answer"],
                    "after_answer": record["after_answer"],
                    "before_source": record["before_source"],
                    "after_source": record["after_source"],
                    "before_latency_ms": record["before_latency_ms"],
                    "after_latency_ms": record["after_latency_ms"],
                    "latency_delta_ms": (
                        record["after_latency_ms"] - record["before_latency_ms"]
                    ),
                    "top_chunk_count": record["top_chunk_count"],
                    "top_chunk_sources": json.dumps(
                        record.get("top_chunk_sources") or [], ensure_ascii=False
                    ),
                    "cache_hit": (
                        record.get("retrieval_metadata") or {}
                    ).get("cache_hit"),
                    "rerank_status": (
                        record.get("retrieval_metadata") or {}
                    ).get("rerank_status"),
                    "latency_breakdown": json.dumps(
                        record.get("latency_breakdown") or {},
                        ensure_ascii=False,
                    ),
                    "retrieval_metadata": json.dumps(
                        record.get("retrieval_metadata") or {},
                        ensure_ascii=False,
                    ),
                    "client_history_snapshot": json.dumps(
                        record.get("client_history_snapshot") or [],
                        ensure_ascii=False,
                    ),
                    "rag_query_count": len(record.get("rag_trace") or []),
                    "rag_candidate_counts": json.dumps(
                        [
                            trace.get("candidate_count")
                            for trace in record.get("rag_trace") or []
                        ]
                    ),
                    "llm_call_count": record.get("llm_call_count"),
                    "http_call_count": len(record.get("http_events") or []),
                    "openrouter_prompt_tokens": prompt,
                    "openrouter_completion_tokens": completion,
                    "jina_tokens": jina,
                    "openrouter_cost_usd": f"{record_cost(record):.8f}",
                    "http_statuses": json.dumps(
                        [
                            event.get("status_code")
                            for event in record.get("http_events") or []
                        ]
                    ),
                    "response_models": json.dumps(
                        [
                            event.get("response_model")
                            for event in record.get("http_events") or []
                            if event.get("response_model")
                        ],
                        ensure_ascii=False,
                    ),
                    "http_request_shapes": json.dumps(
                        [
                            event.get("request_shape")
                            for event in record.get("http_events") or []
                        ],
                        ensure_ascii=False,
                    ),
                    "historical_findings": json.dumps(
                        record.get("before_findings") or [], ensure_ascii=False
                    ),
                    "pipeline_code_sha256": (
                        record.get("benchmark") or {}
                    ).get("pipeline_code_sha256"),
                    "started_at": record.get("started_at"),
                    "finished_at": record.get("finished_at"),
                    "raw_record_sha256": hashlib.sha256(
                        raw.encode("utf-8")
                    ).hexdigest(),
                    "manual_verdict": "",
                    "manual_notes": "",
                }
            )


def build_markdown(records: list[dict[str, Any]], summary: dict[str, Any], path: Path) -> None:
    latency = summary["latency_ms"]
    generation = summary["generation"]
    lines = [
        '<div dir="rtl">',
        "",
        "# تقرير إعادة اختبار 440 سؤالًا — بدون تحكيم",
        "",
        "هذا تقرير وصفي محايد للمراجعة اليدوية. لا يحتوي على أي حكم جديد "
        "بالصحة أو الخطأ. حقول «الملاحظات التاريخية» منسوخة فقط من تقرير "
        "المراجعة السابق كي تساعد في المقارنة.",
        "",
        "## معيار المراجعة اليدوية المقترح",
        "",
        "بناءً على طلب صاحب المشروع: تُعد الإجابة **مقبولة** إذا أجابت السؤال "
        "بصورة جيدة ولم تتضمن خطأً ماديًا، حتى لو لم تكن مثالية أو شاملة لكل "
        "تفصيل. لا يُعد نقص تحسين صياغي أو تفصيل غير لازم سببًا كافيًا لرفضها.",
        "",
        "## ملخص سلامة التشغيل",
        "",
        f"- الأسئلة: **{summary['questions']}** ضمن "
        f"**{summary['scenarios']} سيناريو**.",
        f"- الاستجابات الناجحة: **{summary['successful_responses']}**؛ "
        f"الأخطاء التقنية: **{summary['errors']}**؛ "
        f"الإجابات الفارغة: **{summary['empty_answers']}**.",
        f"- النموذج: `{generation['requested_model']}`؛ "
        f"الكاش: `{summary['benchmark_flags']['cache_enabled']}`؛ "
        f"Reranker: `{summary['benchmark_flags']['rerank_enabled']}`.",
        f"- مجموعات المعرفة الفعالة المشمولة: "
        f"**{summary['benchmark_flags']['uploaded_collection_count']}**.",
        f"- تكلفة OpenRouter المبلّغ عنها: "
        f"**${generation['openrouter_reported_cost_usd']:.8f}**.",
        f"- توكنات OpenRouter: **{generation['openrouter_prompt_tokens']:,}** إدخال "
        f"+ **{generation['openrouter_completion_tokens']:,}** إخراج.",
        f"- توكنات Jina المبلّغ عنها: **{generation['jina_reported_tokens']:,}**.",
        "",
        "## مقارنة الزمن فقط",
        "",
        "| المقياس | قبل (ث) | بعد (ث) |",
        "|---|---:|---:|",
        f"| المتوسط | {fmt_s(latency['before']['mean'])} | "
        f"{fmt_s(latency['after']['mean'])} |",
        f"| الوسيط | {fmt_s(latency['before']['median'])} | "
        f"{fmt_s(latency['after']['median'])} |",
        f"| P95 | {fmt_s(latency['before']['p95'])} | "
        f"{fmt_s(latency['after']['p95'])} |",
        f"| الأقصى | {fmt_s(latency['before']['max'])} | "
        f"{fmt_s(latency['after']['max'])} |",
        "",
        f"- أسرع من السابق: **{latency['paired']['faster_questions']}** سؤالًا؛ "
        f"أبطأ: **{latency['paired']['slower_questions']}**؛ "
        f"متساوٍ: **{latency['paired']['equal_questions']}**.",
        f"- تغيّر نص الإجابة في "
        f"**{summary['answer_text_comparison']['changed']}** سؤالًا، وبقي مطابقًا "
        f"حرفيًا في **{summary['answer_text_comparison']['exactly_unchanged']}**؛ "
        "وهذه مقارنة نصية فقط وليست حكمًا على الجودة.",
        "",
        "## الكاش والـReranker",
        "",
        f"- الكاش كان مفعّلًا، وعدد إصاباته الفعلية: "
        f"**{summary['pipeline_diagnostics']['cache_hits']}**. الأسئلة في هذا "
        "الاختبار فريدة، لذلك لا يُتوقع أن يغيّر الكاش إجاباتها؛ فائدته هنا "
        "زمنية عند تكرار طلب مطابق.",
        f"- طُبّق الـReranker بنجاح في "
        f"**{summary['pipeline_diagnostics']['rerank_status_counts'].get('applied', 0)}** "
        "سؤالًا.",
        f"- تعذر الـReranker وانتقل النظام إلى الترتيب الأصلي الآمن في "
        f"**{summary['pipeline_diagnostics']['rerank_status_counts'].get('error_fallback', 0)}** "
        "سؤالًا. هذه مرشحات تقنية لإعادة منفردة عند استقرار الخدمة، وليست "
        "أحكامًا بأن الإجابات الحالية خاطئة:",
        "",
        "`"
        + "، ".join(
            summary["pipeline_diagnostics"]["rerank_question_ids"].get(
                "error_fallback", []
            )
        )
        + "`",
        "",
        "## طريقة قراءة Metadata",
        "",
        "- `dense_cosine`: التشابه الدلالي.",
        "- `bm25_score`: المطابقة اللفظية.",
        "- `rrf_score/fused_rank`: نتيجة دمج Dense وBM25 وترتيب المقطع.",
        "- `selection_channels`: القناة التي أهلت المقطع (`dense` أو `bm25` أو كلاهما).",
        "- المقاطع النهائية هي النصوص التي دخلت برومبت الإجابة فعلًا بعد قواعد "
        "السياق والمرحلة والاستبعاد والجداول التجميعية.",
        "",
        "---",
        "",
    ]

    for record in records:
        prompt_tokens, completion_tokens, jina_tokens = record_token_totals(record)
        latency_delta = record["after_latency_ms"] - record["before_latency_ms"]
        retrieval_metadata = record.get("retrieval_metadata") or {}
        breakdown = record.get("latency_breakdown") or {}
        lines.extend(
            [
                f"## {record['qid']} — {record['scenario_id']} — "
                f"{record['scenario_title']}",
                "",
                f"**الصعوبة:** `{record['difficulty']}` · "
                f"**الدور:** `{record['turn']}`",
                "",
                "### السؤال",
                "",
                pre(record["question"]),
                "",
                "### الإجابة قبل",
                "",
                pre(record["before_answer"]),
                "",
                "### الإجابة بعد",
                "",
                pre(record["after_answer"]),
                "",
                "### Metadata الأساسية",
                "",
                "| الحقل | القيمة |",
                "|---|---|",
                f"| المصدر قبل | {md_cell(record['before_source'])} |",
                f"| المصدر بعد | {md_cell(record.get('after_source'))} |",
                f"| الزمن قبل | {fmt_s(record['before_latency_ms'])} ث |",
                f"| الزمن بعد | {fmt_s(record['after_latency_ms'])} ث |",
                f"| فرق الزمن | {latency_delta / 1000:+.3f} ث |",
                f"| عدد المقاطع النهائية | {record.get('top_chunk_count')} |",
                f"| ملفات المقاطع النهائية | "
                f"{md_cell('، '.join(record.get('top_chunk_sources') or []))} |",
                f"| عدد عمليات RAG | {len(record.get('rag_trace') or [])} |",
                f"| عدد استدعاءات LLM | {record.get('llm_call_count')} |",
                f"| Cache hit | {md_cell(retrieval_metadata.get('cache_hit'))} |",
                f"| Reranker status | "
                f"{md_cell(retrieval_metadata.get('rerank_status'))} |",
                f"| زمن OpenRouter | "
                f"{fmt_s(breakdown.get('openrouter_generation_ms'))} ث |",
                f"| زمن Reranker | "
                f"{fmt_s(breakdown.get('jina_reranker_ms'))} ث |",
                f"| زمن Embeddings | "
                f"{fmt_s(breakdown.get('jina_embeddings_ms'))} ث |",
                f"| زمن محلي تقديري | "
                f"{fmt_s(breakdown.get('local_pipeline_ms_estimate'))} ث |",
                f"| توكنات OpenRouter | input={prompt_tokens:,}، "
                f"output={completion_tokens:,} |",
                f"| توكنات Jina | {jina_tokens:,} |",
                f"| تكلفة OpenRouter المبلّغ عنها | ${record_cost(record):.8f} |",
                f"| بدأ | {md_cell(record.get('started_at'))} |",
                f"| انتهى | {md_cell(record.get('finished_at'))} |",
                "",
            ]
        )
        findings = record.get("before_findings") or []
        lines.extend(["### ملاحظات المراجعة القديمة", ""])
        if findings:
            for finding in findings:
                lines.append(
                    f"- **{finding['category']}**: {finding['detail']}"
                )
        else:
            lines.append("- لم يسجل التقرير القديم ملاحظة على هذا السؤال.")
        lines.append("")

        lines.extend(
            [
                "### حالة الحوار التي رآها السؤال",
                "",
                pre(json.dumps(
                    record.get("client_history_snapshot") or [],
                    ensure_ascii=False,
                    indent=2,
                )),
                "",
                "### خطة الاسترجاع والفحوص",
                "",
                pre(json.dumps(
                    retrieval_metadata,
                    ensure_ascii=False,
                    indent=2,
                )),
                "",
            ]
        )

        lines.extend(["### استدعاءات HTTP", ""])
        if record.get("http_events"):
            lines.extend(
                [
                    "| # | المضيف | المودل المطلوب | المودل المستجاب | الحالة | "
                    "الزمن ms | finish | usage |",
                    "|---:|---|---|---|---:|---:|---|---|",
                ]
            )
            for index, event in enumerate(record["http_events"], 1):
                lines.append(
                    f"| {index} | {md_cell(event.get('url_host'))} | "
                    f"{md_cell(event.get('request_model'))} | "
                    f"{md_cell(event.get('response_model'))} | "
                    f"{md_cell(event.get('status_code'))} | "
                    f"{md_cell(event.get('latency_ms'))} | "
                    f"{md_cell(event.get('finish_reason'))} | "
                    f"{md_cell(event_usage(event))} |"
                )
        else:
            lines.append("- مسار فوري بلا استدعاء HTTP.")
        lines.append("")
        for index, event in enumerate(record.get("http_events") or [], 1):
            lines.extend(
                [
                    f"<details><summary>تفاصيل استدعاء HTTP رقم {index}</summary>",
                    "",
                    pre(json.dumps(event, ensure_ascii=False, indent=2)),
                    "",
                    "</details>",
                    "",
                ]
            )

        lines.extend(["### تتبع RAG: Dense + BM25 + RRF", ""])
        traces = record.get("rag_trace") or []
        if not traces:
            lines.append("- لا يوجد: استُخدم مسار فوري موثوق بلا استرجاع.")
            lines.append("")
        for trace_index, trace in enumerate(traces, 1):
            lines.extend(
                [
                    f"#### عملية الاسترجاع {trace_index}",
                    "",
                    f"- **النطاق:** `{trace.get('scope')}`",
                    f"- **الاستعلام الفعلي:** {trace.get('query')}",
                    f"- **المرشحون:** {trace.get('candidate_count')} · "
                    f"**Top‑K:** {trace.get('top_k')} · "
                    f"**Dense threshold:** {trace.get('dense_threshold')} · "
                    f"**RRF k:** {trace.get('rrf_k')}",
                    "",
                    "| fused | الملف | RRF | dense rank | cosine | BM25 rank | "
                    "BM25 score | القنوات |",
                    "|---:|---|---:|---:|---:|---:|---:|---|",
                ]
            )
            selected = [
                candidate
                for candidate in trace.get("candidates") or []
                if candidate.get("selected_by_ranker")
            ]
            for candidate in selected:
                lines.append(
                    f"| {candidate.get('fused_rank')} | "
                    f"{md_cell(candidate.get('file') or '<structured>')} | "
                    f"{candidate.get('rrf_score')} | "
                    f"{candidate.get('dense_rank')} | "
                    f"{candidate.get('dense_cosine')} | "
                    f"{md_cell(candidate.get('lexical_rank'))} | "
                    f"{candidate.get('bm25_score')} | "
                    f"{md_cell('+'.join(candidate.get('selection_channels') or []))} |"
                )
            lines.append("")

        lines.extend(["### المقاطع النهائية المرسلة للنموذج", ""])
        chunks = record.get("top_chunks") or []
        if not chunks:
            lines.append("- لا توجد مقاطع؛ الإجابة من مسار فوري.")
            lines.append("")
        for chunk_index, chunk in enumerate(chunks, 1):
            source = chunk_source(chunk)
            lines.extend(
                [
                    f"<details><summary>المقطع {chunk_index} — "
                    f"{html.escape(source)}</summary>",
                    "",
                    pre(chunk),
                    "",
                    "</details>",
                    "",
                ]
            )
        lines.extend(["---", ""])

    lines.extend(["</div>", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--results",
        type=Path,
        default=ROOT / "eval" / "retest_440_detailed_2026-07-18" / "after_results.jsonl",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "eval" / "retest_440_detailed_2026-07-18",
    )
    args = parser.parse_args()
    records = load_jsonl(args.results)
    expected = [f"Q{i:03d}" for i in range(1, 441)]
    qids = [record["qid"] for record in records]
    if len(records) != 440 or qids != expected:
        raise RuntimeError(
            f"Expected ordered Q001..Q440; found {len(records)} records."
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary = build_summary(records)
    (args.output_dir / "summary_no_judgment.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    build_csv(records, args.output_dir / "manual_review_440.csv")
    build_markdown(
        records,
        summary,
        args.output_dir / "تقرير_إعادة_الاختبار_440_بدون_تحكيم.md",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
