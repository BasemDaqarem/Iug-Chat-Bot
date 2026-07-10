"""
FastAPI dependencies.

The chatbot is one heavy, long-lived object (Mongo data + embedding indexes
in memory). It is created ONCE at startup (see create_app's lifespan) and
handed to routes through this dependency — routes never construct services
themselves, which keeps them thin and lets tests inject a fake bot.
"""

from fastapi import Request

from app import config, tokens
from app.api.errors import (
    ForbiddenError,
    ServiceUnavailableError,
    TooManyRequestsError,
    UnauthorizedError,
)
from app.chatbot import IUGChatbot
from app.ratelimit import RateLimiter
from app.tokens import InvalidTokenError

_chat_limiter = RateLimiter(config.RATE_LIMIT_CHAT_PER_MIN)
_login_limiter = RateLimiter(config.RATE_LIMIT_LOGIN_PER_MIN)


def reset_rate_limits() -> None:
    """Clear all rate-limit counters (used by tests, which share a client IP)."""
    _chat_limiter.reset()
    _login_limiter.reset()


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


def rate_limited_student(request: Request) -> str:
    """Authenticated student id, additionally rate-limited per student. Used by
    the chat endpoints so one student can't hammer the LLM/embeddings."""
    student_id = get_current_student(request)
    allowed, retry = _chat_limiter.check(f"chat:{student_id}")
    if not allowed:
        raise TooManyRequestsError(
            "طلبات كثيرة خلال وقت قصير — تمهّل قليلاً ثم أعد المحاولة.", retry_after=retry
        )
    return student_id


def login_rate_limit(request: Request) -> None:
    """Throttle login/registration by client IP to blunt brute-force attempts."""
    ip = request.client.host if request.client else "unknown"
    allowed, retry = _login_limiter.check(f"login:{ip}")
    if not allowed:
        raise TooManyRequestsError(
            "محاولات دخول كثيرة — انتظر قليلاً ثم حاول مجدداً.", retry_after=retry
        )


def require_admin(request: Request) -> None:
    """Gate for corpus-mutating / admin operations. Fail closed: if no admin key
    is configured, access is denied (never open by default)."""
    provided = request.headers.get("X-Admin-Key", "")
    if not config.ADMIN_API_KEY or provided != config.ADMIN_API_KEY:
        raise ForbiddenError("عملية إدارية — تتطلب مفتاح إدارة صحيحاً (X-Admin-Key).")
