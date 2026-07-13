"""
FastAPI dependencies.

The chatbot is one heavy, long-lived object (Mongo data + embedding indexes
in memory). It is created ONCE at startup (see create_app's lifespan) and
handed to routes through this dependency — routes never construct services
themselves, which keeps them thin and lets tests inject a fake bot.
"""

from fastapi import Request

from app import auth, config, tokens
from app.api.errors import ForbiddenError, ServiceUnavailableError, UnauthorizedError
from app.chatbot import IUGChatbot
from app.tokens import InvalidTokenError
from app.rbac import Principal, Role


def get_bot(request: Request) -> IUGChatbot:
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        # Startup failed (e.g. MongoDB unreachable) — surface a clean 503
        # instead of an AttributeError from a half-initialized app.
        raise ServiceUnavailableError("الخدمة لم تكتمل تهيئتها بعد — حاول لاحقاً.")
    return bot


def get_current_principal(request: Request) -> Principal:
    """Build the caller identity exclusively from a verified Bearer JWT."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthorizedError("مطلوب تسجيل الدخول — لا يوجد توكن جلسة.")
    try:
        principal = tokens.decode_principal(token.strip())
    except InvalidTokenError as exc:
        raise UnauthorizedError(exc.message)

    # Production/dev applications backed by the real chatbot validate the
    # account on every protected request.  This makes password resets, role
    # changes, account suspension, and "end sessions" effective across all
    # workers instead of relying only on an in-process revocation map.
    # Injected test bots deliberately skip Mongo so API unit tests stay pure.
    if getattr(request.app.state, "verify_account_tokens", True):
        try:
            account = auth.find_account(principal.subject)
        except Exception as exc:
            raise ServiceUnavailableError(
                "تعذّر التحقق من الجلسة حاليًا — حاول لاحقًا."
            ) from exc
        if account is None or not account.get("active", True):
            raise UnauthorizedError("الحساب غير موجود أو موقوف — سجّل الدخول من جديد.")
        try:
            account_role = Role(str(account.get("role") or Role.STUDENT.value))
            account_version = int(account.get("token_version", 1))
        except (TypeError, ValueError) as exc:
            raise UnauthorizedError("بيانات الجلسة غير صالحة — سجّل الدخول من جديد.") from exc
        if account_role != principal.role or account_version != principal.token_version:
            raise UnauthorizedError("تم إنهاء هذه الجلسة — سجّل الدخول من جديد.")

    return principal


def get_current_student(request: Request) -> str:
    """Backward-compatible dependency for student-only routes."""
    principal = get_current_principal(request)
    if principal.role != Role.STUDENT:
        raise ForbiddenError("هذه العملية متاحة للطلاب فقط.")
    return principal.subject


def require_employee_or_admin(request: Request) -> Principal:
    principal = get_current_principal(request)
    if principal.role not in {Role.EMPLOYEE, Role.ADMIN}:
        raise ForbiddenError("هذه العملية متاحة للموظفين والأدمن فقط.")
    return principal


def require_admin_role(request: Request) -> Principal:
    principal = get_current_principal(request)
    if principal.role != Role.ADMIN:
        raise ForbiddenError("هذه العملية تتطلب صلاحية الأدمن.")
    return principal


def require_admin(request: Request) -> None:
    """Compatibility gate for the legacy file endpoints.

    New admin APIs use the JWT role. The old X-Admin-Key path remains so
    existing deployments and clients do not break during migration.
    """
    # A valid session is mandatory even on the transitional X-Admin-Key path.
    # The key only supplements a non-admin JWT for legacy clients; it is never
    # an anonymous bearer credential by itself.
    if get_current_principal(request).role == Role.ADMIN:
        return
    provided = request.headers.get("X-Admin-Key", "")
    if not config.ADMIN_API_KEY or provided != config.ADMIN_API_KEY:
        raise ForbiddenError("عملية إدارية — تتطلب مفتاح إدارة صحيحاً (X-Admin-Key).")
