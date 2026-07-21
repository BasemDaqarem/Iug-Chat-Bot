"""
Ranking over a normalized index (see app.embeddings).

Two strategies live here:
  • rank_chunks   — pure dense cosine similarity (unchanged, still used as a
                    fallback and by the corpus-equivalence tests).
  • hybrid_rank   — dense + lexical BM25 fused with Reciprocal Rank Fusion,
                    the default retrieval path.
"""

import hashlib
import re
from contextvars import ContextVar, Token
from typing import Any, List, Optional

import numpy as np

from app import config

_TRACE: ContextVar[Optional[list[dict[str, Any]]]] = ContextVar(
    "retrieval_trace", default=None
)
_FILE_RE = re.compile(r"^\[ملف:\s*([^\]]+)\]")


def begin_trace() -> Token:
    """Begin request-local retrieval diagnostics.

    ContextVar keeps this safe when the production server handles concurrent
    requests; callers that do not opt in pay only one cheap `get()`.
    """
    return _TRACE.set([])


def end_trace(token: Token) -> list[dict[str, Any]]:
    events = list(_TRACE.get() or [])
    _TRACE.reset(token)
    return events


def record_trace(event: dict[str, Any]) -> None:
    """Append non-secret diagnostics to the active request trace, if any."""
    sink = _TRACE.get()
    if sink is not None:
        sink.append(event)


def _trace_hybrid_rank(
    chunks: List[str],
    dense_scores: np.ndarray,
    lexical_scores: np.ndarray,
    order: List[int],
    top_k: int,
    threshold: float,
    rrf_k: int,
    trace_meta: Optional[dict[str, Any]],
    tie_keys: Optional[list[tuple[str, str, int]]] = None,
) -> None:
    sink = _TRACE.get()
    if sink is None:
        return
    dense_order = _score_order(dense_scores, tie_keys)
    dense_ranks = {int(idx): rank + 1 for rank, idx in enumerate(dense_order)}
    lexical_order = _score_order(lexical_scores, tie_keys)
    lexical_ranks = {
        int(idx): rank + 1
        for rank, idx in enumerate(lexical_order)
        if lexical_scores[int(idx)] > 0
    }
    candidates = []
    for fused_rank, idx in enumerate(order[: max(top_k, 30)], 1):
        text = chunks[idx]
        dense_rank = dense_ranks[idx]
        lexical_rank = lexical_ranks.get(idx)
        rrf_score = 1.0 / (rrf_k + dense_rank)
        if lexical_rank is not None:
            rrf_score += 1.0 / (rrf_k + lexical_rank)
        file_match = _FILE_RE.match(text)
        selected = fused_rank <= top_k and (
            float(dense_scores[idx]) >= threshold
            or float(lexical_scores[idx]) > 0
        )
        candidates.append(
            {
                "chunk_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "file": file_match.group(1) if file_match else None,
                "preview": text[:240],
                "fused_rank": fused_rank,
                "rrf_score": round(rrf_score, 8),
                "dense_rank": dense_rank,
                "dense_cosine": round(float(dense_scores[idx]), 6),
                "dense_passed_threshold": bool(dense_scores[idx] >= threshold),
                "lexical_rank": lexical_rank,
                "bm25_score": round(float(lexical_scores[idx]), 6),
                "selected_by_ranker": selected,
                "selection_channels": [
                    channel
                    for channel, present in (
                        ("dense", dense_scores[idx] >= threshold),
                        ("bm25", lexical_scores[idx] > 0),
                    )
                    if present
                ],
            }
        )
    sink.append(
        {
            **(trace_meta or {}),
            "strategy": "hybrid_dense_bm25_rrf",
            "candidate_count": len(chunks),
            "top_k": top_k,
            "dense_threshold": threshold,
            "rrf_k": rrf_k,
            "candidates": candidates,
        }
    )


def rank_chunks(
    q_vec: np.ndarray,
    chunks: List[str],
    index: Optional[np.ndarray],
    top_k: int,
    threshold: float,
) -> List[str]:
    """Shared ranking, used by the main index and every per-uploaded-file
    index. Falls back to the single best chunk when nothing clears the
    threshold, so the LLM never gets an empty context from a non-empty
    corpus."""
    if index is None or len(chunks) == 0 or getattr(index, "size", 0) == 0:
        return []

    scores = (index @ q_vec).flatten()
    ranked = _score_order(scores, _candidate_tie_keys(chunks))

    results = []
    for idx in ranked[:top_k]:
        if float(scores[idx]) >= threshold:
            results.append(chunks[int(idx)])

    if not results and len(ranked):
        results.append(chunks[int(ranked[0])])

    return results


def rrf_order(
    dense_scores: np.ndarray,
    lexical_scores: np.ndarray,
    rrf_k: int = None,
    tie_keys: Optional[list[tuple[str, str, int]]] = None,
) -> List[int]:
    """Fuse a dense ranking and a lexical ranking into one ordered list of
    document indices via Reciprocal Rank Fusion:  score(d) = Σ 1/(k + rank).
    A document only earns lexical credit when it actually matched a query
    term (lexical score > 0), so BM25 never reshuffles the dense order on
    queries it has nothing to say about."""
    k = config.RRF_K if rrf_k is None else rrf_k
    fused: dict = {}

    dense_order = _score_order(dense_scores, tie_keys)
    for rank, idx in enumerate(dense_order):
        fused[int(idx)] = fused.get(int(idx), 0.0) + 1.0 / (k + rank + 1)

    lexical_order = _score_order(lexical_scores, tie_keys)
    for rank, idx in enumerate(lexical_order):
        if lexical_scores[int(idx)] > 0:
            fused[int(idx)] = fused.get(int(idx), 0.0) + 1.0 / (k + rank + 1)

    if tie_keys is None:
        tie_keys = [("", "", index) for index in range(len(dense_scores))]
    return sorted(fused, key=lambda i: (-fused[i], tie_keys[i]))


def hybrid_rank(
    chunks: List[str],
    dense_scores: np.ndarray,
    lexical_scores: np.ndarray,
    top_k: int,
    threshold: float,
    rrf_k: int = None,
    trace_meta: Optional[dict[str, Any]] = None,
) -> List[str]:
    """Return the top-K chunk texts by fused dense+lexical relevance.

    A fused candidate is kept only if it is *confident* — either its dense
    cosine clears `threshold` OR it had a real lexical match — which trims the
    weak, hallucination-prone tail. If nothing is confident we fall back to
    the single best fused candidate, so a non-empty corpus never yields an
    empty context.
    """
    n = len(chunks)
    if n == 0 or dense_scores is None or len(dense_scores) != n:
        return []

    effective_rrf_k = config.RRF_K if rrf_k is None else rrf_k
    tie_keys = _candidate_tie_keys(chunks)
    order = rrf_order(
        dense_scores, lexical_scores, effective_rrf_k, tie_keys=tie_keys
    )
    _trace_hybrid_rank(
        chunks,
        dense_scores,
        lexical_scores,
        order,
        top_k,
        threshold,
        effective_rrf_k,
        trace_meta,
        tie_keys,
    )

    results = []
    for idx in order[:top_k]:
        if dense_scores[idx] >= threshold or lexical_scores[idx] > 0:
            results.append(chunks[idx])

    if not results and order:
        results.append(chunks[order[0]])

    return results

# ── Candidate-rich interface (pipeline v2) ────────────────────────────────
from dataclasses import asdict, dataclass, field


@dataclass(slots=True)
class RetrievalCandidate:
    text: str
    original_index: int
    dense_score: float
    bm25_score: float
    rrf_score: float
    fused_rank: int
    source: str | None = None
    chunk_id: str | None = None
    parent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_metadata(self) -> dict[str, Any]:
        candidate_id = self.chunk_id or hashlib.sha256(
            self.text.encode("utf-8")
        ).hexdigest()
        result = asdict(self)
        result["candidate_id"] = candidate_id
        result["preview"] = self.text[:240]
        result.pop("text", None)
        return result


_CHUNK_META_RE = re.compile(
    r"^\[ملف:\s*([^\]]+)\]\n"
    r"\[chunk_id:\s*([^|\]]+)\|\s*parent_id:\s*([^|\]]+)"
)


def _candidate_tie_keys(chunks: List[str]) -> list[tuple[str, str, int]]:
    """Stable source/chunk tie-breakers independent of container ordering."""
    keys = []
    for index, text in enumerate(chunks):
        match = _CHUNK_META_RE.match(text)
        file_match = _FILE_RE.match(text)
        source = (
            match.group(1).strip()
            if match else file_match.group(1).strip() if file_match else ""
        )
        chunk_id = (
            match.group(2).strip()
            if match
            else hashlib.sha256(text.encode("utf-8")).hexdigest()
        )
        keys.append((source, chunk_id, index))
    return keys


def _score_order(
    scores: np.ndarray,
    tie_keys: Optional[list[tuple[str, str, int]]] = None,
) -> list[int]:
    if tie_keys is None:
        tie_keys = [("", "", index) for index in range(len(scores))]
    return sorted(
        range(len(scores)),
        key=lambda index: (-float(scores[index]), tie_keys[index]),
    )


def hybrid_candidates(
    chunks: List[str],
    dense_scores: np.ndarray,
    lexical_scores: np.ndarray,
    top_k: int,
    threshold: float,
    rrf_k: int | None = None,
) -> List[RetrievalCandidate]:
    """Return score-preserving candidates; ``hybrid_rank`` remains the adapter.

    Unlike the old text-only return value, this allows routing, reranking,
    coverage checks, and diagnostics to reason about confidence without
    re-running retrieval.
    """
    n = len(chunks)
    if n == 0 or dense_scores is None or len(dense_scores) != n:
        return []
    k = config.RRF_K if rrf_k is None else rrf_k
    tie_keys = _candidate_tie_keys(chunks)
    order = rrf_order(dense_scores, lexical_scores, k, tie_keys=tie_keys)
    dense_order = _score_order(dense_scores, tie_keys)
    dense_ranks = {int(idx): rank + 1 for rank, idx in enumerate(dense_order)}
    lexical_order = _score_order(lexical_scores, tie_keys)
    lexical_ranks = {
        int(idx): rank + 1
        for rank, idx in enumerate(lexical_order)
        if lexical_scores[int(idx)] > 0
    }
    result: List[RetrievalCandidate] = []
    for fused_rank, idx in enumerate(order, 1):
        dense_value = float(dense_scores[idx])
        lexical_value = float(lexical_scores[idx])
        if dense_value < threshold and lexical_value <= 0:
            continue
        dense_rank = dense_ranks[idx]
        lexical_rank = lexical_ranks.get(idx)
        score = 1.0 / (k + dense_rank)
        if lexical_rank is not None:
            score += 1.0 / (k + lexical_rank)
        text = chunks[idx]
        match = _CHUNK_META_RE.match(text)
        file_match = _FILE_RE.match(text)
        result.append(RetrievalCandidate(
            text=text,
            original_index=int(idx),
            dense_score=dense_value,
            bm25_score=lexical_value,
            rrf_score=score,
            fused_rank=fused_rank,
            source=(match.group(1).strip() if match else (
                file_match.group(1) if file_match else None
            )),
            chunk_id=match.group(2).strip() if match else None,
            parent_id=match.group(3).strip() if match else None,
            metadata={
                "dense_passed_threshold": dense_value >= threshold,
                "lexical_matched": lexical_value > 0,
            },
        ))
        if len(result) >= top_k:
            break
    # Pipeline v2 deliberately returns no candidate when neither semantic
    # nor lexical confidence passes.  The evidence contract then tells the LLM
    # that the corpus does not support a factual answer instead of forcing an
    # unrelated nearest neighbour into the prompt.
    return result
