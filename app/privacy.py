"""
Privacy guard + sensitive-record lookups over chunk metadata.

Structural, not name-based: works for ANY collection whose documents
declare `privacy.allowed_users` (see app.chunking), so there is no
hardcoded "students_rankings"-style coupling.
"""

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


def is_academic_status_question(question: str) -> bool:
    return any(keyword in question for keyword in ACADEMIC_STATUS_KEYWORDS)


def is_ranking_question(question: str) -> bool:
    return any(kw in question for kw in RANKING_KEYWORDS)


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
