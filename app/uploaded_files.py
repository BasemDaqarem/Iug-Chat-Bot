"""
Uploaded-file corpora: each uploaded JSON file lives in its own MongoDB
collection and gets its own chunks + dedicated embeddings index, so chat
can run semantic search over just that file (or across all of them) instead
of sending whole files to the LLM.
"""

from typing import List, Optional

import numpy as np

from app import config, index_store
from app.admissions import AdmissionCatalog, AdmissionResolution
from app.chunking import (
    ChunkRecord,
    build_contextual_uploaded_chunk_records,
    build_uploaded_chunks_with_doc_indexes,
)
from app.db import (
    drop_uploaded_collection,
    get_uploaded_collection,
    list_uploaded_collections,
)
from app.embeddings import build_index, embed_query
from app.lexical import BM25
from app.log import get_logger
from app.retrieval import RetrievalCandidate, hybrid_candidates, hybrid_rank, record_trace
from app.text_norm import normalize_arabic

log = get_logger("uploaded_files")


def _index_name(collection_name: str, *, version: str | None = None) -> str:
    """The ONE key an uploaded file's persisted embedding index lives under
    (disk and Mongo). Build and delete must both use it — a bare name here
    once left orphaned embeddings behind after file deletion."""
    selected = version or ("v2" if config.HIERARCHICAL_CHUNKING_ENABLED else "v1")
    return f"uploaded::{selected}::{collection_name}"


class UploadedFilesStore:

    def __init__(self):
        self._chunks: dict = {}   # {collection_name: [chunk_text, ...]}
        self._indexes: dict = {}  # {collection_name: np.ndarray}
        self._bm25: dict = {}     # {collection_name: (BM25, chunks_ref)} — lazy
        self._chunk_doc_indexes: dict = {}  # {collection_name: [raw_doc_index, ...]}
        self._chunk_records: dict[str, list[ChunkRecord]] = {}
        self._admissions = AdmissionCatalog()

    # ── lifecycle ─────────────────────────────────────────────────────────

    def load_all(self):
        try:
            collections = list_uploaded_collections()
            if not collections:
                log.info("ℹ️  No uploaded files found in MongoDB.")
                return
            for col_name in collections:
                self.load_one(col_name, rebuild_admissions=False)
            self._rebuild_admissions()
            log.info("✅ Loaded %d uploaded file(s).", len(self._chunks))
        except Exception as exc:
            log.warning("⚠️  Could not load uploaded files: %s", exc)

    def _rebuild_admissions(self) -> None:
        def vector_for_fact(fact):
            index = self._usable_index(fact.source)
            doc_indexes = self._chunk_doc_indexes.get(fact.source, [])
            if index is None:
                return None
            try:
                chunk_index = doc_indexes.index(fact.doc_index)
            except ValueError:
                return None
            return index[chunk_index]

        try:
            self._admissions.rebuild(vector_for_fact)
            log.info(
                "✅ Built structured admission catalog (%d facts).",
                len(self._admissions.facts),
            )
        except Exception as exc:
            log.warning("⚠️  Could not build structured admission catalog: %s", exc)

    def load_one(self, collection_name: str, rebuild_admissions: bool = True):
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
            self._chunk_doc_indexes.pop(collection_name, None)
            self._chunk_records.pop(collection_name, None)
            self._admissions.remove_collection(collection_name, rebuild=False)
            if rebuild_admissions:
                self._rebuild_admissions()
            return

        self._admissions.replace_collection(collection_name, docs, rebuild=False)

        if config.HIERARCHICAL_CHUNKING_ENABLED:
            records = build_contextual_uploaded_chunk_records(docs, collection_name)
            chunks = [record.text for record in records]
            doc_indexes = [record.doc_index for record in records]
            self._chunk_records[collection_name] = records
        else:
            chunks, doc_indexes = build_uploaded_chunks_with_doc_indexes(docs, collection_name)
            self._chunk_records.pop(collection_name, None)
        if not chunks:
            self._chunks.pop(collection_name, None)
            self._indexes.pop(collection_name, None)
            self._bm25.pop(collection_name, None)
            self._chunk_doc_indexes.pop(collection_name, None)
            self._chunk_records.pop(collection_name, None)
            if rebuild_admissions:
                self._rebuild_admissions()
            return

        self._chunks[collection_name] = chunks
        self._chunk_doc_indexes[collection_name] = doc_indexes
        try:
            self._indexes[collection_name] = index_store.build_or_load(
                _index_name(collection_name), chunks, build_index
            )
            log.info("✅ Indexed uploaded file '%s' (%d chunks).", collection_name, len(chunks))
        except Exception as exc:
            # Keep the chunks even if embeddings fail — search falls back to
            # a bounded slice — but drop any stale index.
            self._indexes.pop(collection_name, None)
            log.warning("⚠️  Failed to build embeddings for '%s': %s", collection_name, exc)
        if rebuild_admissions:
            self._rebuild_admissions()

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
        self._chunk_doc_indexes.pop(collection_name, None)
        self._chunk_records.pop(collection_name, None)
        self._admissions.remove_collection(collection_name, rebuild=False)
        self._rebuild_admissions()
        # المتجهات المخزّنة دائمياً (قرص/Mongo) تُحذف مع مصدرها — لا يتيمة تبقى.
        # (بنفس الاسم المسبوق الذي خُزّنت به — الاسم المجرد كان يحذف مفتاحاً خاطئاً)
        index_store.delete(_index_name(collection_name, version="v1"))
        index_store.delete(_index_name(collection_name, version="v2"))
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

    def records_of(self, collection_name: str) -> list[ChunkRecord]:
        return list(self._chunk_records.get(collection_name, []))

    def candidate_metadata_for_chunks(self, chunks: List[str]) -> list[dict]:
        lookup = {
            record.text: record
            for records in self._chunk_records.values()
            for record in records
        }
        result = []
        for chunk in chunks:
            record = lookup.get(chunk)
            result.append({
                "chunk_id": record.chunk_id if record else None,
                "parent_id": record.parent_id if record else None,
                "source": record.source if record else None,
                "doc_index": record.doc_index if record else None,
                "kind": record.kind if record else None,
            })
        return result

    def admission_context_lines(
        self,
        allowed_collections: Optional[set[str]] = None,
        *,
        branch: str | None = None,
        max_percentage: float | None = None,
    ) -> List[str]:
        """جدول قبول مضغوط مع إبقاء اسم المصدر مرة لكل مجموعة.

        تكرار «المصدر/الدرجة/الكلية…» في 113 سطراً رفع البرومت إلى عشرات
        آلاف المحارف وسبب ثلاث محاولات توليد. كذلك كانت عشرات البرامج ذات
        الشرط نفسه تُرسل كسطر لكل برنامج، فيسقط النموذج كليات كاملة عند
        التلخيص. نجمع البرامج التي تشترك في (المصدر، الكلية، الفروع، المفتاح)
        في سطر واحد؛ الحقائق الذرية تبقى نفسها بلا رسوم، لكن تصبح قابلة
        للمراجعة كليةً كليةً وبزمن توليد أقل.
        """
        facts = self._admissions.facts
        if allowed_collections is not None:
            facts = [f for f in facts if f.source in allowed_collections]
        if branch:
            normalized_branch = normalize_arabic(branch)
            facts = [
                fact for fact in facts
                if fact.branches and any(
                    normalize_arabic(value) == normalized_branch
                    for value in fact.branches
                )
            ]
        if max_percentage is not None:
            facts = [
                fact for fact in facts
                if fact.min_percentage <= max_percentage
            ]

        grouped: dict[
            tuple[str, str, tuple[str, ...], float], list[str]
        ] = {}
        for fact in facts:
            key = (
                fact.source,
                fact.faculty,
                tuple(fact.branches),
                float(fact.min_percentage),
            )
            programs = grouped.setdefault(key, [])
            if fact.program not in programs:
                programs.append(fact.program)

        lines: List[str] = []
        current_source = None
        for (source, faculty, branches_tuple, min_percentage), programs in sorted(
            grouped.items(),
            key=lambda item: (
                item[0][0], item[0][1], item[0][3], item[0][2], item[1],
            ),
        ):
            if source != current_source:
                current_source = source
                lines.append(f"[المصدر: {current_source}]")
            branches = "، ".join(branches_tuple) if branches_tuple else "غير محدد"
            program_text = "، ".join(programs)
            lines.append(
                f"{faculty} | البرامج: {program_text} | الفروع: {branches} | "
                f"الحد الأدنى: {min_percentage:g}%"
            )
        return lines

    def resolve_admission(
        self,
        question: str,
        allowed_collections: Optional[set[str]] = None,
    ) -> Optional[AdmissionResolution]:
        """Resolve an admission question from atomic structured facts, if confident."""
        if allowed_collections is not None and allowed_collections != set(self._chunks):
            # The structured catalog is a cross-file index. Until it supports
            # source masks, skip this shortcut whenever RBAC narrows sources.
            return None
        try:
            return self._admissions.resolve(question)
        except Exception as exc:
            log.warning("⚠️  Structured admission lookup failed: %s", exc)
            return None

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

    def search_one_candidates(
        self,
        question: str,
        collection_name: str,
        top_k: int = config.TOP_K,
        threshold: float = config.SIM_THRESHOLD,
    ) -> List[RetrievalCandidate]:
        all_chunks = self._chunks[collection_name]
        index = self._usable_index(collection_name)
        if index is not None:
            q_vec = embed_query(question)
            dense = (index @ q_vec).flatten()
            lexical = self._lexical_scores(collection_name, all_chunks, question)
            candidates = hybrid_candidates(
                all_chunks, dense, lexical, top_k, threshold
            )
            record_trace({
                "scope": "uploaded_file_candidates",
                "query": question,
                "collection": collection_name,
                "candidates": [candidate.as_metadata() for candidate in candidates],
            })
            return candidates
        return [
            RetrievalCandidate(
                text=chunk,
                original_index=index,
                dense_score=0.0,
                bm25_score=0.0,
                rrf_score=0.0,
                fused_rank=index + 1,
                source=collection_name,
                metadata={"degraded_fallback": True},
            )
            for index, chunk in enumerate(all_chunks[:top_k])
        ]

    def search_one(
        self,
        question: str,
        collection_name: str,
        top_k: int = config.TOP_K,
        threshold: float = config.SIM_THRESHOLD,
    ) -> List[str]:
        return [
            candidate.text
            for candidate in self.search_one_candidates(
                question, collection_name, top_k, threshold
            )
        ]

    def search_all_candidates(
        self,
        question: str,
        top_k: int = config.TOP_K,
        threshold: float = config.SIM_THRESHOLD,
        allowed_collections: Optional[set[str]] = None,
    ) -> List[RetrievalCandidate]:
        q_vec = embed_query(question)
        pool_chunks: List[str] = []
        pool_dense: List[np.ndarray] = []
        pool_lexical: List[np.ndarray] = []
        permitted = set(self._chunks) if allowed_collections is None else set(allowed_collections)
        for collection_name, chunks in self._chunks.items():
            if collection_name not in permitted:
                continue
            index = self._usable_index(collection_name)
            if index is None:
                continue
            pool_chunks.extend(chunks)
            pool_dense.append((index @ q_vec).flatten())
            pool_lexical.append(self._lexical_scores(collection_name, chunks, question))

        if pool_chunks:
            dense = np.concatenate(pool_dense)
            lexical = np.concatenate(pool_lexical)
            candidates = hybrid_candidates(
                pool_chunks, dense, lexical, top_k, threshold
            )
            record_trace({
                "scope": "uploaded_files_all_candidates",
                "query": question,
                "allowed_collection_count": len(permitted),
                "candidates": [candidate.as_metadata() for candidate in candidates],
            })
            return candidates

        visible = [
            (name, chunks)
            for name, chunks in self._chunks.items()
            if name in permitted
        ]
        per_file_quota = max(1, top_k // max(1, len(visible)))
        result: List[RetrievalCandidate] = []
        for collection_name, chunks in visible:
            for chunk in chunks[:per_file_quota]:
                result.append(RetrievalCandidate(
                    text=chunk,
                    original_index=len(result),
                    dense_score=0.0,
                    bm25_score=0.0,
                    rrf_score=0.0,
                    fused_rank=len(result) + 1,
                    source=collection_name,
                    metadata={"degraded_fallback": True},
                ))
                if len(result) >= top_k:
                    return result
        return result

    def search_all(
        self,
        question: str,
        top_k: int = config.TOP_K,
        threshold: float = config.SIM_THRESHOLD,
        allowed_collections: Optional[set[str]] = None,
    ) -> List[str]:
        return [
            candidate.text
            for candidate in self.search_all_candidates(
                question, top_k, threshold, allowed_collections
            )
        ]

