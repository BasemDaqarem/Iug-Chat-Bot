"""Deterministic conversation state and query planning.

No model call is used here.  The frame is deliberately small and derives only
from user-authored turns, so a mistaken assistant answer cannot become a new
constraint.  It enriches retrieval and the final prompt; it never answers the
user directly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
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
    "الخطة", "الموعد", "الرابط", "القائمة", "المبلغ", "الجهة",
    "المفتاح", "مفتاحه", "مفتاحها",
}

CONTEXT_INDEPENDENT = "independent"
CONTEXT_FOLLOWUP = "followup"
CONTEXT_CORRECTION = "correction"
CONTEXT_ASSISTANT_REFERENCE = "assistant_reference"
CONTEXT_AMBIGUOUS = "ambiguous"


@dataclass(slots=True)
class ConversationFrame:
    context_mode: str = CONTEXT_INDEPENDENT
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
            "context_mode": "نمط السياق",
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
class ConceptResolution:
    """A context-sensitive interpretation of one surface expression.

    This is diagnostic/planning metadata only.  It never contains an answer
    value and therefore cannot bypass retrieval or the final LLM generation.
    """

    surface_text: str
    canonical_concept: str
    source: str
    confidence: float
    context_used: str | None = None

    def as_metadata(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ClaimRequirement:
    """One independently retrievable part of the user's request."""

    claim_id: str
    surface_text: str
    canonical_field: str
    entity: str | None
    scope: dict[str, Any]
    answer_type: str
    time_state: str
    retrieval_query: str
    resolution_source: str = "explicit"
    confidence: float = 1.0

    def as_metadata(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class QueryPlan:
    original_question: str
    standalone_query: str
    context_mode: str
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
    claims: list[ClaimRequirement] = field(default_factory=list)
    unresolved_clauses: list[str] = field(default_factory=list)
    concept_resolutions: list[ConceptResolution] = field(default_factory=list)
    needs_semantic_planner: bool = False
    requires_clarification: bool = False
    live_policy: str = "indexed"

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


def classify_context_mode(question: str, history: list) -> str:
    """Classify how much conversational state this turn may consume.

    Independent questions are deliberately isolated from all earlier turns.
    Follow-ups and corrections may use fresh *user-authored* turns only.  A
    referential question with no fresh anchor is ambiguous and should be
    clarified by the LLM instead of inheriting stale state.
    """
    has_fresh_anchor = bool(
        history
        and is_fresh(history[-1])
        and str(history[-1].get("user") or "").strip()
    )
    if query_rewrite.is_correction(question):
        return CONTEXT_CORRECTION if has_fresh_anchor else CONTEXT_AMBIGUOUS
    if query_rewrite.is_assistant_response_reference(question):
        return (
            CONTEXT_ASSISTANT_REFERENCE
            if has_fresh_anchor else CONTEXT_AMBIGUOUS
        )
    if not query_rewrite.needs_history_context(question):
        return CONTEXT_INDEPENDENT
    if not has_fresh_anchor:
        return CONTEXT_AMBIGUOUS
    expanded = query_rewrite.with_history_context(question, history)
    return CONTEXT_FOLLOWUP if expanded != question else CONTEXT_AMBIGUOUS


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


_CLAUSE_BREAK_RE = re.compile(
    r"(?:[؛;\n]+|[.!؟?]+\s*|\s+(?=و?(?:ما|ماذا|كم|كيف|هل|متى|أين|اين|"
    # «من» غالباً حرف جر داخل الطلب («الانسحاب من المساق»)، بينما
    # «مَن؟» الدارجة تُكتب في البيانات عادةً «مين».  لا نفصل حرف الجر
    # إلى ادعاء وهمي؛ ونبقي «ومن ...؟» كفاصل صريح.
    r"وين|مين|ومن|وشو|وايش)\b)|\s+(?=و(?:المفتاح|مفتاح(?:ه|ها)?|الرسوم|"
    r"رسوم(?:ه|ها)?|السعر|سعر(?:ه|ها)?|الرابط|رابط(?:ه|ها)?|الشروط|"
    r"شروط(?:ه|ها)?|الموعد|موعد(?:ه|ها)?|الفرع|فرع(?:ه|ها)?)(?:\b|$))|"
    r"\s+(?=و(?:ما\s+)?معدل\s+(?:الاستمرار|استمرار(?:ه|ها)?)(?:\b|$)))",
    re.IGNORECASE,
)

_KEY_RE = re.compile(r"(?:مفتاح\s+القبول|المفتاح|مفتاح(?:ه|ها)?)")
_NON_ADMISSION_KEY_MARKERS = (
    "حساب", "كلمه المرور", "كلمة المرور", "باسورد", "password",
    "مفتاح الدوله", "مفتاح الدولة", "مفتاح الاتصال", "لوحه المفاتيح",
    "لوحة المفاتيح", "مفتاح الباب", "مفتاح السياره", "مفتاح السيارة",
)
_ACADEMIC_ENTITY_MARKERS = (
    "تخصص", "برنامج", "كليه", "كلية", "قسم", "هندس", "طب", "تمريض",
    "علم", "حاسوب",
    "قباله", "قبالة", "علوم", "اداب", "آداب", "تربيه", "تربية",
    "شريعه", "شريعة", "اقتصاد", "تكنولوجيا", "بكالوريوس", "ماجستير",
)
_ENTITY_NOISE_TOKENS = {
    "ما", "وما", "ماذا", "وماذا", "كم", "وكم", "كيف", "وكيف", "هل",
    "وهل", "متى", "ومتى", "اين", "وين", "من", "مين", "شو", "وشو",
    "ايش", "وايش", "اعطني", "اعطيني", "اريد", "بدي", "ممكن",
    "يمكن", "التي", "الذي", "اذا", "كان", "تكون", "عن", "في", "على",
    "الى", "مع", "لدى", "عند", "الجامعه", "الاسلاميه", "غزه",
    "رسم", "رسوم", "سعر", "ساعه", "تكلفه", "ثوابت", "مفتاح", "المفتاح",
    "قبول", "القبول", "معدل", "معدلي", "نسبه", "نسبتي", "الحد",
    "الادنى", "الادني", "فرع", "الفرع", "تخصص", "تخصصات", "التخصص", "التخصصات",
    "برنامج", "برامج", "البرنامج", "البرامج", "كليه", "كليات", "الكليه",
    "الكليات", "قسم", "اقسام", "القسم", "الاقسام", "المتاح", "المتاحه", "خيارات",
    "خيار", "الخيار", "الخيارات", "الممكن", "الممكنه", "الممكنة", "اكاديمي", "اكاديميه", "يقبلني", "تقبلني",
    "رابط", "الرابط", "شروط", "متطلبات", "موعد", "تاريخ", "تواصل",
    "بريد", "هاتف", "رئيس", "عميد", "مدير", "مسوول",
    "هذا", "هذه", "ذلك", "تلك", "هاي", "طيب", "تمام", "حسنا",
    "اقدم", "تقديم", "قدم", "اعمل", "انجز", "احصل", "اعرف", "تقلي",
    "بتقدر", "طلب", "الطلب", "التسجيل", "تسجيل", "الالتحاق", "التحاق",
    "مفتاحه", "مفتاحها", "رسومه", "رسومها", "رابطه", "رابطها",
    "شروطه", "شروطها", "موعده", "موعدها", "الان", "اليوم", "حاليا",
    "المفتوح", "المفتوحه", "علامه", "العلامه", "درجه", "الدرجه",
    "اقل", "الاقل", "الأقل",
    "نتيجه", "النتيجه", "اذكر", "اذكرهم", "جميع", "كل", "اسماء",
    "قائمه", "القائمه", "الكامله", "انا", "اقصد", "مش", "نفسه",
    "نفسها", "مطلوب", "المطلوب", "حسب", "عمداء", "عمداءهم", "روساء", "مدراء",
    "احقق", "يحقق", "تحقق", "يتيح", "يمكنني", "يمكنك", "دخول", "ادخل",
    "التقديم", "تقديم", "الشرط", "شرط", "المبديي", "المبدئي",
    "مبديي", "مبدئي", "مبدييا", "مبدئيا", "مضمون", "مضمونه",
    "موكد", "مؤكد", "قبولي", "تسمح", "يسمح", "لي",
    "البيانات", "الحاليه", "الحالية",
    "منحه", "منحة", "المنحه", "المنحة", "استمرار", "استمرارها",
    "استمراره", "الاستمرار",
}
_LIVE_MARKERS = (
    "الان", "الآن", "اليوم", "حاليا", "حالياً", "هذه الساعه", "هذه الساعة",
    "مفتوح حاليا", "متاح حاليا", "ساري حاليا", "احدث", "أحدث",
)

_CLAIM_MARKERS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("fee", ("رسم", "رسوم", "سعر", "تكلف", "دينار", "ثوابت"), "numeric_fee"),
    ("branch", ("اي فرع", "أي فرع", "الفرع", "الفروع"), "branch_or_scope"),
    ("link", ("رابط", "بوابه", "بوابة", "صفحه", "صفحة", "نموذج"), "exact_link"),
    ("contact", ("بريد", "ايميل", "إيميل", "هاتف", "تواصل"), "exact_contact"),
    ("date", ("متى", "موعد", "تاريخ", "اخر يوم", "آخر يوم"), "date_or_schedule"),
    ("documents", ("وثائق", "اوراق", "أوراق", "مستندات"), "document_list"),
    ("requirements", (
        "شرط", "شروط", "متطلبات", "اهليه", "أهلية",
        "تحتاج ترجمه", "تحتاج ترجمة", "مطلوب ترجمه", "مطلوب ترجمة",
        "هل تقبل", "تقبلونه", "تقبلها", "حد ", "اقل علامه",
        "أقل علامة", "علامه للمساق", "علامة للمساق",
        # Policy/decision questions are requirements too.  Treating these
        # clauses as ``general`` made the planner ask the user to clarify the
        # exact thing they had already specified (refund/classification).
        "هل تعتبر", "تعتبرونها", "تعتبرها", "هل استرد", "أستردها",
        "استردها", "استرجع", "استرداد",
        "تأشيره", "تأشيرة", "تصريح الدخول",
    ), "requirements"),
    ("source", ("مصدر", "مرجع"), "source_metadata"),
    ("people", (
        "رئيس", "رؤساء", "عميد", "عمداء", "مدير", "مدراء", "مسؤول", "نائب",
    ), "person_and_role"),
    ("scholarships", ("منحه", "منحة", "منح", "اعفاء", "إعفاء"), "eligibility_or_list"),
    ("procedures", (
        "خطوة", "خطوات", "كيف", "اجراء", "إجراء", "تسجيل", "تأجيل",
        "انسحاب", "تحويل", "طلب الالتحاق", "رقم الجلوس", "رقم الهوية",
        "ينفع", "استخدام الهوية", "استخدام الهويه", "يصدقها", "تصديقها",
    ), "ordered_steps"),
    ("programs", ("تخصصات", "برامج", "مسارات", "كليات", "الاقسام", "الأقسام"), "program_or_list"),
)

_FIELD_QUERY_EXPANSIONS = {
    "fee": "رسوم سعر الساعة والثوابت والعملة",
    "admission_cutoff": "مفتاح القبول الحد الأدنى لمعدل الثانوية النسبة والفرع",
    "branch": "فرع الثانوية المسموح للبرنامج",
    "link": "الرابط الرسمي الصفحة البوابة النموذج",
    "contact": "البريد والهاتف وجهة التواصل الرسمية",
    "date": "الموعد التاريخ وفترة التقديم",
    "documents": "الوثائق والأوراق المطلوبة",
    "requirements": "الشروط والمتطلبات والأهلية",
    "source": "المصدر وتاريخ التحقق",
    "people": "الاسم والمسمى الوظيفي والجهة",
    "scholarships": "المنحة والإعفاء وشروط الأهلية",
    "scholarship_rate": "نسبة المنحة discount_percentage",
    "scholarship_retention": "معدل استمرار المنحة retention_gpa_required",
    "procedures": "الخطوات والإجراء والجهة المسؤولة",
    "programs": "البرامج والتخصصات والمسارات",
}


def split_request_clauses(question: str) -> list[str]:
    """Split explicit request parts without treating ordinary Arabic ``و`` as a separator."""
    clauses = []
    for value in _CLAUSE_BREAK_RE.split(question):
        cleaned = value.strip(" \t\r\n،,:.-؟?!؛;")
        if cleaned.startswith("و") and len(cleaned) > 1:
            possible = cleaned[1:].lstrip()
            if possible.startswith((
                "ما", "ماذا", "كم", "كيف", "هل", "متى", "أين", "اين",
                "وين", "من", "مين", "شو", "ايش", "المفتاح", "مفتاح",
                "الرسوم", "رسوم", "السعر", "سعر", "الرابط", "رابط",
                "الشروط", "شروط", "الموعد", "موعد", "الفرع", "فرع",
                "معدل",
            )):
                cleaned = possible
        if cleaned:
            clauses.append(cleaned)
    while clauses and normalize_arabic(clauses[0]) in {
        "طيب", "تمام", "حسنا", "اوكي", "حلو", "معلش",
    }:
        clauses.pop(0)
    return clauses or ([question.strip()] if question.strip() else [])


def _academic_context(text: str) -> bool:
    norm = normalize_arabic(text)
    return any(normalize_arabic(marker) in norm for marker in _ACADEMIC_ENTITY_MARKERS)


def _latest_academic_anchor(history: list) -> str | None:
    turns = _fresh_user_turns(history)
    for value in reversed(turns):
        if _academic_context(value):
            return value
    return None


def _academic_entity_phrase(text: str | None) -> str | None:
    if not text:
        return None
    values = []
    for token in tokenize(text):
        normalized = normalize_arabic(token)
        if normalized.startswith("بال") and len(normalized) > 4:
            normalized = normalized[1:]
        elif normalized.startswith("لل") and len(normalized) > 3:
            normalized = "ال" + normalized[2:]
        elif (
            normalized.startswith("ل")
            and len(normalized) >= 4
            and _academic_context(normalized[1:])
        ):
            normalized = normalized[1:]
        elif normalized.startswith("و") and len(normalized) > 4:
            normalized = normalized[1:]
        if (
            len(normalized) < 3
            or normalized.isdigit()
            or normalized in _ENTITY_NOISE_TOKENS
            or normalized in values
        ):
            continue
        values.append(normalized)
    return " ".join(values[:8]) or None


def _claim_entity(clause: str, question: str, context: str | None) -> str | None:
    return (
        _academic_entity_phrase(clause)
        or _academic_entity_phrase(question)
        or _academic_entity_phrase(context)
    )


def _resolve_key_concept(
    clause: str,
    question: str,
    history: list,
) -> tuple[str | None, ConceptResolution | None, bool]:
    """Resolve ``المفتاح`` only when its academic meaning is actually anchored."""
    norm_clause = normalize_arabic(clause)
    match = _KEY_RE.search(norm_clause)
    if not match:
        return None, None, False
    surface = match.group(0)
    norm_full = normalize_arabic(question)
    if any(normalize_arabic(marker) in norm_full for marker in _NON_ADMISSION_KEY_MARKERS):
        return "account_access", ConceptResolution(
            surface_text=surface,
            canonical_concept="account_access_key",
            source="explicit",
            confidence=0.99,
            context_used=clause,
        ), False
    if "مفتاح القبول" in norm_clause or "مفتاح قبول" in norm_clause:
        return "admission_cutoff", ConceptResolution(
            surface_text=surface,
            canonical_concept="admission_cutoff",
            source="explicit",
            confidence=1.0,
            context_used=clause,
        ), False
    academic_in_turn = _academic_context(question) or any(
        marker in norm_full for marker in ("قبول", "ثانويه", "توجيهي")
    )
    if academic_in_turn:
        return "admission_cutoff", ConceptResolution(
            surface_text=surface,
            canonical_concept="admission_cutoff",
            source="context",
            confidence=0.94,
            context_used=question,
        ), False
    anchor = _latest_academic_anchor(history)
    if anchor:
        return "admission_cutoff", ConceptResolution(
            surface_text=surface,
            canonical_concept="admission_cutoff",
            source="context",
            confidence=0.9,
            context_used=anchor,
        ), False
    # No safe academic anchor.  A semantic planner may inspect it, but the
    # retrieval layer must not guess a meaning in the meantime.
    return None, None, True


def _field_for_clause(clause: str) -> tuple[str, str, str]:
    norm = normalize_arabic(clause)
    if "معدل" in norm and "استمرار" in norm:
        return "scholarship_retention", "numeric_percentage", "explicit"
    if "نسبه" in norm and any(
        marker in norm for marker in ("منحه", "المنحه", "اعفاء")
    ):
        return "scholarship_rate", "numeric_percentage", "explicit"
    for field_name, markers, answer_type in _CLAIM_MARKERS:
        if any(normalize_arabic(marker) in norm for marker in markers):
            return field_name, answer_type, "explicit"
    return "general", "text", "explicit"


def _claim_query(clause: str, entity: str | None, field_name: str) -> str:
    values = [clause]
    if entity and normalize_arabic(entity) not in normalize_arabic(clause):
        values.append(entity)
    expansion = _FIELD_QUERY_EXPANSIONS.get(field_name)
    query = " — ".join(dict.fromkeys(value for value in values if value))
    if expansion:
        query += f" ({expansion})"
    return query


def _has_live_marker(question: str) -> bool:
    """Match live-state words as tokens, not substrings of words such as انسحاب."""
    norm = normalize_arabic(question)
    question_tokens = set(tokenize(question))
    for marker in _LIVE_MARKERS:
        normalized = normalize_arabic(marker)
        if " " in normalized:
            if normalized in norm:
                return True
        elif normalized in question_tokens:
            return True
    return False


def _build_claims(
    question: str,
    history: list,
    frame: ConversationFrame,
    *,
    retrieval_context: str | None = None,
) -> tuple[list[ClaimRequirement], list[str], list[ConceptResolution]]:
    clauses = split_request_clauses(question)
    claims: list[ClaimRequirement] = []
    unresolved: list[str] = []
    resolutions: list[ConceptResolution] = []
    context = _latest_academic_anchor(history) if frame.followup else None
    norm_question = normalize_arabic(question)
    externally_live = any(mark in norm_question for mark in (
        "تاشيره", "تصريح الدخول", "دخول غزه",
    ))
    time_state = (
        "live" if _has_live_marker(question) or externally_live else "indexed"
    )
    admission_intent = query_rewrite.has_admission_intent(question)
    eligibility_comparison = bool(
        frame.rate is not None
        and any(marker in normalize_arabic(question) for marker in (
            "احقق", "يحقق", "يتيح", "يمكنني", "يمكنك", "دخول",
            "ادخل", "التقديم", "يقبلني", "تقبلني", "قبولي",
        ))
    )
    personalized_entity = None
    if (
        retrieval_context
        and normalize_arabic(retrieval_context) != normalize_arabic(question)
        and " — " in retrieval_context
    ):
        candidate = retrieval_context.rsplit(" — ", 1)[-1].strip()
        if _academic_context(candidate):
            personalized_entity = candidate
    scope = {
        "degree_level": frame.degree_level,
        "branch": frame.branch,
        "rate": frame.rate,
        "transfer_scope": frame.transfer_scope,
    }
    for clause_index, clause in enumerate(clauses):
        norm_clause = normalize_arabic(clause)
        clause_tokens = set(tokenize(clause))
        is_scholarship_retention = (
            "معدل" in norm_clause and "استمرار" in norm_clause
        )
        if len(clauses) > 1 and not is_scholarship_retention and not clause_tokens.intersection({
                "ما", "ماذا", "كم", "كيف", "هل", "متى", "اين",
                "أين", "وين", "مين", "شو", "ايش", "اعطني", "اريد",
            }) and (
            "معدل" in norm_clause
            or "فرعي" in norm_clause
            or norm_clause in {"علمي", "ادبي", "شرعي", "صناعي", "تجاري"}
        ):
            # This is a user constraint that scopes the following request,
            # not an additional fact the answer must supply.
            continue
        key_field, resolution, unresolved_key = _resolve_key_concept(
            clause, question, history if frame.followup else []
        )
        if unresolved_key:
            unresolved.append(clause)
            continue
        if resolution is not None:
            resolutions.append(resolution)
        if key_field == "account_access":
            field_name, answer_type, resolution_source = (
                "account_access", "account_recovery", "explicit"
            )
        elif key_field:
            field_name, answer_type, resolution_source = (
                key_field,
                "admission_threshold" if key_field == "admission_cutoff" else "text",
                resolution.source if resolution else "explicit",
            )
        else:
            field_name, answer_type, resolution_source = _field_for_clause(clause)
        if (
            frame.followup
            and field_name == "requirements"
            and any(mark in norm_clause for mark in (
                "الحد الادني", "والحد الادني", "اقل معدل",
            ))
            and context
            and _academic_context(context)
        ):
            field_name = "admission_cutoff"
            answer_type = "admission_threshold"
            resolution_source = "context"
        declarative_case_fact = norm_clause.startswith((
            "شهادتي ", "دفعت ", "قدمت ", "سجلت ", "ارسلت ", "أرسلت ",
            "انا دفعت ", "أنا دفعت ",
        ))
        descriptive_context = bool(
            len(clauses) > 1
            and clause_index < len(clauses) - 1
            and (
                declarative_case_fact
                or (
                    field_name == "general"
                    and not clause_tokens.intersection({
                    "ما", "ماذا", "كم", "كيف", "هل", "متى", "اين",
                    "أين", "وين", "مين", "شو", "ايش", "اعطني", "اريد",
                    })
                )
                or (field_name == "general" and norm_clause.startswith((
                    "ما عندي", "انا ", "شهادتي ", "نسيت "
                )))
            )
        )
        if descriptive_context:
            # A semicolon often separates case facts from the actual request:
            # ``شهادتي من السعودية؛ شو أول خطوة؟``.  The fact remains in the
            # standalone retrieval query but is not an unresolved answer part.
            continue
        if (
            admission_intent
            and key_field is None
            and (
                field_name in {"programs", "general"}
                or (eligibility_comparison and field_name == "requirements")
            )
        ):
            field_name = "admission_cutoff"
            answer_type = "eligibility_or_list"
            resolution_source = "context"
        if field_name == "general" and len(clauses) > 1:
            # In a multipart request, an unclassified clause must not borrow
            # arbitrary evidence through the permissive ``general`` field.
            # Leave it explicit for the semantic planner or clarification.
            unresolved.append(clause)
            continue
        generic_requirement_reference = bool(
            field_name == "requirements"
            and norm_clause in {
                "ما الشرط", "ما الشرط الاساسي", "الشرط الاساسي",
                "وما الشرط", "وما الشرط الاساسي",
            }
        )
        list_selection_reference = bool(
            field_name == "fee"
            and any(mark in normalize_arabic(question) for mark in (
                "اي واحد", "اي وحده", "مين فيهم", "من فيهم",
            ))
        )
        entity = (
            None
            if field_name in {"account_access", "procedures"}
            or generic_requirement_reference
            or list_selection_reference
            else personalized_entity or _claim_entity(clause, question, context)
        )
        # In ``دفعت رسوم طلب الالتحاق؛ هل أستردها؟`` the noun lives in the
        # declarative case clause while the request carries only a pronoun.
        # Preserve that explicit object in the retrieval claim so a nearby
        # service fee cannot be mistaken for the refund policy.
        if (
            field_name == "requirements"
            and "استرد" in norm_clause
            and "رسوم طلب الالتحاق" in normalize_arabic(question)
        ):
            entity = "رسوم طلب الالتحاق"
        if (
            frame.context_mode == CONTEXT_AMBIGUOUS
            and query_rewrite.has_reference_tokens(question)
            and field_name in {"fee", "admission_cutoff", "branch", "requirements"}
            and not entity
        ):
            unresolved.append(clause)
            continue
        claims.append(ClaimRequirement(
            claim_id=f"claim_{len(claims) + 1}",
            surface_text=clause,
            canonical_field=field_name,
            entity=entity,
            scope=dict(scope),
            answer_type=answer_type,
            time_state=time_state,
            retrieval_query=_claim_query(clause, entity, field_name),
            resolution_source=resolution_source,
            confidence=resolution.confidence if resolution else 1.0,
        ))
    return claims, unresolved, resolutions


def build_conversation_frame(question: str, history: list) -> ConversationFrame:
    context_mode = classify_context_mode(question, history)
    contextual_history = (
        history
        if context_mode in {
            CONTEXT_FOLLOWUP, CONTEXT_CORRECTION, CONTEXT_ASSISTANT_REFERENCE
        }
        else []
    )
    expanded = query_rewrite.with_history_context(question, contextual_history)
    positive = query_rewrite.positive_query(expanded)
    constraints = query_rewrite.latest_academic_constraints(
        question, contextual_history
    )
    domains = _domains(positive)
    reference = _reference(question, contextual_history)
    has_reference = query_rewrite.has_reference_tokens(question)
    followup = context_mode in {
        CONTEXT_FOLLOWUP, CONTEXT_CORRECTION, CONTEXT_ASSISTANT_REFERENCE
    }
    ambiguous = bool(
        context_mode == CONTEXT_AMBIGUOUS
        # A referential word inside an otherwise self-contained academic
        # question (e.g. «سعر هندسة الحاسوب وما المفتاح؟») is anchored by the
        # same turn and must not be mistaken for a missing conversation turn.
        or (
            has_reference
            and reference is None
            and not _academic_context(positive)
        )
        or (_transfer_scope(expanded) == "unspecified")
    )
    intent = domains[0] if len(domains) == 1 else "compound"
    return ConversationFrame(
        context_mode=context_mode,
        intent=intent,
        active_topic=_active_topic(positive, contextual_history),
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
    contextual_history = (
        history
        if frame.context_mode in {
            CONTEXT_FOLLOWUP, CONTEXT_CORRECTION, CONTEXT_ASSISTANT_REFERENCE
        }
        else []
    )
    standalone_base = query_rewrite.with_history_context(
        seed, contextual_history
    )
    standalone_base = query_rewrite.positive_query(standalone_base)
    standalone = query_rewrite.add_canonical_terms(standalone_base)
    claims, unresolved_clauses, concept_resolutions = _build_claims(
        question,
        contextual_history,
        frame,
        retrieval_context=seed,
    )
    # «كم ساعة الهندسة؟» has two common, materially different meanings:
    # credit-hour price and total plan hours.  With neither price nor plan
    # stated, retrieving either number is a guess; ask one short clarification.
    norm_question = normalize_arabic(question)
    bare_hour_question = bool(
        re.fullmatch(r"\s*كم\s+ساع(?:ه|ة)\s+.+?[؟?]?\s*", norm_question)
        and not any(marker in norm_question for marker in (
            "سعر", "رسوم", "تكلف", "خطه", "الخطة", "عدد", "معتمده", "معتمدة",
        ))
    )
    if bare_hour_question:
        claims = []
        unresolved_clauses = [question.strip()]
    # A follow-up claim must search with its user-authored anchor; a
    # personalized student query must retain both the literal wording and the
    # student's major.  The surface clause remains untouched for diagnostics.
    if (
        frame.followup
        or normalize_arabic(seed) != normalize_arabic(question)
    ):
        for claim in claims:
            claim.retrieval_query = _claim_query(
                standalone_base, None, claim.canonical_field
            )
    claim_fields = list(dict.fromkeys(
        claim.canonical_field
        for claim in claims
        if claim.canonical_field not in {"general", "account_access"}
    ))
    if claim_fields:
        frame.requested_fields = claim_fields
    field_domains = {
        "fee": "fees",
        "admission_cutoff": "admissions",
        "branch": "admissions",
        "requirements": "admissions",
        "scholarships": "scholarships",
        "scholarship_rate": "scholarships",
        "scholarship_retention": "scholarships",
        "programs": "programs",
        "procedures": "procedures",
        "date": "deadlines",
        "documents": "documents",
        "link": "contacts",
        "contact": "contacts",
        "source": "general",
        "people": "people",
    }
    derived_domains = [
        field_domains[field_name]
        for field_name in claim_fields
        if field_name in field_domains
    ]
    if derived_domains:
        # Claims describe what this turn asks for; inherited history may help
        # identify the entity but may not add old requested fields/domains.
        frame.domains = list(dict.fromkeys(derived_domains)) or ["general"]
    if unresolved_clauses:
        frame.ambiguous = True
    frame.intent = (
        frame.domains[0] if len(frame.domains) == 1 else "compound"
    )

    # Canonical terminology is appended only *after* the surface expression
    # has been resolved.  Thus a password/country key can never accidentally
    # pull admission documents merely because it contains «مفتاح».
    canonical_additions = []
    for claim in claims:
        expansion = _FIELD_QUERY_EXPANSIONS.get(claim.canonical_field)
        if expansion and expansion not in canonical_additions:
            canonical_additions.append(expansion)
    if canonical_additions:
        standalone += " (" + "؛ ".join(canonical_additions) + ")"
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
        for turn in _fresh_user_turns(contextual_history)
    )
    is_list = query_rewrite.wants_complete_list(question) or inherited_list
    is_compound = (
        query_rewrite.is_multi_part_question(question)
        or len(claims) + len(unresolved_clauses) > 1
        or len(frame.requested_fields) > 1
    )
    exact = query_rewrite.requires_direct_evidence(question)
    advanced = frame.ambiguous or frame.followup or is_list or is_compound or exact
    route = "advanced_rag" if advanced else "fast_rag"
    expected = (
        "multipart"
        if is_compound
        else claims[0].answer_type
        if len(claims) == 1
        else _EXPECTED_BY_DOMAIN.get(frame.domains[0], "text")
    )
    needs_semantic_planner = bool(
        unresolved_clauses
        or frame.ambiguous
        or is_compound
        or any(item.source == "semantic" for item in concept_resolutions)
    )
    requires_clarification = bool(unresolved_clauses and not claims)
    live_policy = (
        "dated_caveat"
        if any(claim.time_state == "live" for claim in claims)
        else "indexed"
    )
    plan = QueryPlan(
        original_question=question,
        standalone_query=standalone,
        context_mode=frame.context_mode,
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
        claims=claims,
        unresolved_clauses=unresolved_clauses,
        concept_resolutions=concept_resolutions,
        needs_semantic_planner=needs_semantic_planner,
        requires_clarification=requires_clarification,
        live_policy=live_policy,
    )
    return frame, plan


__all__ = [
    "ConversationFrame",
    "ClaimRequirement",
    "ConceptResolution",
    "QueryPlan",
    "build_conversation_frame",
    "build_query_plan",
    "classify_context_mode",
    "split_request_clauses",
    "CONTEXT_INDEPENDENT",
    "CONTEXT_FOLLOWUP",
    "CONTEXT_CORRECTION",
    "CONTEXT_ASSISTANT_REFERENCE",
    "CONTEXT_AMBIGUOUS",
]
