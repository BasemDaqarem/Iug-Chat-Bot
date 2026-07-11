"""
Privacy guard + sensitive-record lookups over chunk metadata.

Structural, not name-based: works for ANY collection whose documents
declare `privacy.allowed_users` (see app.chunking), so there is no
hardcoded "students_rankings"-style coupling.
"""

import re
from typing import List, Optional

from app.chunking import flatten_json_to_text
from app.text_norm import normalize_arabic

ACADEMIC_STATUS_KEYWORDS = [
    "حالتي الاكاديمية", "حالتي الأكاديمية", "حالة اكاديمية", "حالة أكاديمية",
    "وضعي الاكاديمي", "وضعي الأكاديمي", "الوضع الاكاديمي", "الوضع الأكاديمي",
    "انا في خطر", "أنا في خطر", "في خطر", "خطر", "تعثر", "متعثر",
    "at risk", "risk",
]

RANKING_KEYWORDS = ["معدل", "ترتيب", "gpa", "معدله", "ترتيبه", "معدلها", "ترتيبها"]

# First-person markers → the student is asking about THEIR OWN record.
OWN_RECORD_KEYWORDS = [
    "معدلي", "ترتيبي", "حالتي", "وضعي", "مستواي", "درجاتي", "تخصصي",
    "معدل تخرجي", "أنا في خطر", "انا في خطر", "أنا متعثر", "انا متعثر",
]

OWN_PROFILE_KEYWORDS = [
    "اسمي", "من انا", "من أنا", "بياناتي", "معلوماتي",
]

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


def wants_own_academic_record(question: str) -> bool:
    """True when the student is asking about THEIR OWN academic record
    (status / gpa / rank), so we may answer from their own profile."""
    return is_academic_status_question(question) or any(
        k in question for k in OWN_RECORD_KEYWORDS
    )


def wants_own_profile(question: str) -> bool:
    """True for any request that can be answered from the caller's profile."""
    normalized = normalize_arabic(question).lower()
    return wants_own_academic_record(question) or any(
        normalize_arabic(keyword).lower() in normalized
        for keyword in OWN_PROFILE_KEYWORDS
    )


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


def build_status_from_profile(profile: dict) -> str:
    """A direct, non-LLM answer built ONLY from the student's own profile."""
    gpa = profile.get("gpa", "غير متوفر")
    rank = profile.get("rank", "غير متوفر")
    major = profile.get("major")
    status = profile.get("academic_status")

    lines = ["📊 حالتك الأكاديمية:"]
    if major:
        lines.append(f"• التخصص: {major}")
    lines.append(f"• المعدل التراكمي: {gpa}")
    lines.append(f"• الترتيب على الدفعة: {rank}")
    if status:
        lines.append(f"• الوضع: {_STATUS_LABELS.get(status, status)}")
        if status in ("at_risk", "probation"):
            lines.append("⚠️ يُنصح بمراجعة مرشدك الأكاديمي لوضع خطة لتحسين مستواك.")
    if profile.get("data_source") == "self_reported_demo":
        lines.append("ℹ️ هذه بيانات تجريبية أدخلتها عند إنشاء الحساب وليست سجلاً رسمياً من الجامعة.")
    return "\n".join(lines)


def build_profile_answer(question: str, profile: dict) -> str:
    """Build a deterministic answer from the authenticated student's profile."""
    normalized = normalize_arabic(question).lower()
    name = profile.get("name")

    if ("اسمي" in normalized or "من انا" in normalized) and "بيانات" not in normalized:
        answer = f"اسمك هو {name}." if name else "اسمك غير متوفر في الحساب حالياً."
        if profile.get("data_source") == "self_reported_demo":
            answer += " هذه معلومة تجريبية أدخلتها عند إنشاء الحساب."
        return answer

    if "بيانات" in normalized or "معلوماتي" in normalized:
        status = profile.get("academic_status")
        lines = ["📋 بياناتك المتوفرة في الحساب التجريبي:"]
        for label, value in (
            ("الاسم", name),
            ("التخصص", profile.get("major")),
            ("المعدل التراكمي", profile.get("gpa")),
            ("الترتيب على الدفعة", profile.get("rank")),
            ("الحالة الأكاديمية", _STATUS_LABELS.get(status, status) if status else None),
        ):
            if value not in (None, ""):
                lines.append(f"• {label}: {value}")
        lines.append("ℹ️ أدخلت هذه البيانات عند إنشاء الحساب وليست سجلاً رسمياً من الجامعة.")
        return "\n".join(lines)

    return build_status_from_profile(profile)


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
