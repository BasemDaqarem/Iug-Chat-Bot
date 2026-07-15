"""Administrator control-plane endpoints (JWT role protected)."""

from fastapi import APIRouter, Depends, Query

from app import audit, auth, file_catalog, tokens
from app.api.deps import get_bot, require_admin_role
from app.api.errors import BadRequestError, ConflictError, NotFoundError, UpstreamError
from app.api.schemas import (
    EmployeeCreateRequest,
    EmployeeUpdateRequest,
    FileAccessUpdateRequest,
    ManagedFileCreateRequest,
)
from app.chatbot import IUGChatbot
from app.rbac import Principal


router = APIRouter(prefix="/admin", tags=["Admin"])


@router.get("/employees")
def employees(_principal: Principal = Depends(require_admin_role)) -> dict:
    items = auth.list_employees()
    return {"employees": items, "count": len(items)}


@router.post("/employees", status_code=201)
def create_employee(
    body: EmployeeCreateRequest,
    principal: Principal = Depends(require_admin_role),
) -> dict:
    if auth.find_account(body.employee_id) is not None:
        raise ConflictError("رقم الموظف مستخدم مسبقاً.")
    item = auth.create_employee(**body.model_dump())
    audit.record(principal.subject, principal.role.value, "employee.create", body.employee_id)
    return item


@router.patch("/employees/{employee_id}")
def patch_employee(
    employee_id: str,
    body: EmployeeUpdateRequest,
    principal: Principal = Depends(require_admin_role),
) -> dict:
    changes = body.model_dump(exclude_none=True)
    item = auth.update_employee(employee_id, changes)
    if item is None:
        raise NotFoundError("حساب الموظف غير موجود.")
    account = auth.find_account(employee_id) or {}
    if changes.get("end_sessions") or changes.get("temporary_password") or changes.get("active") is False:
        tokens.revoke_subject(employee_id, int(account.get("token_version", 1)))
    audit.record(
        principal.subject,
        principal.role.value,
        "employee.update",
        employee_id,
        {"fields": sorted(key for key in changes if key != "temporary_password")},
    )
    return item


@router.get("/files")
def files(
    principal: Principal = Depends(require_admin_role),
    bot: IUGChatbot = Depends(get_bot),
) -> dict:
    items = file_catalog.list_files(bot.get_uploaded_files_list())
    return {"files": items, "count": len(items)}


@router.post("/files", status_code=201)
def create_file(
    body: ManagedFileCreateRequest,
    principal: Principal = Depends(require_admin_role),
) -> dict:
    try:
        item = file_catalog.create_draft(
            body.collection,
            body.documents,
            body.classification,
            body.allowed_roles,
            principal.subject,
            owner_id=body.owner_id,
        )
    except ValueError as exc:
        raise BadRequestError(str(exc))
    audit.record(principal.subject, principal.role.value, "file.draft", item.get("file_id", ""))
    return item


def _resolve_managed(file_id: str, actor_id: str) -> dict | None:
    """Managed catalog entry for the id — adopting pre-catalog files on the
    fly (ids like «legacy:<collection>») so the admin can manage them too."""
    if file_id.startswith("legacy:"):
        return file_catalog.adopt_legacy(file_id.split(":", 1)[1], actor_id)
    return file_catalog.get_file(file_id)


@router.post("/files/adopt-all")
def adopt_all_files(
    principal: Principal = Depends(require_admin_role),
    bot: IUGChatbot = Depends(get_bot),
) -> dict:
    """تسوية جماعية: كل ملف قديم بلا سجل صلاحيات يدخل السجل (منشور/جامعة عام)
    بضغطة واحدة — بعدها يضيّق الأدمن أدوار كل ملف، ويُطفأ متغير
    LEGACY_UNCATALOGUED_FILES_PUBLIC في الإنتاج بلا اختفاء أي ملف."""
    names = {f["collection"] for f in bot.get_uploaded_files_list()}
    adopted = file_catalog.adopt_all(names, principal.subject)
    audit.record(principal.subject, principal.role.value, "file.adopt_all", "*",
                 {"count": len(adopted)})
    return {"success": True, "count": len(adopted), "adopted": adopted,
            "message": (f"استُورد {len(adopted)} ملفاً قديماً إلى سجل الصلاحيات."
                        if adopted else "كل الملفات مسجّلة أصلاً — لا شيء لاستيراده.")}


@router.patch("/files/{file_id}/access")
def patch_file_access(
    file_id: str,
    body: FileAccessUpdateRequest,
    principal: Principal = Depends(require_admin_role),
) -> dict:
    entry = _resolve_managed(file_id, principal.subject)
    if entry is None:
        raise NotFoundError("الملف غير موجود.")
    try:
        item = file_catalog.update_access(
            entry["file_id"], body.classification, body.allowed_roles,
            body.owner_id, principal.subject,
        )
    except ValueError as exc:
        raise BadRequestError(str(exc))
    if item is None:
        raise NotFoundError("الملف غير موجود.")
    audit.record(principal.subject, principal.role.value, "file.access", entry["file_id"])
    return item


@router.delete("/files/{file_id}")
def delete_file(
    file_id: str,
    principal: Principal = Depends(require_admin_role),
    bot: IUGChatbot = Depends(get_bot),
) -> dict:
    """Remove a file from retrieval entirely: drop its uploaded collection
    (content + index) and archive its catalog entry. Works for managed and
    pre-catalog (legacy:*) files alike."""
    entry = _resolve_managed(file_id, principal.subject)
    if entry is None:
        raise NotFoundError("الملف غير موجود.")
    bot.delete_uploaded_file(entry["collection"])       # المحتوى + الفهارس + المتجهات المخزّنة
    purged = file_catalog.purge_versions(entry["file_id"])  # نصوص كل النسخ المحفوظة
    file_catalog.archive(entry["file_id"], principal.subject)
    audit.record(
        principal.subject, principal.role.value, "file.delete",
        entry["file_id"], {"collection": entry["collection"], "versions_purged": purged},
    )
    return {"success": True,
            "message": f"تم حذف «{entry['collection']}» نهائياً: المحتوى والفهارس والمتجهات وكل النسخ المحفوظة."}


@router.post("/files/{file_id}/process")
def process_file(
    file_id: str,
    principal: Principal = Depends(require_admin_role),
) -> dict:
    try:
        item = file_catalog.process(file_id, principal.subject)
    except ValueError as exc:
        raise BadRequestError(str(exc))
    if item is None:
        raise NotFoundError("الملف غير موجود.")
    audit.record(principal.subject, principal.role.value, "file.process", file_id)
    return item


@router.post("/files/{file_id}/publish")
def publish_file(
    file_id: str,
    principal: Principal = Depends(require_admin_role),
    bot: IUGChatbot = Depends(get_bot),
) -> dict:
    try:
        item = file_catalog.publish(file_id, bot, principal.subject)
    except ValueError as exc:
        raise BadRequestError(str(exc))
    except RuntimeError as exc:
        raise UpstreamError(str(exc))
    if item is None:
        raise NotFoundError("الملف غير موجود.")
    audit.record(principal.subject, principal.role.value, "file.publish", file_id)
    return item


@router.post("/files/{file_id}/rollback/{version}")
def rollback_file(
    file_id: str,
    version: int,
    principal: Principal = Depends(require_admin_role),
    bot: IUGChatbot = Depends(get_bot),
) -> dict:
    try:
        item = file_catalog.publish(file_id, bot, principal.subject, version=version)
    except ValueError as exc:
        raise BadRequestError(str(exc))
    except RuntimeError as exc:
        raise UpstreamError(str(exc))
    if item is None:
        raise NotFoundError("الملف أو النسخة غير موجودة.")
    audit.record(
        principal.subject, principal.role.value, "file.rollback", file_id, {"version": version}
    )
    return item


@router.get("/audit")
def audit_log(
    limit: int = Query(default=100, ge=1, le=250),
    _principal: Principal = Depends(require_admin_role),
) -> dict:
    items = audit.recent(limit)
    return {"events": items, "count": len(items)}
