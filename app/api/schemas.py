"""
Pydantic request/response contracts for every endpoint.

These models ARE the API contract: routes never accept or return raw dicts,
so the OpenAPI docs (/docs) are always accurate and the frontend can generate
typed clients directly from them.
"""

from typing import Any, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# ═════════════════════════════════════════════════════════════════════════
#  Authentication
# ═════════════════════════════════════════════════════════════════════════


class LoginRequest(BaseModel):
    student_id: str = Field(
        ...,
        pattern=r"^\d{3,20}$",
        description="الرقم الجامعي (أرقام فقط).",
        examples=["12345"],
    )
    password: str = Field(
        ..., min_length=4, max_length=128, description="كلمة المرور."
    )


class RegisterRequest(LoginRequest):
    name: str = Field(..., min_length=2, max_length=80, description="اسم الطالب.")
    major: str = Field(..., min_length=2, max_length=120, description="تخصص الطالب (بيانات تجريبية).")
    gpa: float = Field(..., ge=0, le=100, description="المعدل التراكمي من 100 (بيانات تجريبية).")
    rank: int = Field(..., ge=1, le=1_000_000, description="الترتيب على الدفعة (بيانات تجريبية).")
    academic_status: Literal[
        "regular", "excellent", "good", "at_risk", "probation", "graduated"
    ] = Field(..., description="الحالة الأكاديمية التي يحددها الطالب لأغراض العرض.")


class StudentProfile(BaseModel):
    # Returned ONLY to the authenticated owner — this is the student's own data.
    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    major: Optional[str] = None
    gpa: Optional[float] = None
    rank: Optional[int] = None
    academic_status: Optional[str] = None
    data_source: Optional[str] = None
    updated_at: Optional[str] = None


class AuthResponse(BaseModel):
    success: bool = True
    student_id: str
    profile: StudentProfile
    access_token: str = Field(description="توكن الجلسة (JWT) — يُرسل في ترويسة Authorization.")
    token_type: str = "bearer"


class StudentChatRequest(BaseModel):
    """Chat as the authenticated student — identity comes from the JWT, NOT the
    body, so there is no session_id to spoof."""

    question: str = Field(..., min_length=1, max_length=2000, examples=["ما هي حالتي الأكاديمية؟"])


# ═════════════════════════════════════════════════════════════════════════
#  Chat
# ═════════════════════════════════════════════════════════════════════════


class ChatRequest(BaseModel):
    """A single chat turn from one session."""

    question: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="سؤال الطالب (نص عربي حر).",
        examples=["ما هي رسوم كلية الهندسة؟"],
    )
    session_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="معرّف الجلسة — يحدد سجل المحادثة وبيانات الطالب المسموح بها.",
        examples=["12345"],
    )


class ChatResponse(BaseModel):
    """The assistant's answer plus the retrieval context that produced it."""

    answer: str = Field(description="إجابة المساعد بالعربية.")
    top_chunks: List[str] = Field(
        default_factory=list,
        description="مقاطع السياق المسترجعة التي بُنيت عليها الإجابة (للشفافية/التصحيح).",
    )
    source: str = Field(
        default="knowledge_base",
        description=(
            "مصدر الإجابة: knowledge_base | uploaded_file | uploaded_files_all | "
            "structured_admission | student_context_rag | trusted_fact"
        ),
    )


# ═════════════════════════════════════════════════════════════════════════
#  Uploaded files
# ═════════════════════════════════════════════════════════════════════════


class FileInfo(BaseModel):
    """Index status of one uploaded file."""

    collection: str = Field(description="اسم الملف (اسم الـ collection في Mongo).")
    chunks_count: int = Field(description="عدد المقاطع المبنية من الملف.")
    indexed: bool = Field(description="هل له فهرس embeddings صالح للبحث الدلالي.")


class FilesListResponse(BaseModel):
    files: List[FileInfo]
    count: int = Field(description="عدد الملفات المرفوعة حالياً.")


class UploadRequest(BaseModel):
    """JSON content to (re)upload under a collection name."""

    documents: Union[List[dict], dict] = Field(
        description="محتوى الملف: JSON object واحد أو قائمة objects.",
        examples=[[{"القسم": "علوم الحاسوب", "الرسوم": 25}]],
    )


class UploadResponse(BaseModel):
    inserted: int = Field(description="عدد الوثائق المخزّنة.")
    collection: str = Field(description="اسم الملف الذي خُزّن تحته.")
    indexed: bool = Field(description="هل بُني فهرس البحث بنجاح بعد الرفع.")


# ═════════════════════════════════════════════════════════════════════════
#  Sessions / history
# ═════════════════════════════════════════════════════════════════════════


class HistoryTurn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user: str
    assistant: str


class HistoryResponse(BaseModel):
    session_id: str
    turns: List[HistoryTurn]
    count: int = Field(description="عدد أدوار المحادثة المحفوظة لهذه الجلسة.")


# ═════════════════════════════════════════════════════════════════════════
#  Health / status / generic
# ═════════════════════════════════════════════════════════════════════════


class HealthResponse(BaseModel):
    status: str = Field(description='"ok" عندما تكون الخدمة جاهزة للإجابة.')
    environment: str = Field(description="development | testing | production")
    collections: int = Field(description="عدد collections قاعدة المعرفة المحمّلة.")
    knowledge_chunks: int = Field(description="عدد مقاطع قاعدة المعرفة المفهرسة.")
    uploaded_files: int = Field(description="عدد الملفات المرفوعة المفهرسة.")
    model: str = Field(description="نموذج الـ LLM المستخدم للإجابات.")
    embed_model: str = Field(description="نموذج الـ embeddings المستخدم للبحث.")


class CacheStat(BaseModel):
    """Runtime stats for one cache (see app.cache.TTLCache.stats)."""

    name: str
    size: int = Field(description="عدد المدخلات الحالية.")
    maxsize: int
    ttl_seconds: int
    hits: int
    misses: int
    hit_rate: float = Field(description="hits / (hits + misses).")
    evictions: int = Field(description="مدخلات أُزيلت لتجاوز الحجم (LRU).")
    expirations: int = Field(description="مدخلات انتهت صلاحيتها (TTL).")


class CacheStatsResponse(BaseModel):
    public_answers: CacheStat = Field(description="كاش الإجابات العامة (غير الخاصة بطالب).")
    query_embeddings: CacheStat = Field(description="كاش متجهات الأسئلة (embeddings).")


class MessageResponse(BaseModel):
    """Generic acknowledgement for delete/clear style operations."""

    message: str


class ErrorDetail(BaseModel):
    code: str = Field(description="رمز آلي ثابت للخطأ، مثل NOT_FOUND / VALIDATION_ERROR.")
    message: str = Field(description="رسالة عربية واضحة للمستخدم.")
    details: Optional[Any] = Field(
        default=None,
        description="تفاصيل إضافية: نص، أو قائمة أخطاء حقول [{field, message}] للتحقّق.",
    )
    timestamp: str = Field(description="وقت الخطأ (ISO 8601, UTC).")
    path: str = Field(description="مسار الطلب الذي أنتج الخطأ.")


class FieldError(BaseModel):
    """One field-level validation error (shape used inside details)."""

    field: str
    message: str


class ErrorResponse(BaseModel):
    """Unified error envelope returned by EVERY failing endpoint."""

    success: bool = Field(default=False, description="دائماً false في الأخطاء.")
    error: ErrorDetail
