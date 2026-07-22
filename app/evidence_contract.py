"""Deterministic evidence requirements for a generated answer.

The contract does not answer questions.  It tells the LLM what is and is not
supported, and exposes structured metadata to tests and diagnostics.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any

from app.conversation_frame import ConversationFrame, QueryPlan
from app.text_norm import normalize_arabic, tokenize


_FIELD_MARKERS = {
    "fee": ("رسوم", "سعر", "دينار", "credit_hour_fee", "ثوابت"),
    "requirements": ("شروط", "متطلبات", "يشترط", "معدل", "min_high_school"),
    "link": ("https://", "رابط", "بوابة", "صفحة"),
    "contact": ("@", "بريد", "هاتف", "تواصل", "+970"),
    "date": ("موعد", "تاريخ", "202", "الفصل"),
    "documents": ("وثائق", "أوراق", "شهادة", "صورة", "تصديق"),
    "programs": ("تخصص", "برنامج", "مسار", "كلية", "البرامج:"),
    "source": ("ملف:", "مصدر", "تاريخ إدخال"),
    "admissions": ("قبول", "مفتاح", "min_high_school", "%"),
    "scholarships": ("منحة", "منح", "إعفاء", "مساعدة مالية"),
    "procedures": ("خطوة", "طلب", "إدخال", "تسجيل", "دفع"),
    "deadlines": ("موعد", "تاريخ", "آخر", "الفصل"),
    "contacts": ("@", "هاتف", "بريد", "عنوان", "تواصل"),
    "people": ("الاسم", "full_name", "عميد", "رئيس", "مدير"),
    "privacy": ("خصوصية", "خاصة", "لا يمكن"),
    "general": (),
}


_DOMAIN_FIELD = {
    "fees": "fee",
    "admissions": "admissions",
    "scholarships": "scholarships",
    "programs": "programs",
    "procedures": "procedures",
    "deadlines": "deadlines",
    "documents": "documents",
    "contacts": "contact",
    "people": "people",
    "privacy": "privacy",
    "general": "general",
}

_SENSITIVE_FIELDS = {
    "fee", "requirements", "link", "contact", "date", "documents",
    "programs", "admissions", "scholarships", "procedures", "deadlines",
    "contacts", "people", "privacy",
}


@dataclass(slots=True)
class EvidenceContract:
    required_fields: list[str] = field(default_factory=list)
    resolved_fields: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    sufficient: bool = False
    fact_sensitive: bool = False
    authoritative_evidence_used: bool = False
    entity_terms: list[str] = field(default_factory=list)
    entity_supported: bool = True
    field_evidence: dict[str, list[int]] = field(default_factory=dict)

    def as_metadata(self) -> dict[str, Any]:
        return asdict(self)

    def prompt_block(self) -> str:
        lines = []
        if self.required_fields:
            lines.append("- المطلوب إثباته: " + "، ".join(self.required_fields))
        if self.resolved_fields:
            lines.append("- المسند بوضوح: " + "، ".join(self.resolved_fields))
        if self.missing_fields:
            lines.append("- غير المسند بوضوح: " + "، ".join(self.missing_fields))
        if self.contradictions:
            lines.append("- تعارضات تحتاج حذراً: " + "؛ ".join(self.contradictions))
        if self.entity_terms:
            lines.append("- نطاق الكيان المطلوب: " + "، ".join(self.entity_terms))
        if not self.entity_supported:
            lines.append("- لم يظهر الكيان المطلوب مع الحقل المطلوب في دليل واحد.")
        lines.append(f"- كفاية الأدلة: {'كافية' if self.sufficient else 'ناقصة أو جزئية'}")
        if self.fact_sensitive:
            lines.append("- الأرقام والروابط والمراحل والجهات يجب نقلها حرفياً من الدليل.")
        if self.missing_fields:
            lines.append("- أجب بالأجزاء المسندة فقط وصرّح بنقص البقية دون تخمين.")
        return "\n".join(lines)


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    normalized = normalize_arabic(text)
    return any(normalize_arabic(marker) in normalized for marker in markers)


_NUMBER_RE = re.compile(r"(?<!\w)[0-9٠-٩۰-۹]+(?:[.,][0-9٠-٩۰-۹]+)?")
_URL_RE = re.compile(r"https?://[^\s<>{}\[\]\"']+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.%+-]+@[\w.-]+\.[A-Za-z\u0600-\u06ff]{2,}")
_PHONE_RE = re.compile(r"(?:\+|00)?[0-9٠-٩۰-۹][0-9٠-٩۰-۹\s().-]{6,}")
_DATE_RE = re.compile(
    r"(?:19|20)[0-9]{2}(?:\s*[-/]\s*[0-9]{1,2}(?:\s*[-/]\s*[0-9]{1,2})?)?"
)
_DATE_WORDS = (
    "يناير", "فبراير", "مارس", "ابريل", "أبريل", "مايو", "يونيو",
    "يوليو", "اغسطس", "أغسطس", "سبتمبر", "اكتوبر", "أكتوبر",
    "نوفمبر", "ديسمبر", "محرم", "صفر", "ربيع", "جمادى", "رجب",
    "شعبان", "رمضان", "شوال", "القعدة", "الحجة",
)
_ENTITY_STOPWORDS = {
    "اعطني", "اريد", "بدي", "ممكن", "السوال", "سوال", "ما", "ماذا",
    "هل", "كم", "كيف", "متى", "اين", "وين", "شو", "عن", "في", "من",
    "الى", "على", "مع", "هذا", "هذه", "ذلك", "الجامعه", "الاسلاميه",
    "غزه", "كليه", "كليات", "قسم", "تخصص", "برنامج", "برامج", "مرحله",
    "بكالوريوس", "ماجستير", "دكتوراه", "رسوم", "سعر", "تكلفه", "ساعه",
    "شروط", "متطلبات", "قبول", "معدل", "رابط", "صفحه", "بوابه", "مصدر",
    "تاريخ", "موعد", "وثايق", "اوراق", "تواصل", "هاتف", "بريد", "اسم",
    "القائمه", "الكامله", "جميع", "كل", "المذكور", "السابق",
}


def _entity_terms(plan: QueryPlan) -> list[str]:
    terms = []
    for token in tokenize(plan.standalone_query):
        normalized = normalize_arabic(token)
        if len(normalized) < 3 or normalized in _ENTITY_STOPWORDS:
            continue
        if normalized.isdigit() or normalized in terms:
            continue
        terms.append(normalized)
    return terms[:8]


def _has_exact_value(field_name: str, text: str) -> bool:
    """Field-aware evidence check; marker presence alone is insufficient."""
    norm = normalize_arabic(text)
    if field_name == "fee":
        return bool(_NUMBER_RE.search(text)) and any(
            mark in norm
            for mark in ("دينار", "شيكل", "دولار", "credit_hour_fee", "رسوم")
        )
    if field_name == "link":
        return bool(_URL_RE.search(text))
    if field_name in {"contact", "contacts"}:
        return bool(_EMAIL_RE.search(text) or _PHONE_RE.search(text))
    if field_name in {"date", "deadlines"}:
        return bool(_DATE_RE.search(text)) or any(
            normalize_arabic(word) in norm for word in _DATE_WORDS
        )
    if field_name == "source":
        return "[ملف:" in text or "[مصدر" in text
    if field_name == "admissions":
        return bool(_NUMBER_RE.search(text)) or any(
            mark in norm for mark in ("تنافسي", "min_high_school", "مفتاح")
        )
    return True


def _matching_evidence_indexes(
    field_name: str,
    evidence: list[str],
    entity_terms: list[str],
) -> list[int]:
    markers = _FIELD_MARKERS.get(field_name, (field_name,))
    matches = []
    for index, item in enumerate(evidence):
        if markers and not _has_marker(item, markers):
            continue
        if not _has_exact_value(field_name, item):
            continue
        # Source/privacy/general fields describe the evidence envelope itself.
        needs_entity = field_name not in {"source", "privacy", "general"}
        norm_item = normalize_arabic(item)
        if needs_entity and entity_terms and not any(
            term in norm_item for term in entity_terms
        ):
            continue
        matches.append(index)
    return matches


def _contradictions(text: str, frame: ConversationFrame) -> list[str]:
    norm = normalize_arabic(text)
    issues = []
    if frame.degree_level == "bachelor" and any(m in norm for m in ("ماجستير", "دكتوراه")):
        issues.append("توجد أدلة من مراحل أعلى مع أن المطلوب بكالوريوس")
    if frame.degree_level in {"masters", "phd"} and "بكالوريوس" in norm:
        issues.append("توجد أدلة بكالوريوس مع أن المطلوب دراسات عليا")
    if frame.rate_type == "high_school" and "المعدل التراكمي" in norm:
        issues.append("قد يختلط معدل الثانوية بالمعدل الجامعي")
    if frame.transfer_scope == "internal" and "من جامعة أخرى" in norm:
        issues.append("قد يختلط التحويل الداخلي بالتحويل الخارجي")
    if frame.transfer_scope == "external" and "بين التخصصات" in norm:
        issues.append("قد يختلط التحويل الخارجي بالتحويل الداخلي")
    return issues


def build_evidence_contract(
    plan: QueryPlan,
    frame: ConversationFrame,
    evidence: list[str],
    *,
    authoritative_evidence: list[str] | None = None,
) -> EvidenceContract:
    authoritative_evidence = authoritative_evidence or []
    all_evidence = [*authoritative_evidence, *evidence]
    combined = "\n".join(all_evidence)
    required = list(dict.fromkeys([
        *frame.requested_fields,
        *(_DOMAIN_FIELD.get(domain, domain) for domain in plan.domains if domain != "general"),
    ]))
    if not required:
        required = ["general"]

    # Names in privacy-sensitive questions must never be echoed into the
    # system prompt merely for evidence matching.
    entity_terms = [] if "privacy" in required else _entity_terms(plan)
    field_evidence: dict[str, list[int]] = {}
    resolved = []
    for field_name in required:
        indexes = _matching_evidence_indexes(
            field_name, all_evidence, entity_terms
        )
        field_evidence[field_name] = indexes
        if indexes:
            resolved.append(field_name)
    missing = [field_name for field_name in required if field_name not in resolved]
    contradictions = _contradictions(combined, frame)
    fact_sensitive = bool(set(required) & _SENSITIVE_FIELDS)
    entity_supported = not entity_terms or any(
        field_evidence.get(field_name)
        for field_name in required
        if field_name not in {"source", "privacy", "general"}
    )
    if set(required) <= {"source", "privacy", "general"}:
        entity_supported = True
    sufficient = (
        not missing
        and not contradictions
        and bool(combined.strip())
        and entity_supported
    )
    return EvidenceContract(
        required_fields=required,
        resolved_fields=resolved,
        missing_fields=missing,
        contradictions=contradictions,
        sufficient=sufficient,
        fact_sensitive=fact_sensitive,
        authoritative_evidence_used=bool(authoritative_evidence),
        entity_terms=entity_terms,
        entity_supported=entity_supported,
        field_evidence=field_evidence,
    )


__all__ = ["EvidenceContract", "build_evidence_contract"]

_FIELD_EXPANSIONS = {
    "fee": "رسوم سعر الساعة الثوابت الفصلية",
    "requirements": "الشروط المتطلبات الأهلية",
    "link": "الرابط الرسمي الصفحة البوابة النموذج",
    "contact": "البريد الهاتف جهة التواصل",
    "date": "الموعد التاريخ التقويم",
    "documents": "الوثائق الأوراق المطلوبة",
    "programs": "البرامج التخصصات المسارات",
    "source": "المصدر المرجع تاريخ التحقق",
    "admissions": "معدل القبول مفتاح القبول الفروع",
    "scholarships": "المنح الإعفاء شروط المنحة",
    "procedures": "الخطوات الإجراء الطلب",
    "deadlines": "آخر موعد تاريخ البداية النهاية",
    "contacts": "جهة التواصل البريد الهاتف العنوان",
    "people": "الاسم المسمى الوظيفي الجهة",
}


def missing_field_query(plan: QueryPlan, contract: EvidenceContract) -> str | None:
    """One bounded deterministic expansion for uncovered/conflicting evidence."""
    additions = [
        _FIELD_EXPANSIONS[field_name]
        for field_name in contract.missing_fields
        if field_name in _FIELD_EXPANSIONS
    ]
    if not additions:
        if contract.contradictions or not contract.entity_supported:
            scope = " ".join(contract.entity_terms)
            suffix = "الدليل المطابق للكيان والمرحلة المطلوبة دون خلط"
            return plan.standalone_query + f" ({scope} {suffix})"
        return None
    return plan.standalone_query + " (" + "؛ ".join(dict.fromkeys(additions)) + ")"


__all__.append("missing_field_query")
