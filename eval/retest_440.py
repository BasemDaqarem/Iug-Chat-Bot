# -*- coding: utf-8 -*-
"""Resumable 440-question before/after benchmark with per-call diagnostics.

The source DOCX contains the original questions, answers, scenario boundaries,
sources, and latencies.  This runner replays the same conversational scenarios
against the current local code, disables no features by itself (the benchmark
flags belong in .env), and appends one durable JSONL record after every answer.

Example:
    python eval/retest_440.py --input "C:\\...\تقرير_الأسئلة_والإجابات_440_سؤال.docx"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree

import requests

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config, file_catalog, retrieval  # noqa: E402
from app.chatbot import IUGChatbot  # noqa: E402
from app.rbac import Principal  # noqa: E402
from app.sessions import SessionStore  # noqa: E402


SCENARIO_RE = re.compile(
    r"^السيناريو\s+(?P<id>[A-Z]\d{3})\s+—\s+(?P<title>.+?)\s+·\s+الصعوبة:\s*(?P<difficulty>\w+)"
)
QUESTION_RE = re.compile(r"^(?P<qid>Q\d{3})\s+\[الدور\s+(?P<turn>\d+)\]\s+(?P<question>.+)$")
META_RE = re.compile(
    r"^المصدر:\s*(?P<source>.+?)\s+·\s+الزمن:\s*(?P<latency>[0-9.]+)\s*ث"
    r"\s+·\s+السياق السابق:\s*(?P<context>\d+)"
)
FILE_RE = re.compile(r"^\[ملف:\s*([^\]]+)\]")
ERROR_ENTRY_RE = re.compile(r"^- \*\*(Q\d{3})\*\* \[(?P<category>[^\]]+)\]:\s*(?P<detail>.+)$")
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PIPELINE_FILES = (
    "app/answer_check.py",
    "app/chatbot.py",
    "app/config.py",
    "app/llm.py",
    "app/query_rewrite.py",
    "app/rerank.py",
    "app/retrieval.py",
    "app/uploaded_files.py",
    "eval/retest_440.py",
)


def pipeline_fingerprint() -> str:
    digest = hashlib.sha256()
    for relative in PIPELINE_FILES:
        path = ROOT / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def latency_breakdown(total_ms: int, events: list[dict[str, Any]]) -> dict[str, int]:
    def total_for(predicate) -> int:
        return sum(
            int(event.get("latency_ms") or 0)
            for event in events
            if predicate(event)
        )

    openrouter = total_for(lambda event: event.get("url_host") == "openrouter.ai")
    reranker = total_for(
        lambda event: str(event.get("request_model") or "").startswith(
            "jina-reranker"
        )
    )
    embeddings = total_for(
        lambda event: str(event.get("request_model") or "").startswith(
            "jina-embeddings"
        )
    )
    http_total = total_for(lambda event: True)
    return {
        "total_ms": int(total_ms),
        "http_total_ms": http_total,
        "openrouter_generation_ms": openrouter,
        "jina_reranker_ms": reranker,
        "jina_embeddings_ms": embeddings,
        "local_pipeline_ms_estimate": max(0, int(total_ms) - http_total),
    }


def parse_source_docx(path: Path) -> list[dict[str, Any]]:
    # Read-only OOXML extraction keeps this benchmark runnable in the project's
    # own Python environment without adding python-docx as a production dep.
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{{{W_NS}}}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{{{W_NS}}}t":
                parts.append(node.text or "")
            elif node.tag == f"{{{W_NS}}}tab":
                parts.append("\t")
            elif node.tag in {f"{{{W_NS}}}br", f"{{{W_NS}}}cr"}:
                parts.append("\n")
        paragraphs.append("".join(parts).strip())
    rows: list[dict[str, Any]] = []
    scenario: dict[str, str] | None = None
    i = 0
    while i < len(paragraphs):
        text = paragraphs[i]
        sm = SCENARIO_RE.match(text)
        if sm:
            scenario = sm.groupdict()
            i += 1
            continue
        qm = QUESTION_RE.match(text)
        if not qm:
            i += 1
            continue
        if scenario is None:
            raise ValueError(f"Question without scenario at paragraph {i}: {text}")
        if i + 2 >= len(paragraphs) or not paragraphs[i + 1].startswith("الإجابة:"):
            raise ValueError(f"Malformed answer block after {qm.group('qid')}")
        answer = paragraphs[i + 1][len("الإجابة:"):].strip()
        mm = META_RE.match(paragraphs[i + 2])
        if not mm:
            raise ValueError(f"Malformed metadata after {qm.group('qid')}: {paragraphs[i + 2]}")
        meta = mm.groupdict()
        rows.append(
            {
                **qm.groupdict(),
                "turn": int(qm.group("turn")),
                "scenario_id": scenario["id"],
                "scenario_title": scenario["title"],
                "difficulty": scenario["difficulty"],
                "before_answer": answer,
                "before_source": meta["source"],
                "before_latency_ms": round(float(meta["latency"]) * 1000),
                "before_context_count": int(meta["context"]),
            }
        )
        i += 3
    qids = [row["qid"] for row in rows]
    if len(rows) != 440 or len(set(qids)) != 440:
        raise ValueError(f"Expected 440 unique questions, found {len(rows)} rows / {len(set(qids))} IDs")
    return rows


def parse_before_findings(path: Path) -> dict[str, list[dict[str, str]]]:
    findings: dict[str, list[dict[str, str]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ERROR_ENTRY_RE.match(line.strip())
        if not match:
            continue
        qid = match.group(1)
        findings.setdefault(qid, []).append(
            {"category": match.group("category"), "detail": match.group("detail")}
        )
    return findings


def load_completed(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    completed: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {number}: {exc}") from exc
            completed[record["qid"]] = record
    return completed


class HttpTelemetry:
    """Capture OpenAI-compatible and embedding HTTP metadata without secrets."""

    def __init__(self) -> None:
        self._original = requests.post
        self.events: list[dict[str, Any]] = []

    def install(self) -> None:
        requests.post = self._post  # type: ignore[method-assign]

    def uninstall(self) -> None:
        requests.post = self._original  # type: ignore[method-assign]

    def reset(self) -> None:
        self.events = []

    def _post(self, url, *args, **kwargs):
        started = time.perf_counter()
        request_json = kwargs.get("json") or {}
        event: dict[str, Any] = {
            "url_host": re.sub(r"^https?://([^/]+).*$", r"\1", str(url)),
            "request_model": request_json.get("model"),
        }
        messages = request_json.get("messages") or []
        documents = request_json.get("documents") or []
        if messages:
            provider = request_json.get("provider") or {}
            event["request_shape"] = {
                "message_count": len(messages),
                "system_chars": len(str(messages[0].get("content", "")))
                if isinstance(messages[0], dict) else None,
                "user_chars": len(str(messages[-1].get("content", "")))
                if isinstance(messages[-1], dict) else None,
                "max_tokens": request_json.get("max_tokens"),
                "reasoning_effort": request_json.get("reasoning_effort"),
                "provider_sort": (
                    provider.get("sort") if isinstance(provider, dict) else None
                ),
            }
        if isinstance(documents, list) and documents:
            event["request_shape"] = {
                "query_chars": len(str(request_json.get("query", ""))),
                "document_count": len(documents),
                "document_chars": sum(len(str(document)) for document in documents),
                "top_n": request_json.get("top_n"),
            }
        try:
            response = self._original(url, *args, **kwargs)
            event["latency_ms"] = round((time.perf_counter() - started) * 1000)
            event["status_code"] = response.status_code
            try:
                body = response.json()
            except Exception:
                body = {}
            event["response_model"] = body.get("model")
            usage = body.get("usage") or {}
            event["usage"] = {
                key: usage.get(key)
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "input_tokens",
                    "output_tokens",
                    "cost",
                )
                if usage.get(key) is not None
            }
            choices = body.get("choices") or []
            if choices:
                event["finish_reason"] = choices[0].get("finish_reason")
            results = body.get("results") or []
            if results:
                scores = [
                    item.get("relevance_score")
                    for item in results
                    if isinstance(item, dict)
                    and isinstance(item.get("relevance_score"), (int, float))
                ]
                event["result_count"] = len(results)
                if scores:
                    event["relevance_score_range"] = {
                        "min": min(scores),
                        "max": max(scores),
                    }
            if response.status_code >= 400:
                error = body.get("error")
                event["error_type"] = error.get("type") if isinstance(error, dict) else None
                event["error_code"] = error.get("code") if isinstance(error, dict) else None
            self.events.append(event)
            return response
        except Exception as exc:
            event["latency_ms"] = round((time.perf_counter() - started) * 1000)
            event["exception_type"] = type(exc).__name__
            self.events.append(event)
            raise


def chunk_sources(chunks: list[str]) -> list[str]:
    names: list[str] = []
    for chunk in chunks:
        match = FILE_RE.match(chunk)
        name = match.group(1) if match else "<structured-or-unknown>"
        if name not in names:
            names.append(name)
    return names


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def write_checkpoint(path: Path, completed: dict[str, dict[str, Any]], total: int) -> None:
    records = list(completed.values())
    successful = [r for r in records if not r.get("error")]
    latencies = [float(r["after_latency_ms"]) for r in successful]
    token_totals = [
        event.get("usage", {}).get("total_tokens")
        for record in successful
        for event in record.get("http_events", [])
        if event.get("usage", {}).get("total_tokens") is not None
    ]
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "completed": len(records),
        "total": total,
        "successful": len(successful),
        "errors": len(records) - len(successful),
        "after_latency_ms": {
            "mean": round(statistics.mean(latencies), 1) if latencies else None,
            "median": round(statistics.median(latencies), 1) if latencies else None,
            "p95": percentile(latencies, 0.95),
            "max": max(latencies) if latencies else None,
        },
        "reported_total_tokens": sum(token_totals) if token_totals else None,
        "model": config.CHAT_API_MODEL,
        "cache_enabled": config.CACHE_ENABLED,
        "rerank_enabled": config.RERANK_ENABLED,
        "max_context_chars": config.MAX_CONTEXT_CHARS,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--before-report",
        type=Path,
        default=ROOT / "docs" / "تقرير_المراجعة_النهائية_2026-07-16.md",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "eval" / "retest_440_2026-07-18",
    )
    parser.add_argument("--start-qid", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument(
        "--improved-pipeline",
        action="store_true",
        help=(
            "Require cache + selective reranker instead of the historical "
            "cache/reranker-off baseline."
        ),
    )
    args = parser.parse_args()

    rows = parse_source_docx(args.input)
    before_findings = parse_before_findings(args.before_report)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = args.output_dir / "after_results.jsonl"
    checkpoint_path = args.output_dir / "checkpoint.json"
    completed = load_completed(jsonl_path)

    if args.improved_pipeline:
        if not config.CACHE_ENABLED:
            raise RuntimeError("Improved benchmark requires CACHE_ENABLED=true")
        if not config.RERANK_ENABLED:
            raise RuntimeError("Improved benchmark requires RERANK_ENABLED=true")
    else:
        if config.CACHE_ENABLED:
            raise RuntimeError("Baseline benchmark requires CACHE_ENABLED=false")
        if config.RERANK_ENABLED:
            raise RuntimeError("Baseline benchmark requires RERANK_ENABLED=false")
    if config.CHAT_API_MODEL != "openai/gpt-oss-20b":
        raise RuntimeError(f"Unexpected CHAT_API_MODEL={config.CHAT_API_MODEL!r}")
    if (
        config.CHAT_FALLBACK_URL
        and config.CHAT_FALLBACK_MODEL
        and config.CHAT_FALLBACK_MODEL != "openai/gpt-oss-20b"
    ):
        raise RuntimeError(
            "Fallback model must be openai/gpt-oss-20b; "
            f"got {config.CHAT_FALLBACK_MODEL!r}"
        )

    telemetry = HttpTelemetry()
    telemetry.install()
    print("Initializing the current chatbot/index ...", flush=True)
    bot = IUGChatbot(sessions=SessionStore())
    bot.initialize()
    available = {item["collection"] for item in bot.get_uploaded_files_list()}
    allowed = file_catalog.allowed_collections(Principal.guest(), available)
    code_sha256 = pipeline_fingerprint()
    corpus_names_sha256 = hashlib.sha256(
        "\n".join(sorted(available)).encode("utf-8")
    ).hexdigest()
    print(
        f"Ready: {len(available)} uploaded collections; "
        f"{len(completed)}/440 already completed; model={config.CHAT_API_MODEL}",
        flush=True,
    )

    started = args.start_qid is None
    executed = 0
    history: list[dict[str, Any]] = []
    current_scenario = None

    try:
        with jsonl_path.open("a", encoding="utf-8", buffering=1) as output:
            for index, row in enumerate(rows, 1):
                if row["scenario_id"] != current_scenario:
                    current_scenario = row["scenario_id"]
                    history = []

                if not started:
                    started = row["qid"] == args.start_qid
                    if not started:
                        existing = completed.get(row["qid"])
                        if existing and not existing.get("error"):
                            history.append(
                                {
                                    "user": row["question"],
                                    "assistant": existing["after_answer"],
                                    "at": time.time(),
                                }
                            )
                        continue

                existing = completed.get(row["qid"])
                if existing and not existing.get("error"):
                    history.append(
                        {
                            "user": row["question"],
                            "assistant": existing["after_answer"],
                            "at": time.time(),
                        }
                    )
                    continue
                if args.limit is not None and executed >= args.limit:
                    break

                telemetry.reset()
                started_at = datetime.now(timezone.utc).isoformat()
                t0 = time.perf_counter()
                answer = ""
                source = ""
                chunks: list[str] = []
                retrieval_metadata: dict[str, Any] = {}
                error = None
                rag_trace: list[dict[str, Any]] = []
                trace_token = retrieval.begin_trace()
                history_snapshot = [
                    {
                        "question": turn.get("user", ""),
                        "answer": turn.get("assistant", ""),
                    }
                    for turn in history[-5:]
                ]
                try:
                    principal = Principal.guest(f"guest:retest-{uuid4().hex}")
                    result = bot.chat_as_principal(
                        row["question"],
                        principal,
                        allowed_collections=allowed,
                        client_history=history[-5:] or None,
                    )
                    answer = result.get("answer", "")
                    source = result.get("source", "")
                    chunks = result.get("top_chunks", [])
                    retrieval_metadata = result.get("retrieval_metadata", {})
                    if not answer.strip():
                        raise ValueError("Empty answer")
                except Exception as exc:
                    error = {"type": type(exc).__name__, "message": str(exc)}
                finally:
                    rag_trace = retrieval.end_trace(trace_token)
                latency_ms = round((time.perf_counter() - t0) * 1000)

                record = {
                    **row,
                    "before_findings": before_findings.get(row["qid"], []),
                    "before_verdict": "incorrect" if row["qid"] in before_findings else "correct",
                    "after_answer": answer,
                    "after_source": source,
                    "after_latency_ms": latency_ms,
                    "top_chunks": chunks,
                    "top_chunk_sources": chunk_sources(chunks),
                    "top_chunk_count": len(chunks),
                    "retrieval_metadata": retrieval_metadata,
                    "client_history_snapshot": history_snapshot,
                    "rag_trace": rag_trace,
                    "http_events": telemetry.events,
                    "latency_breakdown": latency_breakdown(
                        latency_ms, telemetry.events
                    ),
                    "llm_call_count": sum(
                        1 for event in telemetry.events if "chat" in event.get("url_host", "")
                        or event.get("request_model") == config.CHAT_API_MODEL
                    ),
                    "error": error,
                    "started_at": started_at,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "benchmark": {
                        "model": config.CHAT_API_MODEL,
                        "fallback_model": config.CHAT_FALLBACK_MODEL,
                        "reasoning_effort": config.LLM_REASONING_EFFORT,
                        "provider_sort": config.CHAT_PROVIDER_SORT,
                        "cache_enabled": config.CACHE_ENABLED,
                        "rerank_enabled": config.RERANK_ENABLED,
                        "rerank_candidates": config.RERANK_CANDIDATES,
                        "rerank_timeout_seconds": config.RERANK_TIMEOUT_SECONDS,
                        "coverage_top_k": config.COVERAGE_TOP_K,
                        "coverage_max_tokens": config.LLM_COVERAGE_MAX_TOKENS,
                        "default_max_tokens": config.LLM_MAX_TOKENS,
                        "max_context_chars": config.MAX_CONTEXT_CHARS,
                        "pipeline_code_sha256": code_sha256,
                        "pipeline_files": list(PIPELINE_FILES),
                        "uploaded_collection_count": len(available),
                        "uploaded_collection_names_sha256": corpus_names_sha256,
                    },
                }
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                completed[row["qid"]] = record
                write_checkpoint(checkpoint_path, completed, len(rows))
                executed += 1

                if not error:
                    history.append(
                        {"user": row["question"], "assistant": answer, "at": time.time()}
                    )
                status = "OK" if not error else f"ERROR {error['type']}"
                print(
                    f"{index:03d}/440 {row['qid']} {status} {latency_ms / 1000:.2f}s "
                    f"chunks={len(chunks)} calls={record['llm_call_count']}",
                    flush=True,
                )
                if args.delay > 0:
                    time.sleep(args.delay)
    finally:
        telemetry.uninstall()

    print(f"Saved {len(completed)}/440 records to {jsonl_path}", flush=True)


if __name__ == "__main__":
    main()
