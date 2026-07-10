"""
Chat endpoints — thin delegates to IUGChatbot's chat flows.

EVERY chat endpoint requires a valid session token and derives the identity
(and therefore the conversation-history key) FROM the token — never from a
client-supplied session_id. This closes the IDOR/history-leak surface: a
caller can only ever act as, and read the history of, themselves.

All endpoints are plain `def` (not `async def`) ON PURPOSE: the underlying
pipeline is blocking I/O (requests → embeddings API + LLM, pymongo), and
FastAPI runs sync routes in its threadpool, so one slow LLM call never blocks
the event loop or other requests.
"""

from fastapi import APIRouter, Depends

from app.api.deps import get_bot, rate_limited_student
from app.api.errors import NotFoundError
from app.api.schemas import ChatResponse, ErrorResponse, StudentChatRequest
from app.chatbot import IUGChatbot

# Upstream failures (LLM / embeddings) raise UpstreamServiceError inside the
# pipeline and are turned into a clean 502 by the centralized error handler —
# routes stay free of try/except plumbing.
router = APIRouter(
    prefix="/chat",
    tags=["Chat"],
    responses={
        401: {"model": ErrorResponse, "description": "توكن مفقود أو منتهٍ"},
        502: {"model": ErrorResponse, "description": "خطأ من خدمة خارجية (LLM/Embeddings)"},
    },
)


@router.post(
    "/student",
    response_model=ChatResponse,
    summary="محادثة الطالب (تدمج ملفه الأكاديمي)",
    description=(
        "النقطة التي تستخدمها الواجهة. أسئلة «حالتي/معدلي/ترتيبي» تُجاب من ملف "
        "الطالب الموثّق، وأي سؤال آخر من محتوى الجامعة. الهوية من التوكن."
    ),
)
def chat_student(
    body: StudentChatRequest,
    student_id: str = Depends(rate_limited_student),
    bot: IUGChatbot = Depends(get_bot),
) -> ChatResponse:
    return ChatResponse(**bot.chat_as_student(body.question, student_id))


@router.post(
    "",
    response_model=ChatResponse,
    summary="محادثة على قاعدة المعرفة الرئيسية",
    description="استرجاع هجين مُرشَّح بالصلاحية من قاعدة المعرفة ثم إجابة الـ LLM.",
)
def chat(
    body: StudentChatRequest,
    student_id: str = Depends(rate_limited_student),
    bot: IUGChatbot = Depends(get_bot),
) -> ChatResponse:
    return ChatResponse(**bot.chat(body.question, student_id))


@router.post(
    "/files",
    response_model=ChatResponse,
    summary="محادثة على كل الملفات المرفوعة",
    description="بحث هجين عبر جميع الملفات المرفوعة مدموجاً في ترتيب واحد.",
)
def chat_all_files(
    body: StudentChatRequest,
    student_id: str = Depends(rate_limited_student),
    bot: IUGChatbot = Depends(get_bot),
) -> ChatResponse:
    return ChatResponse(**bot.chat_with_all_files(body.question, student_id))


@router.post(
    "/files/{collection_name}",
    response_model=ChatResponse,
    summary="محادثة على ملف مرفوع واحد",
    description="بحث مقصور على الملف المحدد فقط. 404 إذا لم يكن الملف مرفوعاً.",
    responses={404: {"model": ErrorResponse, "description": "الملف غير موجود"}},
)
def chat_one_file(
    collection_name: str,
    body: StudentChatRequest,
    student_id: str = Depends(rate_limited_student),
    bot: IUGChatbot = Depends(get_bot),
) -> ChatResponse:
    files = {f["collection"] for f in bot.get_uploaded_files_list()}
    if collection_name not in files:
        raise NotFoundError(f"الملف '{collection_name}' غير موجود. ارفعه أولاً.")
    return ChatResponse(**bot.chat_with_file(body.question, collection_name, student_id))
