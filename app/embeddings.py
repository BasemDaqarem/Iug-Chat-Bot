"""
Embeddings client (Jina-compatible endpoint) + semantic-index construction.

An "index" here is a float32 matrix of L2-normalized row vectors, so
cosine similarity against a normalized query reduces to a dot product.
"""

import hashlib
import time
from typing import List

import numpy as np
import requests

from app import config
from app.cache import TTLCache
from app.errors import ConfigurationError, UpstreamServiceError
from app.http_util import error_detail, provider_label, status_hint
from app.log import get_logger

log = get_logger("embeddings")


def _provider() -> str:
    return provider_label(config.EMBED_API_URL, "خدمة التضمين")

# question text → its L2-normalized query vector. Deterministic for a fixed
# embedding model, and the value is a vector (never an answer or a record), so
# this is safe to reuse across users. Keyed by a hash so raw text is not stored.
_query_cache = TTLCache("query_embeddings", config.CACHE_EMBED_MAXSIZE, config.CACHE_EMBED_TTL)


def query_cache_stats() -> dict:
    return _query_cache.stats()


def reset_query_cache() -> None:
    _query_cache.clear()


def embed_texts(texts: List[str]) -> np.ndarray:
    provider = _provider()
    if not config.EMBED_API_KEY:
        raise ConfigurationError("❌ EMBED_API_KEY غير موجود في ملف .env — أضف مفتاح خدمة التضمين (embeddings).")
    if not config.EMBED_API_URL:
        raise ConfigurationError("❌ EMBED_API_URL غير موجود في ملف .env — أضف رابط خدمة التضمين.")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.EMBED_API_KEY}",
    }
    data = {"model": config.EMBED_MODEL, "input": texts}
    try:
        resp = requests.post(config.EMBED_API_URL, headers=headers, json=data, timeout=120)
    except requests.exceptions.ConnectionError:
        raise UpstreamServiceError(
            f"❌ تعذّر الاتصال بـ {provider} — تحقّق من الإنترنت ومن EMBED_API_URL في .env."
        )
    except requests.exceptions.Timeout:
        raise UpstreamServiceError(f"❌ {provider} استغرق وقتاً طويلاً في التضمين — حاول مرة أخرى.")

    if resp.status_code >= 400:
        raise UpstreamServiceError(
            f"❌ {provider} رفض طلب التضمين (HTTP {resp.status_code}): "
            f"{error_detail(resp)}{status_hint(resp.status_code)}",
            details={"provider": provider, "status": resp.status_code},
        )

    try:
        vectors = [item["embedding"] for item in resp.json()["data"]]
    except (KeyError, ValueError, TypeError) as exc:
        raise UpstreamServiceError(f"❌ رد غير متوقّع من {provider} (لا يحتوي متجهات صالحة): {exc}")
    return np.array(vectors, dtype=np.float32)


def _estimated_tokens(texts: List[str]) -> int:
    """تقدير توكنات Jina للنص العربي — مقيس حياً: 177 ألف حرف ≈ 135 ألف توكن."""
    return int(sum(len(t) for t in texts) * 0.8)


def _is_rate_limited(exc: UpstreamServiceError) -> bool:
    details = getattr(exc, "details", None) or {}
    return details.get("status") == 429


def build_index(chunks: List[str]) -> np.ndarray:
    """Embed all chunks in batches and L2-normalize each row.

    الملفات الكبيرة (مئات المقاطع) كانت تنفجر على حدّ Jina المجاني
    (100 ألف توكن/دقيقة → HTTP 429 حتمي، ثبت حياً مع 520 مقطعاً): نكبح
    الإرسال تحت ميزانية `EMBED_TPM_BUDGET` توكن/دقيقة، وعند 429 رغم ذلك
    ننتظر نافذة كاملة ونعيد الدفعة بدل إفشال الفهرسة كلها.
    """
    if not chunks:
        return np.array([], dtype=np.float32)
    batch_size = config.EMBED_BATCH_SIZE
    budget = config.EMBED_TPM_BUDGET
    window_start = time.monotonic()
    window_tokens = 0
    all_embeddings = []
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        estimated = _estimated_tokens(batch)
        if budget and window_tokens and window_tokens + estimated > budget:
            wait = 62 - (time.monotonic() - window_start)
            if wait > 0:
                log.info(
                    "⏳ كبح التضمين: بلغنا ~%d توكن في النافذة — انتظار %.0f ثانية "
                    "(دفعة %d/%d).",
                    window_tokens, wait, i // batch_size + 1,
                    (len(chunks) + batch_size - 1) // batch_size,
                )
                time.sleep(wait)
            window_start = time.monotonic()
            window_tokens = 0
        window_tokens += estimated
        log.debug("Embedding batch %d (%d chunks) …", i // batch_size + 1, len(batch))
        for attempt in range(3):
            try:
                all_embeddings.append(embed_texts(batch))
                break
            except UpstreamServiceError as exc:
                if attempt < 2 and _is_rate_limited(exc):
                    log.warning(
                        "⚠️ حدّ معدل التضمين (429) — انتظار 65 ثانية ثم إعادة "
                        "الدفعة (محاولة %d/3).", attempt + 2,
                    )
                    time.sleep(65)
                    window_start = time.monotonic()
                    window_tokens = estimated
                else:
                    raise
    result = np.vstack(all_embeddings) if all_embeddings else np.array([], dtype=np.float32)
    norms = np.linalg.norm(result, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    return result / norms


def embed_query(question: str) -> np.ndarray:
    """Embed a single query and L2-normalize it into a column vector.
    Result is cached per (model, question) so repeated questions skip the
    Jina API call entirely."""
    key = None
    if config.CACHE_ENABLED:
        key = hashlib.sha256(f"{config.EMBED_MODEL}\x00{question}".encode("utf-8")).hexdigest()
        cached = _query_cache.get(key)
        if cached is not None:
            return cached

    q_arr = embed_texts([question])
    norm = np.linalg.norm(q_arr)
    vec = (q_arr / norm if norm != 0 else q_arr).T

    if key is not None:
        _query_cache.set(key, vec)
    return vec
