"""Conversation-history endpoints (read + clear) per session."""

from fastapi import APIRouter, Depends

from app.api.deps import get_bot
from app.api.schemas import HistoryResponse, HistoryTurn, MessageResponse
from app.chatbot import IUGChatbot

router = APIRouter(prefix="/sessions", tags=["Sessions"])


@router.get(
    "/{session_id}/history",
    response_model=HistoryResponse,
    summary="سجل محادثة جلسة",
    description=(
        "آخر أدوار المحادثة المحفوظة لهذه الجلسة (بحد أقصى MAX_HISTORY). "
        "جلسة بلا سجل ترجع قائمة فارغة — ليست خطأ."
    ),
)
def get_history(session_id: str, bot: IUGChatbot = Depends(get_bot)) -> HistoryResponse:
    turns = [HistoryTurn(**t) for t in bot.get_history(session_id)]
    return HistoryResponse(session_id=session_id, turns=turns, count=len(turns))


@router.delete(
    "/{session_id}/history",
    response_model=MessageResponse,
    summary="مسح سجل محادثة جلسة",
    description="يمسح سجل الجلسة من الذاكرة (زر 'محادثة جديدة' في الواجهة).",
)
def clear_history(session_id: str, bot: IUGChatbot = Depends(get_bot)) -> MessageResponse:
    bot.clear_history(session_id)
    return MessageResponse(message=f"تم مسح سجل الجلسة '{session_id}'.")
