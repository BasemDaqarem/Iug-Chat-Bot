"""Build role-authorized private context with direct Mongo projections.

Private records are queried only after authorization and are never embedded,
added to shared caches, returned as ``top_chunks``, or written to logs.
"""

import json
import re

from app import auth
from app.rbac import Principal, Role, prompt_for


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _student_query(question: str) -> str:
    ids = re.findall(r"\d{3,20}", question)
    if ids:
        return ids[0]
    words = [word for word in re.findall(r"[\u0600-\u06ff]{3,}", question)
             if word not in {"الطالب", "طالبة", "معدل", "ترتيب", "بيانات", "الحالة", "الأكاديمية"}]
    return " ".join(words[:3])


def build_private_context(principal: Principal, question: str) -> str:
    sections = ["سياسة الدور:\n" + prompt_for(principal)]
    if principal.role == Role.GUEST:
        return "\n\n".join(sections)

    account = auth.find_account(principal.subject)
    profile = dict((account or {}).get("profile") or {})

    if principal.role == Role.STUDENT:
        safe = {key: profile.get(key) for key in (
            "name", "major", "gpa", "rank", "academic_status", "data_source", "updated_at"
        )}
        sections.append("بيانات الطالب الحالي فقط:\n" + _json(safe))

    if principal.role == Role.EMPLOYEE:
        own = {key: profile.get(key) for key in (
            "name", "department", "job_title", "salary", "data_source", "updated_at"
        )}
        sections.append("الملف الشخصي والمالي للموظف الحالي فقط:\n" + _json(own))
        if any(term in question for term in ("طالب", "معدل", "ترتيب", "أكاديمي", "تخصص")):
            students = auth.list_students(_student_query(question), limit=10)
            sections.append("سجلات الطلاب الأكاديمية المصرح بها:\n" + _json(students))

    if principal.role == Role.ADMIN:
        if any(term in question for term in ("طالب", "معدل", "ترتيب", "أكاديمي", "تخصص")):
            sections.append(
                "سجلات الطلاب الأكاديمية المصرح بها:\n" +
                _json(auth.list_students(_student_query(question), limit=20))
            )
        if any(term in question for term in ("موظف", "راتب", "قسم", "وظيف")):
            sections.append("حسابات الموظفين الآمنة:\n" + _json(auth.list_employees()))

    return "\n\n".join(sections)
