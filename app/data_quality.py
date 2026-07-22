"""Canonical fact fingerprints for retrieval and managed-file preflight.

The module is intentionally storage-agnostic.  It does not rewrite Mongo and
does not answer users.  It normalizes structured facts so equivalent values
(``5`` and ``5.0``) count once, while different values for the same
entity/attribute/scope become an explicit conflict.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from app.text_norm import normalize_arabic


_SOURCE_RE = re.compile(r"^\[ملف:\s*(.+?)\]", re.MULTILINE)
_KEY_VALUE_RE = re.compile(r"^\s*[-*]?\s*([^:\n]{1,100})\s*[:：]\s*(.+?)\s*$")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)]+)\)", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>{}\[\]\"']+", re.IGNORECASE)
_ARABIC_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)

_ENTITY_KEYS = (
    "program_name", "program", "specialization", "major", "department",
    "faculty", "faculty_name", "college", "degree_or_request", "resource_name", "full_name",
    "course", "course_name", "course_code", "subject", "id", "title",
    "topic", "case", "service_type", "category", "document_type",
    "اسم البرنامج", "البرنامج", "التخصص", "القسم", "الكليه", "الكلية",
    "الاسم", "المورد", "المساق", "اسم المساق", "رمز المساق", "الماده", "المادة",
    "الموضوع", "العنوان", "الحاله", "الحالة", "التصنيف", "الفئه", "الفئة",
)
_SCOPE_KEYS = (
    "branch", "degree", "level", "category", "currency", "semester",
    "الفرع", "المرحله", "المرحلة", "الفئه", "الفئة", "العمله", "العملة",
)
_ADDITIVE_LIST_KEYS = {
    "keyword", "keywords", "tag", "tags", "intent", "intents",
    "rule", "rules", "alias", "aliases", "synonym", "synonyms",
    "question_forms", "embedding_texts", "صيغ_السوال", "صيغ_السؤال",
    "كلمات_مفتاحيه", "كلمات_مفتاحية", "حقايق", "حقائق",
}


def _canonical_field(path: str) -> str:
    value = normalize_arabic(path).lower().replace(" ", "_")
    if "discount_percentage" in value or "نسبه_المنحه" in value:
        return "scholarship_rate"
    if "retention_gpa_required" in value or "معدل_الاستمرار" in value:
        return "scholarship_retention"
    if any(mark in value for mark in (
        "min_high_school", "admission_cutoff", "مفتاح_القبول",
        "معدل_القبول", "الحد_الادنى",
    )):
        return "admission_cutoff"
    if any(mark in value for mark in (
        "credit_hour_fee", "hour_fee", "tuition", "amount", "fee", "رسوم", "سعر_الساعه",
        "سعر_الساعة", "ثوابت",
    )):
        return "fee"
    if any(mark in value for mark in ("branch", "الفرع", "فروع")):
        return "branch"
    if any(mark in value for mark in ("url", "link", "رابط", "بوابه", "بوابة")):
        return "link"
    if any(mark in value for mark in ("email", "phone", "بريد", "هاتف")):
        return "contact"
    if any(mark in value for mark in (
        "last_verified", "verified_at", "deadline", "date", "تاريخ", "موعد",
    )):
        return "date"
    if any(mark in value for mark in ("document", "وثائق", "اوراق", "أوراق")):
        return "documents"
    if any(mark in value for mark in ("requirement", "شرط", "متطلبات")):
        return "requirements"
    return value or "general"


def normalize_fact_value(value: Any) -> str:
    """Normalize values without collapsing meaningfully different strings."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            decimal = Decimal(str(value))
            normalized = format(decimal.normalize(), "f")
            return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized
        except InvalidOperation:
            pass
    text = str(value).strip()
    markdown = _MARKDOWN_LINK_RE.fullmatch(text)
    if markdown:
        text = markdown.group(1)
    translated = text.translate(_ARABIC_DIGITS).replace(",", ".")
    numeric = translated.rstrip("% ")
    try:
        if numeric and re.fullmatch(r"[-+]?\d+(?:\.\d+)?", numeric):
            decimal = Decimal(numeric)
            normalized = format(decimal.normalize(), "f")
            normalized = normalized.rstrip("0").rstrip(".") if "." in normalized else normalized
            return normalized + ("%" if translated.rstrip().endswith("%") else "")
    except InvalidOperation:
        pass
    if _URL_RE.fullmatch(text.strip("<>.,،؛;")):
        return text.strip("<>.,،؛;").rstrip("/").lower()
    return normalize_arabic(re.sub(r"\s+", " ", text)).lower()


def _stable_hash(*parts: Any) -> str:
    raw = "\x00".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _recency_key(value: str) -> tuple[int, int, int, int, int, int, int] | None:
    """Return a comparable date key, or None when recency is not knowable."""
    raw = str(value or "").strip().translate(_ARABIC_DIGITS)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return (
            parsed.year, parsed.month, parsed.day, parsed.hour,
            parsed.minute, parsed.second, parsed.microsecond,
        )
    except ValueError:
        match = re.fullmatch(
            r"((?:19|20)\d{2})(?:[-/]([01]?\d)(?:[-/]([0-3]?\d))?)?",
            raw,
        )
        if not match:
            return None
        try:
            parsed = datetime(
                int(match.group(1)), int(match.group(2) or 1),
                int(match.group(3) or 1),
            )
        except ValueError:
            return None
        return (parsed.year, parsed.month, parsed.day, 0, 0, 0, 0)


@dataclass(slots=True)
class EvidenceItem:
    source: str
    chunk_id: str | None
    doc_index: int | None
    entity: str
    canonical_field: str
    attribute: str
    value: str
    scope: dict[str, str] = field(default_factory=dict)
    verified_at: str | None = None
    fact_key: str = ""
    fact_fingerprint: str = ""

    def as_metadata(self) -> dict[str, Any]:
        return asdict(self)


def _normalized_mapping(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {normalize_arabic(str(key)).lower(): value for key, value in doc.items()}


def _identity_and_scope(doc: Mapping[str, Any]) -> tuple[str, dict[str, str], str | None]:
    normalized = _normalized_mapping(doc)
    entities = []
    for marker in _ENTITY_KEYS:
        value = normalized.get(normalize_arabic(marker).lower())
        if value not in (None, "", [], {}):
            entities.append(normalize_fact_value(value))
    entity = " | ".join(dict.fromkeys(entities)) or "global"
    scope: dict[str, str] = {}
    for marker in _SCOPE_KEYS:
        normalized_marker = normalize_arabic(marker).lower()
        value = normalized.get(normalized_marker)
        if value not in (None, "", [], {}):
            scope[normalized_marker] = normalize_fact_value(value)
    verified = None
    for marker in ("last_verified", "verified_at", "تاريخ التحقق", "آخر تحقق"):
        value = normalized.get(normalize_arabic(marker).lower())
        if value not in (None, ""):
            verified = str(value)
            break
    return entity, scope, verified


def _flatten(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key in sorted(value, key=lambda item: normalize_arabic(str(item))):
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _flatten(value[key], path)
        return
    if isinstance(value, list):
        # Descriptive/retrieval metadata is additive: its members are valid
        # aliases, not rival values.  Other scalar lists remain one semantic
        # value so a changed eligibility/branch set can still be preflighted.
        if all(not isinstance(item, (Mapping, list)) for item in value):
            leaf = normalize_arabic(prefix).lower().replace(" ", "_")
            if leaf.rsplit(".", 1)[-1] in _ADDITIVE_LIST_KEYS:
                for item in value:
                    yield f"{prefix}[]", item
            else:
                yield prefix, value
        else:
            for index, item in enumerate(value):
                yield from _flatten(item, f"{prefix}[{index}]")
        return
    yield prefix, value


_ENTITY_KEY_NORMALIZED = {
    normalize_arabic(value).lower().replace(" ", "_")
    for value in _ENTITY_KEYS
}


def _entity_for_fact(
    flat_values: list[tuple[str, Any]],
    path: str,
    raw_value: Any,
    default_entity: str,
) -> str:
    """Bind a nested fact to its sibling program/faculty instead of list index."""
    prefix, _, leaf = path.rpartition(".")
    leaf_norm = normalize_arabic(leaf).lower().replace(" ", "_")
    if leaf_norm in _ENTITY_KEY_NORMALIZED:
        return normalize_fact_value(raw_value)
    local = []
    for candidate_path, candidate_value in flat_values:
        candidate_prefix, _, candidate_leaf = candidate_path.rpartition(".")
        candidate_leaf_norm = normalize_arabic(candidate_leaf).lower().replace(" ", "_")
        if (
            candidate_prefix == prefix
            and candidate_leaf_norm in _ENTITY_KEY_NORMALIZED
            and candidate_value not in (None, "", [], {})
        ):
            local.append(normalize_fact_value(candidate_value))
    return " | ".join(dict.fromkeys(local)) or default_entity


def extract_document_facts(
    doc: Mapping[str, Any],
    *,
    source: str,
    doc_index: int,
) -> list[EvidenceItem]:
    entity, scope, verified_at = _identity_and_scope(doc)
    result = []
    flat_values = list(_flatten(doc))
    scope_json = json.dumps(scope, ensure_ascii=False, sort_keys=True)
    for path, raw_value in flat_values:
        if path.split(".")[-1] == "_id" or raw_value in (None, "", [], {}):
            continue
        attribute = normalize_arabic(path).lower().replace(" ", "_")
        field_name = _canonical_field(path)
        value = normalize_fact_value(raw_value)
        fact_entity = _entity_for_fact(
            flat_values, path, raw_value, entity
        )
        # Repeated/list values are a set (keywords, rules, eligible branches,
        # and so on), not mutually exclusive alternatives.  Include the item
        # value in their identity so two legitimate list members can never be
        # reported as a conflict; exact duplicate members still collapse.
        multi_value = "[]" in path
        fact_key = _stable_hash(
            fact_entity,
            field_name,
            attribute,
            scope_json,
            value if multi_value else "",
        )
        result.append(EvidenceItem(
            source=source,
            chunk_id=None,
            doc_index=doc_index,
            entity=fact_entity,
            canonical_field=field_name,
            attribute=attribute,
            value=value,
            scope=dict(scope),
            verified_at=verified_at,
            fact_key=fact_key,
            fact_fingerprint=_stable_hash(fact_key, value),
        ))
    return result


def _chunk_mapping(chunk: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for line in chunk.splitlines():
        if line.lstrip().startswith(("[ملف:", "[chunk_id:")):
            continue
        match = _KEY_VALUE_RE.match(line)
        if match:
            key = match.group(1).strip()
            value = match.group(2).strip()
            if key not in parsed:
                parsed[key] = value
            elif isinstance(parsed[key], list):
                parsed[key].append(value)
            else:
                parsed[key] = [parsed[key], value]
    if not parsed:
        parsed["content"] = chunk
    return parsed


def _chunk_sections(chunk: str) -> list[str]:
    """Split synthetic projection chunks back into their source records."""
    matches = list(_SOURCE_RE.finditer(chunk))
    if len(matches) <= 1:
        return [chunk]
    sections = []
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(chunk)
        section = chunk[match.start():end].strip()
        if section:
            sections.append(section)
    return sections or [chunk]


def evidence_items_from_chunks(
    chunks: list[str],
    candidate_metadata: list[dict[str, Any]] | None = None,
) -> list[list[EvidenceItem]]:
    metadata = candidate_metadata or []
    result: list[list[EvidenceItem]] = []
    for index, chunk in enumerate(chunks):
        item_meta = metadata[index] if index < len(metadata) else {}
        doc_index = item_meta.get("doc_index")
        facts: list[EvidenceItem] = []
        for section in _chunk_sections(chunk):
            source_match = _SOURCE_RE.search(section)
            source = str(
                (source_match.group(1).strip() if source_match else None)
                or item_meta.get("source")
                or "authoritative"
            )
            mapping = _chunk_mapping(section)
            if set(mapping) == {"content"}:
                # Narrative/list chunks are separate pieces of evidence, not
                # competing values of one global ``content`` attribute.
                body = "\n".join(
                    line for line in section.splitlines()
                    if not line.lstrip().startswith(("[ملف:", "[chunk_id:"))
                ).strip() or section.strip()
                value = normalize_fact_value(body)
                fact_key = _stable_hash("unstructured_content", value)
                section_facts = [EvidenceItem(
                    source=source,
                    chunk_id=None,
                    doc_index=(
                        int(doc_index) if isinstance(doc_index, int) else index
                    ),
                    entity=f"content:{fact_key[:16]}",
                    canonical_field="general",
                    attribute="content",
                    value=value,
                    fact_key=fact_key,
                    fact_fingerprint=_stable_hash(fact_key, value),
                )]
            else:
                section_facts = extract_document_facts(
                    mapping,
                    source=source,
                    doc_index=(
                        int(doc_index) if isinstance(doc_index, int) else index
                    ),
                )
            for fact in section_facts:
                fact.chunk_id = item_meta.get("chunk_id") or _stable_hash(section)
            facts.extend(section_facts)
        result.append(facts)
    return result


@dataclass(slots=True)
class EvidenceDeduplication:
    chunks: list[str]
    kept_indexes: list[int]
    items: list[EvidenceItem]
    duplicate_count: int
    conflicts: list[dict[str, Any]]
    resolved_conflicts: list[dict[str, Any]]


def deduplicate_evidence(
    chunks: list[str],
    candidate_metadata: list[dict[str, Any]] | None = None,
    *,
    source_recency: Mapping[str, str] | None = None,
    conflict_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> EvidenceDeduplication:
    """Deduplicate complete fact-equivalent chunks and surface value conflicts."""
    grouped = evidence_items_from_chunks(chunks, candidate_metadata)
    seen_fingerprints: set[str] = set()
    values_by_key: dict[str, dict[str, list[EvidenceItem]]] = {}
    kept_indexes: list[int] = []
    kept_items: list[EvidenceItem] = []
    duplicate_count = 0
    for index, facts in enumerate(grouped):
        fingerprints = {fact.fact_fingerprint for fact in facts}
        # Duplicate chunks are removed from the prompt, but their source/date
        # still matters when deciding which of two conflicting values is the
        # newest.  Record provenance before skipping their text.
        for fact in facts:
            values_by_key.setdefault(fact.fact_key, {}).setdefault(
                fact.value, []
            ).append(fact)
        if fingerprints and fingerprints <= seen_fingerprints:
            duplicate_count += 1
            continue
        kept_indexes.append(index)
        kept_items.extend(facts)
        seen_fingerprints.update(fingerprints)
    source_recency = source_recency or {}
    conflict_overrides = conflict_overrides or {}
    conflicts = []
    resolved_conflicts = []
    for fact_key, values in values_by_key.items():
        if len(values) < 2:
            continue
        samples = [
            item for value_samples in values.values() for item in value_samples
        ]
        base = {
            "conflict_id": _stable_hash(fact_key, *sorted(values)),
            "fact_key": fact_key,
            "canonical_field": samples[0].canonical_field,
            "attribute": samples[0].attribute,
            "entity": samples[0].entity,
            "values": sorted(values),
            "sources": sorted({item.source for item in samples}),
            "message": (
                f"تعارض {samples[0].canonical_field} للكيان "
                f"{samples[0].entity}: {', '.join(sorted(values))}"
            ),
        }
        override = conflict_overrides.get(fact_key)
        if override:
            selected_source = str(override.get("selected_source") or "")
            selected_value = str(override.get("selected_value") or "")
            selected = next((
                item for item in samples
                if (
                    (not selected_source or item.source == selected_source)
                    and (not selected_value or item.value == selected_value)
                )
            ), None)
            if selected is not None:
                resolved_conflicts.append({
                    **base,
                    "resolution": str(override.get("decision") or "admin"),
                    "selected_value": selected.value,
                    "selected_source": selected.source,
                    "selected_date": str(
                        selected.verified_at
                        or source_recency.get(selected.source)
                        or "admin_decision"
                    ),
                    "rejected_values": sorted(
                        value for value in values if value != selected.value
                    ),
                })
                continue
        freshest_by_value = {}
        for value, value_samples in values.items():
            dated = []
            for item in value_samples:
                verified = str(
                    item.verified_at or source_recency.get(item.source) or ""
                ).strip()
                key = _recency_key(verified)
                if key is not None:
                    dated.append((key, verified, item))
            if dated:
                freshest_by_value[value] = max(
                    dated, key=lambda row: row[0]
                )
        # Every competing value needs at least one comparable dated source.
        # An entirely undated value cannot safely be assumed older.
        if len(freshest_by_value) == len(values):
            latest_key = max(row[0] for row in freshest_by_value.values())
            latest_rows = [
                row for row in freshest_by_value.values()
                if row[0] == latest_key
            ]
            latest_values = {row[2].value for row in latest_rows}
            if len(latest_values) == 1:
                _key, latest_date, selected = latest_rows[0]
                resolved_conflicts.append({
                    **base,
                    "resolution": "newest_source",
                    "selected_value": selected.value,
                    "selected_source": selected.source,
                    "selected_date": latest_date,
                    "rejected_values": sorted(
                        value for value in values if value != selected.value
                    ),
                })
                continue
        conflicts.append({**base, "resolution": None})
    return EvidenceDeduplication(
        chunks=[chunks[index] for index in kept_indexes],
        kept_indexes=kept_indexes,
        items=kept_items,
        duplicate_count=duplicate_count,
        conflicts=conflicts,
        resolved_conflicts=resolved_conflicts,
    )


def suppress_rejected_conflict_values(
    chunks: list[str],
    resolutions: list[dict[str, Any]],
    candidate_metadata: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Remove older structured fact lines after an unambiguous resolution.

    Keeping both values in the final prompt and merely telling the LLM which
    one is newer is not deterministic enough.  This function preserves the
    rest of each record but removes lines whose normalized fact value was
    explicitly rejected by a newest-source or administrator decision.
    """
    if not chunks or not resolutions:
        return list(chunks)
    by_key = {
        str(item.get("fact_key")): {
            str(value) for value in item.get("rejected_values", [])
        }
        for item in resolutions
        if item.get("fact_key") and item.get("rejected_values")
    }
    if not by_key:
        return list(chunks)
    grouped = evidence_items_from_chunks(chunks, candidate_metadata)
    output: list[str] = []
    for chunk, facts in zip(chunks, grouped):
        rejected_pairs = {
            (fact.attribute, fact.value)
            for fact in facts
            if fact.value in by_key.get(fact.fact_key, set())
        }
        if not rejected_pairs:
            output.append(chunk)
            continue
        kept_lines = []
        for line in chunk.splitlines():
            match = _KEY_VALUE_RE.match(line)
            if match:
                attribute = normalize_arabic(
                    match.group(1).strip()
                ).lower().replace(" ", "_")
                value = normalize_fact_value(match.group(2).strip())
                if (attribute, value) in rejected_pairs:
                    continue
            kept_lines.append(line)
        cleaned = "\n".join(kept_lines).strip()
        if cleaned:
            output.append(cleaned)
    return output


def preflight_documents(
    incoming_documents: list[dict[str, Any]],
    existing_documents: Mapping[str, list[dict[str, Any]]],
    *,
    incoming_source: str,
) -> dict[str, Any]:
    incoming = [
        fact
        for index, doc in enumerate(incoming_documents)
        for fact in extract_document_facts(
            doc, source=incoming_source, doc_index=index
        )
    ]
    existing = [
        fact
        for source, docs in existing_documents.items()
        for index, doc in enumerate(docs)
        for fact in extract_document_facts(doc, source=source, doc_index=index)
    ]
    existing_by_fingerprint = {
        fact.fact_fingerprint: fact for fact in existing
    }
    existing_by_key: dict[str, dict[str, EvidenceItem]] = {}
    for fact in existing:
        existing_by_key.setdefault(fact.fact_key, {})[fact.value] = fact

    duplicates = []
    conflicts = []
    seen_duplicate_ids: set[str] = set()
    seen_conflict_ids: set[str] = set()
    for fact in incoming:
        duplicate = existing_by_fingerprint.get(fact.fact_fingerprint)
        if duplicate and fact.fact_fingerprint not in seen_duplicate_ids:
            seen_duplicate_ids.add(fact.fact_fingerprint)
            duplicates.append({
                "fact_fingerprint": fact.fact_fingerprint,
                "canonical_field": fact.canonical_field,
                "attribute": fact.attribute,
                "entity": fact.entity,
                "value": fact.value,
                "incoming_doc_index": fact.doc_index,
                "existing_source": duplicate.source,
                "existing_doc_index": duplicate.doc_index,
            })
        differing = {
            value: item
            for value, item in existing_by_key.get(fact.fact_key, {}).items()
            if value != fact.value
        }
        if differing:
            conflict_id = _stable_hash(
                fact.fact_key, fact.value, *sorted(differing)
            )
            if conflict_id in seen_conflict_ids:
                continue
            seen_conflict_ids.add(conflict_id)
            conflicts.append({
                "conflict_id": conflict_id,
                "fact_key": fact.fact_key,
                "canonical_field": fact.canonical_field,
                "attribute": fact.attribute,
                "entity": fact.entity,
                "incoming_value": fact.value,
                "incoming_doc_index": fact.doc_index,
                "existing_values": sorted(differing),
                "existing_sources": sorted({item.source for item in differing.values()}),
                "existing_candidates": sorted(
                    (
                        {"source": item.source, "value": value}
                        for value, item in differing.items()
                    ),
                    key=lambda item: (item["source"], item["value"]),
                ),
                "resolution": None,
            })

    return {
        "incoming_source": incoming_source,
        "incoming_fact_count": len(incoming),
        "existing_fact_count": len(existing),
        "exact_duplicate_count": len(duplicates),
        "exact_duplicates": duplicates,
        "conflict_count": len(conflicts),
        "unresolved_conflict_count": len(conflicts),
        "conflicts": conflicts,
        "can_publish": not conflicts,
    }


def apply_keep_existing_overrides(
    incoming_documents: list[dict[str, Any]],
    existing_documents: Mapping[str, list[dict[str, Any]]],
    report: Mapping[str, Any],
    resolutions: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build runtime bytes while preserving the untouched incoming version.

    Only the published/indexed copy is overlaid.  The incoming and previous
    source documents remain stored in their version records for audit/rollback.
    """
    runtime = copy.deepcopy(incoming_documents)

    def original_path(doc: Mapping[str, Any], wanted: str) -> tuple[str, Any] | None:
        for path, raw in _flatten(doc):
            attribute = normalize_arabic(path).lower().replace(" ", "_")
            if attribute == wanted and "[]" not in path:
                return path, raw
        return None

    def assign(doc: dict[str, Any], path: str, value: Any) -> bool:
        parts = path.split(".")
        current: dict[str, Any] = doc
        for part in parts[:-1]:
            child = current.get(part)
            if not isinstance(child, dict):
                return False
            current = child
        current[parts[-1]] = copy.deepcopy(value)
        return True

    conflicts = {
        str(item.get("conflict_id")): item
        for item in (report.get("conflicts") or [])
    }
    for conflict_id, resolution in resolutions.items():
        if resolution.get("decision") != "keep_existing":
            continue
        conflict = conflicts.get(str(conflict_id))
        if not conflict:
            continue
        source = str(resolution.get("selected_source") or "")
        selected_value = str(resolution.get("selected_value") or "")
        selected_doc = None
        selected_fact = None
        for doc_index, doc in enumerate(existing_documents.get(source, [])):
            for fact in extract_document_facts(
                doc, source=source, doc_index=doc_index
            ):
                if (
                    fact.fact_key == conflict.get("fact_key")
                    and fact.value == selected_value
                ):
                    selected_doc = doc
                    selected_fact = fact
                    break
            if selected_fact:
                break
        incoming_index = conflict.get("incoming_doc_index")
        if (
            selected_doc is None
            or selected_fact is None
            or not isinstance(incoming_index, int)
            or not (0 <= incoming_index < len(runtime))
        ):
            continue
        selected_path = original_path(selected_doc, selected_fact.attribute)
        incoming_path = original_path(
            runtime[incoming_index], selected_fact.attribute
        )
        if selected_path and incoming_path:
            assign(runtime[incoming_index], incoming_path[0], selected_path[1])
    return runtime


__all__ = [
    "EvidenceItem",
    "EvidenceDeduplication",
    "normalize_fact_value",
    "extract_document_facts",
    "evidence_items_from_chunks",
    "deduplicate_evidence",
    "suppress_rejected_conflict_values",
    "preflight_documents",
    "apply_keep_existing_overrides",
]
