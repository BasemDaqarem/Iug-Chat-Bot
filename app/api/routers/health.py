"""Health & AI status — the frontend's readiness probe."""

import hashlib

from fastapi import APIRouter, Depends

from app import config
from app.api.deps import get_bot
from app.api.schemas import HealthResponse
from app.chatbot import IUGChatbot

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="فحص جاهزية الخدمة وحالة الذكاء الاصطناعي",
    description=(
        "يرجع حالة الخدمة مع إحصاءات قاعدة المعرفة (collections/chunks) "
        "والملفات المرفوعة والنماذج المستخدمة. مناسب كـ readiness probe "
        "وللعرض في شاشة 'حالة النظام' في الواجهة."
    ),
)
def health(bot: IUGChatbot = Depends(get_bot)) -> HealthResponse:
    if hasattr(bot, "readiness"):
        state = bot.readiness()
    else:
        data = bot.data or {}
        files = bot.get_uploaded_files_list()
        state = {
            "status": "ready",
            "index_ready": True,
            "document_count": sum(len(items) for items in data.values()),
            "chunk_count": len(bot.chunks or []),
            "uploaded_chunk_count": sum(
                int(item.get("chunks_count", 0)) for item in files
            ),
            "index_version": "injected-test-double",
            "failed_sources": [],
            "failed_refresh_sources": [],
            "initialization_error": None,
        }
    source_hash = lambda value: hashlib.sha256(
        str(value).encode("utf-8")
    ).hexdigest()[:12]
    return HealthResponse(
        status=state["status"],
        index_ready=state["index_ready"],
        environment=config.API_ENV,
        collections=len(bot.data or {}),
        document_count=state["document_count"],
        knowledge_chunks=state["chunk_count"],
        uploaded_files=len(bot.get_uploaded_files_list()),
        uploaded_chunks=state["uploaded_chunk_count"],
        index_version=state["index_version"],
        failed_sources=[source_hash(item) for item in state["failed_sources"]],
        failed_refresh_sources=[
            source_hash(item) for item in state["failed_refresh_sources"]
        ],
        initialization_error=state["initialization_error"],
        model=config.CHAT_API_MODEL,
        embed_model=config.EMBED_MODEL,
    )
