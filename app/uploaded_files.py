"""
Uploaded-file corpora: each uploaded JSON file lives in its own MongoDB
collection and gets its own chunks + dedicated embeddings index, so chat
can run semantic search over just that file (or across all of them) instead
of sending whole files to the LLM.
"""

from typing import List, Optional

import numpy as np

from app import config, index_store
from app.chunking import build_uploaded_chunks
from app.db import (
    drop_uploaded_collection,
    get_uploaded_collection,
    list_uploaded_collections,
)
from app.embeddings import build_index, embed_query
from app.lexical import BM25
from app.log import get_logger
from app.retrieval import hybrid_rank

log = get_logger("uploaded_files")


class UploadedFilesStore:

    def __init__(self):
        self._chunks: dict = {}   # {collection_name: [chunk_text, ...]}
        self._indexes: dict = {}  # {collection_name: np.ndarray}
        self._bm25: dict = {}     # {collection_name: (BM25, chunks_ref)} — lazy

    # ── lifecycle ─────────────────────────────────────────────────────────

    def load_all(self):
        try:
            collections = list_uploaded_collections()
            if not collections:
                log.info("ℹ️  No uploaded files found in MongoDB.")
                return
            for col_name in collections:
                self.load_one(col_name)
            log.info("✅ Loaded %d uploaded file(s).", len(self._chunks))
        except Exception as exc:
            log.warning("⚠️  Could not load uploaded files: %s", exc)

    def load_one(self, collection_name: str):
        """
        Load one uploaded file's documents, build its chunks, AND build a
        dedicated embeddings index for it, so search_one() can run semantic
        search over just this file instead of sending it whole.
        """
        col = get_uploaded_collection(collection_name)
        docs = list(col.find({}))
        if not docs:
            self._chunks.pop(collection_name, None)
            self._indexes.pop(collection_name, None)
            self._bm25.pop(collection_name, None)
            return

        chunks = build_uploaded_chunks(docs, collection_name)
        if not chunks:
            return

        self._chunks[collection_name] = chunks
        try:
            self._indexes[collection_name] = index_store.build_or_load(
                f"uploaded::{collection_name}", chunks, build_index
            )
            log.info("✅ Indexed uploaded file '%s' (%d chunks).", collection_name, len(chunks))
        except Exception as exc:
            # Keep the chunks even if embeddings fail — search falls back to
            # a bounded slice — but drop any stale index.
            self._indexes.pop(collection_name, None)
            log.warning("⚠️  Failed to build embeddings for '%s': %s", collection_name, exc)

    def upload_json(self, collection_name: str, json_data) -> dict:
        col = get_uploaded_collection(collection_name)
        if isinstance(json_data, dict):
            json_data = [json_data]
        if not isinstance(json_data, list):
            raise ValueError("محتوى الملف يجب أن يكون JSON object أو array.")
        cleaned = []
        for item in json_data:
            if isinstance(item, dict):
                item.pop("_id", None)
                cleaned.append(item)
            else:
                cleaned.append({"value": item})
        col.drop()
        if cleaned:
            col.insert_many(cleaned)
        # Rebuilds chunks + embeddings + index for this file.
        self.load_one(collection_name)
        return {"inserted": len(cleaned), "collection": collection_name}

    def delete(self, collection_name: str) -> bool:
        drop_uploaded_collection(collection_name)
        self._chunks.pop(collection_name, None)
        self._indexes.pop(collection_name, None)
        self._bm25.pop(collection_name, None)
        return True

    def reload(self, collection_name: str) -> bool:
        try:
            self.load_one(collection_name)
            return True
        except Exception:
            return False

    # ── state queries ─────────────────────────────────────────────────────

    def is_empty(self) -> bool:
        return not self._chunks

    def has(self, collection_name: str) -> bool:
        return collection_name in self._chunks

    def chunks_of(self, collection_name: str) -> List[str]:
        return self._chunks.get(collection_name, [])

    def list_files(self) -> List[dict]:
        return [
            {
                "collection":   name,
                "chunks_count": len(chunks),
                "indexed":      name in self._indexes,
            }
            for name, chunks in self._chunks.items()
        ]

    def _usable_index(self, collection_name: str) -> Optional[np.ndarray]:
        """The file's index, only if it exists and still matches its chunks."""
        chunks = self._chunks.get(collection_name)
        index = self._indexes.get(collection_name)
        if (
            index is not None
            and getattr(index, "size", 0)
            and chunks is not None
            and len(index) == len(chunks)
        ):
            return index
        return None

    def _lexical_scores(self, collection_name: str, chunks: List[str], question: str) -> np.ndarray:
        """BM25 scores for one file, with a lazily-built (and self-refreshing)
        BM25 index. Returns zeros when hybrid search is disabled."""
        if not config.HYBRID_ENABLED:
            return np.zeros(len(chunks), dtype=np.float32)
        cached = self._bm25.get(collection_name)
        if cached is None or cached[1] is not chunks:
            cached = (BM25(chunks), chunks)
            self._bm25[collection_name] = cached
        return cached[0].scores(question)

    # ── search ────────────────────────────────────────────────────────────

    def search_one(
        self,
        question: str,
        collection_name: str,
        top_k: int = config.TOP_K,
        threshold: float = config.SIM_THRESHOLD,
    ) -> List[str]:
        all_chunks = self._chunks[collection_name]
        index = self._usable_index(collection_name)
        if index is not None:
            # Normal path: hybrid (dense + lexical) search restricted to THIS
            # file — the LLM only ever sees the top-K relevant chunks.
            q_vec = embed_query(question)
            dense = (index @ q_vec).flatten()
            lexical = self._lexical_scores(collection_name, all_chunks, question)
            return hybrid_rank(all_chunks, dense, lexical, top_k, threshold)
        # Degraded fallback (e.g. embeddings API was unreachable when the
        # file was indexed): bound the payload instead of sending everything.
        return all_chunks[:top_k]

    def search_all(
        self,
        question: str,
        top_k: int = config.TOP_K,
        threshold: float = config.SIM_THRESHOLD,
    ) -> List[str]:
        """
        Hybrid search across ALL currently uploaded files, merged into a
        single global ranking — the LLM only ever sees the best top-K chunks
        overall, regardless of which file they came from. Each chunk already
        carries a "[ملف: <اسم الملف>]" header (added in build_uploaded_chunks),
        so both the LLM and the caller can tell which file context came from.
        """
        q_vec = embed_query(question)

        # ── Pool candidates from every usably-indexed file into flat arrays ─
        pool_chunks: List[str] = []
        pool_dense: List[np.ndarray] = []
        pool_lexical: List[np.ndarray] = []
        for collection_name, chunks in self._chunks.items():
            index = self._usable_index(collection_name)
            if index is None:
                continue  # this file has no usable embeddings yet — skip it
            pool_chunks.extend(chunks)
            pool_dense.append((index @ q_vec).flatten())
            pool_lexical.append(self._lexical_scores(collection_name, chunks, question))

        if pool_chunks:
            dense = np.concatenate(pool_dense)
            lexical = np.concatenate(pool_lexical)
            return hybrid_rank(pool_chunks, dense, lexical, top_k, threshold)

        # Degraded fallback: no file has a usable index yet — take a
        # bounded sample across files instead of dumping everything.
        relevant_chunks: List[str] = []
        per_file_quota = max(1, top_k // max(1, len(self._chunks)))
        for chunks in self._chunks.values():
            relevant_chunks.extend(chunks[:per_file_quota])
        return relevant_chunks[:top_k]
