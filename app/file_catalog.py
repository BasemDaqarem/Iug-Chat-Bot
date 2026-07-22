"""Managed file catalog and pre-retrieval access filtering.

Uploaded JSON remains in the existing uploaded-files database.  This module
adds the missing control plane: draft versions, processing state, published
version, classifications, and role/owner access.  The RAG layer asks this
catalog for the permitted collection names *before* ranking any chunks.
"""

from datetime import datetime, timezone
from typing import Iterable, Optional
from uuid import uuid4

from app import config, data_quality, db
from app.chunking import build_uploaded_chunks
from app.rbac import Principal, Role


CATALOG = "file_catalog"
VERSIONS = "managed_file_versions"


class UnresolvedDataConflictError(ValueError):
    """Publishing would activate facts that conflict with current evidence."""


def _as_documents(documents: list | dict) -> list[dict]:
    values = documents if isinstance(documents, list) else [documents]
    return [dict(item) for item in values if isinstance(item, dict)]


def _runtime_document_map() -> dict[str, list[dict]]:
    """Read current published bytes without changing any collection."""
    # Tests exercise the pure preflight logic with isolated catalog doubles;
    # do not wait for a real Mongo server in that environment.
    if config.API_ENV == "testing":
        return {}
    result: dict[str, list[dict]] = {}
    try:
        for name in db.list_uploaded_collections():
            if name in config.RAG_EXCLUDE_COLLECTIONS:
                continue
            docs = []
            for item in db.get_uploaded_collection(name).find({}):
                value = dict(item)
                value.pop("_id", None)
                docs.append(value)
            if docs:
                result[str(name)] = docs
    except Exception as exc:
        # Data-quality publication must fail closed: an unavailable current
        # corpus is not evidence that no conflicts exist.
        raise RuntimeError(
            "تعذر قراءة البيانات المنشورة لإجراء فحص التعارض؛ لم يتم النشر."
        ) from exc
    return result


def preflight(
    collection: str,
    documents: list | dict,
) -> dict:
    docs = _as_documents(documents)
    if not docs:
        raise ValueError("المحتوى يجب أن يكون JSON object أو قائمة objects غير فارغة.")
    return data_quality.preflight_documents(
        docs,
        _runtime_document_map(),
        incoming_source=str(collection),
    )


def _apply_conflict_resolutions(
    report: dict,
    resolutions: dict[str, dict],
) -> dict:
    unresolved = 0
    for conflict in report.get("conflicts", []):
        resolution = resolutions.get(conflict.get("conflict_id"))
        if resolution:
            conflict["resolution"] = resolution.get("decision")
            conflict["selected_source"] = resolution.get("selected_source")
            conflict["selected_value"] = resolution.get("selected_value")
        else:
            unresolved += 1
    report["unresolved_conflict_count"] = unresolved
    report["can_publish"] = unresolved == 0
    return report

# The MAXIMUM roles each classification may ever grant. A file's allowed_roles
# is intersected with this set, so an over-broad request (e.g. the schema's
# default "all roles" left on an admin_only file) can never widen access beyond
# what the classification means. Enforced on BOTH write and read.
_CLASSIFICATION_MAX_ROLES = {
    "university_public": {"guest", "student", "employee", "admin"},
    # الموظف مخوّل قراءة سجلات الطلاب الأكاديمية بحكم عمله (rbac.can_read_student)
    "student_records":   {"student", "employee", "admin"},
    "employee_internal": {"employee", "admin"},
    "employee_private":  {"employee", "admin"},
    "admin_only":        {"admin"},
}
CLASSIFICATIONS = set(_CLASSIFICATION_MAX_ROLES)

# Classifications that identify ONE person's record: a non-empty owner_id is
# mandatory, else the file would be readable by every same-role user.
_OWNER_REQUIRED = {"student_records", "employee_private"}

# Roles that may read an owner-scoped file WITHOUT being its owner — mirrors
# app.rbac: employees read any student's academic record; only admins read
# another employee's private record.
_OWNER_BYPASS = {
    "student_records":  {Role.ADMIN, Role.EMPLOYEE},
    "employee_private": {Role.ADMIN},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owner_present(owner_id) -> bool:
    return bool(owner_id and str(owner_id).strip())


def _sanitize_policy(classification: str, allowed_roles: Iterable[str], owner_id) -> list[str]:
    """Validate a (classification, roles, owner) triple and return the roles
    actually permitted by the classification. Fails closed on any inconsistency.

    Single source of truth used by create_draft, update_access, publish, and
    the read-time gate — so a policy that is rejected on write can never slip in
    through a different path either."""
    if classification not in CLASSIFICATIONS:
        raise ValueError("تصنيف الملف غير صالح.")
    requested = {Role(str(role)).value for role in allowed_roles}
    roles = sorted(requested & _CLASSIFICATION_MAX_ROLES[classification])
    if not roles:
        raise ValueError("الأدوار المحددة غير متوافقة مع تصنيف الملف.")
    if classification in _OWNER_REQUIRED and not _owner_present(owner_id):
        raise ValueError("هذا التصنيف خاص بطالب/موظف محدد ويتطلب تحديد صاحبه (owner_id).")
    return roles


def _catalog():
    return db.get_collection(CATALOG)


def _versions():
    return db.get_collection(VERSIONS)


def _clean_doc(doc: Optional[dict]) -> Optional[dict]:
    if not doc:
        return None
    out = dict(doc)
    out.pop("_id", None)
    return out


def create_draft(
    collection: str,
    documents: list | dict,
    classification: str,
    allowed_roles: Iterable[str],
    actor_id: str,
    *,
    owner_id: Optional[str] = None,
) -> dict:
    docs = documents if isinstance(documents, list) else [documents]
    if not docs or not all(isinstance(item, dict) for item in docs):
        raise ValueError("المحتوى يجب أن يكون JSON object أو قائمة objects غير فارغة.")
    roles = _sanitize_policy(classification, allowed_roles, owner_id)

    existing = _catalog().find_one({"collection": collection})
    now = _now()
    if existing:
        file_id = existing["file_id"]
        version = int(existing.get("latest_version", 0)) + 1
        _catalog().update_one(
            {"file_id": file_id},
            {"$set": {
                "latest_version": version,
                "status": "draft",
                "classification": classification,
                "allowed_roles": roles,
                "owner_id": owner_id,
                "updated_at": now,
                "updated_by": actor_id,
            }},
        )
    else:
        file_id = uuid4().hex
        version = 1
        _catalog().insert_one({
            "file_id": file_id,
            "collection": collection,
            "name": collection,
            "classification": classification,
            "allowed_roles": roles,
            "owner_id": owner_id,
            "status": "draft",
            "latest_version": version,
            "published_version": None,
            "created_at": now,
            "created_by": actor_id,
            "updated_at": now,
            "updated_by": actor_id,
        })
    _versions().insert_one({
        "file_id": file_id,
        "version": version,
        "documents": [dict(item) for item in docs],
        # Snapshot the access policy WITH the content, so a later rollback to
        # this version restores its original policy — not whatever the current
        # (possibly broader) policy happens to be (security report finding 3).
        "classification": classification,
        "allowed_roles": roles,
        "owner_id": owner_id,
        "status": "draft",
        "created_at": now,
        "created_by": actor_id,
    })
    return get_file(file_id) or {}


def get_file(file_id: str) -> Optional[dict]:
    return _clean_doc(_catalog().find_one({"file_id": str(file_id)}))


def find_by_collection(collection: str) -> Optional[dict]:
    return _clean_doc(_catalog().find_one({"collection": str(collection)}))


def adopt_legacy(collection: str, actor_id: str) -> dict:
    """Bring a pre-catalog (legacy) collection under catalog management so the
    admin can edit its access or delete it like any managed file. The content
    is already uploaded/indexed, so it enters as PUBLISHED with the same open
    policy it effectively had (university_public / all roles)."""
    existing = find_by_collection(collection)
    if existing:
        return existing
    now = _now()
    doc = {
        "file_id": uuid4().hex,
        "collection": collection,
        "name": collection,
        "classification": "university_public",
        "allowed_roles": sorted(_CLASSIFICATION_MAX_ROLES["university_public"]),
        "owner_id": None,
        "status": "published",
        "latest_version": 1,
        "published_version": 1,
        "adopted_legacy": True,
        "created_at": now,
        "created_by": actor_id,
        "updated_at": now,
        "updated_by": actor_id,
    }
    _catalog().insert_one(doc)
    return _clean_doc(doc)


def adopt_all(available, actor_id: str) -> list:
    """Bring EVERY uncatalogued (pre-catalog) collection under management in
    one shot — the migration step that lets a deployment turn
    LEGACY_UNCATALOGUED_FILES_PUBLIC off without files vanishing. Each file
    enters published/university_public (its effective visibility today), then
    the admin narrows roles per file as needed."""
    adopted = []
    for name in sorted({str(n) for n in available}):
        if find_by_collection(name) is None:
            adopt_legacy(name, actor_id)
            adopted.append(name)
    return adopted


def recency_map() -> dict:
    """{collection: آخر تحديث ISO} لكل الملفات المسجّلة — تُستخدم لتفضيل الملف
    الأحدث عند تعارض المعلومات بين مصدرين في الإجابة."""
    if config.API_ENV == "testing":
        return {}
    try:
        return {
            doc["collection"]: str(doc.get("updated_at") or doc.get("created_at") or "")
            for doc in _catalog().find({}, {"collection": 1, "updated_at": 1, "created_at": 1})
        }
    except Exception:
        return {}  # بلا تواريخ ⇒ لا تفضيل حداثة، والشات يستمر طبيعياً


def list_files(runtime_files: Optional[list[dict]] = None) -> list[dict]:
    managed = [_clean_doc(doc) for doc in _catalog().find({})]
    by_collection = {doc["collection"]: doc for doc in managed if doc}
    for info in runtime_files or []:
        collection = info.get("collection")
        if collection in by_collection:
            by_collection[collection].update({
                "chunks_count": info.get("chunks_count", 0),
                "indexed": bool(info.get("indexed")),
            })
        else:
            # Existing deployments may have files predating the catalog.
            by_collection[collection] = {
                "file_id": f"legacy:{collection}",
                "collection": collection,
                "name": collection,
                "classification": "university_public",
                "allowed_roles": [role.value for role in Role],
                "owner_id": None,
                "status": "published",
                "latest_version": 1,
                "published_version": 1,
                "chunks_count": info.get("chunks_count", 0),
                "indexed": bool(info.get("indexed")),
                "legacy": True,
            }
    return sorted(by_collection.values(), key=lambda item: item.get("updated_at", ""), reverse=True)


def update_access(
    file_id: str,
    classification: str,
    allowed_roles: Iterable[str],
    owner_id: Optional[str],
    actor_id: str,
) -> Optional[dict]:
    roles = _sanitize_policy(classification, allowed_roles, owner_id)
    result = _catalog().update_one(
        {"file_id": str(file_id)},
        {"$set": {
            "classification": classification,
            "allowed_roles": roles,
            "owner_id": owner_id,
            "updated_at": _now(),
            "updated_by": actor_id,
        }},
    )
    return get_file(file_id) if getattr(result, "matched_count", 0) else None


def process(file_id: str, actor_id: str) -> Optional[dict]:
    entry = get_file(file_id)
    if entry is None:
        return None
    version = int(entry["latest_version"])
    version_doc = _versions().find_one({"file_id": file_id, "version": version})
    docs = list((version_doc or {}).get("documents") or [])
    chunks = build_uploaded_chunks(docs, entry["collection"])
    if not chunks:
        raise ValueError("لم ينتج عن الملف أي مقطع صالح للفهرسة.")
    quality_report = preflight(entry["collection"], docs)
    now = _now()
    _versions().update_one(
        {"file_id": file_id, "version": version},
        {"$set": {
            "status": "ready",
            "chunks_count": len(chunks),
            "processed_at": now,
            "preflight": quality_report,
            "conflict_resolutions": {},
        }},
    )
    _catalog().update_one(
        {"file_id": file_id},
        {"$set": {
            "status": "ready",
            "preflight": quality_report,
            "updated_at": now,
            "updated_by": actor_id,
        }},
    )
    return get_file(file_id)


def resolve_conflicts(
    file_id: str,
    actor_id: str,
    *,
    decision: str,
    conflict_ids: list[str] | None = None,
) -> Optional[dict]:
    if decision not in {"keep_existing", "prefer_incoming"}:
        raise ValueError("قرار التعارض غير صالح.")
    entry = get_file(file_id)
    if entry is None:
        return None
    version = int(entry["latest_version"])
    version_doc = _versions().find_one({"file_id": file_id, "version": version})
    if not version_doc:
        return None
    report = preflight(
        entry["collection"], list(version_doc.get("documents") or [])
    )
    requested = set(conflict_ids or [])
    available = {
        conflict["conflict_id"] for conflict in report.get("conflicts", [])
    }
    if requested - available:
        raise ValueError("يتضمن الطلب معرّف تعارض غير موجود في آخر تقرير.")
    targets = requested or available
    resolutions = dict(version_doc.get("conflict_resolutions") or {})
    dates = recency_map()
    for conflict in report.get("conflicts", []):
        conflict_id = conflict["conflict_id"]
        if conflict_id not in targets:
            continue
        if decision == "prefer_incoming":
            selected_source = entry["collection"]
            selected_value = conflict.get("incoming_value")
        else:
            candidates = list(conflict.get("existing_candidates") or [])
            candidates.sort(
                key=lambda item: (
                    dates.get(str(item.get("source"))) or "",
                    str(item.get("source") or ""),
                ),
                reverse=True,
            )
            selected = candidates[0] if candidates else {
                "source": (conflict.get("existing_sources") or [None])[0],
                "value": (conflict.get("existing_values") or [None])[0],
            }
            selected_source = selected.get("source")
            selected_value = selected.get("value")
        resolutions[conflict_id] = {
            "conflict_id": conflict_id,
            "fact_key": conflict.get("fact_key"),
            "decision": decision,
            "incoming_source": entry["collection"],
            "incoming_value": conflict.get("incoming_value"),
            "selected_source": selected_source,
            "selected_value": selected_value,
            "resolved_at": _now(),
            "resolved_by": actor_id,
        }
    report = _apply_conflict_resolutions(report, resolutions)
    _versions().update_one(
        {"file_id": file_id, "version": version},
        {"$set": {
            "preflight": report,
            "conflict_resolutions": resolutions,
        }},
    )
    _catalog().update_one(
        {"file_id": file_id},
        {"$set": {
            "preflight": report,
            "updated_at": _now(),
            "updated_by": actor_id,
        }},
    )
    return {
        "file_id": file_id,
        "version": version,
        "decision": decision,
        "resolved_count": len(targets),
        "preflight": report,
    }


def fact_resolution_map() -> dict[str, dict]:
    """Published admin decisions used by runtime evidence conflict handling."""
    if config.API_ENV == "testing":
        return {}
    result: dict[str, dict] = {}
    try:
        entries = [
            _clean_doc(item) for item in _catalog().find({"status": "published"})
        ]
        entries = [item for item in entries if item]
        entries.sort(key=lambda item: str(item.get("updated_at") or ""))
        for entry in entries:
            version = entry.get("published_version")
            if version is None:
                continue
            version_doc = _versions().find_one({
                "file_id": entry["file_id"], "version": int(version),
            }) or {}
            for value in (version_doc.get("conflict_resolutions") or {}).values():
                fact_key = value.get("fact_key")
                if fact_key:
                    result[str(fact_key)] = dict(value)
    except Exception:
        return {}
    return result


def _version_documents(file_id: str, version: int) -> list[dict]:
    doc = _versions().find_one({"file_id": file_id, "version": int(version)})
    return list((doc or {}).get("documents") or [])


def publish(file_id: str, bot, actor_id: str, version: Optional[int] = None) -> Optional[dict]:
    entry = get_file(file_id)
    if entry is None:
        return None
    target = int(version or entry["latest_version"])
    target_doc = _versions().find_one({"file_id": file_id, "version": target})
    if not target_doc or target_doc.get("status") not in {"ready", "published", "archived"}:
        raise ValueError("يجب معالجة النسخة بنجاح قبل نشرها.")

    previous = entry.get("published_version")
    documents = list(target_doc.get("documents") or [])
    existing_documents = _runtime_document_map()
    quality_report = data_quality.preflight_documents(
        documents,
        existing_documents,
        incoming_source=entry["collection"],
    )
    resolutions = dict(target_doc.get("conflict_resolutions") or {})
    # A rollback endpoint is itself an explicit administrative choice to make
    # that archived version authoritative again.
    if version is not None and target != int(entry.get("latest_version") or target):
        for conflict in quality_report.get("conflicts", []):
            conflict_id = conflict["conflict_id"]
            resolutions.setdefault(conflict_id, {
                "conflict_id": conflict_id,
                "fact_key": conflict.get("fact_key"),
                "decision": "prefer_incoming",
                "incoming_source": entry["collection"],
                "incoming_value": conflict.get("incoming_value"),
                "selected_source": entry["collection"],
                "selected_value": conflict.get("incoming_value"),
                "resolved_at": _now(),
                "resolved_by": actor_id,
                "reason": "explicit_rollback",
            })
    quality_report = _apply_conflict_resolutions(
        quality_report, resolutions
    )
    _versions().update_one(
        {"file_id": file_id, "version": target},
        {"$set": {
            "preflight": quality_report,
            "conflict_resolutions": resolutions,
        }},
    )
    if quality_report.get("unresolved_conflict_count", 0):
        raise UnresolvedDataConflictError(
            "يتضمن الملف تعارضات غير محلولة. استخدم resolve-conflicts "
            "واختر الاحتفاظ بالموجود أو اعتماد الوارد قبل النشر."
        )
    runtime_documents = data_quality.apply_keep_existing_overrides(
        documents,
        existing_documents,
        quality_report,
        resolutions,
    )
    bot.upload_json_file(entry["collection"], runtime_documents)
    indexed = any(
        item.get("collection") == entry["collection"] and item.get("indexed")
        for item in bot.get_uploaded_files_list()
    )
    if not indexed:
        # Preserve the last published version if the new index cannot be built.
        if previous:
            bot.upload_json_file(entry["collection"], _version_documents(file_id, int(previous)))
        else:
            bot.delete_uploaded_file(entry["collection"])
        raise RuntimeError("فشلت فهرسة النسخة الجديدة؛ بقيت النسخة المنشورة السابقة فعّالة.")

    now = _now()
    _versions().update_many(
        {"file_id": file_id, "status": "published"},
        {"$set": {"status": "archived"}},
    )
    _versions().update_one(
        {"file_id": file_id, "version": target},
        {"$set": {"status": "published", "published_at": now, "published_by": actor_id}},
    )
    # Restore the TARGET version's own access policy alongside its content, so
    # rolling back to an old version cannot serve its bytes under a newer,
    # broader policy (finding 3). Versions predating policy snapshots fall back
    # to the current catalog policy (unchanged behavior, still consistent).
    policy_set = {}
    if "classification" in target_doc:
        policy_set = {
            "classification": target_doc["classification"],
            "allowed_roles": _sanitize_policy(
                target_doc["classification"],
                target_doc.get("allowed_roles") or [],
                target_doc.get("owner_id"),
            ),
            "owner_id": target_doc.get("owner_id"),
        }
    _catalog().update_one(
        {"file_id": file_id},
        {"$set": {
            "status": "published",
            "published_version": target,
            "updated_at": now,
            "updated_by": actor_id,
            "preflight": quality_report,
            **policy_set,
        }},
    )
    return get_file(file_id)


def purge_versions(file_id: str) -> int:
    """Delete ALL stored version documents (the raw file content) for a
    deleted file. An admin deleting a file expects its content gone — keeping
    full document bodies in `managed_file_versions` would silently retain
    (possibly sensitive) data forever. The catalog entry itself stays as an
    archived tombstone (metadata only) and the audit log records the act."""
    try:
        result = _versions().delete_many({"file_id": str(file_id)})
        return int(getattr(result, "deleted_count", 0))
    except Exception:
        return 0


def archive(file_id: str, actor_id: str) -> Optional[dict]:
    result = _catalog().update_one(
        {"file_id": str(file_id)},
        {"$set": {"status": "archived", "updated_at": _now(), "updated_by": actor_id}},
    )
    return get_file(file_id) if getattr(result, "matched_count", 0) else None


def allowed_collections(principal: Principal, available: Iterable[str]) -> set[str]:
    """Return the only collections that may become retrieval candidates."""
    names = {str(name) for name in available}
    try:
        entries = {doc["collection"]: doc for doc in _catalog().find({"collection": {"$in": list(names)}})}
    except Exception:
        # Production fails closed. Development keeps pre-catalog projects usable.
        return set() if config.API_ENV == "production" else names

    allowed: set[str] = set()
    for name in names:
        entry = entries.get(name)
        if entry is None:
            # Development preserves old projects. Production fails closed:
            # an admin must classify/publish pre-catalog collections first.
            if config.LEGACY_UNCATALOGUED_FILES_PUBLIC:
                allowed.add(name)
            continue
        if entry.get("status") != "published":
            continue
        classification = entry.get("classification", "university_public")
        # Re-derive effective roles from the classification at READ time too:
        # a stored policy that was over-broad (created before this rule, or by a
        # future bug) can never leak beyond what the classification allows.
        max_roles = _CLASSIFICATION_MAX_ROLES.get(classification, {"admin"})
        effective_roles = set(entry.get("allowed_roles") or []) & max_roles
        if principal.role.value not in effective_roles:
            continue
        owner_id = entry.get("owner_id")
        bypass = _OWNER_BYPASS.get(classification, {Role.ADMIN})
        if classification in _OWNER_REQUIRED and not _owner_present(owner_id):
            # Owner-scoped record with no owner → fail closed for everyone but
            # admin (who needs to see it to fix the misconfiguration).
            if principal.role != Role.ADMIN:
                continue
        elif _owner_present(owner_id) and principal.role not in bypass \
                and str(owner_id) != principal.subject:
            continue
        allowed.add(name)
    return allowed
