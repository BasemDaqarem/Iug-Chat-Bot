"""Employee portal read APIs with explicit field projections."""

from fastapi import APIRouter, Depends, Query

from app import auth
from app.api.deps import require_employee_or_admin
from app.api.errors import NotFoundError
from app.rbac import Principal


router = APIRouter(prefix="/portal", tags=["Employee Portal"])


@router.get("/me")
def my_profile(principal: Principal = Depends(require_employee_or_admin)) -> dict:
    account = auth.find_account(principal.subject)
    if account is None:
        raise NotFoundError("الحساب غير موجود.")
    return auth.public_account(account)


@router.get("/students")
def students(
    query: str = Query(default="", max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    _principal: Principal = Depends(require_employee_or_admin),
) -> dict:
    items = auth.list_students(query, limit)
    return {"students": items, "count": len(items)}
