"""
Uploaded-file corpora: each uploaded JSON file lives in its own MongoDB
collection and gets its own chunks + dedicated embeddings index, so chat
can run semantic search over just that file (or across all of them) instead
of sending whole files to the LLM.
"""

import copy
import hashlib
import json
from threading import RLock
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
        self._state_lock = RLock()
        self._load_errors: dict[str, str] = {}
        self._refresh_errors: dict[str, str] = {}

    # ── lifecycle ─────────────────────────────────────────────────────────

    def load_all(self):
        try:
            with self._state_lock:
                self._load_errors.pop("__load_all__", None)
            collections = sorted(list_uploaded_collections())
            if not collections:
                log.info("ℹ️  No uploaded files found in MongoDB.")
                return
            for col_name in collections:
                self.load_one(col_name, rebuild_admissions=False)
            self._rebuild_admissions()
            if self._load_errors:
                log.warning(
                    "⚠️ Loaded %d uploaded file(s); %d failed indexing: %s",
                    len(self._chunks), len(self._load_errors),
                    ", ".join(sorted(self._load_errors)),
                )
            else:
                log.info("✅ Loaded %d uploaded file(s).", len(self._chunks))
        except Exception as exc:
            with self._state_lock:
                self._load_errors["__load_all__"] = type(exc).__name__
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
            with self._state_lock:
                self._admissions.rebuild(vector_for_fact)
            log.info(
                "✅ Built structured admission catalog (%d facts).",
                len(self._admissions.facts),
            )
        except Exception as exc:
            log.warning("⚠️  Could not build structured admission catalog: %s", exc)

    @staticmethod
    def _stable_doc_key(doc: dict) -> tuple[str, str]:
        return (
            str(doc.get("_id", "")),
            json.dumps(doc, ensure_ascii=False, sort_keys=True, default=str),
        )

    def load_one(
        self, collection_name: str, rebuild_admissions: bool = True
    ) -> bool:
        """
        Load one uploaded file's documents, build its chunks, AND build a
        dedicated embeddings index for it, so search_one() can run semantic
        search over just this file instead of sending it whole.
        """
        col = get_uploaded_collection(collection_name)
        docs = list(col.find({}))
        docs.sort(key=self._stable_doc_key)
        if not docs:
            self._publish_empty(collection_name, rebuild_admissions)
            return True

        try:
            generation = self._build_generation(collection_name, docs)
        except Exception as exc:
            self._record_generation_failure(collection_name, exc)
            return False
        self._publish_generation(
            collection_name, docs, generation, rebuild_admissions
        )
        return True

    def _build_generation(self, collection_name: str, docs: list[dict]) -> dict:
        """Build dense+lexical state without mutating the active generation."""
        if config.HIERARCHICAL_CHUNKING_ENABLED:
            records = build_contextual_uploaded_chunk_records(docs, collection_name)
            chunks = [record.text for record in records]
            doc_indexes = [record.doc_index for record in records]
        else:
            chunks, doc_indexes = build_uploaded_chunks_with_doc_indexes(docs, collection_name)
            records = []
        if not chunks:
            raise ValueError("no searchable chunks")
        new_index = index_store.build_or_load(
            _index_name(collection_name), chunks, build_index
        )
        if len(new_index) != len(chunks):
            raise RuntimeError("embedding matrix length mismatch")
        new_bm25 = BM25(chunks)
        if len(new_bm25.scores("الجامعة")) != len(chunks):
            raise RuntimeError("BM25 warm-up length mismatch")
        if chunks and len(
            (new_index @ new_index[0].reshape(-1, 1)).flatten()
        ) != len(chunks):
            raise RuntimeError("semantic warm-up length mismatch")
        return {
            "chunks": chunks,
            "index": new_index,
            "bm25": new_bm25,
            "doc_indexes": doc_indexes,
            "records": records,
        }

    def _record_generation_failure(
        self, collection_name: str, exc: Exception
    ) -> None:
        with self._state_lock:
            has_active_generation = bool(
                collection_name in self._chunks
                and collection_name in self._indexes
                and len(self._indexes[collection_name])
                == len(self._chunks[collection_name])
            )
            target = (
                self._refresh_errors
                if has_active_generation else self._load_errors
            )
            target[collection_name] = (
                "no_chunks" if isinstance(exc, ValueError)
                else type(exc).__name__
            )
        log.warning(
            "⚠️ Failed to build indexes for '%s': %s", collection_name, exc
        )

    def _publish_empty(
        self, collection_name: str, rebuild_admissions: bool
    ) -> None:
        with self._state_lock:
            self._chunks.pop(collection_name, None)
            self._indexes.pop(collection_name, None)
            self._bm25.pop(collection_name, None)
            self._chunk_doc_indexes.pop(collection_name, None)
            self._chunk_records.pop(collection_name, None)
            self._load_errors.pop(collection_name, None)
            self._refresh_errors.pop(collection_name, None)
            self._admissions.remove_collection(
                collection_name, rebuild=False
            )
        if rebuild_admissions:
            self._rebuild_admissions()

    def _publish_generation(
        self,
        collection_name: str,
        docs: list[dict],
        generation: dict,
        rebuild_admissions: bool,
    ) -> None:
        # One atomic publication point after dense + lexical indexes are both
        # complete. Searches take the same lock and therefore see either the
        # old generation or this new one, never an in-between mixture.
        with self._state_lock:
            # Extraction evaluates before assignment inside AdmissionCatalog;
            # do it before publishing chunk/index dictionaries so malformed
            # structured data cannot leave a half-published generation.
            self._admissions.replace_collection(
                collection_name, docs, rebuild=False
            )
            chunks = generation["chunks"]
            self._chunks[collection_name] = chunks
            self._indexes[collection_name] = generation["index"]
            self._bm25[collection_name] = (generation["bm25"], chunks)
            self._chunk_doc_indexes[collection_name] = generation["doc_indexes"]
            records = generation["records"]
            if records:
                self._chunk_records[collection_name] = records
            else:
                self._chunk_records.pop(collection_name, None)
            self._load_errors.pop(collection_name, None)
            self._refresh_errors.pop(collection_name, None)
        log.info("✅ Indexed uploaded file '%s' (%d chunks).", collection_name, len(chunks))
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
                item_copy = copy.deepcopy(item)
                item_copy.pop("_id", None)
                cleaned.append(item_copy)
            else:
                cleaned.append({"value": item})
        # Build the entire next generation before touching the live Mongo
        # collection. During this potentially slow embeddings call both the
        # database and runtime continue serving the previous version.
        try:
            generation = (
                self._build_generation(collection_name, cleaned)
                if cleaned else None
            )
        except Exception as exc:
            self._record_generation_failure(collection_name, exc)
            raise RuntimeError(
                f"فشلت فهرسة '{collection_name}'؛ بقيت النسخة السابقة فعّالة."
            ) from exc

        previous = [copy.deepcopy(item) for item in col.find({})]
        try:
            col.drop()
            if cleaned:
                col.insert_many(cleaned)
            if generation is None:
                self._publish_empty(collection_name, rebuild_admissions=True)
            else:
                self._publish_generation(
                    collection_name, cleaned, generation,
                    rebuild_admissions=True,
                )
        except Exception as exc:
            # Runtime publication happens only after all potentially failing
            # build work. Restore Mongo if its final commit/publish still fails.
            col.drop()
            if previous:
                col.insert_many(previous)
            raise RuntimeError(
                f"فشلت فهرسة '{collection_name}'؛ بقيت النسخة السابقة فعّالة."
            ) from exc
        return {"inserted": len(cleaned), "collection": collection_name}

    def delete(self, collection_name: str) -> bool:
        drop_uploaded_collection(collection_name)
        with self._state_lock:
            self._chunks.pop(collection_name, None)
            self._indexes.pop(collection_name, None)
            self._bm25.pop(collection_name, None)
            self._chunk_doc_indexes.pop(collection_name, None)
            self._chunk_records.pop(collection_name, None)
            self._load_errors.pop(collection_name, None)
            self._refresh_errors.pop(collection_name, None)
            self._admissions.remove_collection(collection_name, rebuild=False)
        self._rebuild_admissions()
        # المتجهات المخزّنة دائمياً (قرص/Mongo) تُحذف مع مصدرها — لا يتيمة تبقى.
        # (بنفس الاسم المسبوق الذي خُزّنت به — الاسم المجرد كان يحذف مفتاحاً خاطئاً)
        index_store.delete(_index_name(collection_name, version="v1"))
        index_store.delete(_index_name(collection_name, version="v2"))
        return True

    def reload(self, collection_name: str) -> bool:
        try:
            return self.load_one(collection_name)
        except Exception:
            return False

    # ── state queries ─────────────────────────────────────────────────────

    def is_empty(self) -> bool:
        with self._state_lock:
            return not self._chunks

    def has(self, collection_name: str) -> bool:
        with self._state_lock:
            return collection_name in self._chunks

    def chunks_of(self, collection_name: str) -> List[str]:
        with self._state_lock:
            return list(self._chunks.get(collection_name, []))

    def records_of(self, collection_name: str) -> list[ChunkRecord]:
        with self._state_lock:
            return list(self._chunk_records.get(collection_name, []))

    @property
    def index_ready(self) -> bool:
        with self._state_lock:
            return not self._load_errors and all(
                name in self._indexes
                and len(self._indexes[name]) == len(chunks)
                and name in self._bm25
                and self._bm25[name][1] is chunks
                for name, chunks in self._chunks.items()
            )

    @property
    def failed_sources(self) -> list[str]:
        with self._state_lock:
            return sorted(self._load_errors)

    @property
    def failed_refresh_sources(self) -> list[str]:
        with self._state_lock:
            return sorted(self._refresh_errors)

    @property
    def index_version(self) -> str:
        with self._state_lock:
            digest = hashlib.sha256()
            digest.update(config.EMBED_MODEL.encode("utf-8"))
            for name in sorted(self._chunks):
                digest.update(b"\x00")
                digest.update(name.encode("utf-8"))
                digest.update(b"\x00")
                digest.update(
                    index_store.fingerprint(self._chunks[name]).encode("ascii")
                )
            return digest.hexdigest()

    def candidate_metadata_for_chunks(self, chunks: List[str]) -> list[dict]:
        with self._state_lock:
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

    def expand_parent_chunks(
        self,
        chunks: List[str],
        *,
        max_additions: int = 4,
    ) -> tuple[List[str], int]:
        """Expand selected child chunks with bounded same-parent context.

        Search stays precise at child level; after selection the agent may add
        the overview and nearest siblings from the same source document.  This
        is retrieval-time hierarchical RAG rather than metadata-only tagging.
        """
        if max_additions <= 0 or not chunks:
            return list(chunks), 0
        with self._state_lock:
            by_text = {
                record.text: record
                for records in self._chunk_records.values()
                for record in records
            }
            by_parent: dict[str, list[ChunkRecord]] = {}
            for records in self._chunk_records.values():
                for record in records:
                    by_parent.setdefault(record.parent_id, []).append(record)

        result = list(chunks)
        selected = set(result)
        expanded_parents: set[str] = set()
        for chunk in chunks:
            record = by_text.get(chunk)
            if (
                record is None
                or not record.kind.startswith("child:")
                or record.parent_id in expanded_parents
            ):
                continue
            expanded_parents.add(record.parent_id)
            siblings = by_parent.get(record.parent_id, [])
            item_index = int(record.metadata.get("item_index", 0))
            siblings = sorted(
                siblings,
                key=lambda item: (
                    0 if item.kind == "overview" else 1,
                    abs(int(item.metadata.get("item_index", item_index)) - item_index),
                    item.chunk_id,
                ),
            )
            for sibling in siblings:
                if sibling.text in selected:
                    continue
                result.append(sibling.text)
                selected.add(sibling.text)
                if len(result) - len(chunks) >= max_additions:
                    return result, len(result) - len(chunks)
        return result, len(result) - len(chunks)

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
        with self._state_lock:
            facts = list(self._admissions.facts)
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
        with self._state_lock:
            collection_names = set(self._chunks)
        if allowed_collections is not None and allowed_collections != collection_names:
            # The structured catalog is a cross-file index. Until it supports
            # source masks, skip this shortcut whenever RBAC narrows sources.
            return None
        try:
            with self._state_lock:
                return self._admissions.resolve(question)
        except Exception as exc:
            log.warning("⚠️  Structured admission lookup failed: %s", exc)
            return None

    def list_files(self) -> List[dict]:
        with self._state_lock:
            return [
                {
                    "collection":   name,
                    "chunks_count": len(chunks),
                    "indexed":      (
                        name in self._indexes
                        and len(self._indexes[name]) == len(chunks)
                    ),
                }
                for name, chunks in sorted(self._chunks.items())
            ]

    def _usable_index(self, collection_name: str) -> Optional[np.ndarray]:
        """The file's index, only if it exists and still matches its chunks."""
        with self._state_lock:
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

    # ── search ────────────────────────────────────────────────────────────

    def search_one_candidates(
        self,
        question: str,
        collection_name: str,
        top_k: int = config.TOP_K,
        threshold: float = config.SIM_THRESHOLD,
    ) -> List[RetrievalCandidate]:
        with self._state_lock:
            all_chunks = self._chunks[collection_name]
            index = self._indexes.get(collection_name)
            if index is not None and len(index) != len(all_chunks):
                index = None
            cached_bm25 = self._bm25.get(collection_name)
            if cached_bm25 is None or cached_bm25[1] is not all_chunks:
                cached_bm25 = (BM25(all_chunks), all_chunks)
                self._bm25[collection_name] = cached_bm25
            lexical_index = cached_bm25[0]
        q_vec = embed_query(question) if index is not None else None
        dense = (
            (index @ q_vec).flatten()
            if index is not None
            else np.zeros(len(all_chunks), dtype=np.float32)
        )
        lexical = (
            lexical_index.scores(question)
            if config.HYBRID_ENABLED
            else np.zeros(len(all_chunks), dtype=np.float32)
        )
        candidates = hybrid_candidates(
            all_chunks, dense, lexical, top_k, threshold
        )
        if index is None:
            for candidate in candidates:
                candidate.metadata["lexical_only_fallback"] = True
        record_trace({
            "scope": "uploaded_file_candidates",
            "query": question,
            "collection": collection_name,
            "index_ready": index is not None,
            "candidates": [candidate.as_metadata() for candidate in candidates],
        })
        return candidates

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
        with self._state_lock:
            snapshot = []
            for name, chunks in sorted(self._chunks.items()):
                cached_bm25 = self._bm25.get(name)
                if cached_bm25 is None or cached_bm25[1] is not chunks:
                    cached_bm25 = (BM25(chunks), chunks)
                    self._bm25[name] = cached_bm25
                snapshot.append(
                    (name, chunks, self._indexes.get(name), cached_bm25[0])
                )
        permitted = (
            {name for name, _, _, _ in snapshot}
            if allowed_collections is None
            else set(allowed_collections)
        )
        for collection_name, chunks, index, lexical_index in snapshot:
            if collection_name not in permitted:
                continue
            pool_chunks.extend(chunks)
            pool_dense.append(
                (index @ q_vec).flatten()
                if index is not None and len(index) == len(chunks)
                else np.zeros(len(chunks), dtype=np.float32)
            )
            pool_lexical.append(
                lexical_index.scores(question)
                if config.HYBRID_ENABLED
                else np.zeros(len(chunks), dtype=np.float32)
            )

        if pool_chunks:
            dense = np.concatenate(pool_dense)
            lexical = np.concatenate(pool_lexical)
            candidates = hybrid_candidates(
                pool_chunks, dense, lexical, top_k, threshold
            )
            has_usable_index = any(
                name in permitted
                and index is not None
                and len(index) == len(chunks)
                for name, chunks, index, _ in snapshot
            )
            if not candidates and not has_usable_index:
                # Compatibility/degraded mode for injected or pre-readiness
                # stores only. Production chat is gated by index_ready, so an
                # arbitrary slice can never be mistaken for a complete search.
                candidates = [
                    RetrievalCandidate(
                        text=chunk,
                        original_index=index,
                        dense_score=0.0,
                        bm25_score=0.0,
                        rrf_score=0.0,
                        fused_rank=index + 1,
                        source=name,
                        metadata={"degraded_fallback": True},
                    )
                    for name, chunks, _, _ in snapshot
                    if name in permitted
                    for index, chunk in enumerate(chunks)
                ][:top_k]
            record_trace({
                "scope": "uploaded_files_all_candidates",
                "query": question,
                "allowed_collection_count": len(permitted),
                "degraded_fallback": bool(
                    candidates
                    and candidates[0].metadata.get("degraded_fallback")
                ),
                "candidates": [candidate.as_metadata() for candidate in candidates],
            })
            return candidates

        visible = [
            (name, chunks)
            for name, chunks, _, _ in snapshot
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
