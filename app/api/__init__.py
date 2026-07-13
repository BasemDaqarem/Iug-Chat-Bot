"""
FastAPI application factory.

Assembly only — no business logic lives here: middleware from
app.api.middleware, endpoints from app.api.routers.*, and the one heavy
IUGChatbot instance created at startup (lifespan) and shared via app.state.

    create_app()          → production/dev app (initializes the real bot)
    create_app(bot=fake)  → tests inject a stub, no Mongo/APIs needed
"""

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import auth as auth_service, config
from app.api import middleware
from app.api.errors import setup_error_handlers
from app.api.routers import admin, auth, cache, chat, files, health, portal, sessions
from app.chatbot import IUGChatbot
from app.log import get_logger

log = get_logger("api")

_FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend"))


class _NoCacheStatic(StaticFiles):
    """Serve the frontend with caching disabled so the browser can NEVER run a
    stale app.js/chat.js — the root cause of "login succeeds but doesn't
    redirect" after a code update. (For an official production build, switch to
    hashed asset URLs + long cache instead.)"""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

_DESCRIPTION = """
واجهة برمجية لشات بوت الجامعة الإسلامية بغزة.

- **Chat**: محادثة RAG مفلترة حسب دور الزائر/الطالب/الموظف/الأدمن.
- **Uploaded Files**: رفع ملفات JSON وإدارة فهارسها.
- **Admin**: إدارة الموظفين ودورة مسودة/معالجة/نشر الملفات وسجل التدقيق.
- **Sessions**: قراءة/مسح سجل المحادثة.
- **Health**: جاهزية الخدمة وإحصاءاتها.

الهوية والدور يؤخذان من JWT الموقّع ولا يقبلان من جسم الطلب.
"""


def create_app(bot: Optional[IUGChatbot] = None) -> FastAPI:
    config.assert_secure_for_production()  # refuse to boot prod with a weak JWT secret

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if bot is not None:
            app.state.bot = bot          # injected (tests)
        else:
            instance = IUGChatbot()
            instance.initialize()        # Mongo + cached embedding indexes
            app.state.bot = instance
            auth_service.ensure_bootstrap_admin(
                config.ADMIN_BOOTSTRAP_ID,
                config.ADMIN_BOOTSTRAP_PASSWORD,
                config.ADMIN_BOOTSTRAP_NAME,
            )
        log.info("🚀 IUG Chatbot API جاهزة.")
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
    # Real deployments re-check account state/version on every protected
    # request. Injected bots are isolated test doubles and have no Mongo.
    app.state.verify_account_tokens = bot is None

    middleware.setup(app)
    setup_error_handlers(app)

    app.include_router(health.router)
    app.include_router(auth.router, prefix="/api")
    app.include_router(chat.router, prefix="/api")
    app.include_router(files.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    app.include_router(cache.router, prefix="/api")
    app.include_router(admin.router, prefix="/api")
    app.include_router(portal.router, prefix="/api")

    # Premium auth UI (static, offline) — served at /app/ (mounted last so it
    # never shadows the /api and /health routes above). No-cache so a frontend
    # update is always picked up on the next request.
    if os.path.isdir(_FRONTEND_DIR):
        app.mount("/app", _NoCacheStatic(directory=_FRONTEND_DIR, html=True), name="frontend")

    return app
