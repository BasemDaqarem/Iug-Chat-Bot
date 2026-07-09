"""
FastAPI dependencies.

The chatbot is one heavy, long-lived object (Mongo data + embedding indexes
in memory). It is created ONCE at startup (see create_app's lifespan) and
handed to routes through this dependency — routes never construct services
themselves, which keeps them thin and lets tests inject a fake bot.
"""

from fastapi import Request

from app.api.errors import ServiceUnavailableError
from app.chatbot import IUGChatbot


def get_bot(request: Request) -> IUGChatbot:
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        # Startup failed (e.g. MongoDB unreachable) — surface a clean 503
        # instead of an AttributeError from a half-initialized app.
        raise ServiceUnavailableError("الخدمة لم تكتمل تهيئتها بعد — حاول لاحقاً.")
    return bot
