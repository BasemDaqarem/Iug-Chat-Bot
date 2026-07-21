# -*- coding: utf-8 -*-
"""Replay only grounded-evaluation candidates with cache + reranker enabled."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config, file_catalog, retrieval  # noqa: E402
from app.chatbot import IUGChatbot  # noqa: E402
from app.rbac import Principal  # noqa: E402
from app.sessions import SessionStore  # noqa: E402
from eval.retest_440 import HttpTelemetry, chunk_sources, load_completed  # noqa: E402


DEFAULT_DIR = ROOT / "eval" / "retest_440_detailed_2026-07-18"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_DIR / "after_results.jsonl",
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=DEFAULT_DIR / "reranker_retest_candidates.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_DIR / "ranker_retest_results.jsonl",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--tiers",
        default="high",
        help="Comma-separated candidate tiers to replay (default: high).",
    )
    parser.add_argument(
        "--qids",
        default="",
        help="Optional comma-separated QIDs after tier filtering.",
    )
    args = parser.parse_args()

    if not config.RERANK_ENABLED:
        raise RuntimeError("Run with RERANK_ENABLED=true")
    if not config.CACHE_ENABLED:
        raise RuntimeError("Run with CACHE_ENABLED=true as requested")
    if config.CHAT_API_MODEL != "openai/gpt-oss-20b":
        raise RuntimeError(
            "Safety gate: this retest is locked to openai/gpt-oss-20b; "
            f"got {config.CHAT_API_MODEL!r}"
        )
    if (
        config.CHAT_FALLBACK_URL
        and config.CHAT_FALLBACK_MODEL
        and config.CHAT_FALLBACK_MODEL != "openai/gpt-oss-20b"
    ):
        raise RuntimeError(
            "Safety gate: fallback model must also be openai/gpt-oss-20b; "
            f"got {config.CHAT_FALLBACK_MODEL!r}"
        )

    baseline_rows = load_jsonl(args.baseline)
    baseline_by_qid = {row["qid"]: row for row in baseline_rows}
    candidates_payload = json.loads(args.candidates.read_text(encoding="utf-8"))
    tiers = {value.strip() for value in args.tiers.split(",") if value.strip()}
    candidates = [
        item
        for item in candidates_payload["candidates"]
        if item.get("reranker_tier") in tiers
    ]
    requested_qids = {
        value.strip() for value in args.qids.split(",") if value.strip()
    }
    if requested_qids:
        candidates = [
            item for item in candidates if item.get("qid") in requested_qids
        ]
    candidate_by_qid = {item["qid"]: item for item in candidates}
    qids = sorted(candidate_by_qid)
    completed = load_completed(args.output)

    telemetry = HttpTelemetry()
    telemetry.install()
    print("Initializing chatbot/index with cache + reranker enabled ...", flush=True)
    bot = IUGChatbot(sessions=SessionStore())
    bot.initialize()
    available = {item["collection"] for item in bot.get_uploaded_files_list()}
    allowed = file_catalog.allowed_collections(Principal.guest(), available)
    print(
        f"Ready: {len(qids)} candidates; {len(completed)} completed; "
        f"{len(available)} collections.",
        flush=True,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    executed = 0
    try:
        with args.output.open("a", encoding="utf-8", buffering=1) as output:
            for index, qid in enumerate(qids, 1):
                if qid in completed and not completed[qid].get("error"):
                    continue
                if args.limit is not None and executed >= args.limit:
                    break
                baseline = baseline_by_qid[qid]
                history_rows = [
                    row
                    for row in baseline_rows
                    if row["scenario_id"] == baseline["scenario_id"]
                    and int(row["turn"]) < int(baseline["turn"])
                ]
                history = [
                    {
                        "user": row["question"],
                        "assistant": row["after_answer"],
                        "at": time.time(),
                    }
                    for row in history_rows[-5:]
                ]
                telemetry.reset()
                trace_token = retrieval.begin_trace()
                started_at = datetime.now(timezone.utc).isoformat()
                t0 = time.perf_counter()
                answer = ""
                source = ""
                chunks: list[str] = []
                error = None
                try:
                    principal = Principal.guest(f"guest:ranker-retest-{uuid4().hex}")
                    result = bot.chat_as_principal(
                        baseline["question"],
                        principal,
                        allowed_collections=allowed,
                        client_history=history or None,
                    )
                    answer = result.get("answer", "")
                    source = result.get("source", "")
                    chunks = result.get("top_chunks", [])
                    retrieval_metadata = result.get("retrieval_metadata", {})
                    if not answer.strip():
                        raise ValueError("Empty answer")
                except Exception as exc:
                    retrieval_metadata = {}
                    error = {"type": type(exc).__name__, "message": str(exc)}
                finally:
                    rag_trace = retrieval.end_trace(trace_token)
                latency_ms = round((time.perf_counter() - t0) * 1000)

                row = {
                    **baseline,
                    "after_answer": answer,
                    "after_source": source,
                    "after_latency_ms": latency_ms,
                    "top_chunks": chunks,
                    "top_chunk_sources": chunk_sources(chunks),
                    "top_chunk_count": len(chunks),
                    "retrieval_metadata": retrieval_metadata,
                    "rag_trace": rag_trace,
                    "http_events": telemetry.events,
                    "llm_call_count": sum(
                        1
                        for event in telemetry.events
                        if event.get("request_model") == config.CHAT_API_MODEL
                    ),
                    "error": error,
                    "started_at": started_at,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "benchmark": {
                        "model": config.CHAT_API_MODEL,
                        "cache_enabled": config.CACHE_ENABLED,
                        "rerank_enabled": config.RERANK_ENABLED,
                        "rerank_candidates": config.RERANK_CANDIDATES,
                        "rerank_timeout_seconds": config.RERANK_TIMEOUT_SECONDS,
                        "coverage_top_k": config.COVERAGE_TOP_K,
                        "max_context_chars": config.MAX_CONTEXT_CHARS,
                    },
                    "baseline_answer": baseline["after_answer"],
                    "baseline_latency_ms": baseline["after_latency_ms"],
                    "baseline_top_chunks": baseline.get("top_chunks") or [],
                    "baseline_top_chunk_sources": baseline.get("top_chunk_sources") or [],
                    "candidate_evaluation": candidate_by_qid[qid]["evaluation"],
                    "candidate_tier": candidate_by_qid[qid]["reranker_tier"],
                    "candidate_reason": candidate_by_qid[qid]["reranker_tier_reason"],
                    "client_history_snapshot": [
                        {
                            "question": turn["user"],
                            "answer": turn["assistant"],
                        }
                        for turn in history
                    ],
                }
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                executed += 1
                status = "OK" if not error else f"ERROR {error['type']}"
                print(
                    f"{index:03d}/{len(qids)} {qid} {status} "
                    f"{latency_ms / 1000:.2f}s chunks={len(chunks)}",
                    flush=True,
                )
    finally:
        telemetry.uninstall()
    print(f"Saved ranker retest to {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
