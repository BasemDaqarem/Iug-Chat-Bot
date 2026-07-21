"""Uploaded-files management — list / upload / reload / delete."""

from fastapi import APIRouter, Depends

from app import file_catalog
from app.api.deps import get_bot, get_current_principal, require_admin
from app.api.errors import BadRequestError, NotFoundError, UpstreamError
from app.api.schemas import (
    ErrorResponse,
    FileInfo,
    FilesListResponse,
    MessageResponse,
    UploadRequest,
    UploadResponse,
)
from app.chatbot import IUGChatbot

# Reads require a valid student token; corpus MUTATIONS require the admin key —
# so no one can anonymously wipe or poison the shared knowledge base.
router = APIRouter(
    prefix="/files",
    tags=["Uploaded Files"],
    responses={
        401: {"model": ErrorResponse, "description": "توكن مفقود أو منتهٍ"},
        403: {"model": ErrorResponse, "description": "صلاحية إدارية مطلوبة"},
    },
)


def _file_names(bot: IUGChatbot) -> set:
    return {f["collection"] for f in bot.get_uploaded_files_list()}


@router.get(
    "",
    response_model=FilesListResponse,
    summary="قائمة الملفات المرفوعة",
    description="كل ملف مع عدد مقاطعه وهل فهرس البحث الدلالي جاهز له.",
    dependencies=[Depends(get_current_principal)],
)
def list_files(
    principal=Depends(get_current_principal),
    bot: IUGChatbot = Depends(get_bot),
) -> FilesListResponse:
    runtime = bot.get_uploaded_files_list()
    if isinstance(bot, IUGChatbot):
        allowed = file_catalog.allowed_collections(
            principal, {item["collection"] for item in runtime}
        )
        runtime = [item for item in runtime if item["collection"] in allowed]
    files = [FileInfo(**f) for f in runtime]
    return FilesListResponse(files=files, count=len(files))


@router.put(
    "/{collection_name}",
    response_model=UploadResponse,
    summary="رفع/استبدال ملف JSON",
    description=(
        "يخزّن المحتوى تحت الاسم المحدد (يستبدل أي محتوى سابق بنفس الاسم) "
        "ثم يبني مقاطعه وفهرس بحثه فوراً. PUT لأن العملية idempotent: "
        "نفس الاسم + نفس المحتوى = نفس النتيجة."
    ),
    responses={400: {"model": ErrorResponse, "description": "محتوى غير صالح"}},
    dependencies=[Depends(require_admin)],
)
def upload_file(
    collection_name: str,
    body: UploadRequest,
    bot: IUGChatbot = Depends(get_bot),
) -> UploadResponse:
    # نفس قيد الاسم في المسار الإداري الجديد — الاسم يصير مجموعة Mongo
    # وملف فهرس على القرص، فلا يُمرَّر حراً من الـ path.
    import re
    if not re.fullmatch(r"[^.$/\\]{2,100}", collection_name):
        raise BadRequestError("اسم الملف غير صالح: 2-100 حرفاً وبدون . أو $ أو / أو \\")
    try:
        result = bot.upload_json_file(collection_name, body.documents)
    except ValueError as exc:
        raise BadRequestError(str(exc))
    except RuntimeError as exc:
        raise UpstreamError(str(exc))
    indexed = any(
        f["collection"] == collection_name and f["indexed"]
        for f in bot.get_uploaded_files_list()
    )
    return UploadResponse(**result, indexed=indexed)


@router.post(
    "/{collection_name}/reload",
    response_model=MessageResponse,
    summary="إعادة فهرسة ملف",
    description="يعيد بناء مقاطع الملف وفهرسه من محتواه المخزّن في Mongo.",
    responses={404: {"model": ErrorResponse, "description": "الملف غير موجود"}},
    dependencies=[Depends(require_admin)],
)
def reload_file(collection_name: str, bot: IUGChatbot = Depends(get_bot)) -> MessageResponse:
    if collection_name not in _file_names(bot):
        raise NotFoundError(f"الملف '{collection_name}' غير موجود.")
    ok = bot.reload_uploaded_file(collection_name)
    if not ok:
        raise UpstreamError(f"فشلت إعادة فهرسة '{collection_name}' — تعذّر بناء الفهرس.")
    return MessageResponse(message=f"تمت إعادة فهرسة '{collection_name}'.")


@router.delete(
    "/{collection_name}",
    response_model=MessageResponse,
    summary="حذف ملف مرفوع",
    description="يحذف الملف من Mongo ومن فهارس البحث في الذاكرة.",
    responses={404: {"model": ErrorResponse, "description": "الملف غير موجود"}},
    dependencies=[Depends(require_admin)],
)
def delete_file(collection_name: str, bot: IUGChatbot = Depends(get_bot)) -> MessageResponse:
    if collection_name not in _file_names(bot):
        raise NotFoundError(f"الملف '{collection_name}' غير موجود.")
    bot.delete_uploaded_file(collection_name)
    return MessageResponse(message=f"تم حذف '{collection_name}'.")
