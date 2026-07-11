"""
Authentication endpoints — Student ID + password login & registration.

Thin delegates to app.auth; all failures use the unified error envelope
(401 invalid credentials, 409 duplicate id, 422 validation).
"""

from fastapi import APIRouter

from app import auth, tokens
from app.api.errors import ConflictError, UnauthorizedError
from app.api.schemas import AuthResponse, ErrorResponse, LoginRequest, RegisterRequest

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _to_response(account: dict) -> AuthResponse:
    student_id = str(account.get("student_id"))
    return AuthResponse(
        student_id=student_id,
        profile=account.get("profile") or {},
        access_token=tokens.create_access_token(student_id),
    )


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="تسجيل الدخول بالرقم الجامعي",
    description="يتحقّق من الرقم الجامعي وكلمة المرور (bcrypt) ويُرجع ملف الطالب.",
    responses={401: {"model": ErrorResponse, "description": "بيانات دخول غير صحيحة"}},
)
def login(body: LoginRequest) -> AuthResponse:
    account = auth.authenticate(body.student_id, body.password)
    if account is None:
        # One generic message — never reveal which field was wrong.
        raise UnauthorizedError("الرقم الجامعي أو كلمة المرور غير صحيحة.")
    return _to_response(account)


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=201,
    summary="إنشاء حساب طالب جديد",
    description="ينشئ حساباً جديداً بكلمة مرور مشفّرة (bcrypt).",
    responses={409: {"model": ErrorResponse, "description": "الرقم الجامعي مسجّل مسبقاً"}},
)
def register(body: RegisterRequest) -> AuthResponse:
    if auth.find_account(body.student_id) is not None:
        raise ConflictError("هذا الرقم الجامعي مسجّل مسبقاً — سجّل الدخول بدلاً من ذلك.")
    account = auth.create_account(
        body.student_id,
        body.password,
        body.name,
        body.major,
        body.gpa,
        body.rank,
        body.academic_status,
    )
    return _to_response(account)
