"""Deterministic evidence requirements for a generated answer.

The contract does not answer questions.  It tells the LLM what is and is not
supported, and exposes structured metadata to tests and diagnostics.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.conversation_frame import ConversationFrame, QueryPlan
from app.text_norm import normalize_arabic


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
        lines.append(f"- كفاية الأدلة: {'كافية' if self.sufficient else 'ناقصة أو جزئية'}")
        if self.fact_sensitive:
            lines.append("- الأرقام والروابط والمراحل والجهات يجب نقلها حرفياً من الدليل.")
        if self.missing_fields:
            lines.append("- أجب بالأجزاء المسندة فقط وصرّح بنقص البقية دون تخمين.")
        return "\n".join(lines)


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    normalized = normalize_arabic(text)
    return any(normalize_arabic(marker) in normalized for marker in markers)


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
    combined = "\n".join([*authoritative_evidence, *evidence])
    required = list(dict.fromkeys([
        *frame.requested_fields,
        *(_DOMAIN_FIELD.get(domain, domain) for domain in plan.domains if domain != "general"),
    ]))
    if not required:
        required = ["general"]

    resolved = []
    for field_name in required:
        markers = _FIELD_MARKERS.get(field_name, (field_name,))
        if not markers or _has_marker(combined, markers):
            resolved.append(field_name)
    missing = [field_name for field_name in required if field_name not in resolved]
    contradictions = _contradictions(combined, frame)
    fact_sensitive = bool(set(required) & _SENSITIVE_FIELDS)
    sufficient = not missing and not contradictions and bool(combined.strip())
    return EvidenceContract(
        required_fields=required,
        resolved_fields=resolved,
        missing_fields=missing,
        contradictions=contradictions,
        sufficient=sufficient,
        fact_sensitive=fact_sensitive,
        authoritative_evidence_used=bool(authoritative_evidence),
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
    """One bounded deterministic expansion for uncovered answer fields."""
    additions = [
        _FIELD_EXPANSIONS[field_name]
        for field_name in contract.missing_fields
        if field_name in _FIELD_EXPANSIONS
    ]
    if not additions:
        return None
    return plan.standalone_query + " (" + "؛ ".join(dict.fromkeys(additions)) + ")"


__all__.append("missing_field_query")
