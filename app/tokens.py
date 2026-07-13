"""
Signed session tokens (JWT).

Login/registration issue a short-lived HS256 token whose claims include the
server-selected role and token version. Every protected request derives its
Principal FROM this token — never from a client-supplied role or user id.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from app import config
from app.errors import ChatbotError
from app.log import get_logger
from app.rbac import Principal, Role, coerce_role

log = get_logger("tokens")

if config.JWT_SECRET == "dev-insecure-change-me" and config.API_ENV == "production":
    log.error("🔴 JWT_SECRET غير مضبوط في الإنتاج — عيّن JWT_SECRET قوياً في .env فوراً.")


class InvalidTokenError(ChatbotError):
    """Missing / malformed / expired / wrong-signature token."""

    code = "INVALID_TOKEN"


_minimum_token_versions: dict[str, int] = {}


def create_access_token(
    subject: str,
    role: str | Role = Role.STUDENT,
    token_version: int = 1,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(subject),
        "role": coerce_role(role).value,
        "token_version": int(token_version),
        "iat": now,
        "exp": now + timedelta(hours=config.JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def revoke_subject(subject: str, new_token_version: int) -> None:
    """Immediately invalidate older tokens in this process.

    The version is also persisted on the account by the admin service.  This
    in-process floor closes the window immediately for the current worker.
    """
    sid = str(subject)
    _minimum_token_versions[sid] = max(
        int(new_token_version), _minimum_token_versions.get(sid, 0)
    )


def decode_principal(token: str) -> Principal:
    """Verify signature/expiry and return the server-issued Principal."""
    try:
        payload = jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=[config.JWT_ALGORITHM],  # pin the algorithm (no "alg=none")
            options={"require": ["exp", "sub", "role", "token_version"]},
        )
    except jwt.ExpiredSignatureError:
        raise InvalidTokenError("انتهت صلاحية جلستك — سجّل الدخول من جديد.")
    except jwt.PyJWTError:
        raise InvalidTokenError("جلسة غير صالحة — سجّل الدخول من جديد.")

    sub: Optional[str] = payload.get("sub")
    if not sub:
        raise InvalidTokenError("جلسة غير صالحة — سجّل الدخول من جديد.")
    try:
        role = Role(str(payload.get("role")))
        token_version = int(payload.get("token_version"))
    except (TypeError, ValueError):
        raise InvalidTokenError("جلسة غير صالحة — سجّل الدخول من جديد.")

    if token_version < _minimum_token_versions.get(str(sub), 0):
        raise InvalidTokenError("تم إنهاء هذه الجلسة — سجّل الدخول من جديد.")
    return Principal(str(sub), role, token_version)


def decode_token(token: str) -> str:
    """Backward-compatible helper returning only the verified subject."""
    return decode_principal(token).subject
