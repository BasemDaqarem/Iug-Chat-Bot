"""
On-disk cache for embedding indexes.

Embeddings are deterministic for a fixed (model, chunk-text) pair, so once a
chunk's vector is computed it never needs recomputing until the chunk itself
changes. This module persists an index next to a fingerprint of the exact
(model + chunk texts) it was built from; on load it recomputes that
fingerprint and only reuses the cache on an exact match — otherwise it
reports a miss and the caller rebuilds. This makes cold start near-instant
and stops re-billing the embeddings API on every run.
"""

import hashlib
import json
import os
from typing import List, Optional

import numpy as np

from app import config
from app.log import get_logger

log = get_logger("index_store")


def _paths(name: str):
    safe = hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]
    base = os.path.join(config.INDEX_CACHE_DIR, safe)
    return base + ".npy", base + ".meta.json"


def _fingerprint(chunks: List[str], model: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    for c in chunks:
        h.update(b"\x00")
        h.update(c.encode("utf-8"))
    return h.hexdigest()


def load(name: str, chunks: List[str], model: str) -> Optional[np.ndarray]:
    """Return the cached index for `name` iff it was built from exactly these
    chunks with this model; otherwise None (cache miss → caller rebuilds)."""
    npy, meta = _paths(name)
    if not (os.path.exists(npy) and os.path.exists(meta)):
        return None
    try:
        with open(meta, encoding="utf-8") as f:
            info = json.load(f)
        if info.get("fingerprint") != _fingerprint(chunks, model):
            return None
        arr = np.load(npy)
        if len(arr) != len(chunks):
            return None
        return arr
    except Exception:
        return None  # any corruption → treat as a miss, rebuild cleanly


def save(name: str, chunks: List[str], index: np.ndarray, model: str) -> None:
    try:
        os.makedirs(config.INDEX_CACHE_DIR, exist_ok=True)
        npy, meta = _paths(name)
        np.save(npy, index)
        with open(meta, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "name": name,
                    "model": model,
                    "count": len(chunks),
                    "fingerprint": _fingerprint(chunks, model),
                },
                f,
                ensure_ascii=False,
            )
    except Exception as exc:
        log.warning("⚠️  تعذّر حفظ فهرس '%s' على القرص: %s", name, exc)


def build_or_load(name: str, chunks: List[str], build_fn) -> np.ndarray:
    """Load `name`'s index from cache, or build it via build_fn(chunks) and
    cache the result. `build_fn` is the (expensive) embeddings call."""
    model = config.EMBED_MODEL
    cached = load(name, chunks, model)
    if cached is not None:
        log.info("✅ فهرس '%s' مُحمّل من الكاش (%d متجه) — بلا إعادة حساب.", name, len(cached))
        return cached
    index = build_fn(chunks)
    save(name, chunks, index, model)
    return index
