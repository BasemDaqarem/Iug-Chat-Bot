"""
FastAPI application factory.

Assembly only — no business logic lives here: middleware from
app.api.middleware, endpoints from app.api.routers.*, and the one heavy
IUGChatbot instance created at startup (lifespan) and shared via app.state.

    create_app()          → production/dev app (initializes the real bot)
    create_app(bot=fake)  → tests inject a stub, no Mongo/APIs needed
"""

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from app import config
from app.api import middleware
from app.api.routers import chat, files, health, sessions
from app.chatbot import IUGChatbot

_DESCRIPTION = """
واجهة برمجية لشات بوت الجامعة الإسلامية بغزة.

- **Chat**: محادثة RAG (قاعدة المعرفة أو الملفات المرفوعة) مع سجل جلسات.
- **Uploaded Files**: رفع ملفات JSON وإدارة فهارسها.
- **Sessions**: قراءة/مسح سجل المحادثة.
- **Health**: جاهزية الخدمة وإحصاءاتها.

المصادقة تُضاف لاحقاً كطبقة مستقلة — حالياً `session_id` يحدد هوية الجلسة.
"""


def create_app(bot: Optional[IUGChatbot] = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if bot is not None:
            app.state.bot = bot          # injected (tests)
        else:
            instance = IUGChatbot()
            instance.initialize()        # Mongo + cached embedding indexes
            app.state.bot = instance
        print("🚀 IUG Chatbot API جاهزة.")
        yield

    app = FastAPI(
        title="IUG Chatbot API",
        version="1.0.0",
        description=_DESCRIPTION,
        lifespan=lifespan,
        # Hide interactive docs in production; keep the OpenAPI JSON.
        docs_url="/docs" if config.API_ENV != "production" else None,
        redoc_url="/redoc" if config.API_ENV != "production" else None,
    )

    middleware.setup(app)

    app.include_router(health.router)
    app.include_router(chat.router, prefix="/api")
    app.include_router(files.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")

    return app
