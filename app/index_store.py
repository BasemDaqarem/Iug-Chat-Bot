"""
Persistence/cache for embedding indexes, with two interchangeable backends
(config.INDEX_BACKEND):

  • "disk"  → .npy + .meta.json files under INDEX_CACHE_DIR (great locally).
  • "mongo" → an `embedding_index` collection (survives ephemeral disks, e.g.
              Render redeploys, so Jina is not re-billed on every boot).

Both key an index by a fingerprint of the exact (model + chunk texts) it was
built from; on load the fingerprint is recomputed and the cache is reused only
on an exact match — otherwise it's a miss and the caller rebuilds.
"""

import hashlib
import io
import json
import os
from typing import List, Optional

import numpy as np

from app import config
from app.log import get_logger

log = get_logger("index_store")

INDEX_COLLECTION = "embedding_index"


def _fingerprint(chunks: List[str], model: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    for c in chunks:
        h.update(b"\x00")
        h.update(c.encode("utf-8"))
    return h.hexdigest()


def _to_bytes(index: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, index)
    return buf.getvalue()


def _from_bytes(blob: bytes) -> np.ndarray:
    return np.load(io.BytesIO(blob))


# ── disk backend ──────────────────────────────────────────────────────────

def _disk_paths(name: str):
    safe = hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]
    base = os.path.join(config.INDEX_CACHE_DIR, safe)
    return base + ".npy", base + ".meta.json"


def _disk_load(name: str, chunks: List[str], model: str) -> Optional[np.ndarray]:
    npy, meta = _disk_paths(name)
    if not (os.path.exists(npy) and os.path.exists(meta)):
        return None
    try:
        with open(meta, encoding="utf-8") as f:
            info = json.load(f)
        if info.get("fingerprint") != _fingerprint(chunks, model):
            return None
        arr = np.load(npy)
        return arr if len(arr) == len(chunks) else None
    except Exception:
        return None


def _disk_save(name: str, chunks: List[str], index: np.ndarray, model: str) -> None:
    os.makedirs(config.INDEX_CACHE_DIR, exist_ok=True)
    npy, meta = _disk_paths(name)
    np.save(npy, index)
    with open(meta, "w", encoding="utf-8") as f:
        json.dump({"name": name, "model": model, "count": len(chunks),
                   "fingerprint": _fingerprint(chunks, model)}, f, ensure_ascii=False)


# ── mongo backend ─────────────────────────────────────────────────────────

def _index_col():
    from app import db  # lazy import
    return db.get_collection(INDEX_COLLECTION)


def _mongo_load(name: str, chunks: List[str], model: str) -> Optional[np.ndarray]:
    try:
        doc = _index_col().find_one({"_id": name})
        if not doc or doc.get("fingerprint") != _fingerprint(chunks, model):
            return None
        arr = _from_bytes(doc["npy"])
        return arr if len(arr) == len(chunks) else None
    except Exception:
        return None


def _mongo_save(name: str, chunks: List[str], index: np.ndarray, model: str) -> None:
    _index_col().update_one(
        {"_id": name},
        {"$set": {
            "model": model,
            "count": len(chunks),
            "fingerprint": _fingerprint(chunks, model),
            "npy": _to_bytes(index),
        }},
        upsert=True,
    )


# ── dispatch ──────────────────────────────────────────────────────────────

def load(name: str, chunks: List[str], model: str) -> Optional[np.ndarray]:
    if config.INDEX_BACKEND == "mongo":
        return _mongo_load(name, chunks, model)
    return _disk_load(name, chunks, model)


def save(name: str, chunks: List[str], index: np.ndarray, model: str) -> None:
    try:
        if config.INDEX_BACKEND == "mongo":
            _mongo_save(name, chunks, index, model)
        else:
            _disk_save(name, chunks, index, model)
    except Exception as exc:
        log.warning("⚠️  تعذّر حفظ فهرس '%s' (%s): %s", name, config.INDEX_BACKEND, exc)


def delete(name: str) -> None:
    """Purge `name`'s persisted index from BOTH backends — called when its
    source file is deleted, so no orphaned embedding blob outlives the content
    (storage leak + a lingering vector representation of deleted data)."""
    npy, meta = _disk_paths(name)
    for path in (npy, meta):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            log.warning("⚠️  تعذّر حذف ملف فهرس '%s': %s", path, exc)
    try:
        _index_col().delete_one({"_id": name})
    except Exception as exc:
        log.warning("⚠️  تعذّر حذف فهرس '%s' من Mongo: %s", name, exc)


def build_or_load(name: str, chunks: List[str], build_fn) -> np.ndarray:
    """Load `name`'s index from the configured backend, or build it via
    build_fn(chunks) (the expensive embeddings call) and persist the result."""
    model = config.EMBED_MODEL
    cached = load(name, chunks, model)
    if cached is not None:
        log.info("✅ فهرس '%s' مُحمّل من الكاش (%d متجه) — بلا إعادة حساب.", name, len(cached))
        return cached
    index = build_fn(chunks)
    save(name, chunks, index, model)
    return index
