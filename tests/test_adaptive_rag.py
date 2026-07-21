import time
from unittest.mock import patch

import numpy as np

from app import config, query_rewrite, rerank
from app.chunking import build_contextual_uploaded_chunk_records
from app.conversation_frame import (
    CONTEXT_ASSISTANT_REFERENCE,
    CONTEXT_AMBIGUOUS,
    CONTEXT_CORRECTION,
    CONTEXT_FOLLOWUP,
    CONTEXT_INDEPENDENT,
    build_query_plan,
)
from app.domain_router import route_query
from app.evidence_contract import build_evidence_contract, missing_field_query
from app.prompts import PromptContext, PromptRoute, build_system_prompt
from app.retrieval import hybrid_candidates
from app.rag_agent import plan_rag_actions
from app.uploaded_files import UploadedFilesStore


def _turn(user: str) -> dict:
    return {"user": user, "assistant": "", "at": time.time()}


def test_unified_prompt_has_ordered_priority_and_no_hard_300_limit():
    prompt = build_system_prompt(PromptContext(
        route=PromptRoute.UPLOADED_FILES,
        evidence="[ملف: مثال]\nرسوم الساعة: 28 دينار",
    ))
    assert "ترتيب الأولوية الملزم" in prompt
    assert "الأمان والخصوصية" in prompt
    assert "الإجابة موجزة افتراضياً" in prompt
    assert "300 حرف" not in prompt


def test_latest_user_correction_wins_for_branch_and_rate():
    history = [
        _turn("فرعي علمي ومعدلي 90"),
        _turn("تصحيح: فرعي أدبي ومعدلي 85"),
    ]
    frame, plan = build_query_plan("رتبهم", history)
    assert frame.branch == "أدبي"
    assert frame.rate == 85
    assert "أدبي" in plan.standalone_query
    assert "85%" in plan.standalone_query


def test_independent_question_does_not_inherit_old_academic_constraints():
    history = [_turn("معدلي 85% وفرعي علمي، ما التخصصات؟")]
    frame, plan = build_query_plan("من رئيس الجامعة؟", history)
    assert frame.context_mode == CONTEXT_INDEPENDENT
    assert frame.branch is None
    assert frame.rate is None
    assert "85" not in plan.standalone_query


def test_context_modes_cover_followup_correction_and_missing_anchor():
    history = [_turn("ما برامج كلية الهندسة؟")]
    followup, _ = build_query_plan("وما رسومها؟", history)
    correction, _ = build_query_plan("لا أقصد الهندسة، بل الطب", history)
    ambiguous, _ = build_query_plan("وما رسومها؟", [])
    assert followup.context_mode == CONTEXT_FOLLOWUP
    assert correction.context_mode == CONTEXT_CORRECTION
    assert ambiguous.context_mode == CONTEXT_AMBIGUOUS


def test_explicit_previous_answer_reference_has_its_own_context_mode():
    history = [_turn("ما رسوم التأجيل؟")]
    frame, plan = build_query_plan("وضح إجابتك السابقة", history)
    assert frame.context_mode == CONTEXT_ASSISTANT_REFERENCE
    assert frame.followup is True
    assert "رسوم التاجيل" in query_rewrite.positive_query(
        plan.standalone_query
    )


def test_scientific_subjects_do_not_imply_science_branch():
    frame, _ = build_query_plan("درست مواد علمية، ما التخصصات المناسبة؟", [])
    assert frame.branch is None


def test_reference_words_resolve_previous_user_turn():
    history = [_turn("كيف أقدم طلب تأجيل الفصل؟")]
    frame, plan = build_query_plan("وما شروطه؟", history)
    assert frame.followup is True
    assert frame.reference == "كيف أقدم طلب تأجيل الفصل؟"
    assert "تاجيل" in query_rewrite.positive_query(plan.standalone_query)


def test_positive_query_removes_excluded_topic_but_preserves_intent():
    frame, plan = build_query_plan(
        "خلينا من الهندسة، شو منح المتفوقين؟", []
    )
    normalized = query_rewrite.positive_query(plan.standalone_query)
    assert "الهندسه" not in normalized
    assert "منح" in normalized
    assert frame.exclusions == ["الهندسه"]
    assert frame.domains == ["scholarships"]


def test_scholarship_gpa_is_not_silently_classified_as_admission():
    frame, plan = build_query_plan("معدلي الجامعي 88، هل أستحق منحة؟", [])
    assert "scholarships" in frame.domains
    assert "admissions" not in frame.domains
    assert frame.rate_type == "university_gpa"
    assert plan.intent == "scholarships"


def test_evidence_contract_marks_missing_and_builds_one_retry_query():
    frame, plan = build_query_plan(
        "ما شروط القبول في الطب وكم الرسوم؟", []
    )
    contract = build_evidence_contract(
        plan,
        frame,
        ["[ملف: قبول]\nشروط القبول: تنافسي"],
    )
    assert "fee" in contract.missing_fields
    assert contract.sufficient is False
    retry = missing_field_query(plan, contract)
    assert retry is not None
    assert "رسوم" in retry


def test_evidence_contract_detects_degree_conflict():
    frame, plan = build_query_plan("كم رسوم بكالوريوس الهندسة؟", [])
    contract = build_evidence_contract(
        plan,
        frame,
        ["[ملف: دراسات عليا]\nرسوم ماجستير الهندسة: 60 دينار"],
    )
    assert contract.contradictions
    assert contract.sufficient is False


def test_evidence_contract_requires_exact_link_and_entity_bound_fee():
    frame, plan = build_query_plan("ما رابط بوابة الطالب؟", [])
    contract = build_evidence_contract(
        plan, frame, ["[ملف: دليل]\nرابط البوابة محفوظ في link_id فقط"]
    )
    assert "link" in contract.missing_fields

    fee_frame, fee_plan = build_query_plan("كم رسوم ساعة الطب؟", [])
    fee_contract = build_evidence_contract(
        fee_plan,
        fee_frame,
        ["[ملف: رسوم]\nرسوم ساعة التمريض 22 ديناراً"],
    )
    assert "fee" in fee_contract.missing_fields
    assert fee_contract.entity_supported is False


def test_domain_router_uses_structured_plus_rag_without_answering():
    frame, plan = build_query_plan("كم سعر ساعة الطب؟", [])
    route = route_query(plan, frame)
    assert route.mode == "structured_plus_rag"
    assert route.structured_first is True
    # Router is a plan only; final generation remains the LLM's job.
    assert not hasattr(route, "answer")


def test_bounded_agent_selects_tools_and_hard_limits_retries():
    frame, plan = build_query_plan("وما رابطها؟", [_turn("ما هي بوابة الطالب؟")])
    route = route_query(plan, frame)
    agent = plan_rag_actions(
        plan,
        frame,
        route,
        has_authoritative_evidence=False,
        safety_directive=False,
    )
    assert agent.use_hybrid_retrieval is True
    assert agent.use_parent_expansion is True
    assert agent.allow_evidence_retry is True
    assert agent.max_retrieval_attempts == 2
    assert "evidence_retry" in agent.tools


def test_hierarchical_chunks_keep_parent_context_and_stable_ids():
    docs = [{
        "college": "كلية الهندسة",
        "degree": "بكالوريوس",
        "programs": [
            {"name": "هندسة الحاسوب", "fee": 28},
            {"name": "الهندسة المدنية", "fee": 28},
        ],
    }]
    first = build_contextual_uploaded_chunk_records(docs, "رسوم الهندسة")
    second = build_contextual_uploaded_chunk_records(docs, "رسوم الهندسة")
    assert len(first) == 3  # overview + two children
    assert [r.chunk_id for r in first] == [r.chunk_id for r in second]
    children = [r for r in first if r.kind.startswith("child:")]
    assert all("كلية الهندسة" in r.text for r in children)
    assert len({r.parent_id for r in children}) == 1


def test_parent_expansion_adds_overview_and_nearest_sibling():
    docs = [{
        "college": "كلية الهندسة",
        "degree": "بكالوريوس",
        "programs": [
            {"name": "هندسة الحاسوب", "fee": 28},
            {"name": "الهندسة المدنية", "fee": 28},
        ],
    }]
    store = UploadedFilesStore()
    records = build_contextual_uploaded_chunk_records(docs, "رسوم الهندسة")
    store._chunk_records["رسوم الهندسة"] = records
    selected = [next(r.text for r in records if "هندسة الحاسوب" in r.text)]
    expanded, added = store.expand_parent_chunks(selected, max_additions=2)
    assert added == 2
    assert any("النوع: overview" in chunk for chunk in expanded)
    assert any("الهندسة المدنية" in chunk for chunk in expanded)


def test_hybrid_candidates_preserve_scores_and_do_not_force_weak_tail():
    chunks = [
        "[ملف: أ]\n[chunk_id: c1 | parent_id: p1 | النوع: overview]\nأ",
        "[ملف: ب]\n[chunk_id: c2 | parent_id: p2 | النوع: overview]\nب",
    ]
    strong = hybrid_candidates(
        chunks,
        np.array([0.8, 0.2], dtype=np.float32),
        np.array([0.0, 3.0], dtype=np.float32),
        top_k=2,
        threshold=0.25,
    )
    assert len(strong) == 2
    assert strong[0].rrf_score > 0
    assert all(candidate.chunk_id for candidate in strong)

    weak = hybrid_candidates(
        chunks,
        np.array([0.1, 0.2], dtype=np.float32),
        np.array([0.0, 0.0], dtype=np.float32),
        top_k=2,
        threshold=0.25,
    )
    assert weak == []


def test_reranker_circuit_breaker_fails_open_after_two_errors():
    rerank._CIRCUIT_FAILURE_COUNT = 0
    rerank._CIRCUIT_OPEN_UNTIL = 0.0
    chunks = ["a", "b", "c"]
    with patch.object(config, "RERANK_ENABLED", True), \
         patch.object(config, "RERANK_CIRCUIT_FAILURES", 2), \
         patch("app.rerank.requests.post", side_effect=TimeoutError("down")) as post:
        first, first_status = rerank.rerank_with_status("q", chunks, 2)
        second, second_status = rerank.rerank_with_status("q", chunks, 2)
        third, third_status = rerank.rerank_with_status("q", chunks, 2)

    assert first == chunks and second == chunks and third == chunks
    assert first_status == "error_fallback"
    assert second_status == "error_fallback"
    assert third_status == "circuit_open_fallback"
    assert post.call_count == 2
    rerank._CIRCUIT_FAILURE_COUNT = 0
    rerank._CIRCUIT_OPEN_UNTIL = 0.0


def test_final_answer_cache_is_off_when_every_question_must_reach_llm():
    assert config.LLM_ALWAYS_ANSWER is True
    assert config.ANSWER_CACHE_ENABLED is False
