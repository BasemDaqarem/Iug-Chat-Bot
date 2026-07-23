"""
iug_kb_v2 — طبقة تحويل حتمية لملفات المعرفة القانونية (Canonical) المرفوعة.

الملف بفورمات `iug_kb_v2` يصل سجلاتٍ منظمةً (canonical records) لا نصاً خاماً.
هذه الوحدة تحوّله — بلا أي استدعاء LLM — إلى إسقاط استرجاع (Retrieval
Projection): لكل سجل قابل للفهرسة نصان متوازيان:

  • retrieval_text — محشو بالمرادفات والكلمات المفتاحية وأمثلة الأسئلة؛
    يُبنى منه الـEmbedding وفهرس BM25 (طُعم البحث).
  • evidence_text — دليل نظيف (عنوان/إجابة/نطاق/شروط/مصدر)؛ هو ما يصل
    نموذجَ الإجابة بعد الاسترجاع، بدل النص المحشو.

السجلات غير النشطة (draft/expired/superseded/suspended/archived/scheduled)
تُخزَّن مع ملفها في Mongo لكنها لا تدخل الفهرس إطلاقاً.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, List, Optional

SCHEMA_VERSION = "iug_kb_v2"

# مفاتيح يجب أن تكون **حاضرة** في كل سجل (ولو كانت قيمتها null —
# البيانات الحقيقية تحمل verified_at: null في مئات السجلات الموثوقة).
_REQUIRED_PRESENT = (
    "schema_version", "record_id", "canonical_id", "record_type", "domain",
    "title", "answer_text",
    ("retrieval", "contextual_text"),
    ("validity", "status"),
    ("validity", "verified_at"),
    ("governance", "version"),
    ("governance", "access_scope"),
)

# مفاتيح لا يقبل فيها null/فارغ.
_REQUIRED_NON_NULL = (
    "schema_version", "record_id", "canonical_id", "record_type", "title",
    "answer_text",
    ("validity", "status"),
)

_EXCLUDED_STATUSES = frozenset(
    {"draft", "expired", "superseded", "suspended", "archived", "scheduled"}
)

# درجات v2 → مفردات فلتر المرحلة في chatbot (query_rewrite levels)
_DEGREE_LEVEL_MAP = {
    "bachelor": "bachelor",
    "master": "masters",
    "masters": "masters",
    "doctorate": "phd",
    "phd": "phd",
}

_IGNORED_DOC_KEYS = ("_id", "__file_meta__")


def _clean(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in _IGNORED_DOC_KEYS}


def is_v2_payload(docs: list) -> bool:
    """فحص الفورمات من أول سجل — كما تنص مواصفة المسار الجديد."""
    if not docs:
        return False
    first = docs[0]
    return isinstance(first, dict) and first.get("schema_version") == SCHEMA_VERSION


def _path_label(key) -> str:
    return ".".join(key) if isinstance(key, tuple) else str(key)


def _lookup(record: dict, key):
    """(present, value) لمفتاح مباشر أو متداخل بمستوى واحد."""
    if isinstance(key, tuple):
        parent = record.get(key[0])
        if not isinstance(parent, dict) or key[1] not in parent:
            return False, None
        return True, parent[key[1]]
    if key not in record:
        return False, None
    return True, record[key]


def validate_records(docs: list) -> None:
    """يرفض الملف كاملاً برسالة واضحة (رقم السجل + الحقل) عند أول سجل فاسد."""
    for position, raw in enumerate(docs, start=1):
        if not isinstance(raw, dict):
            raise ValueError(
                f"ملف iug_kb_v2 مرفوض: السجل رقم {position} ليس JSON object."
            )
        record = _clean(raw)
        ident = record.get("record_id") or f"#{position}"
        for key in _REQUIRED_PRESENT:
            present, _ = _lookup(record, key)
            if not present:
                raise ValueError(
                    f"ملف iug_kb_v2 مرفوض: السجل {position} ({ident}) "
                    f"ينقصه الحقل الإلزامي «{_path_label(key)}»."
                )
        for key in _REQUIRED_NON_NULL:
            _, value = _lookup(record, key)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValueError(
                    f"ملف iug_kb_v2 مرفوض: السجل {position} ({ident}) "
                    f"حقله «{_path_label(key)}» فارغ وهو لا يقبل الفراغ."
                )
        if record.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"ملف iug_kb_v2 مرفوض: السجل {position} ({ident}) "
                f"يحمل schema_version مختلفاً «{record.get('schema_version')}»."
            )


def is_indexable(record: dict, today: Optional[date] = None) -> bool:
    """active + is_canonical + داخل نافذة السريان (إن حُددت)."""
    record = _clean(record)
    validity = record.get("validity") or {}
    status = str(validity.get("status") or "").strip().lower()
    if status != "active" or status in _EXCLUDED_STATUSES:
        return False
    governance = record.get("governance") or {}
    if governance.get("is_canonical") is False:
        return False
    today = today or date.today()
    today_iso = today.isoformat()
    effective_from = validity.get("effective_from")
    if effective_from and str(effective_from)[:10] > today_iso:
        return False
    effective_to = validity.get("effective_to")
    if effective_to and str(effective_to)[:10] < today_iso:
        return False
    return True


# ─── بناء النصوص ──────────────────────────────────────────────────────────

def _texts(value) -> List[str]:
    """قيم نصية غير فارغة من scalar أو list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if v is not None and str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _scope_line(scope: dict) -> str:
    parts = []
    for key, value in (scope or {}).items():
        vals = _texts(value)
        if vals:
            parts.append(f"{key}: " + "، ".join(vals))
    return "؛ ".join(parts)


def build_retrieval_text(record: dict, collection_name: str) -> str:
    """نص البحث: institution/title/domain/subdomain/scope + aliases +
    keywords + example_queries + contextual_text + answer_text + notes.
    بلا ambiguous_queries ولا clarification_question (مخصصة لاكتشاف الغموض
    لا للإجابة)، وبلا تكرار ولا حقول فارغة."""
    record = _clean(record)
    retrieval = record.get("retrieval") or {}
    parts: List[str] = [f"[ملف: {collection_name}]"]
    seen = set(parts)

    def add(text: str) -> None:
        text = (text or "").strip()
        if text and text not in seen:
            parts.append(text)
            seen.add(text)

    add(str(record.get("institution") or ""))
    add(str(record.get("title") or ""))
    add(str(record.get("domain") or ""))
    add(str(record.get("subdomain") or ""))
    add(_scope_line(record.get("scope") or {}))
    for source in (retrieval.get("aliases"), retrieval.get("keywords"),
                   retrieval.get("example_queries")):
        for text in _texts(source):
            add(text)
    add(str(retrieval.get("contextual_text") or ""))
    add(str(record.get("answer_text") or ""))
    for note in _texts(record.get("notes")):
        add(note)
    return "\n".join(parts)


def _source_line(record: dict) -> str:
    titles = []
    for source in record.get("sources") or []:
        if isinstance(source, dict):
            title = str(source.get("source_title") or "").strip()
            if title and title not in titles:
                titles.append(title)
    return "، ".join(titles)


def build_evidence_text(record: dict, collection_name: str) -> str:
    """دليل الإجابة النظيف: عنوان/إجابة/نطاق/شروط/ملاحظات/تواصل/مصدر/سريان.
    يبدأ بترويسة `[ملف: ...]` التي تتعرف عليها طبقات الاستبعاد/الحداثة/الخصوصية."""
    record = _clean(record)
    lines: List[str] = [f"[ملف: {collection_name}]"]
    title = str(record.get("title") or "").strip()
    if title:
        lines.append(title)
    answer = str(record.get("answer_text") or "").strip()
    if answer:
        lines.append(answer)
    scope_line = _scope_line(record.get("scope") or {})
    if scope_line:
        lines.append("النطاق: " + scope_line)
    conditions = _texts(record.get("conditions"))
    if conditions:
        lines.append("الشروط: " + "؛ ".join(conditions))
    notes = _texts(record.get("notes"))
    if notes:
        lines.append("ملاحظات: " + "؛ ".join(notes))
    contact = record.get("contact") or {}
    if isinstance(contact, dict) and any(_texts(v) for v in contact.values()):
        contact_parts = [
            f"{key}: {'، '.join(_texts(value))}"
            for key, value in contact.items() if _texts(value)
        ]
        lines.append("التواصل: " + "؛ ".join(contact_parts))
    source_line = _source_line(record)
    if source_line:
        lines.append("المصدر: " + source_line)
    validity = record.get("validity") or {}
    validity_parts = [f"الحالة: {validity.get('status')}"]
    if validity.get("verified_at"):
        validity_parts.append(f"آخر_تحقق: {validity['verified_at']}")
    lines.append("السريان: " + "؛ ".join(validity_parts))
    return "\n".join(lines)


def flat_metadata(record: dict) -> dict:
    """Metadata مسطحة — قيم scalar فقط، لا كائنات متداخلة."""
    record = _clean(record)
    scope = record.get("scope") or {}
    data = record.get("data") or {}
    validity = record.get("validity") or {}
    governance = record.get("governance") or {}
    student_status = scope.get("student_status")
    if isinstance(student_status, (list, tuple)):
        student_status = "، ".join(str(v) for v in student_status)
    raw_level = str(
        scope.get("degree_level") or data.get("degree_level") or ""
    ).strip().lower()
    return {
        "canonical_id": record.get("canonical_id"),
        "record_id": record.get("record_id"),
        "record_type": record.get("record_type"),
        "domain": record.get("domain"),
        "subdomain": record.get("subdomain"),
        "degree_level": _DEGREE_LEVEL_MAP.get(raw_level) or (raw_level or None),
        "faculty": scope.get("faculty") or data.get("faculty_name"),
        "program": scope.get("program") or data.get("program_name"),
        "campus": scope.get("campus"),
        "student_status": student_status,
        "status": validity.get("status"),
        "verified_at": validity.get("verified_at"),
        "effective_from": validity.get("effective_from"),
        "effective_to": validity.get("effective_to"),
        "access_scope": governance.get("access_scope"),
        "source_priority": governance.get("source_priority"),
        "version": governance.get("version"),
    }


@dataclass(slots=True)
class V2Chunk:
    """سجل استرجاع داخلي واحد (سجل canonical ذري = chunk واحد)."""

    id: str
    canonical_id: str
    record_id: str
    chunk_number: int
    retrieval_text: str
    evidence_text: str
    doc_index: int
    metadata: dict = field(default_factory=dict)


def build_projection(
    docs: list, collection_name: str, today: Optional[date] = None
) -> List[V2Chunk]:
    """التحويل الكامل: تحقّق ← فلترة الحالة ← نصا الاسترجاع والدليل ← metadata.

    doc_index يشير إلى موضع السجل في مصفوفة الوثائق **الفعلية** (كما تُخزّن
    في Mongo وتصل AdmissionCatalog)، لا في القائمة المفلترة — وعليه تعتمد
    خريطة chunk→doc في vector_for_fact بعد استبعاد سجلات draft.
    """
    validate_records(docs)
    projection: List[V2Chunk] = []
    for doc_index, raw in enumerate(docs):
        record = _clean(raw)
        if not is_indexable(record, today=today):
            continue
        meta = flat_metadata(record)
        version = meta.get("version") or 1
        chunk_number = 0
        projection.append(V2Chunk(
            id=f"{record['canonical_id']}#v{version}#chunk{chunk_number}",
            canonical_id=str(record["canonical_id"]),
            record_id=str(record["record_id"]),
            chunk_number=chunk_number,
            retrieval_text=build_retrieval_text(record, collection_name),
            evidence_text=build_evidence_text(record, collection_name),
            doc_index=doc_index,
            metadata=meta,
        ))
    return projection
