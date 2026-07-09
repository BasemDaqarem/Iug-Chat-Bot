"""
Main RAG corpus lifecycle: discover MongoDB collections → load documents →
build chunks + metadata → build the semantic index → search.
"""

from typing import List, Optional

import numpy as np
from pymongo.errors import PyMongoError

from app import config, db, index_store
from app.chunking import build_chunks
from app.embeddings import build_index, embed_query
from app.lexical import BM25
from app.retrieval import hybrid_rank, rank_chunks


class KnowledgeBase:

    def __init__(self):
        self._data: Optional[dict] = None             # {collection_name: [raw_docs...]}
        self._chunks: Optional[List[str]] = None      # flat list of chunk texts
        self._chunk_meta: Optional[List[dict]] = None # parallel metadata, same length/order
        self._index: Optional[np.ndarray] = None
        self._bm25: Optional[BM25] = None             # lazy lexical index
        self._bm25_for: Optional[List[str]] = None    # chunks the BM25 was built for

    # ── public accessors ──────────────────────────────────────────────────

    @property
    def data(self) -> dict:
        """{collection_name: [raw documents]} — fully dynamic, mirrors MongoDB."""
        return self._data

    @property
    def chunks(self) -> List[str]:
        return self._chunks

    @property
    def chunk_meta(self) -> List[dict]:
        return self._chunk_meta

    @property
    def index(self) -> np.ndarray:
        return self._index

    # ── lifecycle ─────────────────────────────────────────────────────────

    def load(self):
        """Load data, build chunks, build semantic index."""
        print("⏳ Discovering & loading MongoDB collections …")
        self._data = self._load_documents()
        self._chunks, self._chunk_meta = build_chunks(self._data)
        print(f"✅ Built {len(self._chunks)} chunks from {len(self._data)} collection(s).")

        print(f"⏳ Using Jina Embeddings API — model: '{config.EMBED_MODEL}' …")
        if not config.EMBED_API_KEY:
            raise RuntimeError("❌ EMBED_API_KEY غير موجود — أضفه في ملف .env")

        print("⏳ Building semantic index …")
        self._index = index_store.build_or_load("knowledge_base", self._chunks, build_index)
        print(f"✅ Semantic index ready — shape: {self._index.shape}")

    @staticmethod
    def _load_documents() -> dict:
        """
        Discover every collection in MongoDB and load ALL of its documents,
        with no assumption about field names or document shape. Adding a new
        collection to the database is enough for it to show up here — no
        code change required.
        """
        try:
            names = [
                n for n in db.list_collection_names()
                if n not in config.RAG_EXCLUDE_COLLECTIONS
            ]
            data: dict = {}
            for name in names:
                docs = list(db.get_collection(name).find({}))
                for doc in docs:
                    if "_id" in doc:
                        doc["_id"] = str(doc["_id"])
                data[name] = docs
        except PyMongoError as exc:
            raise RuntimeError(
                f"❌ MongoDB query failed: {exc}\n"
                "Check MONGO_URI in .env and Atlas network access."
            ) from exc

        total_docs = sum(len(v) for v in data.values())
        print(f"✅ Data loaded from MongoDB — {len(data)} collection(s), {total_docs} document(s) total.")
        return data

    # ── search ────────────────────────────────────────────────────────────

    def semantic_search(
        self,
        question: str,
        top_k: int = config.TOP_K,
        threshold: float = config.SIM_THRESHOLD,
    ) -> List[str]:
        """Pure dense search over the whole corpus (no access filtering) —
        kept for backward compatibility. Chat uses search_for()."""
        q_vec = embed_query(question)
        return rank_chunks(q_vec, self._chunks, self._index, top_k, threshold)

    def _ensure_bm25(self) -> BM25:
        # Rebuild only when the chunk list itself changed (identity check),
        # so injected test corpora and reloaded data both stay consistent.
        if self._bm25 is None or self._bm25_for is not self._chunks:
            self._bm25 = BM25(self._chunks or [])
            self._bm25_for = self._chunks
        return self._bm25

    def _allowed_indices(self, session_id: str) -> np.ndarray:
        """Indices of chunks visible to this session: every public chunk, plus
        the session-owner's own sensitive record. Everything else is dropped
        BEFORE ranking, so another student's private vectors are never even
        candidates — structural isolation, not post-filtering."""
        sid = str(session_id)
        meta = self._chunk_meta or []
        return np.array(
            [i for i, m in enumerate(meta)
             if (not m.get("sensitive")) or str(m.get("owner_id")) == sid],
            dtype=int,
        )

    def search_for(
        self,
        question: str,
        session_id: str,
        top_k: int = config.TOP_K,
        threshold: float = config.SIM_THRESHOLD,
    ) -> List[str]:
        """Access-filtered hybrid (dense + BM25) search — the retrieval path
        used by chat. Only public chunks and the caller's own sensitive record
        are candidates (they are the only chunks that reach the ranker)."""
        if not self._chunks:
            return []

        allowed = self._allowed_indices(session_id)
        if allowed.size == 0:
            return []

        q_vec = embed_query(question)
        dense = (self._index @ q_vec).flatten()[allowed]

        if config.HYBRID_ENABLED:
            lexical = self._ensure_bm25().scores(question)[allowed]
        else:
            lexical = np.zeros(allowed.size, dtype=np.float32)

        sub_chunks = [self._chunks[i] for i in allowed]
        return hybrid_rank(sub_chunks, dense, lexical, top_k, threshold)
