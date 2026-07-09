"""
Ranking over a normalized index (see app.embeddings).

Two strategies live here:
  • rank_chunks   — pure dense cosine similarity (unchanged, still used as a
                    fallback and by the corpus-equivalence tests).
  • hybrid_rank   — dense + lexical BM25 fused with Reciprocal Rank Fusion,
                    the default retrieval path.
"""

from typing import List, Optional

import numpy as np

from app import config


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
    ranked = np.argsort(scores)[::-1]

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
) -> List[int]:
    """Fuse a dense ranking and a lexical ranking into one ordered list of
    document indices via Reciprocal Rank Fusion:  score(d) = Σ 1/(k + rank).
    A document only earns lexical credit when it actually matched a query
    term (lexical score > 0), so BM25 never reshuffles the dense order on
    queries it has nothing to say about."""
    k = config.RRF_K if rrf_k is None else rrf_k
    fused: dict = {}

    dense_order = np.argsort(dense_scores)[::-1]
    for rank, idx in enumerate(dense_order):
        fused[int(idx)] = fused.get(int(idx), 0.0) + 1.0 / (k + rank + 1)

    lexical_order = np.argsort(lexical_scores)[::-1]
    for rank, idx in enumerate(lexical_order):
        if lexical_scores[int(idx)] > 0:
            fused[int(idx)] = fused.get(int(idx), 0.0) + 1.0 / (k + rank + 1)

    return sorted(fused, key=lambda i: fused[i], reverse=True)


def hybrid_rank(
    chunks: List[str],
    dense_scores: np.ndarray,
    lexical_scores: np.ndarray,
    top_k: int,
    threshold: float,
    rrf_k: int = None,
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

    order = rrf_order(dense_scores, lexical_scores, rrf_k)

    results = []
    for idx in order[:top_k]:
        if dense_scores[idx] >= threshold or lexical_scores[idx] > 0:
            results.append(chunks[idx])

    if not results and order:
        results.append(chunks[order[0]])

    return results
