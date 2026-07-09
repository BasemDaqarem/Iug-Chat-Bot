"""
FastAPI dependencies.

The chatbot is one heavy, long-lived object (Mongo data + embedding indexes
in memory). It is created ONCE at startup (see create_app's lifespan) and
handed to routes through this dependency — routes never construct services
themselves, which keeps them thin and lets tests inject a fake bot.
"""

from fastapi import Request

from app import config, tokens
from app.api.errors import ForbiddenError, ServiceUnavailableError, UnauthorizedError
from app.chatbot import IUGChatbot
from app.tokens import InvalidTokenError


def get_bot(request: Request) -> IUGChatbot:
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        # Startup failed (e.g. MongoDB unreachable) — surface a clean 503
        # instead of an AttributeError from a half-initialized app.
        raise ServiceUnavailableError("الخدمة لم تكتمل تهيئتها بعد — حاول لاحقاً.")
    return bot


def get_current_student(request: Request) -> str:
    """The authenticated student id, taken from the verified JWT in the
    `Authorization: Bearer <token>` header. Raises 401 if the token is
    missing, malformed, or expired — so personal-data routes can trust it."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthorizedError("مطلوب تسجيل الدخول — لا يوجد توكن جلسة.")
    try:
        return tokens.decode_token(token.strip())
    except InvalidTokenError as exc:
        raise UnauthorizedError(exc.message)


def require_admin(request: Request) -> None:
    """Gate for corpus-mutating / admin operations. Fail closed: if no admin key
    is configured, access is denied (never open by default)."""
    provided = request.headers.get("X-Admin-Key", "")
    if not config.ADMIN_API_KEY or provided != config.ADMIN_API_KEY:
        raise ForbiddenError("عملية إدارية — تتطلب مفتاح إدارة صحيحاً (X-Admin-Key).")
