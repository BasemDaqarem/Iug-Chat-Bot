"""
Groq chat-completion client: payload building, retry policy, and error
mapping in ONE place — previously this block was copy-pasted into every
chat flow.
"""

import time

import requests

from app import config
from app.log import get_logger

log = get_logger("llm")


def chat_completion(system: str, user_message: str) -> str:
    """One RAG-style completion: Arabic system prompt (context already
    inlined) + the user's message (history already folded in)."""
    if not config.CHAT_API_KEY:
        raise RuntimeError("❌ CHAT_API_KEY غير موجود — أضفه في ملف .env")

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
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(config.CHAT_API_URL, headers=headers, json=payload, timeout=60)

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else (2 ** attempt)
                log.warning("⚠️  Groq 429 — المحاولة %d/%d، انتظار %.1fs …", attempt, max_retries, wait)
                time.sleep(wait)
                continue

            resp.raise_for_status()
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
                "❌ Groq لم يُرجع نصاً للإجابة — جرّب صياغة أبسط أو أقصر للسؤال."
            )

        except requests.exceptions.ConnectionError:
            raise RuntimeError("❌ تعذّر الاتصال بـ Groq API — تحقق من الاتصال بالإنترنت.")
        except requests.exceptions.Timeout:
            if attempt < max_retries:
                wait = 2 ** attempt
                log.warning("⏱️  Groq Timeout — المحاولة %d/%d، انتظار %ds …", attempt, max_retries, wait)
                time.sleep(wait)
                continue
            raise RuntimeError("❌ Groq API استغرق وقتاً طويلاً — حاول مرة أخرى.")
        except RuntimeError:
            raise  # our own already-clean errors — don't re-wrap
        except Exception as exc:
            raise RuntimeError(f"❌ خطأ في Groq: {exc}")

    raise RuntimeError(
        "❌ Groq API: تجاوزنا الحد المسموح به من الطلبات (429). "
        "حاول بعد لحظات أو تحقق من خطة Groq الخاصة بك."
    )
