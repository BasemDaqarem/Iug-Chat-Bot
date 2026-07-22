"""
Pydantic request/response contracts for every endpoint.

These models ARE the API contract: routes never accept or return raw dicts,
so the OpenAPI docs (/docs) are always accurate and the frontend can generate
typed clients directly from them.
"""

from typing import Any, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ═════════════════════════════════════════════════════════════════════════
#  Authentication
# ═════════════════════════════════════════════════════════════════════════


class LoginRequest(BaseModel):
    student_id: Optional[str] = Field(
        default=None,
        pattern=r"^\d{3,20}$",
        description="الرقم الجامعي (أرقام فقط).",
        examples=["12345"],
    )
    identifier: Optional[str] = Field(
        default=None,
        pattern=r"^[A-Za-z0-9._-]{3,40}$",
        description="رقم المستخدم للموظف أو الأدمن.",
        examples=["EMP-1001"],
    )
    password: str = Field(
        ..., min_length=4, max_length=128, description="كلمة المرور."
    )

    @model_validator(mode="after")
    def require_identifier(self):
        if not (self.identifier or self.student_id):
            raise ValueError("أدخل الرقم الجامعي أو رقم المستخدم.")
        return self


class RegisterRequest(LoginRequest):
    identifier: None = None
    student_id: str = Field(..., pattern=r"^\d{3,20}$")
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
    department: Optional[str] = None
    job_title: Optional[str] = None
    salary: Optional[float] = None
    data_source: Optional[str] = None
    updated_at: Optional[str] = None


class AuthResponse(BaseModel):
    success: bool = True
    student_id: str
    user_id: Optional[str] = None
    role: Literal["student", "employee", "admin"] = "student"
    must_change_password: bool = False
    profile: StudentProfile
    access_token: str = Field(description="توكن الجلسة (JWT) — يُرسل في ترويسة Authorization.")
    token_type: str = "bearer"


class GuestTurn(BaseModel):
    """دور محادثة يحمله متصفح الزائر — الزوار بلا جلسات مخزّنة على الخادم
    (قرار أمني: معرّف الزائر يُستخدم مرة واحدة)، فيرسل العميل آخر أدواره مع
    السؤال ليفهم البوت المتابعات («هل ممكن انقبل بالتمريض؟» بعد ذكر معدله)."""

    user: str = Field(..., min_length=1, max_length=2000)
    assistant: str = Field(..., min_length=1, max_length=6000)


class StudentChatRequest(BaseModel):
    """Chat as the authenticated student — identity comes from the JWT, NOT the
    body, so there is no session_id to spoof."""

    question: str = Field(..., min_length=1, max_length=2000, examples=["ما هي حالتي الأكاديمية؟"])
    # للزائر فقط (مسار /chat/guest): سياق محادثته من متصفحه — لا يُخزَّن أبداً.
    # للمسارات الموثّقة يُتجاهَل (سجلهم على الخادم أوثق من أي مدخل عميل).
    history: Optional[List[GuestTurn]] = Field(default=None, max_length=5)


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
            "مصدر مسار الأدلة/التوليد: main_corpus_llm | uploaded_file_llm | "
            "uploaded_files_all_llm | structured_admission_llm | "
            "student_context_rag_llm | trusted_fact_llm | privacy_policy_llm"
        ),
    )
    trace_id: Optional[str] = Field(
        default=None,
        description="معرّف آمن لمقارنة سجل الاسترجاع عند الإبلاغ عن جواب.",
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
#  Admin / employee portal
# ═════════════════════════════════════════════════════════════════════════


class EmployeeCreateRequest(BaseModel):
    employee_id: str = Field(..., pattern=r"^[A-Za-z0-9._-]{3,40}$")
    temporary_password: str = Field(..., min_length=8, max_length=128)
    name: str = Field(..., min_length=2, max_length=100)
    department: str = Field(..., min_length=2, max_length=120)
    job_title: str = Field(..., min_length=2, max_length=120)
    salary: Optional[float] = Field(default=None, ge=0)
    access_groups: List[str] = Field(default_factory=list, max_length=50)


class EmployeeUpdateRequest(BaseModel):
    active: Optional[bool] = None
    name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    department: Optional[str] = Field(default=None, min_length=2, max_length=120)
    job_title: Optional[str] = Field(default=None, min_length=2, max_length=120)
    salary: Optional[float] = Field(default=None, ge=0)
    access_groups: Optional[List[str]] = None
    temporary_password: Optional[str] = Field(default=None, min_length=8, max_length=128)
    end_sessions: bool = False


class ManagedFileCreateRequest(BaseModel):
    collection: str = Field(..., pattern=r"^[^.$/\\]{2,100}$")
    documents: Union[List[dict], dict]
    classification: Literal[
        "university_public", "student_records", "employee_internal",
        "employee_private", "admin_only",
    ] = "university_public"
    allowed_roles: List[Literal["guest", "student", "employee", "admin"]] = Field(
        default_factory=lambda: ["guest", "student", "employee", "admin"]
    )
    owner_id: Optional[str] = Field(default=None, max_length=40)


class FilePreflightRequest(BaseModel):
    collection: str = Field(..., pattern=r"^[^.$/\\]{2,100}$")
    documents: Union[List[dict], dict]


class FileConflictResolutionRequest(BaseModel):
    decision: Literal["keep_existing", "prefer_incoming"]
    conflict_ids: List[str] = Field(default_factory=list, max_length=500)


class FileAccessUpdateRequest(BaseModel):
    classification: Literal[
        "university_public", "student_records", "employee_internal",
        "employee_private", "admin_only",
    ]
    allowed_roles: List[Literal["guest", "student", "employee", "admin"]]
    owner_id: Optional[str] = Field(default=None, max_length=40)


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
    status: Literal["starting", "ready", "failed"] = Field(
        description="حالة تهيئة فهارس RAG الفعلية."
    )
    index_ready: bool = Field(
        description="لا تقبل مسارات المحادثة طلباً قبل أن تصبح true."
    )
    environment: str = Field(description="development | testing | production")
    collections: int = Field(description="عدد collections قاعدة المعرفة المحمّلة.")
    document_count: int = Field(description="عدد الوثائق الأساسية المحمّلة.")
    knowledge_chunks: int = Field(description="عدد مقاطع قاعدة المعرفة المفهرسة.")
    uploaded_files: int = Field(description="عدد الملفات المرفوعة المفهرسة.")
    uploaded_chunks: int = Field(description="إجمالي مقاطع الملفات المرفوعة.")
    index_version: str = Field(
        description="بصمة المحتوى والنموذج ونسخة خط أنابيب RAG."
    )
    failed_sources: List[str] = Field(
        default_factory=list,
        description="بصمات مصادر فشلت تهيئتها؛ الأسماء الخام تبقى في سجل الخادم.",
    )
    failed_refresh_sources: List[str] = Field(
        default_factory=list,
        description="بصمات تحديثات فاشلة مع بقاء النسخة السابقة فعالة.",
    )
    initialization_error: Optional[str] = None
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
    public_answers: CacheStat = Field(
        description="حقل توافق قديم؛ يبقى صفر لأن الإجابات النهائية لا تُكاش."
    )
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
