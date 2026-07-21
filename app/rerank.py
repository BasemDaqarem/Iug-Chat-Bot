"""إعادة ترتيب المقاطع بـ Jina Reranker (المرحلة 2 من خطة التحسين).

RRF يدمج ترتيبَي البحث الدلالي واللفظي جيداً لكنه لا «يفهم» هل المقطع يجيب
السؤال نفسه (تحويل داخلي أم من جامعة أخرى؟ eportal أم Moodle؟) — النماذج
المتقاطعة cross-encoders تفعل. خلف فلاغ RERANK_ENABLED (مطفأ افتراضياً حتى
يثبت القياس جدواه)، بنفس مفتاح Jina المستخدم للتضمين، و**fail-open**: أي
فشل في النداء يعيد الترتيب الأصلي كما هو — الميزة لا تُسقط البوت أبداً.
"""

import hashlib
import re
import time
from threading import Lock
from typing import List

import requests

from app import config
from app.log import get_logger
from app.retrieval import record_trace

log = get_logger("rerank")

_CIRCUIT_LOCK = Lock()
_CIRCUIT_FAILURE_COUNT = 0
_CIRCUIT_OPEN_UNTIL = 0.0


def _circuit_allows_request() -> bool:
    global _CIRCUIT_FAILURE_COUNT, _CIRCUIT_OPEN_UNTIL
    now = time.monotonic()
    with _CIRCUIT_LOCK:
        if _CIRCUIT_OPEN_UNTIL and now < _CIRCUIT_OPEN_UNTIL:
            return False
        if _CIRCUIT_OPEN_UNTIL and now >= _CIRCUIT_OPEN_UNTIL:
            _CIRCUIT_OPEN_UNTIL = 0.0
            _CIRCUIT_FAILURE_COUNT = 0
        return True


def _circuit_success() -> None:
    global _CIRCUIT_FAILURE_COUNT, _CIRCUIT_OPEN_UNTIL
    with _CIRCUIT_LOCK:
        _CIRCUIT_FAILURE_COUNT = 0
        _CIRCUIT_OPEN_UNTIL = 0.0


def _circuit_failure() -> None:
    global _CIRCUIT_FAILURE_COUNT, _CIRCUIT_OPEN_UNTIL
    with _CIRCUIT_LOCK:
        _CIRCUIT_FAILURE_COUNT += 1
        if _CIRCUIT_FAILURE_COUNT >= max(1, config.RERANK_CIRCUIT_FAILURES):
            _CIRCUIT_OPEN_UNTIL = (
                time.monotonic() + config.RERANK_CIRCUIT_COOLDOWN_SECONDS
            )

_FILE_RE = re.compile(r"^\[ملف:\s*([^\]]+)\]")


def _candidate_meta(chunk: str, index: int, score=None) -> dict:
    match = _FILE_RE.match(chunk)
    result = {
        "original_index": index,
        "chunk_sha256": hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
        "file": match.group(1) if match else None,
    }
    if isinstance(score, (int, float)):
        result["relevance_score"] = round(float(score), 6)
    return result


def rerank_with_status(
    query: str, chunks: List[str], top_n: int
) -> tuple[List[str], str]:
    """الترتيب مع حالة صريحة.

    عند فشل المزود نعيد مجموعة المرشحين الأصلية كاملة كي يقرر المنسّق كم
    يحتفظ منها؛ فالـfail-open لا ينبغي أن يخفي مستنداً صحيحاً في المرتبة 13.
    """
    if not config.RERANK_ENABLED or len(chunks) <= 1:
        return (chunks[:top_n] if top_n else chunks), "disabled"
    if not _circuit_allows_request():
        record_trace({
            "scope": "reranker",
            "strategy": "cross_encoder",
            "status": "circuit_open_fallback",
            "candidate_count": len(chunks),
            "top_n": min(top_n, len(chunks)),
        })
        return list(chunks), "circuit_open_fallback"
    started = time.perf_counter()
    try:
        resp = requests.post(
            config.RERANK_URL,
            headers={"Authorization": f"Bearer {config.EMBED_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": config.RERANK_MODEL, "query": query,
                  "documents": chunks, "top_n": min(top_n, len(chunks))},
            timeout=config.RERANK_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        valid = [
            r for r in results
            if isinstance(r.get("index"), int) and 0 <= r["index"] < len(chunks)
        ]
        ordered = [chunks[r["index"]] for r in valid]
        if ordered:
            _circuit_success()
            record_trace({
                "scope": "reranker",
                "strategy": "cross_encoder",
                "status": "applied",
                "model": config.RERANK_MODEL,
                "candidate_count": len(chunks),
                "top_n": min(top_n, len(chunks)),
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "selected": [
                    _candidate_meta(
                        chunks[r["index"]],
                        r["index"],
                        r.get("relevance_score"),
                    )
                    for r in valid
                ],
            })
            return ordered, "applied"
        log.warning("⚠️ الـ Reranker أعاد نتيجة فارغة — نبقي الترتيب الأصلي.")
        _circuit_failure()
        record_trace({
            "scope": "reranker",
            "strategy": "cross_encoder",
            "status": "empty_fallback",
            "model": config.RERANK_MODEL,
            "candidate_count": len(chunks),
            "top_n": min(top_n, len(chunks)),
            "latency_ms": round((time.perf_counter() - started) * 1000),
        })
        return list(chunks), "empty_fallback"
    except Exception as exc:
        _circuit_failure()
        log.warning("⚠️ فشل نداء الـ Reranker (%s) — نبقي الترتيب الأصلي.", exc)
        record_trace({
            "scope": "reranker",
            "strategy": "cross_encoder",
            "status": "error_fallback",
            "model": config.RERANK_MODEL,
            "candidate_count": len(chunks),
            "top_n": min(top_n, len(chunks)),
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "error_type": type(exc).__name__,
        })
        return list(chunks), "error_fallback"


def rerank(query: str, chunks: List[str], top_n: int) -> List[str]:
    """واجهة التوافق القديمة: أفضل top_n، وتفشل إلى أول top_n كما سابقاً."""
    ordered, status = rerank_with_status(query, chunks, top_n)
    if status == "applied":
        return ordered
    return ordered[:top_n] if top_n else ordered
