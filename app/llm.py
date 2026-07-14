"""
Chat-completion client for any OpenAI-compatible endpoint (OpenRouter, Groq,
NVIDIA, … — whatever CHAT_API_URL points to): payload building, retry policy,
and error mapping in ONE place. Error messages name the real provider (from
the URL) and surface the provider's own reason, so failures are actionable.
"""

import json
import time
from typing import Iterator

import requests

from app import config
from app.errors import ChatbotError, ConfigurationError, UpstreamServiceError
from app.http_util import error_detail, provider_label, status_hint
from app.log import get_logger

log = get_logger("llm")


def _provider() -> str:
    return provider_label(config.CHAT_API_URL, "خدمة المحادثة")


def _require_config() -> None:
    if not config.CHAT_API_KEY:
        raise ConfigurationError("❌ CHAT_API_KEY غير موجود في ملف .env — أضف مفتاح مزوّد المحادثة.")
    if not config.CHAT_API_URL:
        raise ConfigurationError("❌ CHAT_API_URL غير موجود في ملف .env — أضف رابط مزوّد المحادثة.")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.CHAT_API_KEY}",
        "Content-Type":  "application/json",
    }


def _base_payload(system: str, user_message: str) -> dict:
    return {
        "model":            config.CHAT_API_MODEL,
        "messages":         [
            {"role": "system", "content": system},
            {"role": "user",   "content": user_message},
        ],
        "temperature":      config.LLM_TEMPERATURE,
        "max_tokens":       config.LLM_MAX_TOKENS,
        "reasoning_effort": config.LLM_REASONING_EFFORT,
    }


def chat_completion(system: str, user_message: str) -> str:
    """One RAG-style completion: Arabic system prompt (context already
    inlined) + the user's message (history already folded in)."""
    _require_config()
    return _post_with_retry(_headers(), _base_payload(system, user_message))


def stream_completion(system: str, user_message: str) -> Iterator[str]:
    """Stream the answer token-by-token from an OpenAI-compatible SSE endpoint.

    Yields visible-content deltas only. Robust to two real-world quirks proven
    live against gpt-oss on OpenRouter:
      • the `delta.content` field is an empty string during the model's hidden
        reasoning phase — those deltas are skipped, real text follows;
      • `requests.iter_lines(decode_unicode=True)` can split a `data:` line
        mid-JSON (and mid-multibyte Arabic) → we buffer BYTES and only decode
        complete `\\n`-terminated lines, so JSON is never parsed half-formed.
    """
    _require_config()
    provider = _provider()
    payload = {**_base_payload(system, user_message), "stream": True}
    try:
        resp = requests.post(
            config.CHAT_API_URL, headers=_headers(), json=payload, stream=True, timeout=60
        )
    except requests.exceptions.ConnectionError:
        raise UpstreamServiceError(
            f"❌ تعذّر الاتصال بـ {provider} — تحقّق من الاتصال بالإنترنت ومن CHAT_API_URL في .env."
        )
    except requests.exceptions.Timeout:
        raise UpstreamServiceError(f"❌ {provider} استغرق وقتاً طويلاً ولم يستجب — حاول مرة أخرى.")

    if resp.status_code >= 400:
        raise UpstreamServiceError(
            f"❌ {provider} رفض الطلب (HTTP {resp.status_code}): "
            f"{error_detail(resp)}{status_hint(resp.status_code)}",
            details={"provider": provider, "status": resp.status_code},
        )

    buf = b""
    with resp:
        for chunk in resp.iter_content(chunk_size=None):
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                # A complete line is valid UTF-8; decoding it (not the arbitrary
                # network chunk) is what avoids the mid-multibyte split.
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    delta = json.loads(data)["choices"][0].get("delta", {})
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue  # keep-alive / partial / reasoning-only frames
                content = delta.get("content")
                if content:
                    yield content


def _post_with_retry(headers: dict, payload: dict, max_retries: int = 4) -> str:
    provider = _provider()
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(config.CHAT_API_URL, headers=headers, json=payload, timeout=60)

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else (2 ** attempt)
                log.warning("⚠️  %s 429 (طلبات كثيرة) — محاولة %d/%d، انتظار %.1fs …",
                            provider, attempt, max_retries, wait)
                time.sleep(wait)
                continue

            if resp.status_code >= 400:
                # Surface the provider's OWN reason + an actionable hint,
                # instead of a bare status code.
                raise UpstreamServiceError(
                    f"❌ {provider} رفض الطلب (HTTP {resp.status_code}): "
                    f"{error_detail(resp)}{status_hint(resp.status_code)}",
                    details={"provider": provider, "status": resp.status_code},
                )

            choice = resp.json()["choices"][0]
            content = (choice.get("message") or {}).get("content")
            if content:
                return content.strip()

            # Reasoning models can consume the whole token budget on hidden
            # reasoning and return null/empty content (finish_reason "length").
            # Retry once with a larger budget before giving up, so a complex
            # question degrades into a slower answer rather than a hard error.
            if choice.get("finish_reason") == "length" and attempt < max_retries:
                payload = {**payload, "max_tokens": payload.get("max_tokens", 450) * 2}
                continue
            raise UpstreamServiceError(
                f"❌ {provider} لم يُرجع نصاً للإجابة (استُهلكت ميزانية التوليد على التفكير) "
                "— جرّب صياغة أبسط أو أقصر للسؤال."
            )

        except requests.exceptions.ConnectionError:
            raise UpstreamServiceError(
                f"❌ تعذّر الاتصال بـ {provider} — تحقّق من الاتصال بالإنترنت ومن CHAT_API_URL في .env."
            )
        except requests.exceptions.Timeout:
            if attempt < max_retries:
                wait = 2 ** attempt
                log.warning("⏱️  %s Timeout — محاولة %d/%d، انتظار %ds …",
                            provider, attempt, max_retries, wait)
                time.sleep(wait)
                continue
            raise UpstreamServiceError(f"❌ {provider} استغرق وقتاً طويلاً ولم يستجب — حاول مرة أخرى.")
        except ChatbotError:
            raise  # our own already-clean domain errors — don't re-wrap
        except Exception as exc:
            raise UpstreamServiceError(f"❌ خطأ غير متوقّع أثناء مخاطبة {provider}: {exc}")

    raise UpstreamServiceError(
        f"❌ {provider}: تجاوزنا حدّ الطلبات المسموح (429) بعد {max_retries} محاولات. "
        "انتظر لحظات أو تحقّق من خطة/رصيد مزوّد المحادثة."
    )
