import time
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app import config, query_rewrite, rerank
from app.chatbot import IUGChatbot, _conflicts_relevant_to_plan
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
from app.data_quality import (
    deduplicate_evidence,
    preflight_documents,
    suppress_rejected_conflict_values,
)
from app.evidence_contract import build_evidence_contract, missing_field_query
from app.prompts import PromptContext, PromptRoute, build_system_prompt
from app.retrieval import hybrid_candidates
from app.rag_agent import plan_rag_actions
from app.semantic_rag import run_semantic_planner, run_semantic_verifier
from app.sessions import SessionStore
from app.text_norm import normalize_arabic
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


def test_compound_key_is_resolved_to_admission_cutoff_per_claim():
    frame, plan = build_query_plan(
        "كم سعر ساعة هندسة الحاسوب وما المفتاح؟", []
    )
    assert [claim.canonical_field for claim in plan.claims] == [
        "fee", "admission_cutoff"
    ]
    resolution = plan.concept_resolutions[0]
    assert resolution.surface_text == "المفتاح"
    assert resolution.canonical_concept == "admission_cutoff"
    assert resolution.source == "context"
    assert "مفتاح القبول" in plan.standalone_query
    assert frame.ambiguous is False


def test_compound_following_field_inherits_program_from_same_question():
    _, plan = build_query_plan(
        "ما سعر ساعة الطب البشري، وما الفرع المطلوب؟", []
    )
    assert [claim.canonical_field for claim in plan.claims] == ["fee", "branch"]
    assert all(
        normalize_arabic("الطب البشري") in (claim.entity or "")
        for claim in plan.claims
    )


def test_unknown_multipart_clause_is_never_silently_general_grounded():
    _frame, plan = build_query_plan(
        "كم الرسوم وما لون البطاقة؟", []
    )
    assert [claim.canonical_field for claim in plan.claims] == ["fee"]
    assert plan.unresolved_clauses == ["ما لون البطاقة"]
    assert plan.needs_semantic_planner is True


def test_short_key_followup_uses_fresh_program_context():
    _, plan = build_query_plan(
        "طيب شو مفتاحه؟", [_turn("كم سعر ساعة هندسة الحاسوب؟")]
    )
    assert plan.context_mode == CONTEXT_FOLLOWUP
    assert [claim.canonical_field for claim in plan.claims] == [
        "admission_cutoff"
    ]
    assert normalize_arabic("هندسة الحاسوب") in (plan.claims[0].entity or "")


def test_short_branch_and_cutoff_followups_inherit_program_entity():
    _frame, branch = build_query_plan(
        "طيب شو الفرع المطلوب؟", [_turn("كم سعر ساعة الطب البشري؟")]
    )
    assert branch.context_mode == CONTEXT_FOLLOWUP
    assert branch.claims[0].canonical_field == "branch"
    assert normalize_arabic("الطب البشري") in (branch.claims[0].entity or "")

    _frame, cutoff = build_query_plan(
        "والحد الأدنى؟", [_turn("كم سعر ساعة علم الحاسوب؟")]
    )
    assert cutoff.context_mode == CONTEXT_FOLLOWUP
    assert cutoff.claims[0].canonical_field == "admission_cutoff"
    assert normalize_arabic("علم الحاسوب") in (cutoff.claims[0].entity or "")

    _frame, bare = build_query_plan("والحد الأدنى؟", [])
    assert bare.requires_clarification is True


def test_ordinal_and_missing_object_followups_keep_whole_clause_and_history():
    _frame, second = build_query_plan(
        "والثانية شو بتعطي؟",
        [_turn("ما تفاصيل منحة الامتياز الأولى؟")],
    )
    assert second.context_mode == CONTEXT_FOLLOWUP
    assert second.unresolved_clauses == []
    assert len(second.claims) == 1
    assert normalize_arabic("منحة الامتياز الأولى") in second.standalone_query

    _frame, missing = build_query_plan(
        "طيب ما عندي، شو أعمل؟",
        [_turn("شو رقم الجلوس المطلوب وأنا شهادتي من السعودية؟")],
    )
    assert missing.context_mode == CONTEXT_FOLLOWUP
    assert missing.unresolved_clauses == []
    assert len(missing.claims) == 1
    assert normalize_arabic("رقم الجلوس") in missing.standalone_query


def test_which_one_fee_followup_uses_list_context_without_fake_entity():
    _frame, plan = build_query_plan(
        "أي واحد رسومه 25؟",
        [_turn("ما تخصصات كلية العلوم ورسومها؟")],
    )
    assert plan.context_mode == CONTEXT_FOLLOWUP
    assert plan.claims[0].canonical_field == "fee"
    assert plan.claims[0].entity is None
    assert normalize_arabic("كلية العلوم") in plan.standalone_query


def test_bare_key_requires_clarification_and_account_key_is_not_admission():
    _, bare = build_query_plan("ما المفتاح؟", [])
    assert bare.requires_clarification is True
    assert bare.unresolved_clauses == ["ما المفتاح"]
    assert bare.claims == []

    _, account = build_query_plan("نسيت مفتاح حسابي", [])
    assert account.requires_clarification is False
    assert account.claims[0].canonical_field == "account_access"
    assert account.claims[0].entity is None
    assert all(
        item.canonical_concept != "admission_cutoff"
        for item in account.concept_resolutions
    )


def test_explicit_admission_key_binds_program_name_without_a_fixed_dictionary():
    _, plan = build_query_plan("ما مفتاح القبول للغة الإنجليزية؟", [])
    assert plan.claims[0].canonical_field == "admission_cutoff"
    assert "الانجليزيه" in (plan.claims[0].entity or "")


def test_named_program_eligibility_keeps_clean_program_entity():
    _, plan = build_query_plan(
        "معدلي 79 علمي، هل أحقق مفتاح هندسة الحاسوب في البيانات الحالية؟",
        [],
    )
    cutoff = next(
        claim for claim in plan.claims
        if claim.canonical_field == "admission_cutoff"
    )
    assert cutoff.entity == normalize_arabic("هندسة الحاسوب")


def test_eligibility_phrasings_map_to_admission_cutoff_and_clean_entity():
    cases = [
        ("معدلي 82 علمي، هل أحقق الشرط المبدئي للهندسة؟", "الهندسة"),
        ("معدلي 85 أدبي، هل يتيح لي التقديم لهندسة الحاسوب؟", "هندسة الحاسوب"),
        ("معدلي 65 علمي، هل أحقق شرط كلية العلوم العام؟", "العلوم العام"),
        ("معدلي 70 أدبي، هل يمكنني دخول كلية العلوم؟", "العلوم"),
        ("معدلي 69 علمي، هل أحقق شرط التمريض العام؟", "التمريض العام"),
        ("معدلي 70 علمي، هل أحقق شرط التمريض مبدئياً؟", "التمريض"),
        ("معدلي 80 أدبي، هل أحقق شرط التمريض مبدئياً؟", "التمريض"),
        ("معدلي 80 أدبي، هل يمكنني التقديم لعلم الحاسوب؟", "علم الحاسوب"),
        (
            "معدلي 70 أدبي، هل الوسائط المتعددة وتطوير الويب من الخيارات الممكنة؟",
            "الوسائط المتعددة تطوير الويب",
        ),
        ("معدلي 90 علمي، هل الطب مضمون؟", "الطب"),
        ("معدلي 92 علمي، هل قبولي بالطب مؤكد؟", "الطب"),
        (
            "أنا فرع صناعي ومعدلي 82، هل البيانات الحالية تسمح بالهندسة الصناعية؟",
            "الهندسة الصناعية",
        ),
        ("معدلي 65 علمي، هل تكنولوجيا المعلومات خيار مبدئي؟", "تكنولوجيا المعلومات"),
        (
            "معدلي 65 شرعي، هل اللغة الإنجليزية في كلية الآداب خيار مبدئي؟",
            "اللغة الإنجليزية الآداب",
        ),
    ]
    for question, entity in cases:
        _, plan = build_query_plan(question, [])
        assert plan.requires_clarification is False
        assert plan.unresolved_clauses == []
        assert len(plan.claims) == 1
        assert plan.claims[0].canonical_field == "admission_cutoff"
        assert plan.claims[0].entity == normalize_arabic(entity)


def test_admission_contract_matches_optional_arabic_definite_article():
    frame, plan = build_query_plan(
        "معدلي 65 علمي، هل أحقق شرط كلية العلوم العام؟", []
    )
    contract = build_evidence_contract(plan, frame, [
        "العلوم | البرامج: عام | الفروع: علمي | الحد الأدنى: 65%"
    ])
    assert contract.sufficient is True
    assert contract.claim_coverage["claim_1"]["resolved"] is True


def test_procedure_questions_keep_case_facts_as_context_not_unresolved_claims():
    questions = [
        "شهادتي من السعودية وما عندي رقم جلوس فلسطيني؛ شو أول خطوة؟",
        "نسيت رقم الجلوس لكن بياناتي موجودة بالجامعة؛ هل ينفع رقم الهوية؟",
        "أنا خارج غزة والمعبر مغلق؛ اشرح بداية طلب الالتحاق الإلكتروني.",
    ]
    for question in questions:
        _, plan = build_query_plan(question, [])
        assert plan.requires_clarification is False
        assert plan.unresolved_clauses == []
        assert plan.claims
        assert all(claim.canonical_field == "procedures" for claim in plan.claims)
        assert all(claim.entity is None for claim in plan.claims)


def test_claim_contract_requires_same_program_field_and_value():
    frame, plan = build_query_plan(
        "كم سعر ساعة هندسة الحاسوب وما المفتاح؟", []
    )
    contract = build_evidence_contract(plan, frame, [
        "[ملف: الرسوم]\nprogram_name: هندسة الحاسوب\ncredit_hour_fee: 28 دينار",
        "[ملف: القبول]\nprogram_name: الهندسة المدنية\nmin_high_school_percentage: 80%",
    ])
    assert contract.claim_coverage["claim_1"]["resolved"] is True
    assert contract.claim_coverage["claim_2"]["resolved"] is False
    assert contract.sufficient is False


def test_admission_list_uses_rate_as_scope_not_as_fake_entity():
    frame, plan = build_query_plan(
        "ما هي التخصصات التي يمكن أن تقبلني إذا كان معدلي 81؟", []
    )
    assert len(plan.claims) == 1
    assert plan.claims[0].canonical_field == "admission_cutoff"
    assert plan.claims[0].entity is None
    contract = build_evidence_contract(plan, frame, [
        "[ملف: القبول]\nprogram_name: هندسة الحاسوب\n"
        "min_high_school_percentage: 80%",
    ])
    assert contract.claim_coverage["claim_1"]["resolved"] is True


def test_fact_fingerprints_dedupe_numeric_equivalents_and_resolve_newest():
    duplicate = deduplicate_evidence([
        "[ملف: قديم]\nprogram_name: هندسة الحاسوب\ncredit_hour_fee: 5",
        "[ملف: جديد]\nprogram_name: هندسة الحاسوب\ncredit_hour_fee: 5.0",
    ])
    assert duplicate.duplicate_count == 1
    assert len(duplicate.chunks) == 1

    conflict = deduplicate_evidence([
        "[ملف: قديم]\nprogram_name: هندسة الحاسوب\ncredit_hour_fee: 5",
        "[ملف: جديد]\nprogram_name: هندسة الحاسوب\ncredit_hour_fee: 7",
    ], source_recency={"قديم": "2025-01-01", "جديد": "2026-01-01"})
    assert conflict.conflicts == []
    assert conflict.resolved_conflicts[0]["selected_value"] == "7"
    assert conflict.resolved_conflicts[0]["selected_source"] == "جديد"
    filtered = suppress_rejected_conflict_values(
        conflict.chunks, conflict.resolved_conflicts
    )
    assert all("credit_hour_fee: 5" not in chunk for chunk in filtered)
    assert any("credit_hour_fee: 7" in chunk for chunk in filtered)

    unknown_date = deduplicate_evidence([
        "[ملف: مؤرخ]\nprogram_name: هندسة الحاسوب\ncredit_hour_fee: 5",
        "[ملف: بلا_تاريخ]\nprogram_name: هندسة الحاسوب\ncredit_hour_fee: 7",
    ], source_recency={"مؤرخ": "2026-01-01"})
    assert unknown_date.resolved_conflicts == []
    assert len(unknown_date.conflicts) == 1


def test_repeated_keyword_values_are_multivalued_not_conflicting_facts():
    result = deduplicate_evidence([
        "[ملف: أ]\ndegree_or_request: بدل فاقد شهادة\n"
        "amount: 10\nkeywords: بدل فاقد\nkeywords: شهادة ضائعة",
        "[ملف: ب]\ndegree_or_request: بدل فاقد شهادة\n"
        "amount: 10.0\nkeywords: بدل فاقد\nkeywords: مستخرج شهادة",
    ])
    assert result.conflicts == []
    assert result.resolved_conflicts == []


def test_projected_multi_record_chunk_does_not_merge_unrelated_records():
    result = deduplicate_evidence([
        "[إسقاط حقلي]\n"
        "[ملف: دليل]\nالموضوع: الرسوم الثابتة\nالإجابة: 13 دينار\n"
        "[ملف: دليل]\nالموضوع: طلب الالتحاق\nالإجابة: 20 دينار"
    ])
    assert result.conflicts == []


def test_nested_program_facts_use_program_identity_not_list_position():
    result = deduplicate_evidence([
        "[ملف: رسوم]\ntitle: جدول الرسوم\n"
        "programs[0].program_name: الطب البشري\n"
        "programs[0].credit_hour_fee: 100",
        "[ملف: رسوم]\ntitle: جدول الرسوم\n"
        "programs[0].program_name: هندسة الحاسوب\n"
        "programs[0].credit_hour_fee: 25",
    ])
    assert result.conflicts == []


def test_runtime_conflicts_are_scoped_to_claim_field_and_entity():
    _, plan = build_query_plan("كم سعر ساعة الطب البشري؟", [])
    conflicts = [
        {"conflict_id": "global", "canonical_field": "fee", "entity": "global"},
        {"conflict_id": "other", "canonical_field": "fee", "entity": "هندسة الحاسوب"},
        {"conflict_id": "target", "canonical_field": "fee", "entity": "الطب البشري"},
        {"conflict_id": "branch", "canonical_field": "branch", "entity": "الطب البشري"},
    ]
    filtered = _conflicts_relevant_to_plan(plan, conflicts)
    assert [item["conflict_id"] for item in filtered] == ["target"]


def test_fee_claim_requires_value_in_same_indexed_program_record():
    frame, plan = build_query_plan(
        "كم سعر ساعة الهندسة الميكانيكية؟", [],
    )
    mixed = (
        "programs[0].program_name: الهندسة الميكانيكية\n"
        "programs[1].program_name: هندسة الحاسوب\n"
        "programs[1].credit_hour_fee: 28"
    )
    missing = build_evidence_contract(plan, frame, [mixed])
    assert missing.sufficient is False
    assert missing.claim_coverage["claim_1"]["resolved"] is False

    bound = mixed + "\nprograms[0].credit_hour_fee: 28"
    covered = build_evidence_contract(plan, frame, [bound])
    assert covered.sufficient is True
    assert covered.claim_coverage["claim_1"]["resolved"] is True


def test_scholarship_percentage_and_retention_are_separate_claims():
    question = "ما نسبة منحة امتياز الفيزياء ومعدل استمرارها؟"
    frame, plan = build_query_plan(question, [])
    assert [claim.canonical_field for claim in plan.claims] == [
        "scholarship_rate", "scholarship_retention",
    ]
    assert all(claim.entity == "امتياز الفيزياء" for claim in plan.claims)
    assert plan.is_compound is True
    evidence = [
        "[ملف: internal_scholarships]\n"
        "scholarship_name: منحة امتياز الفيزياء\n"
        "discount_percentage: 35%\n"
        "retention_gpa_required: 80%"
    ]
    contract = build_evidence_contract(plan, frame, evidence)
    assert contract.sufficient is True
    assert contract.missing_fields == []
    assert query_rewrite.has_admission_intent(question) is False
    assert query_rewrite.has_admission_intent(
        "ما نسبة منحة تخصص الكيمياء ومعدل استمرارها؟"
    ) is False


def test_upload_preflight_reports_duplicates_and_blocks_conflicts():
    existing = {"published": [{
        "program_name": "هندسة الحاسوب", "credit_hour_fee": 5,
    }]}
    same = preflight_documents(
        [{"program_name": "هندسة الحاسوب", "credit_hour_fee": 5.0}],
        existing,
        incoming_source="incoming",
    )
    assert same["exact_duplicate_count"] >= 1
    assert same["can_publish"] is True

    changed = preflight_documents(
        [{"program_name": "هندسة الحاسوب", "credit_hour_fee": 7}],
        existing,
        incoming_source="incoming",
    )
    assert changed["conflict_count"] == 1
    assert changed["can_publish"] is False


def test_semantic_planner_is_json_bounded_and_never_answers():
    result = run_semantic_planner(
        question="كم الرسوم ومفتاحه؟",
        deterministic_plan={"claims": [], "unresolved_clauses": ["مفتاحه"]},
        recent_user_turns=["أسأل عن هندسة الحاسوب"],
        completion=lambda *_args, **_kwargs: (
            '{"decision":"planned","claims":[{"surface_text":"مفتاحه",'
            '"canonical_field":"admission_cutoff","entity":"هندسة الحاسوب",'
            '"answer_type":"admission_threshold","confidence":0.92,'
            '"refined_query":"مفتاح قبول هندسة الحاسوب"}],'
            '"unresolved_clauses":[],"concept_resolutions":[],'
            '"clarification_question":null}'
        ),
    )
    assert result["status"] == "applied"
    assert result["claims"][0]["canonical_field"] == "admission_cutoff"
    assert "answer" not in result


def test_semantic_verifier_and_pipeline_accept_path_are_bounded():
    verifier_json = (
        '{"supported_claims":["خطوة تقديم الطلب"],'
        '"unsupported_claims":[],"missing_required":[],'
        '"contradictions":[],"decision":"accept",'
        '"repair_instructions":[]}'
    )
    direct = run_semantic_verifier(
        question="كيف أقدم الطلب؟",
        answer="قدّم الطلب عبر الجهة المختصة.",
        evidence=["[ملف: الإجراءات]\nخطوة: تقديم الطلب"],
        claim_coverage={"claim_1": {"resolved": True}},
        live_policy="indexed",
        completion=lambda *_args, **_kwargs: verifier_json,
    )
    assert direct["status"] == "applied"
    assert direct["decision"] == "accept"

    bot = IUGChatbot(sessions=SessionStore())
    prepared = {
        "system": "أجب من الدليل فقط.",
        "chunks": ["[ملف: الإجراءات]\nخطوة: تقديم الطلب"],
        "validation_chunks": ["[ملف: الإجراءات]\nخطوة: تقديم الطلب"],
        "excluded": [],
        "asked_level": None,
        "generation_max_tokens": None,
        "retrieval_metadata": {
            "query_plan": {
                "claims": [{"canonical_field": "procedures"}],
                "concept_resolutions": [],
                "context_mode": "independent",
                "is_list_question": False,
                "live_policy": "indexed",
                "requires_clarification": False,
            },
            "conversation_frame": {},
            "evidence_contract": {
                "entity_terms": [],
                "sufficient": True,
                "resolved_fields": ["procedures"],
                "missing_fields": [],
                "unresolved_clauses": [],
                "claim_coverage": {"claim_1": {"resolved": True}},
            },
            "active_academic_constraints": {},
            "agentic_rag": {"max_generation_attempts": 3},
            "semantic_planner_call_count": 0,
            "live_policy": "indexed",
        },
    }
    with patch.object(config, "SEMANTIC_RAG_ENABLED", True), \
         patch("app.chatbot.embeddings.embed_query", return_value=np.array([1.0])), \
         patch.object(
             bot,
             "_complete_llm",
             side_effect=["قدّم الطلب عبر الجهة المختصة.", verifier_json],
         ) as completion:
        answer, _vector, metadata = bot._generate_validated_answer(
            prepared, "كيف أقدم الطلب؟", "semantic-verifier", client_history=[]
        )
    assert answer == "قدّم الطلب عبر الجهة المختصة."
    assert completion.call_count == 2
    assert metadata["semantic_verifier_call_count"] == 1
    assert metadata["verification_outcome"] == "accept"


def test_semantic_verifier_ignores_invented_claim_identifier():
    result = run_semantic_verifier(
        question="كيف أتأكد أن الطلب أرسل؟",
        answer="تحقق من البوابة أو تواصل مع القبول والتسجيل.",
        evidence=["[ملف: الإجراءات]\nراجع البوابة أو القبول والتسجيل."],
        claim_coverage={"claim_1": {"resolved": True}},
        live_policy="indexed",
        completion=lambda *_args, **_kwargs: (
            '{"supported_claims":["claim_1"],'
            '"unsupported_claims":[],"missing_required":["claim_4"],'
            '"contradictions":[],"decision":"repair",'
            '"repair_instructions":["غط claim_4"]}'
        ),
    )
    assert result["decision"] == "accept"
    assert result["missing_required"] == []
    assert result["repair_instructions"] == []


def test_semantic_verifier_false_missing_numeric_claim_is_bounded():
    result = run_semantic_verifier(
        question="ما حد المعادلة وما أقل علامة للمساق؟",
        answer="الحد الأقصى للمعادلة 50%، وأقل علامة للمساق 65%.",
        evidence=["الحد 50% وعلامة المساق 65% فأكثر."],
        claim_coverage={
            "claim_1": {
                "resolved": True,
                "surface_text": "ما حد معادلة المساقات",
            },
            "claim_2": {
                "resolved": True,
                "surface_text": "ما أقل علامة للمساق",
            },
        },
        live_policy="indexed",
        completion=lambda *_args, **_kwargs: (
            '{"supported_claims":["claim_1"],'
            '"unsupported_claims":[],"missing_required":["claim_2"],'
            '"contradictions":[],"decision":"repair",'
            '"repair_instructions":["أكمل claim_2"]}'
        ),
    )
    assert result["decision"] == "accept"
    assert result["missing_required"] == []


def test_certificate_acceptance_and_translation_clauses_are_planned():
    _frame, acceptance = build_query_plan(
        "ضاع أصل شهادة الثانوية وعندي كشف مصدق؛ هل تقبلونه؟", []
    )
    assert [claim.canonical_field for claim in acceptance.claims] == [
        "requirements"
    ]
    assert acceptance.unresolved_clauses == []

    _frame, translation = build_query_plan(
        "شهادتي صادرة بالإنجليزية؛ هل تحتاج ترجمة ومن يصدقها؟", []
    )
    assert [claim.canonical_field for claim in translation.claims] == [
        "requirements", "procedures"
    ]
    assert translation.unresolved_clauses == []


def test_generic_requirement_reference_does_not_become_fake_entity():
    frame, plan = build_query_plan(
        "كم مرة أستطيع التحويل داخلياً وما الشرط الأساسي؟", []
    )
    assert plan.claims[1].canonical_field == "requirements"
    assert plan.claims[1].entity is None
    contract = build_evidence_contract(plan, frame, [
        "التحويل الداخلي متاح مرتين كحد أقصى، بشرط تحقيق مفتاح قبول التخصص الجديد."
    ])
    assert contract.claim_coverage["claim_2"]["resolved"] is True


def test_transfer_equivalency_requirement_claims_tolerate_arabic_inflection():
    frame, plan = build_query_plan(
        "ما حد معادلة المساقات عند التحويل وما أقل علامة للمساق؟", []
    )
    contract = build_evidence_contract(plan, frame, [
        "عند التحويل تُعادَل المساقات بحد أقصى 50% من ساعات الخطة، "
        "وبشرط ألا تقل علامتك في المساق عن 65%."
    ])
    assert contract.sufficient is True
    assert all(
        value["resolved"] for value in contract.claim_coverage.values()
    )


def test_semantic_repair_is_verified_again_before_becoming_grounded():
    repair_json = (
        '{"supported_claims":[],"unsupported_claims":[],'
        '"missing_required":["procedures"],"contradictions":[],'
        '"decision":"repair","repair_instructions":["أكمل الإجراء"]}'
    )
    accept_json = (
        '{"supported_claims":["claim_1"],"unsupported_claims":[],'
        '"missing_required":[],"contradictions":[],'
        '"decision":"accept","repair_instructions":[]}'
    )
    bot = IUGChatbot(sessions=SessionStore())
    prepared = {
        "system": "أجب من الدليل فقط.",
        "chunks": ["[ملف: الإجراءات]\nخطوة: تقديم الطلب"],
        "validation_chunks": ["[ملف: الإجراءات]\nخطوة: تقديم الطلب"],
        "excluded": [],
        "asked_level": None,
        "generation_max_tokens": None,
        "retrieval_metadata": {
            "query_plan": {
                "claims": [{"canonical_field": "procedures"}],
                "concept_resolutions": [],
                "context_mode": "independent",
                "is_list_question": False,
                "live_policy": "indexed",
                "requires_clarification": False,
            },
            "conversation_frame": {},
            "evidence_contract": {
                "entity_terms": [],
                "sufficient": True,
                "resolved_fields": ["procedures"],
                "missing_fields": [],
                "unresolved_clauses": [],
                "claim_coverage": {"claim_1": {"resolved": True}},
            },
            "active_academic_constraints": {},
            "agentic_rag": {"max_generation_attempts": 3},
            "semantic_planner_call_count": 0,
            "live_policy": "indexed",
        },
    }
    with patch.object(config, "SEMANTIC_RAG_ENABLED", True), \
         patch("app.chatbot.embeddings.embed_query", return_value=np.array([1.0])), \
         patch.object(
             bot,
             "_complete_llm",
             side_effect=[
                 "راجع الجهة المختصة.", repair_json,
                 "قدّم الطلب عبر الجهة المختصة.", accept_json,
             ],
         ) as completion:
        answer, _vector, metadata = bot._generate_validated_answer(
            prepared, "كيف أقدم الطلب؟", "semantic-repair", client_history=[]
        )
    assert answer == "قدّم الطلب عبر الجهة المختصة."
    assert completion.call_count == 4
    assert metadata["semantic_verifier_call_count"] == 2
    assert metadata["verification_outcome"] == "accept_after_repair"


def test_unresolved_conflict_cannot_silently_select_one_value():
    bot = IUGChatbot(sessions=SessionStore())
    chunks = [
        "[ملف: أ]\nprogram_name: الهندسة\ncredit_hour_fee: 5 دينار",
        "[ملف: ب]\nprogram_name: الهندسة\ncredit_hour_fee: 7 دينار",
    ]
    prepared = {
        "system": "لا ترجح تعارضاً غير محسوم.",
        "chunks": chunks,
        "validation_chunks": chunks,
        "excluded": [],
        "asked_level": None,
        "generation_max_tokens": None,
        "retrieval_metadata": {
            "query_plan": {
                "claims": [{"canonical_field": "fee"}],
                "concept_resolutions": [],
                "context_mode": "independent",
                "is_list_question": False,
                "live_policy": "indexed",
                "requires_clarification": False,
            },
            "conversation_frame": {},
            "evidence_contract": {
                "entity_terms": [],
                "sufficient": False,
                "resolved_fields": [],
                "missing_fields": ["fee"],
                "unresolved_clauses": [],
                "claim_coverage": {},
            },
            "evidence_conflicts": [{
                "canonical_field": "fee", "values": ["5", "7"]
            }],
            "active_academic_constraints": {},
            "agentic_rag": {"max_generation_attempts": 3},
            "semantic_planner_call_count": 0,
            "live_policy": "indexed",
        },
    }
    with patch.object(config, "SEMANTIC_RAG_ENABLED", False), \
         patch("app.chatbot.embeddings.embed_query", return_value=np.array([1.0])), \
         patch.object(bot, "_complete_llm", side_effect=[
             "سعر الساعة 5 دينار.",
             "يوجد تعارض غير محسوم بين مصدرين: 5 دنانير و7 دنانير؛ يحتاج حسم الإدارة.",
         ]) as completion:
        answer, _vector, metadata = bot._generate_validated_answer(
            prepared, "كم سعر ساعة الهندسة؟", "conflict", client_history=[]
        )
    assert completion.call_count == 2
    assert "تعارض" in answer
    assert metadata["answer_check_retry"] is True


def test_strict_safe_retry_keeps_sufficient_exact_evidence():
    bot = IUGChatbot(sessions=SessionStore())
    exact_url = "https://admission.iugaza.edu.ps/form"
    prepared = {
        "system": "أجب من الدليل فقط.",
        "chunks": [f"[ملف: الدليل]\nرابط النموذج: {exact_url}"],
        "validation_chunks": [f"رابط النموذج: {exact_url}"],
        "excluded": [],
        "asked_level": None,
        "generation_max_tokens": 120,
        "retrieval_metadata": {
            "evidence_contract": {
                "sufficient": True,
                "entity_terms": [],
                "claim_coverage": {},
            },
            "evidence_conflicts": [],
            "active_academic_constraints": {},
            "query_plan": {
                "context_mode": "independent",
                "claims": [{"canonical_field": "link"}],
            },
            "conversation_frame": {},
            "agentic_rag": {"max_generation_attempts": 3},
            "semantic_planner_call_count": 0,
        },
    }
    with patch.object(bot, "_complete_llm", side_effect=[
        "الرابط هو admission_application",
        "الرابط هو admission_application",
        f"الرابط الرسمي: {exact_url}",
    ]) as completion:
        answer, _vector, metadata = bot._generate_validated_answer(
            prepared, "أعطني رابط النموذج", "exact-retry", client_history=[]
        )
    assert completion.call_count == 3
    assert exact_url in answer
    assert metadata["answer_check_post_retry_issues"] == []


def test_live_question_uses_dated_policy_without_web_route():
    _, plan = build_query_plan("ما المنح المفتوحة الآن؟", [])
    assert plan.live_policy == "dated_caveat"
    assert all(claim.time_state == "live" for claim in plan.claims)

    _, indexed = build_query_plan("ما شروط الانسحاب من المساق؟", [])
    assert indexed.live_policy == "indexed"
    assert len(indexed.claims) == 1


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


def test_bare_engineering_hours_asks_price_or_plan_clarification():
    _frame, plan = build_query_plan("كم ساعة الهندسة؟", [])
    assert plan.requires_clarification is True
    assert plan.claims == []
    assert plan.unresolved_clauses == ["كم ساعة الهندسة؟"]


def test_explicit_certificate_classification_is_a_policy_claim():
    _frame, plan = build_query_plan(
        "شهادتي لا تذكر علمي أو أدبي؛ هل تعتبرونها علمية تلقائياً؟", []
    )
    assert plan.requires_clarification is False
    assert any(c.canonical_field == "requirements" for c in plan.claims)


def test_explicit_application_fee_refund_is_not_reasked():
    _frame, plan = build_query_plan(
        "دفعت رسوم طلب الالتحاق ولم أسجل؛ هل أستردها؟", []
    )
    assert plan.requires_clarification is False
    assert any(c.canonical_field == "requirements" for c in plan.claims)
    assert [c.canonical_field for c in plan.claims] == ["requirements"]
    assert "رسوم طلب الالتحاق" in plan.claims[0].retrieval_query


def test_visa_question_is_treated_as_live_external_requirement():
    _frame, plan = build_query_plan(
        "أي نوع تأشيرة أحتاج لدخول غزة للدراسة؟", []
    )
    assert plan.live_policy == "dated_caveat"
    assert [c.canonical_field for c in plan.claims] == ["requirements"]


def test_twenty_question_client_reset_clears_history_and_reloads():
    script = (
        Path(__file__).resolve().parents[1] / "frontend" / "chat.js"
    ).read_text(encoding="utf-8")
    assert config.CHAT_TURN_LIMIT == 20
    assert "const MAX_QUESTIONS = 20;" in script
    assert "وصلت إلى 20 سؤالًا" in script
    assert 'fetch("/api/sessions/me/history"' in script
    assert 'method: "DELETE"' in script
    assert "guestHistory.length = 0" in script
    assert "window.location.reload()" in script
