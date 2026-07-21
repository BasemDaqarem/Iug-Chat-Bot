"""Deterministic conversation state and query planning.

No model call is used here.  The frame is deliberately small and derives only
from user-authored turns, so a mistaken assistant answer cannot become a new
constraint.  It enriches retrieval and the final prompt; it never answers the
user directly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app import query_rewrite
from app.sessions import is_fresh
from app.text_norm import normalize_arabic, tokenize


_DOMAIN_MARKERS = {
    "fees": ("رسوم", "سعر", "تكلف", "ادفع", "دفع", "دينار", "ساعة"),
    "admissions": ("قبول", "يقبل", "توجيهي", "ثانوية", "التحاق"),
    "scholarships": ("منح", "منحة", "اعفاء", "إعفاء", "مساعدة مالية"),
    "programs": ("تخصص", "برنامج", "مسار", "كلية", "قسم"),
    "procedures": ("خطوات", "طلب", "تسجيل", "تأجيل", "انسحاب", "تحويل"),
    "deadlines": ("موعد", "متى", "تاريخ", "آخر يوم"),
    "documents": ("وثائق", "اوراق", "أوراق", "شهادة", "تصديق"),
    "contacts": ("تواصل", "بريد", "ايميل", "هاتف", "رقم", "عنوان", "مكان"),
    "people": ("رئيس", "عميد", "مدير", "مسؤول", "نائب"),
    "privacy": ("ترتيب", "هوية", "بيانات طالب", "معدل طالب"),
}

_EXPECTED_BY_DOMAIN = {
    "fees": "numeric_fee",
    "admissions": "eligibility_or_requirements",
    "scholarships": "eligibility_or_list",
    "programs": "program_or_list",
    "procedures": "ordered_steps",
    "deadlines": "date_or_schedule",
    "documents": "document_list",
    "contacts": "exact_contact",
    "people": "person_and_role",
    "privacy": "safe_refusal_or_own_record",
}

_TOPIC_MARKERS = (
    "تأجيل", "انسحاب", "تحويل", "تسجيل", "قبول", "منح", "رسوم",
    "تخصصات", "برامج", "كليات", "معدل", "رقم الجلوس", "وصف المساق",
    "بوابة", "نموذج", "تخرج", "وثائق", "عميد", "رئيس",
)

_REFERENCE_WORDS = {
    "الرقم", "رقمه", "رقمها", "مكانه", "مكانها", "شروطه", "شروطها",
    "رسومه", "رسومها", "رابطه", "رابطها", "الطلبات", "الطلبات السابقة",
    "المذكور", "السابق", "هؤلاء", "هذه", "هذا",
}


@dataclass(slots=True)
class ConversationFrame:
    intent: str = "general"
    active_topic: str | None = None
    reference: str | None = None
    degree_level: str | None = None
    branch: str | None = None
    rate: float | None = None
    rate_type: str | None = None
    transfer_scope: str | None = None
    requested_fields: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    ambiguous: bool = False
    followup: bool = False

    def as_metadata(self) -> dict[str, Any]:
        return asdict(self)

    def prompt_block(self) -> str:
        rows = []
        labels = {
            "intent": "النية",
            "active_topic": "الموضوع النشط",
            "reference": "المرجع",
            "degree_level": "المرحلة",
            "branch": "فرع الثانوية",
            "rate": "المعدل",
            "rate_type": "نوع المعدل",
            "transfer_scope": "نطاق التحويل",
        }
        for key, label in labels.items():
            value = getattr(self, key)
            if value is not None and value != "":
                rows.append(f"- {label}: {value}")
        if self.domains:
            rows.append("- المجالات: " + "، ".join(self.domains))
        if self.requested_fields:
            rows.append("- الحقول المطلوبة: " + "، ".join(self.requested_fields))
        if self.exclusions:
            rows.append("- المستبعد صراحة: " + "، ".join(self.exclusions))
        rows.append(f"- سؤال تابع: {'نعم' if self.followup else 'لا'}")
        rows.append(f"- ما زال مبهمًا: {'نعم' if self.ambiguous else 'لا'}")
        return "\n".join(rows)


@dataclass(slots=True)
class QueryPlan:
    original_question: str
    standalone_query: str
    intent: str
    domains: list[str]
    entities: dict[str, Any]
    expected_answer_type: str
    is_followup: bool
    is_ambiguous: bool
    is_compound: bool
    is_list_question: bool
    route: str
    needs_reranking: bool
    needs_query_expansion: bool

    def as_metadata(self) -> dict[str, Any]:
        return asdict(self)


def _fresh_user_turns(history: list) -> list[str]:
    result = []
    for turn in history[-5:]:
        if not is_fresh(turn):
            continue
        value = str(turn.get("user") or "").strip()
        if value:
            result.append(value)
    return result


def _domains(text: str) -> list[str]:
    norm = normalize_arabic(text)
    found = []
    for domain, markers in _DOMAIN_MARKERS.items():
        if any(normalize_arabic(marker) in norm for marker in markers):
            found.append(domain)
    # «معدلي» وحدها قد تكون عن منحة أو المعدل الجامعي. نية القبول تحتاج
    # اقتران المعدل ببرنامج/تخصص أو فعل قبول، فلا نخلط المجالين.
    if "معدل" in norm and any(
        marker in norm
        for marker in ("تخصص", "برنامج", "كليه", "يقبل", "قبول")
    ):
        if "admissions" not in found:
            found.append("admissions")
    return found or ["general"]


def _active_topic(question: str, history: list) -> str | None:
    texts = [question] + list(reversed(_fresh_user_turns(history)))
    for text in texts:
        norm = normalize_arabic(text)
        for marker in _TOPIC_MARKERS:
            if normalize_arabic(marker) in norm:
                return marker
    return None


def _reference(question: str, history: list) -> str | None:
    tokens = set(tokenize(question))
    reference_tokens = {normalize_arabic(word) for word in _REFERENCE_WORDS}
    if not tokens.intersection(reference_tokens):
        return None
    previous = _fresh_user_turns(history)
    return previous[-1] if previous else None


def _rate_type(question: str, domains: list[str]) -> str | None:
    norm = normalize_arabic(question)
    if "تراكمي" in norm or "جامعي" in norm:
        return "university_gpa"
    if "ثان" in norm or "توجيهي" in norm or "admissions" in domains:
        return "high_school"
    return None


def _transfer_scope(text: str) -> str | None:
    norm = normalize_arabic(text)
    if "تحويل" not in norm:
        return None
    if any(mark in norm for mark in ("من جامعه", "جامعة اخرى", "خارجي", "بين الجامعات")):
        return "external"
    if any(mark in norm for mark in ("داخل", "بين التخصصات", "تخصص لتخصص")):
        return "internal"
    return "unspecified"


def _requested_fields(question: str, domains: list[str]) -> list[str]:
    norm = normalize_arabic(question)
    fields = []
    mapping = {
        "fee": ("رسوم", "سعر", "تكلف", "ادفع"),
        "requirements": ("شرط", "شروط", "متطلبات"),
        "link": ("رابط", "بوابه", "صفحه", "نموذج"),
        "contact": ("بريد", "ايميل", "هاتف", "تواصل"),
        "date": ("متى", "موعد", "تاريخ"),
        "documents": ("وثائق", "اوراق", "أوراق"),
        "programs": ("تخصص", "برنامج", "مسار", "كلية"),
        "source": ("مصدر", "مرجع"),
    }
    for field_name, markers in mapping.items():
        if any(normalize_arabic(marker) in norm for marker in markers):
            fields.append(field_name)
    return fields or list(dict.fromkeys(domains))


def build_conversation_frame(question: str, history: list) -> ConversationFrame:
    expanded = query_rewrite.with_history_context(question, history)
    positive = query_rewrite.positive_query(expanded)
    constraints = query_rewrite.latest_academic_constraints(question, history)
    domains = _domains(positive)
    reference = _reference(question, history)
    has_reference = query_rewrite.has_reference_tokens(question)
    followup = bool(history and expanded != question)
    ambiguous = bool(
        (has_reference and reference is None)
        or (_transfer_scope(expanded) == "unspecified")
    )
    intent = domains[0] if len(domains) == 1 else "compound"
    return ConversationFrame(
        intent=intent,
        active_topic=_active_topic(positive, history),
        reference=reference,
        degree_level=constraints.get("degree"),
        branch=constraints.get("branch"),
        rate=constraints.get("rate"),
        rate_type=_rate_type(positive, domains),
        transfer_scope=_transfer_scope(positive),
        requested_fields=_requested_fields(positive, domains),
        exclusions=query_rewrite.extract_exclusions(question),
        domains=domains,
        ambiguous=ambiguous,
        followup=followup,
    )


def build_query_plan(
    question: str,
    history: list,
    *,
    retrieval_question: str | None = None,
) -> tuple[ConversationFrame, QueryPlan]:
    seed = retrieval_question or question
    frame = build_conversation_frame(seed, history)
    standalone = query_rewrite.with_history_context(seed, history)
    standalone = query_rewrite.positive_query(standalone)
    standalone = query_rewrite.add_canonical_terms(standalone)
    constraints = []
    # Only inherited constraints are appended.  Constraints stated in the
    # current question are already present and appending them caused a needless
    # second raw search for clear questions.
    if frame.followup:
        if frame.branch:
            constraints.append(f"الفرع: {frame.branch}")
        if frame.rate is not None:
            constraints.append(f"المعدل: {frame.rate:g}%")
        if frame.degree_level:
            constraints.append(f"المرحلة: {frame.degree_level}")
    if constraints:
        standalone += " (" + "، ".join(constraints) + ")"

    inherited_list = frame.followup and any(
        query_rewrite.wants_complete_list(turn)
        for turn in _fresh_user_turns(history)
    )
    is_list = query_rewrite.wants_complete_list(question) or inherited_list
    is_compound = (
        query_rewrite.is_multi_part_question(question)
        or len(frame.requested_fields) > 1
    )
    exact = query_rewrite.requires_direct_evidence(question)
    advanced = frame.ambiguous or frame.followup or is_list or is_compound or exact
    route = "advanced_rag" if advanced else "fast_rag"
    expected = "multipart" if is_compound else _EXPECTED_BY_DOMAIN.get(frame.domains[0], "text")
    plan = QueryPlan(
        original_question=question,
        standalone_query=standalone,
        intent=frame.intent,
        domains=frame.domains,
        entities={
            "degree_level": frame.degree_level,
            "branch": frame.branch,
            "rate": frame.rate,
            "rate_type": frame.rate_type,
            "transfer_scope": frame.transfer_scope,
            "topic": frame.active_topic,
        },
        expected_answer_type=expected,
        is_followup=frame.followup,
        is_ambiguous=frame.ambiguous,
        is_compound=is_compound,
        is_list_question=is_list,
        route=route,
        needs_reranking=(advanced and not is_list and not query_rewrite.has_admission_intent(standalone)),
        needs_query_expansion=(frame.ambiguous or exact),
    )
    return frame, plan


__all__ = [
    "ConversationFrame",
    "QueryPlan",
    "build_conversation_frame",
    "build_query_plan",
]
