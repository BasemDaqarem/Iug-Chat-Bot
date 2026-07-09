"""
Signed session tokens (JWT).

Login/registration issue a short-lived HS256 token whose `sub` claim is the
student id. Every request that touches personal data verifies the token and
derives the identity FROM IT — never from a client-supplied field — which is
what closes the "anyone can pass any student_id" gap.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from app import config
from app.errors import ChatbotError
from app.log import get_logger

log = get_logger("tokens")

if config.JWT_SECRET == "dev-insecure-change-me" and config.API_ENV == "production":
    log.error("🔴 JWT_SECRET غير مضبوط في الإنتاج — عيّن JWT_SECRET قوياً في .env فوراً.")


class InvalidTokenError(ChatbotError):
    """Missing / malformed / expired / wrong-signature token."""

    code = "INVALID_TOKEN"


def create_access_token(student_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(student_id),
        "iat": now,
        "exp": now + timedelta(hours=config.JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_token(token: str) -> str:
    """Return the student id (`sub`) from a valid token, else raise
    InvalidTokenError. Signature AND expiry are always verified."""
    try:
        payload = jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=[config.JWT_ALGORITHM],  # pin the algorithm (no "alg=none")
            options={"require": ["exp", "sub"]},  # a token without exp/sub is invalid
        )
    except jwt.ExpiredSignatureError:
        raise InvalidTokenError("انتهت صلاحية جلستك — سجّل الدخول من جديد.")
    except jwt.PyJWTError:
        raise InvalidTokenError("جلسة غير صالحة — سجّل الدخول من جديد.")

    sub: Optional[str] = payload.get("sub")
    if not sub:
        raise InvalidTokenError("جلسة غير صالحة — سجّل الدخول من جديد.")
    return str(sub)
