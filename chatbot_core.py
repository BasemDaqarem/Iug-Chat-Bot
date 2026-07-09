"""
Backward-compatibility shim — the implementation now lives in the app/
package (see README.md for the architecture map).

Existing imports keep working unchanged, e.g.:
    from chatbot_core import IUGChatbot, SYSTEM_PROMPT_TEMPLATE, TOP_K
Running `python chatbot_core.py` still starts the console harness.
"""

from app.chatbot import IUGChatbot
from app.chunking import SENSITIVE_MARKER
from app.config import (
    CHAT_API_KEY,
    CHAT_API_MODEL,
    CHAT_API_URL,
    EMBED_API_KEY,
    EMBED_API_URL,
    EMBED_MODEL,
    LLM_MAX_TOKENS,
    MAX_HISTORY,
    RAG_EXCLUDE_COLLECTIONS,
    SIM_THRESHOLD,
    TOP_K,
)
from app.prompts import SYSTEM_PROMPT_TEMPLATE, UPLOADED_FILE_SYSTEM_PROMPT

__all__ = [
    "IUGChatbot",
    "SENSITIVE_MARKER",
    "SYSTEM_PROMPT_TEMPLATE",
    "UPLOADED_FILE_SYSTEM_PROMPT",
    "CHAT_API_MODEL", "CHAT_API_KEY", "CHAT_API_URL",
    "EMBED_MODEL", "EMBED_API_KEY", "EMBED_API_URL",
    "TOP_K", "MAX_HISTORY", "SIM_THRESHOLD", "LLM_MAX_TOKENS",
    "RAG_EXCLUDE_COLLECTIONS",
]

if __name__ == "__main__":
    from cli import main
    main()
