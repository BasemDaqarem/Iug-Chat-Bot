"""Uploaded-files management — list / upload / reload / delete."""

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_bot
from app.api.schemas import (
    ErrorResponse,
    FileInfo,
    FilesListResponse,
    MessageResponse,
    UploadRequest,
    UploadResponse,
)
from app.chatbot import IUGChatbot

router = APIRouter(prefix="/files", tags=["Uploaded Files"])


def _file_names(bot: IUGChatbot) -> set:
    return {f["collection"] for f in bot.get_uploaded_files_list()}


@router.get(
    "",
    response_model=FilesListResponse,
    summary="قائمة الملفات المرفوعة",
    description="كل ملف مع عدد مقاطعه وهل فهرس البحث الدلالي جاهز له.",
)
def list_files(bot: IUGChatbot = Depends(get_bot)) -> FilesListResponse:
    files = [FileInfo(**f) for f in bot.get_uploaded_files_list()]
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
)
def upload_file(
    collection_name: str,
    body: UploadRequest,
    bot: IUGChatbot = Depends(get_bot),
) -> UploadResponse:
    try:
        result = bot.upload_json_file(collection_name, body.documents)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
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
)
def reload_file(collection_name: str, bot: IUGChatbot = Depends(get_bot)) -> MessageResponse:
    if collection_name not in _file_names(bot):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"الملف '{collection_name}' غير موجود.",
        )
    ok = bot.reload_uploaded_file(collection_name)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"فشلت إعادة فهرسة '{collection_name}'.",
        )
    return MessageResponse(message=f"تمت إعادة فهرسة '{collection_name}'.")


@router.delete(
    "/{collection_name}",
    response_model=MessageResponse,
    summary="حذف ملف مرفوع",
    description="يحذف الملف من Mongo ومن فهارس البحث في الذاكرة.",
    responses={404: {"model": ErrorResponse, "description": "الملف غير موجود"}},
)
def delete_file(collection_name: str, bot: IUGChatbot = Depends(get_bot)) -> MessageResponse:
    if collection_name not in _file_names(bot):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"الملف '{collection_name}' غير موجود.",
        )
    bot.delete_uploaded_file(collection_name)
    return MessageResponse(message=f"تم حذف '{collection_name}'.")
