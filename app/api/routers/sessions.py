"""Conversation-history endpoints — the authenticated student's OWN history."""

from fastapi import APIRouter, Depends

from app.api.deps import get_bot, get_current_student
from app.api.schemas import ErrorResponse, HistoryResponse, HistoryTurn, MessageResponse
from app.chatbot import IUGChatbot

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
    student_id: str = Depends(get_current_student),
    bot: IUGChatbot = Depends(get_bot),
) -> HistoryResponse:
    turns = [HistoryTurn(**t) for t in bot.get_history(student_id)]
    return HistoryResponse(session_id=student_id, turns=turns, count=len(turns))


@router.delete(
    "/me/history",
    response_model=MessageResponse,
    summary="مسح سجل محادثتي",
    description="يمسح سجل محادثة الطالب الموثّق (زر 'محادثة جديدة').",
)
def clear_history(
    student_id: str = Depends(get_current_student),
    bot: IUGChatbot = Depends(get_bot),
) -> MessageResponse:
    bot.clear_history(student_id)
    return MessageResponse(message="تم مسح سجل محادثتك.")
