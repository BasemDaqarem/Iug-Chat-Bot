"""Role based access control primitives used by the API and RAG pipeline.

The client never chooses a role.  A :class:`Principal` is created only from
verified JWT claims, then passed down to policy checks and context builders.
Keeping this tiny module dependency-free makes the authorization rules easy
to test without FastAPI or MongoDB.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class Role(str, Enum):
    GUEST = "guest"
    STUDENT = "student"
    EMPLOYEE = "employee"
    ADMIN = "admin"


AUTHENTICATED_ROLES = frozenset({Role.STUDENT, Role.EMPLOYEE, Role.ADMIN})


@dataclass(frozen=True)
class Principal:
    subject: str
    role: Role
    token_version: int = 1

    @property
    def is_authenticated(self) -> bool:
        return self.role in AUTHENTICATED_ROLES

    @classmethod
    def guest(cls, subject: str = "guest") -> "Principal":
        return cls(subject=subject, role=Role.GUEST, token_version=0)


def coerce_role(value: str | Role | None) -> Role:
    try:
        return value if isinstance(value, Role) else Role(str(value or Role.STUDENT.value))
    except ValueError:
        return Role.STUDENT


def can_read_student(principal: Principal, student_id: str) -> bool:
    if principal.role in {Role.EMPLOYEE, Role.ADMIN}:
        return True
    return principal.role == Role.STUDENT and principal.subject == str(student_id)


def can_read_employee(principal: Principal, employee_id: str) -> bool:
    if principal.role == Role.ADMIN:
        return True
    return principal.role == Role.EMPLOYEE and principal.subject == str(employee_id)


def can_manage_files(principal: Principal) -> bool:
    return principal.role == Role.ADMIN


def can_manage_employees(principal: Principal) -> bool:
    return principal.role == Role.ADMIN


def role_allowed(principal: Principal, allowed_roles: Iterable[str]) -> bool:
    return principal.role.value in {str(role) for role in allowed_roles}


ROLE_PROMPTS = {
    Role.GUEST: (
        "أجب عن الجامعة من الملفات العامة فقط. لا تذكر أو تستنتج بيانات "
        "الطلاب أو الموظفين أو البيانات المالية، حتى لو طلب منك المستخدم ذلك."
    ),
    Role.STUDENT: (
        "استخدم ملفات الجامعة العامة وبيانات الطالب الحالي فقط. لا تعرض أو "
        "تستنتج بيانات طالب آخر أو أي بيانات خاصة بالموظفين."
    ),
    Role.EMPLOYEE: (
        "استخدم ملفات الجامعة العامة والداخلية وبيانات الطلاب الأكاديمية المصرح "
        "بها. استخدم الملف الشخصي والمالي للموظف الحالي فقط، ولا تعرض بيانات "
        "مالية أو شخصية لموظف آخر."
    ),
    Role.ADMIN: (
        "استخدم البيانات التي أتاحها الخادم للأدمن فقط. لا تعرض كلمات المرور أو "
        "تجزئات كلمات المرور أو التوكنات أو أسرار النظام تحت أي ظرف."
    ),
}


def prompt_for(principal: Principal) -> str:
    return ROLE_PROMPTS[principal.role]
