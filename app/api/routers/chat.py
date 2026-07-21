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

import time

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from uuid import uuid4

from app import file_catalog
from app.api.deps import (
    get_bot,
    guest_chat_rate_limit,
    rate_limited_principal,
    rate_limited_student,
)
from app.api.errors import NotFoundError
from app.api.schemas import ChatResponse, ErrorResponse, StudentChatRequest
from app.chatbot import IUGChatbot
from app.rbac import Principal, Role

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
    if not isinstance(bot, IUGChatbot):
        return ChatResponse(**bot.chat_as_student(body.question, student_id))
    principal = Principal(student_id, Role.STUDENT)
    available = {item["collection"] for item in bot.get_uploaded_files_list()}
    allowed = file_catalog.allowed_collections(principal, available)
    return ChatResponse(**bot.chat_as_principal(
        body.question, principal, allowed_collections=allowed
    ))


@router.post(
    "/student/stream",
    summary="محادثة موثّقة — بثّ الإجابة كلمة‑كلمة (كل الأدوار)",
    description=(
        "بثّ الإجابة تدريجياً كنصّ UTF-8 لأي دور موثّق (طالب/موظف/أدمن) — نفس "
        "الترشيح بالصلاحية والذاكرة والخصوصية لكلٍّ حسب دوره من التوكن. أي سؤال "
        "يجيب عنه البوت للطالب يجيب عنه للموظف والأدمن أيضاً (مع سياق دورهم). "
        "الأخطاء قبل أول بايت تُرجَع 401/429 عادية؛ خطأ أثناء البثّ يظهر كنصّ. "
        "(الاسم /student/stream تاريخيّ — أبقيناه كي لا تنكسر الواجهات.)"
    ),
)
def chat_stream(
    body: StudentChatRequest,
    principal: Principal = Depends(rate_limited_principal),
    bot: IUGChatbot = Depends(get_bot),
) -> StreamingResponse:
    # Belt-and-braces: tell any proxy NOT to buffer this response either.
    trace_id = uuid4().hex
    stream_headers = {
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache",
        "X-Trace-ID": trace_id,
    }
    if not isinstance(bot, IUGChatbot):  # injected test/legacy bot — no Mongo
        return StreamingResponse(
            bot.stream_answer(body.question, principal.subject),
            media_type="text/plain; charset=utf-8", headers=stream_headers,
        )
    # Streaming sends HTTP 200 before consuming the generator.  Readiness must
    # therefore be checked here, while a clean 503 can still be returned.
    bot.ensure_ready()
    available = {item["collection"] for item in bot.get_uploaded_files_list()}
    allowed = file_catalog.allowed_collections(principal, available)
    return StreamingResponse(
        bot.stream_answer(
            body.question,
            principal,
            allowed_collections=allowed,
            trace_id=trace_id,
        ),
        media_type="text/plain; charset=utf-8", headers=stream_headers,
    )


@router.post(
    "",
    response_model=ChatResponse,
    summary="محادثة على قاعدة المعرفة الرئيسية",
    description="استرجاع هجين مُرشَّح بالصلاحية من قاعدة المعرفة ثم إجابة الـ LLM.",
)
def chat(
    body: StudentChatRequest,
    principal: Principal = Depends(rate_limited_principal),
    bot: IUGChatbot = Depends(get_bot),
) -> ChatResponse:
    # Keep lightweight injected test/legacy bots compatible during migration.
    if not hasattr(bot, "chat_as_principal"):
        return ChatResponse(**bot.chat(body.question, principal.subject))
    available = {item["collection"] for item in bot.get_uploaded_files_list()}
    allowed = file_catalog.allowed_collections(principal, available)
    return ChatResponse(**bot.chat_as_principal(
        body.question, principal, allowed_collections=allowed
    ))


@router.post(
    "/guest",
    response_model=ChatResponse,
    summary="محادثة الزائر من الملفات العامة فقط",
    dependencies=[Depends(guest_chat_rate_limit)],  # بلا توكن → تحديد بالـ IP
)
def chat_guest(
    body: StudentChatRequest,
    bot: IUGChatbot = Depends(get_bot),
) -> ChatResponse:
    principal = Principal.guest(f"guest:{uuid4().hex}")
    if not hasattr(bot, "chat_as_principal"):
        return ChatResponse(**bot.chat(body.question, principal.subject))
    available = {item["collection"] for item in bot.get_uploaded_files_list()}
    allowed = file_catalog.allowed_collections(principal, available)
    # الزوار بلا جلسات مخزّنة (قرار أمني) — متصفح الزائر يرسل آخر أدواره
    # ليفهم البوت المتابعات؛ تُستخدم لهذا الطلب فقط ولا تُخزَّن أبداً.
    client_history = None
    if body.history:
        now = time.time()
        # قصّ أجوبة السجل الطويلة قبل طيّها في البرومت: 5 أدوار بأجوبة كاملة
        # (حتى 6000 حرف للدور) تُضخّم السياق فتتفكك أمانة الإجابة في
        # المحادثات الطويلة (ثبت من ترانسكريبت حي) — أول الجواب يحمل جوهره.
        client_history = [
            {"user": t.user[:500], "assistant": t.assistant[:1200], "at": now}
            for t in body.history
        ]
    return ChatResponse(**bot.chat_as_principal(
        body.question, principal, allowed_collections=allowed,
        client_history=client_history,
    ))


@router.post(
    "/files",
    response_model=ChatResponse,
    summary="محادثة على كل الملفات المرفوعة",
    description="بحث هجين عبر جميع الملفات المرفوعة مدموجاً في ترتيب واحد.",
)
def chat_all_files(
    body: StudentChatRequest,
    principal: Principal = Depends(rate_limited_principal),
    bot: IUGChatbot = Depends(get_bot),
) -> ChatResponse:
    if not isinstance(bot, IUGChatbot):
        return ChatResponse(**bot.chat_with_all_files(body.question, principal.subject))
    available = {item["collection"] for item in bot.get_uploaded_files_list()}
    allowed = file_catalog.allowed_collections(principal, available)
    return ChatResponse(**bot.chat_as_principal(
        body.question, principal, allowed_collections=allowed
    ))


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
    principal: Principal = Depends(rate_limited_principal),
    bot: IUGChatbot = Depends(get_bot),
) -> ChatResponse:
    # رسالة واحدة للحالتين (غير موجود / خارج الصلاحية) — اختلافها كان يكشف
    # وجود ملفات خاصة لمن لا يملك حق رؤيتها.
    files = {f["collection"] for f in bot.get_uploaded_files_list()}
    if collection_name not in files:
        raise NotFoundError(f"الملف '{collection_name}' غير موجود.")
    if isinstance(bot, IUGChatbot):
        allowed = file_catalog.allowed_collections(principal, files)
        if collection_name not in allowed:
            raise NotFoundError(f"الملف '{collection_name}' غير موجود.")
    return ChatResponse(**bot.chat_with_file(body.question, collection_name, principal.subject))
