"""Reliability acceptance tests for the bounded Agentic-RAG pipeline."""

import copy
from unittest.mock import patch

import numpy as np
import pytest

from app import config
from app.chunking import build_uploaded_chunks
from app.lexical import BM25
from app.rbac import Principal
from app.retrieval import hybrid_candidates
from app.sessions import TurnStatus
from app.uploaded_files import UploadedFilesStore
from tests.test_chat import ChatBase
from tests.test_equivalence import fake_embed


class TestTurnReliability(ChatBase):

    def test_repeatability_keeps_same_evidence_across_sessions(self):
        results = [
            self._chat(
                "chat_with_all_files",
                "كم علامة الفيزياء؟",
                f"repeat-{index}",
            )
            for index in range(20)
        ]

        evidence_sets = [
            result["retrieval_metadata"]["diagnostic_trace"]
            ["selected_evidence_ids"]
            for result in results
        ]
        assert all(value == evidence_sets[0] for value in evidence_sets)
        assert all(
            result["retrieval_metadata"]["query_plan"]["context_mode"]
            == "independent"
            for result in results
        )
        assert all(
            result["retrieval_metadata"]["final_answer_origin"] == "llm"
            for result in results
        )

    def test_trace_contains_versions_candidates_prompt_and_validation(self):
        result = self._chat(
            "chat_with_all_files", "كم علامة الرياضيات؟", "trace-session"
        )
        trace = result["retrieval_metadata"]["diagnostic_trace"]

        assert result["trace_id"] == trace["trace_id"]
        assert trace["pipeline_version"] == config.RAG_PIPELINE_VERSION
        assert len(trace["index_version"]) == 64
        assert trace["question_hash"]
        assert trace["prompt_sha256"]
        assert trace["candidate_ids_before_rerank"]
        assert trace["selected_evidence_ids"]
        assert trace["answer_validation"]["final_answer_origin"] == "llm"
        assert trace["latency_ms"]["total"] >= 0

    def test_same_session_retry_after_weak_turn_forces_fresh_wide_search(self):
        question = "كم علامة الفيزياء؟"
        self.bot.push_history(
            "retry-session",
            question,
            "لا أعرف.",
            status=TurnStatus.INSUFFICIENT_EVIDENCE,
        )

        result = self._chat(
            "chat_with_all_files", question, "retry-session"
        )

        metadata = result["retrieval_metadata"]
        assert metadata["force_refresh"] is True
        assert metadata["coverage_requested"] is True
        assert metadata["retrieval_cache_hit"] is False
        user_prompt = self.llm_calls[-1]["messages"][1]["content"]
        assert "لا أعرف" not in user_prompt

    def test_llm_generation_budget_is_a_hard_cap(self):
        calls = []

        def unsafe_llm(headers, payload):
            calls.append(payload)
            return "رابط النموذج هو admission_application"

        with patch.object(config, "CHAT_API_KEY", "test-key"), \
             patch("app.embeddings.embed_texts", side_effect=fake_embed), \
             patch.object(
                 self.bot._uploaded,
                 "search_all",
                 return_value=["[ملف: الدليل] لا يتوفر رابط مباشر."],
             ), \
             patch("app.llm._post_with_retry", side_effect=unsafe_llm):
            result = self.bot.chat_with_all_files(
                "أعطني رابط النموذج نفسه", "generation-budget"
            )

        metadata = result["retrieval_metadata"]
        assert len(calls) == 3
        assert metadata["llm_generation_count"] == 3
        assert metadata["llm_generation_limit"] == 3
        assert metadata["turn_status"] == TurnStatus.VALIDATION_FAILURE
        assert metadata["final_answer_origin"] == "llm"

    def test_stream_and_blocking_use_identical_validated_pipeline(self):
        answer = "رسوم التأجيل 10 دنانير."
        evidence = ["[ملف: الرسوم] رسوم التأجيل 10 دنانير."]
        guest = Principal.guest("guest:stream-equivalence")

        with patch.object(config, "CHAT_API_KEY", "test-key"), \
             patch("app.embeddings.embed_texts", side_effect=fake_embed), \
             patch.object(
                 self.bot, "_search_all_for_question", return_value=evidence
             ), \
             patch("app.llm._post_with_retry", return_value=answer):
            blocking = self.bot.chat_with_all_files(
                "كم رسوم التأجيل؟", "blocking-equivalence"
            )["answer"]
            streamed = "".join(self.bot.stream_answer(
                "كم رسوم التأجيل؟", guest, allowed_collections=None
            ))

        assert streamed == blocking == answer


def test_tied_candidates_are_stable_across_input_order():
    chunks = [
        "[ملف: ب]\n[chunk_id: 2 | parent_id: p | النوع: overview]\nنص",
        "[ملف: أ]\n[chunk_id: 1 | parent_id: p | النوع: overview]\nنص",
    ]
    dense = np.asarray([0.8, 0.8], dtype=np.float32)
    lexical = np.asarray([1.0, 1.0], dtype=np.float32)

    first = hybrid_candidates(chunks, dense, lexical, 2, 0.1)
    second = hybrid_candidates(
        list(reversed(chunks)), dense, lexical, 2, 0.1
    )

    assert [item.chunk_id for item in first] == ["1", "2"]
    assert [item.chunk_id for item in second] == ["1", "2"]


class _FakeCollection:
    def __init__(self, documents):
        self.documents = copy.deepcopy(documents)

    def find(self, _query):
        return copy.deepcopy(self.documents)

    def drop(self):
        self.documents = []

    def insert_many(self, documents):
        self.documents.extend(copy.deepcopy(list(documents)))


def _seed_store(store: UploadedFilesStore, name: str, documents: list[dict]):
    chunks = build_uploaded_chunks(documents, name)
    store._chunks[name] = chunks
    store._indexes[name] = np.ones((len(chunks), 3), dtype=np.float32)
    store._bm25[name] = (BM25(chunks), chunks)
    store._chunk_doc_indexes[name] = list(range(len(chunks)))


def test_failed_file_rebuild_keeps_previous_atomic_generation_and_database():
    name = "دليل"
    old_documents = [{"value": "النسخة القديمة"}]
    collection = _FakeCollection(old_documents)
    store = UploadedFilesStore()
    _seed_store(store, name, old_documents)
    version_before = store.index_version
    chunks_before = store.chunks_of(name)

    with patch(
        "app.uploaded_files.get_uploaded_collection", return_value=collection
    ), patch(
        "app.uploaded_files.index_store.build_or_load",
        side_effect=RuntimeError("embedding unavailable"),
    ), patch.object(store, "_rebuild_admissions"):
        with pytest.raises(RuntimeError):
            store.upload_json(name, [{"value": "النسخة الجديدة"}])

    assert collection.documents == old_documents
    assert store.chunks_of(name) == chunks_before
    assert store.index_version == version_before


def test_successful_file_rebuild_changes_version_only_after_complete_index():
    name = "دليل"
    old_documents = [{"value": "النسخة القديمة"}]
    new_documents = [{"value": "النسخة الجديدة"}]
    collection = _FakeCollection(old_documents)
    store = UploadedFilesStore()
    _seed_store(store, name, old_documents)
    version_before = store.index_version
    database_seen_during_build = []

    def built_index(_name, chunks, _builder):
        database_seen_during_build.append(copy.deepcopy(collection.documents))
        return np.ones((len(chunks), 3), dtype=np.float32)

    with patch(
        "app.uploaded_files.get_uploaded_collection", return_value=collection
    ), patch(
        "app.uploaded_files.index_store.build_or_load",
        side_effect=built_index,
    ), patch.object(store, "_rebuild_admissions"):
        store.upload_json(name, new_documents)

    assert collection.documents == new_documents
    assert database_seen_during_build == [old_documents]
    assert store.index_ready is True
    assert store.index_version != version_before
