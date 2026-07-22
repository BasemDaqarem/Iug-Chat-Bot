"""Deterministic routing and structured evidence projection.

The router never answers.  It decides how much retrieval/reranking is useful
and projects exact key-value lines from selected chunks into a compact evidence
block for the final LLM call.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any

from app.conversation_frame import ConversationFrame, QueryPlan
from app.text_norm import normalize_arabic


_STRUCTURED_DOMAINS = {
    "fees", "admissions", "scholarships", "programs", "deadlines",
    "documents", "contacts", "people", "procedures",
}

_LINE_MARKERS = {
    "fees": (
        "رسوم", "fee", "amount", "currency", "دينار", "سعر الساعة", "ثوابت",
    ),
    "admissions": ("قبول", "min_high_school", "الحد الأدنى", "الفروع"),
    "scholarships": (
        "منحة", "منح", "إعفاء", "coverage", "eligib",
        "discount_percentage", "retention_gpa_required",
    ),
    "programs": ("برنامج", "تخصص", "مسار", "كلية", "قسم"),
    "deadlines": ("موعد", "تاريخ", "start", "end", "الفصل"),
    "documents": ("وثيقة", "وثائق", "أوراق", "شهادة", "تصديق"),
    "contacts": ("@", "هاتف", "بريد", "عنوان", "https://"),
    "people": ("full_name", "الاسم", "عميد", "رئيس", "مدير"),
    "procedures": ("خطوة", "إجراء", "طلب", "تسجيل", "دفع"),
}

_IDENTITY_AND_SCOPE_KEYS = {
    "program_name", "program", "specialization", "major", "department",
    "faculty", "faculty_name", "college", "degree_or_request", "resource_name",
    "full_name", "course", "course_name", "course_code", "subject", "id",
    "title", "topic", "case", "service_type", "category", "document_type",
    "branch", "degree", "level", "currency", "semester",
    "اسم_البرنامج", "البرنامج", "التخصص", "القسم", "الكليه", "الكلية",
    "الاسم", "المورد", "المساق", "اسم_المساق", "رمز_المساق", "الماده",
    "المادة", "الموضوع", "العنوان", "الحاله", "الحالة", "التصنيف",
    "الفئه", "الفئة", "الفرع", "المرحله", "المرحلة", "العمله", "العملة",
}


def _is_identity_or_scope_line(line: str) -> bool:
    if ":" not in line:
        return False
    key = normalize_arabic(line.split(":", 1)[0]).lower().replace(" ", "_")
    key = re.sub(r"\[\d+\]", "", key)
    leaf = key.rsplit(".", 1)[-1]
    return leaf in _IDENTITY_AND_SCOPE_KEYS


@dataclass(slots=True)
class DomainRoute:
    mode: str
    domains: list[str]
    structured_first: bool
    use_wide_retrieval: bool
    use_reranker: bool
    reason: str

    def as_metadata(self) -> dict[str, Any]:
        return asdict(self)


def route_query(plan: QueryPlan, frame: ConversationFrame) -> DomainRoute:
    structured = bool(set(plan.domains) & _STRUCTURED_DOMAINS)
    if "privacy" in plan.domains:
        mode = "privacy"
        reason = "privacy-sensitive request"
    elif plan.is_compound:
        mode = "compound"
        reason = "multiple requested domains or fields"
    elif structured:
        mode = "structured_plus_rag"
        reason = "domain has exact fields that should be projected"
    else:
        mode = "general_rag"
        reason = "narrative/general university question"
    return DomainRoute(
        mode=mode,
        domains=plan.domains,
        structured_first=structured,
        use_wide_retrieval=plan.is_compound or plan.is_list_question,
        use_reranker=plan.needs_reranking and not plan.is_list_question,
        reason=reason,
    )


def project_structured_evidence(
    route: DomainRoute,
    chunks: list[str],
    *,
    max_lines: int = 80,
) -> list[str]:
    """Extract exact relevant lines while retaining each source header."""
    if not route.structured_first or not chunks:
        return []
    markers = tuple(
        normalize_arabic(marker)
        for domain in route.domains
        for marker in _LINE_MARKERS.get(domain, ())
    )
    if not markers:
        return []
    selected: list[str] = []
    for chunk in chunks:
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        header = lines[0] if lines and lines[0].startswith("[ملف:") else "[مصدر غير مسمى]"
        matching = [
            line for line in lines[1:]
            if (
                _is_identity_or_scope_line(line)
                or any(marker in normalize_arabic(line) for marker in markers)
            )
        ]
        if matching:
            selected.append(header + "\n" + "\n".join(matching))
        if sum(item.count("\n") + 1 for item in selected) >= max_lines:
            break
    return list(dict.fromkeys(selected))


__all__ = ["DomainRoute", "route_query", "project_structured_evidence"]
