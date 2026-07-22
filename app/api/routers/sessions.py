"""Conversation-history endpoints — the authenticated principal's OWN history."""

from fastapi import APIRouter, Depends

from app.api.deps import get_bot, get_current_principal
from app.api.schemas import ErrorResponse, HistoryResponse, HistoryTurn, MessageResponse
from app.api.errors import ServiceUnavailableError
from app.chatbot import IUGChatbot
from app.rbac import Principal

router = APIRouter(
    prefix="/sessions",
    tags=["Sessions"],
    responses={401: {"model": ErrorResponse, "description": "توكن مفقود أو منتهٍ"}},
)


@router.get(
    "/me/history",
    response_model=HistoryResponse,
    summary="سجل محادثتي",
    description=(
        "آخر أدوار محادثة الطالب الموثّق (بحد أقصى MAX_HISTORY). الهوية من التوكن، "
        "فلا يمكن لأحد قراءة سجل غيره. سجل فارغ يرجع قائمة فارغة — ليس خطأ."
    ),
)
def get_history(
    principal: Principal = Depends(get_current_principal),
    bot: IUGChatbot = Depends(get_bot),
) -> HistoryResponse:
    turns = [HistoryTurn(**t) for t in bot.get_history(principal.subject)]
    return HistoryResponse(
        session_id=principal.subject, turns=turns, count=len(turns)
    )


@router.delete(
    "/me/history",
    response_model=MessageResponse,
    summary="مسح سجل محادثتي",
    description="يمسح سجل محادثة الطالب الموثّق (زر 'محادثة جديدة').",
)
def clear_history(
    principal: Principal = Depends(get_current_principal),
    bot: IUGChatbot = Depends(get_bot),
) -> MessageResponse:
    if bot.clear_history(principal.subject) is False:
        raise ServiceUnavailableError(
            "تعذّر مسح سجل المحادثة الآن؛ لم يتم بدء محادثة جديدة."
        )
    return MessageResponse(message="تم مسح سجل محادثتك.")
