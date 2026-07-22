"""Bounded semantic planner and verifier for risky RAG turns.

Both helpers are advisory, JSON-only LLM calls.  They never return a final
user answer, never search the web, and cannot open an unbounded loop.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable


Completion = Callable[..., str]

_ALLOWED_FIELDS = {
    "fee", "admission_cutoff", "branch", "requirements", "link", "contact",
    "date", "documents", "programs", "source", "people", "scholarships",
    "scholarship_rate", "scholarship_retention", "procedures",
    "account_access", "general",
}
_PLANNER_DECISIONS = {"planned", "clarify"}
_VERIFIER_DECISIONS = {"accept", "repair", "clarify", "dated_caveat"}


def _answer_materially_mentions_claim(answer: str, coverage: dict[str, Any]) -> bool:
    """Bound false-negative verifier output with a conservative lexical check."""
    surface = str(coverage.get("surface_text") or "").lower()
    candidate = (answer or "").lower()
    if not surface or not candidate or not coverage.get("resolved"):
        return False
    words = [
        value for value in re.findall(r"[\w\u0600-\u06ff]+", surface)
        if len(value) >= 3 and value not in {
            "ماذا", "كيف", "وما", "المطلوب", "الاساسي", "الأساسي",
        }
    ]
    overlap = sum(value in candidate for value in set(words))
    needed = 1 if len(set(words)) <= 2 else 2
    if overlap < needed:
        return False
    numeric_shape = any(
        value in surface for value in ("كم", "حد", "اقل", "أقل", "علام", "معدل", "نسب")
    )
    return not numeric_shape or bool(re.search(r"\d", candidate))


def _extract_json(text: str) -> dict[str, Any]:
    value = (text or "").strip()
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    decoder = json.JSONDecoder()
    for index, char in enumerate(value):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(value[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("semantic LLM did not return a JSON object")


def run_semantic_planner(
    *,
    question: str,
    deterministic_plan: dict[str, Any],
    recent_user_turns: list[str],
    evidence_gaps: list[str] | None = None,
    completion: Completion,
    max_tokens: int = 700,
) -> dict[str, Any]:
    system = """\
أنت مخطط بحث دلالي جامعي، ولست مجيباً للمستخدم.
حلل المطلوب إلى ادعاءات مستقلة فقط، ولا تنتج أي قيمة أو إجابة معرفية.
لا تعتبر كلمة «المفتاح» مفتاح قبول إلا بوجود برنامج/تخصص/قبول في السؤال
أو في دور مستخدم حديث ذي صلة. كلمات الحساب وكلمة المرور ومفتاح الدولة ليست
قبولاً. لا تستخدم نص المساعد السابق كحقيقة.

أعد JSON صالحاً فقط بهذا الشكل:
{
  "decision": "planned|clarify",
  "claims": [{
    "surface_text": "...",
    "canonical_field": "fee|admission_cutoff|branch|requirements|link|contact|date|documents|programs|source|people|scholarships|scholarship_rate|scholarship_retention|procedures|account_access|general",
    "entity": "... أو null",
    "answer_type": "...",
    "confidence": 0.0,
    "refined_query": "استعلام بحث بلا إجابة"
  }],
  "unresolved_clauses": ["..."],
  "concept_resolutions": [{
    "surface_text": "...",
    "canonical_concept": "...",
    "confidence": 0.0,
    "context_used": "... أو null"
  }],
  "clarification_question": "سؤال عربي قصير أو null"
}
لا تحذف ادعاءً موجوداً في الخطة الحتمية؛ أضف أو صحح فقط عند ثقة واضحة.
في أسئلة المنح: scholarship_rate تعني discount_percentage، و
scholarship_retention تعني retention_gpa_required؛ لا تحولهما إلى تاريخ.
"""
    payload = {
        "question": question,
        "recent_user_turns_only": recent_user_turns[-4:],
        "deterministic_plan": deterministic_plan,
        "evidence_gaps": evidence_gaps or [],
    }
    try:
        raw = completion(
            system,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            max_tokens=max_tokens,
        )
        parsed = _extract_json(raw)
        decision = str(parsed.get("decision") or "").strip()
        if decision not in _PLANNER_DECISIONS:
            raise ValueError("invalid planner decision")
        claims = []
        for item in parsed.get("claims") or []:
            if not isinstance(item, dict):
                continue
            field_name = str(item.get("canonical_field") or "")
            if field_name not in _ALLOWED_FIELDS:
                continue
            try:
                confidence = float(item.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            claims.append({
                "surface_text": str(item.get("surface_text") or "").strip(),
                "canonical_field": field_name,
                "entity": (
                    str(item.get("entity")).strip()
                    if item.get("entity") not in (None, "") else None
                ),
                "answer_type": str(item.get("answer_type") or "text"),
                "confidence": max(0.0, min(1.0, confidence)),
                "refined_query": str(item.get("refined_query") or "").strip(),
            })
        return {
            "called": True,
            "status": "applied",
            "decision": decision,
            "claims": claims,
            "unresolved_clauses": [
                str(value).strip()
                for value in (parsed.get("unresolved_clauses") or [])
                if str(value).strip()
            ],
            "concept_resolutions": [
                value for value in (parsed.get("concept_resolutions") or [])
                if isinstance(value, dict)
            ],
            "clarification_question": (
                str(parsed.get("clarification_question")).strip()
                if parsed.get("clarification_question") else None
            ),
        }
    except Exception as exc:
        return {
            "called": True,
            "status": "unavailable",
            "decision": None,
            "claims": [],
            "unresolved_clauses": list(
                deterministic_plan.get("unresolved_clauses") or []
            ),
            "concept_resolutions": [],
            "clarification_question": None,
            "error_type": type(exc).__name__,
        }


def should_run_semantic_verifier(
    plan: dict[str, Any],
    frame: dict[str, Any],
) -> bool:
    fields = {
        str(item.get("canonical_field"))
        for item in (plan.get("claims") or [])
        if isinstance(item, dict)
    }
    sensitive_shapes = {
        "procedures", "requirements", "scholarships", "programs",
        "scholarship_rate", "scholarship_retention", "admission_cutoff",
    }
    semantic_resolution = any(
        item.get("source") == "semantic"
        for item in (plan.get("concept_resolutions") or [])
        if isinstance(item, dict)
    )
    return bool(
        fields & sensitive_shapes
        or plan.get("is_list_question")
        or plan.get("context_mode") == "correction"
        or semantic_resolution
        or plan.get("live_policy") == "dated_caveat"
        or (
            frame.get("rate") is not None
            and "admission_cutoff" in fields
        )
    )


def run_semantic_verifier(
    *,
    question: str,
    answer: str,
    evidence: list[str],
    claim_coverage: dict[str, Any],
    live_policy: str,
    completion: Completion,
    max_tokens: int = 650,
    max_evidence_chars: int = 14000,
) -> dict[str, Any]:
    system = """\
أنت مدقق دلالي لإجابة RAG جامعية، ولست طبقة صياغة.
قارن كل ادعاء في الإجابة بالأدلة وعقد التغطية. لا تستخدم معرفتك الخارجية.
افحص ربط القيم بالبرنامج/الكيان، اكتمال الحقول، ترتيب الخطوات، الأهلية،
اتجاه مقارنة المعدل بمفتاح القبول، والتصحيحات. في سؤال حي لا تقبل ادعاء
الحالة الحالية من فهرس مؤرخ؛ يلزم ذكر آخر معلومة وتاريخها أو طلب تحقق رسمي.
في الإجراءات، اعتبر أسماء البنوك والدول وحقول الواجهة وأرقام الحسابات
والخطوات الفرعية ادعاءات مستقلة: لا تقبل واحداً منها إلا إذا ورد حرفياً في
الدليل، ولا تسمح للنموذج بتفصيل عبارة عامة مثل «حوالات بنكية» من ذاكرته.

أعد JSON صالحاً فقط:
{
  "supported_claims": ["..."],
  "unsupported_claims": ["..."],
  "missing_required": ["..."],
  "contradictions": ["..."],
  "decision": "accept|repair|clarify|dated_caveat",
  "repair_instructions": ["..."]
}
اختر accept فقط إذا كانت كل أجزاء السؤال مغطاة ولا يوجد ادعاء غير مسند.
لا تُنشئ معرف claim_N غير موجود في claim_coverage.
"""
    evidence_text = "\n\n---\n\n".join(evidence)
    evidence_text = evidence_text[:max_evidence_chars]
    payload = {
        "question": question,
        "candidate_answer": answer,
        "claim_coverage": claim_coverage,
        "live_policy": live_policy,
        "evidence": evidence_text,
    }
    try:
        raw = completion(
            system,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            max_tokens=max_tokens,
        )
        parsed = _extract_json(raw)
        decision = str(parsed.get("decision") or "")
        if decision not in _VERIFIER_DECISIONS:
            raise ValueError("invalid verifier decision")
        result = {
            "called": True,
            "status": "applied",
            "decision": decision,
        }
        for key in (
            "supported_claims", "unsupported_claims", "missing_required",
            "contradictions", "repair_instructions",
        ):
            result[key] = [
                str(value).strip()
                for value in (parsed.get(key) or [])
                if str(value).strip()
            ]
        # The verifier is advisory and may itself invent a claim identifier.
        # Never reject a sound answer for ``claim_N`` unless that identifier
        # actually exists in the deterministic evidence contract.
        known_claims = {str(value) for value in claim_coverage}
        claim_ref_re = re.compile(r"\bclaim_\d+\b", re.IGNORECASE)

        def valid_claim_refs(value: str) -> bool:
            refs = set(claim_ref_re.findall(value))
            return not refs or refs.issubset(known_claims)

        for key in (
            "supported_claims", "unsupported_claims", "missing_required",
            "contradictions", "repair_instructions",
        ):
            result[key] = [
                value for value in result[key] if valid_claim_refs(value)
            ]
        removed_missing_refs: set[str] = set()
        retained_missing: list[str] = []
        for value in result["missing_required"]:
            refs = set(claim_ref_re.findall(value))
            if refs and all(
                ref in claim_coverage
                and _answer_materially_mentions_claim(
                    answer, claim_coverage[ref]
                )
                for ref in refs
            ):
                removed_missing_refs.update(refs)
                continue
            retained_missing.append(value)
        result["missing_required"] = retained_missing
        if removed_missing_refs:
            result["repair_instructions"] = [
                value for value in result["repair_instructions"]
                if not set(claim_ref_re.findall(value)).issubset(
                    removed_missing_refs
                )
                or not claim_ref_re.findall(value)
            ]
        if (
            result["decision"] in {"repair", "clarify"}
            and not result["unsupported_claims"]
            and not result["missing_required"]
            and not result["contradictions"]
            and not result["repair_instructions"]
        ):
            result["decision"] = "accept"
        return result
    except Exception as exc:
        return {
            "called": True,
            "status": "unavailable",
            "decision": None,
            "supported_claims": [],
            "unsupported_claims": [],
            "missing_required": [],
            "contradictions": [],
            "repair_instructions": [],
            "error_type": type(exc).__name__,
        }


__all__ = [
    "run_semantic_planner",
    "run_semantic_verifier",
    "should_run_semantic_verifier",
]
