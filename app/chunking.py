"""
Document → chunk-text conversion, shared by the main knowledge base and by
uploaded files. Fully generic and STRUCTURAL (not name-based): no hardcoded
collection names — any collection whose documents follow these conventions
is handled automatically:

  • sensitive record → doc has a `privacy.allowed_users` list
  • owner id         → first of: student_id / id_student / user_id /
                       owner_id fields, else first allowed_users entry
  • display name     → first of: student_name / name / full_name / title
"""

from typing import List, Optional, Tuple

# Marker used to tag chunks built from documents that declare an
# access-control list (`privacy.allowed_users`).
SENSITIVE_MARKER = "SENSITIVE"


# ═════════════════════════════════════════════════════════════════════════
#  Structural document introspection
# ═════════════════════════════════════════════════════════════════════════

def is_sensitive_doc(doc: dict) -> bool:
    privacy = doc.get("privacy")
    return isinstance(privacy, dict) and bool(privacy.get("allowed_users"))


def extract_owner_id(doc: dict) -> Optional[str]:
    for key in ("student_id", "id_student", "user_id", "owner_id"):
        if doc.get(key) not in (None, ""):
            return str(doc[key])
    privacy = doc.get("privacy") if isinstance(doc.get("privacy"), dict) else None
    if privacy and privacy.get("allowed_users"):
        return str(privacy["allowed_users"][0])
    return None


def extract_display_name(doc: dict) -> Optional[str]:
    for key in ("student_name", "name", "full_name", "title"):
        if doc.get(key):
            return str(doc[key])
    return None


# ═════════════════════════════════════════════════════════════════════════
#  Chunk building
# ═════════════════════════════════════════════════════════════════════════

def flatten_json_to_text(obj, prefix: str = "") -> List[str]:
    """Recursively flatten any JSON-like structure into 'key: value' lines.

    Scalar list items are emitted under the parent key (not 'key[i]') —
    kept as-is so chunk texts stay byte-identical with the corpus the
    existing embeddings were built from.
    """
    lines = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, (dict, list)):
                lines.extend(flatten_json_to_text(value, full_key))
            else:
                lines.append(f"{full_key}: {value}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            full_key = f"{prefix}[{i}]"
            if isinstance(item, (dict, list)):
                lines.extend(flatten_json_to_text(item, full_key))
            else:
                lines.append(f"{prefix}: {item}")
    else:
        lines.append(f"{prefix}: {obj}")
    return lines


def doc_to_chunk_texts(
    collection_name: str,
    doc: dict,
    sensitive: bool,
    owner_id: Optional[str],
) -> List[str]:
    """
    Turn ONE document into one or more chunk texts.

    Generic rule, applied uniformly to every collection (no per-type logic):
    scalar fields become one "overview" chunk; any field that is a list of
    sub-objects is additionally split into one chunk per item (each carrying
    the parent's scalar fields as shared context). This keeps retrieval
    granularity comparable to hand-written chunking (e.g. one chunk per
    program/grant/faculty) without any bespoke code.
    """
    scalars, nested_lists = {}, {}
    for key, value in doc.items():
        if isinstance(value, list) and value and all(isinstance(i, dict) for i in value):
            nested_lists[key] = value
        else:
            scalars[key] = value

    header = (
        f"[{SENSITIVE_MARKER}|collection={collection_name}|owner={owner_id}]"
        if sensitive else f"[{collection_name}]"
    )

    parent_lines = flatten_json_to_text(scalars)
    parent_ctx = "\n".join(parent_lines)

    texts: List[str] = []
    if parent_lines or not nested_lists:
        texts.append(header + (f"\n{parent_ctx}" if parent_ctx else ""))

    for field_name, items in nested_lists.items():
        for item in items:
            item_lines = flatten_json_to_text(item)
            if not item_lines:
                continue
            piece = f"{header} :: {field_name}\n"
            if parent_ctx:
                piece += parent_ctx + "\n"
            piece += "\n".join(item_lines)
            texts.append(piece)

    return texts


def build_chunks(data: dict) -> Tuple[List[str], List[dict]]:
    """
    Build chunks for ALL collections generically. Returns (chunks, meta)
    where meta[i] describes chunks[i] (same order/length) — used by the
    privacy guard / academic-status shortcut without ever hardcoding a
    collection name.
    """
    chunks: List[str] = []
    meta: List[dict] = []

    for collection_name, docs in data.items():
        for doc in docs:
            doc_copy = dict(doc)
            doc_id = doc_copy.pop("_id", None)
            doc_copy.pop("seeded_at", None)

            sensitive = is_sensitive_doc(doc_copy)
            owner_id = extract_owner_id(doc_copy) if sensitive else None
            display_name = extract_display_name(doc_copy)

            for text in doc_to_chunk_texts(collection_name, doc_copy, sensitive, owner_id):
                chunks.append(text)
                meta.append({
                    "collection": collection_name,
                    "doc_id": doc_id,
                    "sensitive": sensitive,
                    "owner_id": owner_id,
                    "display_name": display_name,
                    "raw": doc_copy,
                })

    return chunks, meta


def build_uploaded_chunks_with_doc_indexes(
    docs: List[dict],
    collection_name: str,
) -> Tuple[List[str], List[int]]:
    """Build uploaded chunks and preserve each chunk's source document index."""
    chunks = []
    doc_indexes = []
    for doc_index, doc in enumerate(docs):
        doc = dict(doc)
        doc.pop("_id", None)
        doc.pop("__file_meta__", None)
        flat_lines = flatten_json_to_text(doc)
        if flat_lines:
            chunk_text = f"[ملف: {collection_name}]\n" + "\n".join(flat_lines)
            chunks.append(chunk_text)
            doc_indexes.append(doc_index)
    return chunks, doc_indexes


def build_uploaded_chunks(docs: List[dict], collection_name: str) -> List[str]:
    chunks, _doc_indexes = build_uploaded_chunks_with_doc_indexes(docs, collection_name)
    return chunks

# ═════════════════════════════════════════════════════════════════════════
#  Contextual / hierarchical uploaded-file chunks (pipeline v2)
# ═════════════════════════════════════════════════════════════════════════

from dataclasses import dataclass, field
import hashlib
from typing import Any


@dataclass(slots=True)
class ChunkRecord:
    """One searchable child with a stable parent relationship."""

    text: str
    chunk_id: str
    parent_id: str
    source: str
    doc_index: int
    kind: str
    parent_text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _stable_id(*parts: object) -> str:
    raw = "\x00".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _contextual_text(
    collection_name: str,
    *,
    chunk_id: str,
    parent_id: str,
    kind: str,
    parent_text: str,
    child_text: str,
) -> str:
    lines = [
        f"[ملف: {collection_name}]",
        f"[chunk_id: {chunk_id} | parent_id: {parent_id} | النوع: {kind}]",
    ]
    if parent_text:
        lines.append("سياق الوثيقة الأصلية:")
        lines.append(parent_text)
    if child_text and child_text != parent_text:
        lines.append("المعلومة المحددة:")
        lines.append(child_text)
    return "\n".join(lines)


def build_contextual_uploaded_chunk_records(
    docs: List[dict],
    collection_name: str,
) -> List[ChunkRecord]:
    """Build parent/child records without hard-coding a university domain.

    Small flat documents remain one overview record.  Lists of sub-objects are
    split into children, each carrying the parent's scalar context.  This keeps
    exact retrieval granularity while preserving enough document context for
    the LLM and reranker.
    """
    records: List[ChunkRecord] = []
    for doc_index, raw_doc in enumerate(docs):
        doc = dict(raw_doc)
        doc.pop("_id", None)
        doc.pop("__file_meta__", None)
        if not doc:
            continue

        parent_id = _stable_id(collection_name, doc_index, "parent")
        scalars: dict[str, Any] = {}
        child_groups: dict[str, list[dict]] = {}
        for key, value in doc.items():
            if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                child_groups[key] = value
            else:
                scalars[key] = value

        parent_text = "\n".join(flatten_json_to_text(scalars))
        if parent_text or not child_groups:
            chunk_id = _stable_id(collection_name, doc_index, "overview")
            child_text = parent_text or "\n".join(flatten_json_to_text(doc))
            records.append(ChunkRecord(
                text=_contextual_text(
                    collection_name,
                    chunk_id=chunk_id,
                    parent_id=parent_id,
                    kind="overview",
                    parent_text=parent_text,
                    child_text=child_text,
                ),
                chunk_id=chunk_id,
                parent_id=parent_id,
                source=collection_name,
                doc_index=doc_index,
                kind="overview",
                parent_text=parent_text,
                metadata={"field": None},
            ))

        for field_name, items in child_groups.items():
            for item_index, item in enumerate(items):
                item_text = "\n".join(flatten_json_to_text(item))
                if not item_text:
                    continue
                chunk_id = _stable_id(collection_name, doc_index, field_name, item_index)
                records.append(ChunkRecord(
                    text=_contextual_text(
                        collection_name,
                        chunk_id=chunk_id,
                        parent_id=parent_id,
                        kind=f"child:{field_name}",
                        parent_text=parent_text,
                        child_text=item_text,
                    ),
                    chunk_id=chunk_id,
                    parent_id=parent_id,
                    source=collection_name,
                    doc_index=doc_index,
                    kind=f"child:{field_name}",
                    parent_text=parent_text,
                    metadata={"field": field_name, "item_index": item_index},
                ))
    return records
