"""
Chat endpoints — thin delegates to IUGChatbot's three chat flows.

All endpoints are plain `def` (not `async def`) ON PURPOSE: the underlying
pipeline is blocking I/O (requests → embeddings API + LLM, pymongo), and
FastAPI runs sync routes in its threadpool, so one slow LLM call never blocks
the event loop or other requests.
"""

from fastapi import APIRouter, Depends

from app.api.deps import get_bot
from app.api.errors import NotFoundError
from app.api.schemas import ChatRequest, ChatResponse, ErrorResponse
from app.chatbot import IUGChatbot

# Upstream failures (LLM / embeddings) raise UpstreamServiceError inside the
# pipeline and are turned into a clean 502 by the centralized error handler —
# routes stay free of try/except plumbing.
router = APIRouter(
    prefix="/chat",
    tags=["Chat"],
    responses={
        502: {"model": ErrorResponse, "description": "خطأ من خدمة خارجية (LLM/Embeddings)"},
    },
)


@router.post(
    "",
    response_model=ChatResponse,
    summary="محادثة على قاعدة المعرفة الرئيسية",
    description=(
        "التدفق الكامل: استرجاع هجين مُرشَّح بالصلاحية من قاعدة المعرفة، "
        "ثم حارس الخصوصية، ثم إجابة الـ LLM. سجل المحادثة يُدار تلقائياً "
        "حسب session_id."
    ),
)
def chat(body: ChatRequest, bot: IUGChatbot = Depends(get_bot)) -> ChatResponse:
    return ChatResponse(**bot.chat(body.question, body.session_id))


@router.post(
    "/files",
    response_model=ChatResponse,
    summary="محادثة على كل الملفات المرفوعة",
    description=(
        "بحث هجين عبر جميع الملفات المرفوعة مدموجاً في ترتيب واحد — "
        "الـ LLM يرى أفضل top-K مقاطع فقط أياً كان مصدرها."
    ),
)
def chat_all_files(body: ChatRequest, bot: IUGChatbot = Depends(get_bot)) -> ChatResponse:
    return ChatResponse(**bot.chat_with_all_files(body.question, body.session_id))


@router.post(
    "/student",
    response_model=ChatResponse,
    summary="محادثة الطالب (تدمج ملفه الأكاديمي)",
    description=(
        "الواجهة بعد الدخول تستخدم هذه النقطة: أسئلة الطالب عن حالته/معدله/ترتيبه "
        "تُجاب من ملفه الشخصي (بحثاً عن هويته في students_auth، فلا يرى إلا بياناته)؛ "
        "وأي سؤال آخر يُجاب من محتوى الجامعة."
    ),
)
def chat_student(body: ChatRequest, bot: IUGChatbot = Depends(get_bot)) -> ChatResponse:
    return ChatResponse(**bot.chat_as_student(body.question, body.session_id))


@router.post(
    "/files/{collection_name}",
    response_model=ChatResponse,
    summary="محادثة على ملف مرفوع واحد",
    description="بحث مقصور على الملف المحدد فقط. 404 إذا لم يكن الملف مرفوعاً.",
    responses={404: {"model": ErrorResponse, "description": "الملف غير موجود"}},
)
def chat_one_file(
    collection_name: str,
    body: ChatRequest,
    bot: IUGChatbot = Depends(get_bot),
) -> ChatResponse:
    files = {f["collection"] for f in bot.get_uploaded_files_list()}
    if collection_name not in files:
        raise NotFoundError(f"الملف '{collection_name}' غير موجود. ارفعه أولاً.")
    return ChatResponse(**bot.chat_with_file(body.question, collection_name, body.session_id))
