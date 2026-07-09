"""
Pydantic request/response contracts for every endpoint.

These models ARE the API contract: routes never accept or return raw dicts,
so the OpenAPI docs (/docs) are always accurate and the frontend can generate
typed clients directly from them.
"""

from typing import List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

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
        description="مصدر الإجابة: knowledge_base | uploaded_file | uploaded_files_all",
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


class MessageResponse(BaseModel):
    """Generic acknowledgement for delete/clear style operations."""

    message: str


class ErrorResponse(BaseModel):
    """Uniform error body (also what HTTPException renders as)."""

    detail: str
