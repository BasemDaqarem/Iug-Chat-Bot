"""
Privacy guard + sensitive-record lookups over chunk metadata.

Structural, not name-based: works for ANY collection whose documents
declare `privacy.allowed_users` (see app.chunking), so there is no
hardcoded "students_rankings"-style coupling.
"""

import re
from typing import List, Optional

from app.chunking import flatten_json_to_text

ACADEMIC_STATUS_KEYWORDS = [
    "حالتي الاكاديمية", "حالتي الأكاديمية", "حالة اكاديمية", "حالة أكاديمية",
    "وضعي الاكاديمي", "وضعي الأكاديمي", "الوضع الاكاديمي", "الوضع الأكاديمي",
    "انا في خطر", "أنا في خطر", "في خطر", "خطر", "تعثر", "متعثر",
    "at risk", "risk",
]

RANKING_KEYWORDS = ["معدل", "ترتيب", "gpa", "معدله", "ترتيبه", "معدلها", "ترتيبها"]

BLOCKED_ANSWER = "عذراً، بيانات الترتيب والمعدلات خاصة بكل طالب ولا يمكن الاطلاع عليها."

# academic_status codes → Arabic label.
_STATUS_LABELS = {
    "regular": "منتظم",
    "excellent": "ممتاز",
    "good": "جيد",
    "at_risk": "متعثّر (في خطر أكاديمي)",
    "probation": "تحت إنذار أكاديمي",
    "graduated": "متخرّج",
}


def is_academic_status_question(question: str) -> bool:
    return any(keyword in question for keyword in ACADEMIC_STATUS_KEYWORDS)


def is_ranking_question(question: str) -> bool:
    return any(kw in question for kw in RANKING_KEYWORDS)


# Third-person / by-name / by-other-id signals that the question targets a
# DIFFERENT student's private record — those must be refused, never answered.
OTHER_RECORD_KEYWORDS = [
    "معدله", "معدلها", "ترتيبه", "ترتيبها",
    "معدل الطالب", "ترتيب الطالب", "حالة الطالب",
    "معدل زميل", "ترتيب زميل", "معدل صديق", "ترتيب صديق",
    "معدل الطالبة", "ترتيب الطالبة",
]
_STUDENT_ID_RE = re.compile(r"\d{5,}")   # a 5+ digit run looks like a student id


def asks_about_other_student(question: str, own_student_id: Optional[str] = None) -> bool:
    """True when the question targets ANOTHER student's private record: a
    third-person/by-name phrasing, or a ranking/status question that names a
    student id different from the caller's own."""
    if any(k in question for k in OTHER_RECORD_KEYWORDS):
        return True
    if is_ranking_question(question) or is_academic_status_question(question):
        for num in _STUDENT_ID_RE.findall(question):
            if own_student_id is None or num != str(own_student_id):
                return True
    return False


def format_authenticated_profile_context(profile: dict) -> str:
    """Serialize only approved profile fields for the authenticated caller.

    This text is injected into the private system prompt.  It is deliberately
    not embedded, cached, logged, or returned as a retrieved public chunk.
    """
    status = profile.get("academic_status")
    fields = (
        ("الاسم", profile.get("name")),
        ("التخصص", profile.get("major")),
        ("المعدل التراكمي", profile.get("gpa")),
        ("الترتيب على الدفعة", profile.get("rank")),
        ("الحالة الأكاديمية", _STATUS_LABELS.get(status, status) if status else None),
        ("مصدر البيانات", profile.get("data_source")),
        ("آخر تحديث", profile.get("updated_at")),
    )
    lines = ["بيانات الطالب الحالي المصادق عليه (خاصة):"]
    lines.extend(
        f"- {label}: {value}"
        for label, value in fields
        if value not in (None, "")
    )
    if profile.get("data_source") == "self_reported_demo":
        lines.append("ملاحظة: هذه بيانات تجريبية أدخلها الطالب، وليست سجلاً رسمياً من الجامعة.")
    return "\n".join(lines)


def find_sensitive_record(chunk_meta: List[dict], session_id: str) -> Optional[dict]:
    """Chunk-meta of the sensitive record owned by session_id, regardless
    of which collection it lives in."""
    sid = str(session_id)
    for m in chunk_meta:
        if m.get("sensitive") and str(m.get("owner_id")) == sid:
            return m
    return None


def other_sensitive_display_names(chunk_meta: List[dict], session_id: str) -> List[str]:
    sid = str(session_id)
    names = set()
    for m in chunk_meta:
        if m.get("sensitive") and str(m.get("owner_id")) != sid and m.get("display_name"):
            names.add(m["display_name"])
    return list(names)


def mentions_other_student(question: str, other_names: List[str]) -> bool:
    """True when the question contains the first name of another student
    who owns a sensitive record (first-token substring match)."""
    first_tokens = [n.split()[0] for n in other_names if n]
    return any(tok in question for tok in first_tokens)


def format_sensitive_record_context(meta: dict) -> str:
    raw = dict(meta.get("raw") or {})
    raw.pop("privacy", None)
    lines = flatten_json_to_text(raw)
    body = "\n".join(lines)
    return f"بيانات الطالب الحالي (سري — للطالب نفسه فقط):\n{body}"


def build_status_from_sensitive_record(raw: dict) -> str:
    gpa  = raw.get("gpa", "غير متوفر")
    rank = raw.get("rank", "غير متوفر")
    return f"حالتك الأكاديمية الحالية: المعدل التراكمي {gpa}، والترتيب على الدفعة {rank}."
