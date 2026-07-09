"""Health & AI status — the frontend's readiness probe."""

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
    return HealthResponse(
        status="ok",
        environment=config.API_ENV,
        collections=len(bot.data or {}),
        knowledge_chunks=len(bot.chunks or []),
        uploaded_files=len(bot.get_uploaded_files_list()),
        model=config.CHAT_API_MODEL,
        embed_model=config.EMBED_MODEL,
    )
