"""
Centralized API error handling.

ONE place decides the HTTP status, the machine-readable code, and the response
shape for every error the API can produce, so:

  • every error body is identical in structure (the envelope below),
  • routes raise small typed errors instead of hand-building responses,
  • unexpected exceptions are logged with a full traceback but NEVER leak a
    stack trace to the client (only in non-production, only as `details`).

Unified error envelope (errors only — success responses keep their own model):

    {
      "success": false,
      "error": {
        "code": "NOT_FOUND",
        "message": "…رسالة عربية واضحة…",
        "details": null | "…" | [{"field": "...", "message": "..."}],
        "timestamp": "2026-07-09T12:00:00+00:00",
        "path": "/api/chat"
      }
    }
"""

from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config
from app.errors import ChatbotError, ConfigurationError
from app.log import get_logger
from app.tokens import InvalidTokenError

log = get_logger("api.errors")


# ═════════════════════════════════════════════════════════════════════════
#  API-layer error classes — routes raise these; the handler renders them.
# ═════════════════════════════════════════════════════════════════════════

class APIError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, details=None, *, status_code=None, code=None):
        super().__init__(message)
        self.message = message
        self.details = details
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class BadRequestError(APIError):
    status_code, code = 400, "BAD_REQUEST"


class UnauthorizedError(APIError):
    status_code, code = 401, "UNAUTHORIZED"


class ForbiddenError(APIError):
    status_code, code = 403, "FORBIDDEN"


class NotFoundError(APIError):
    status_code, code = 404, "NOT_FOUND"


class ConflictError(APIError):
    status_code, code = 409, "CONFLICT"


class UpstreamError(APIError):
    status_code, code = 502, "UPSTREAM_ERROR"


class ServiceUnavailableError(APIError):
    status_code, code = 503, "SERVICE_UNAVAILABLE"


# Fallback machine codes for raw HTTPExceptions (e.g. FastAPI's own 404/405).
_STATUS_CODE_NAMES = {
    400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED", 409: "CONFLICT", 422: "VALIDATION_ERROR",
    429: "RATE_LIMITED", 500: "INTERNAL_ERROR", 502: "UPSTREAM_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


# ═════════════════════════════════════════════════════════════════════════
#  Envelope + handlers
# ═════════════════════════════════════════════════════════════════════════

def _render(status: int, code: str, message: str, details, request: Request) -> JSONResponse:
    body = {
        "success": False,
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "path": request.url.path,
        },
    }
    return JSONResponse(status_code=status, content=body)


def setup_error_handlers(app: FastAPI) -> None:

    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError):
        if exc.status_code >= 500:
            log.error("APIError %d %s at %s: %s",
                      exc.status_code, exc.code, request.url.path, exc.message)
        return _render(exc.status_code, exc.code, exc.message, exc.details, request)

    @app.exception_handler(InvalidTokenError)
    async def _invalid_token(request: Request, exc: InvalidTokenError):
        # Belt-and-suspenders: even if an InvalidTokenError escapes the auth
        # dependency, it renders as 401 (never a 502 via the ChatbotError path).
        return _render(401, "UNAUTHORIZED", exc.message, None, request)

    @app.exception_handler(ChatbotError)
    async def _domain_error(request: Request, exc: ChatbotError):
        # Service-layer failure: config → 503, everything else (upstream) → 502.
        status = 503 if isinstance(exc, ConfigurationError) else 502
        log.warning("%s at %s: %s", exc.code, request.url.path, exc.message)
        return _render(status, exc.code, exc.message, exc.details, request)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        fields = []
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", []) if p != "body"]
            fields.append({"field": ".".join(loc) or "body",
                           "message": err.get("msg", "قيمة غير صحيحة")})
        return _render(422, "VALIDATION_ERROR",
                       "بيانات الطلب غير صحيحة — راجع الحقول المذكورة.", fields, request)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = _STATUS_CODE_NAMES.get(exc.status_code, "ERROR")
        message = exc.detail if isinstance(exc.detail, str) else "حدث خطأ في الطلب."
        return _render(exc.status_code, code, message, None, request)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # A real bug: log the full traceback for developers, but return a
        # clean, non-technical message — and only expose the exception text as
        # `details` outside production.
        log.exception("خطأ داخلي غير متوقّع at %s", request.url.path)
        details = f"{type(exc).__name__}: {exc}" if config.API_ENV != "production" else None
        return _render(500, "INTERNAL_ERROR",
                       "حدث خطأ داخلي غير متوقّع. حاول مرة أخرى لاحقاً.", details, request)
