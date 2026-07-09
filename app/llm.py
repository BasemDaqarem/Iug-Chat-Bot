"""
Chat-completion client for any OpenAI-compatible endpoint (OpenRouter, Groq,
NVIDIA, … — whatever CHAT_API_URL points to): payload building, retry policy,
and error mapping in ONE place. Error messages name the real provider (from
the URL) and surface the provider's own reason, so failures are actionable.
"""

import time

import requests

from app import config
from app.http_util import error_detail, provider_label, status_hint
from app.log import get_logger

log = get_logger("llm")


def _provider() -> str:
    return provider_label(config.CHAT_API_URL, "خدمة المحادثة")


def chat_completion(system: str, user_message: str) -> str:
    """One RAG-style completion: Arabic system prompt (context already
    inlined) + the user's message (history already folded in)."""
    if not config.CHAT_API_KEY:
        raise RuntimeError("❌ CHAT_API_KEY غير موجود في ملف .env — أضف مفتاح مزوّد المحادثة.")
    if not config.CHAT_API_URL:
        raise RuntimeError("❌ CHAT_API_URL غير موجود في ملف .env — أضف رابط مزوّد المحادثة.")

    headers = {
        "Authorization": f"Bearer {config.CHAT_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model":            config.CHAT_API_MODEL,
        "messages":         [
            {"role": "system", "content": system},
            {"role": "user",   "content": user_message},
        ],
        "temperature":      config.LLM_TEMPERATURE,
        "max_tokens":       config.LLM_MAX_TOKENS,
        "reasoning_effort": config.LLM_REASONING_EFFORT,
    }
    return _post_with_retry(headers, payload)


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
                raise RuntimeError(
                    f"❌ {provider} رفض الطلب (HTTP {resp.status_code}): "
                    f"{error_detail(resp)}{status_hint(resp.status_code)}"
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
            raise RuntimeError(
                f"❌ {provider} لم يُرجع نصاً للإجابة (استُهلكت ميزانية التوليد على التفكير) "
                "— جرّب صياغة أبسط أو أقصر للسؤال."
            )

        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                f"❌ تعذّر الاتصال بـ {provider} — تحقّق من الاتصال بالإنترنت ومن CHAT_API_URL في .env."
            )
        except requests.exceptions.Timeout:
            if attempt < max_retries:
                wait = 2 ** attempt
                log.warning("⏱️  %s Timeout — محاولة %d/%d، انتظار %ds …",
                            provider, attempt, max_retries, wait)
                time.sleep(wait)
                continue
            raise RuntimeError(f"❌ {provider} استغرق وقتاً طويلاً ولم يستجب — حاول مرة أخرى.")
        except RuntimeError:
            raise  # our own already-clean errors — don't re-wrap
        except Exception as exc:
            raise RuntimeError(f"❌ خطأ غير متوقّع أثناء مخاطبة {provider}: {exc}")

    raise RuntimeError(
        f"❌ {provider}: تجاوزنا حدّ الطلبات المسموح (429) بعد {max_retries} محاولات. "
        "انتظر لحظات أو تحقّق من خطة/رصيد مزوّد المحادثة."
    )
