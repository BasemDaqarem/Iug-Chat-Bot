"""
Shared helpers for turning HTTP API failures into useful, honest errors.

The chat and embeddings endpoints are BOTH configurable (any OpenAI-compatible
/ Jina-compatible URL), so error messages must name the ACTUAL provider from
the configured URL — never a hardcoded vendor — and surface the real reason
the provider returned (status code + its own error message), not a generic
"something failed".
"""

from urllib.parse import urlparse


def provider_label(url: str, fallback: str) -> str:
    """Human-readable provider name taken from the endpoint host, e.g.
    'https://openrouter.ai/api/...' → 'openrouter.ai'. Falls back to a generic
    label when the URL is missing/unparseable."""
    try:
        host = urlparse(url or "").hostname
    except Exception:
        host = None
    return host or fallback


def error_detail(resp) -> str:
    """Pull the provider's own error message out of a failed response body,
    handling the common shapes ({"error": {"message": ...}}, {"error": "..."},
    {"message": ...}) and falling back to a trimmed raw body."""
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                return err.get("message") or err.get("code") or str(err)
            if isinstance(err, str) and err:
                return err
            if isinstance(data.get("message"), str) and data["message"]:
                return data["message"]
    except Exception:
        pass
    text = (getattr(resp, "text", "") or "").strip()
    return text[:300] if text else "بلا تفاصيل من الخادم"


def status_hint(code: int) -> str:
    """Actionable Arabic hint for a status code — tells the user what to check."""
    if code in (401, 403):
        return " — تحقّق من صحة مفتاح الـ API في ملف .env"
    if code == 404:
        return " — تحقّق من اسم النموذج ورابط الـ API في .env"
    if code == 400:
        return " — قد يكون النموذج أو صيغة الطلب غير صحيحة"
    if code == 429:
        return " — تجاوزت حدّ الطلبات المسموح، انتظر قليلاً"
    if code >= 500:
        return " — الخطأ من مزوّد الخدمة نفسه، حاول لاحقاً"
    return ""
