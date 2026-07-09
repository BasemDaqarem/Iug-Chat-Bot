"""
Cache monitoring & manual invalidation.

Stats expose ONLY aggregate counters (hits/misses/size) — never cached
content — so this router leaks nothing sensitive. Manual clear supports the
"invalidate on demand" requirement; it should sit behind admin auth once the
auth layer lands.
"""

from fastapi import APIRouter, Depends

from app.api.deps import get_bot
from app.api.schemas import CacheStatsResponse, MessageResponse
from app.chatbot import IUGChatbot

router = APIRouter(prefix="/cache", tags=["Cache"])


@router.get(
    "/stats",
    response_model=CacheStatsResponse,
    summary="إحصاءات الكاش",
    description=(
        "معدّل الإصابة (hit rate)، الحجم، الإزالات، وانتهاء الصلاحية لكل كاش. "
        "أرقام تجميعية فقط — لا تكشف أي محتوى مخزّن."
    ),
)
def cache_stats(bot: IUGChatbot = Depends(get_bot)) -> CacheStatsResponse:
    return CacheStatsResponse(**bot.cache_stats())


@router.post(
    "/clear",
    response_model=MessageResponse,
    summary="مسح الكاش يدوياً",
    description="يُفرغ كاش الإجابات ومتجهات الأسئلة (إبطال يدوي فوري).",
)
def cache_clear(bot: IUGChatbot = Depends(get_bot)) -> MessageResponse:
    bot.clear_caches()
    return MessageResponse(message="تم مسح الكاش (الإجابات + متجهات الأسئلة).")
