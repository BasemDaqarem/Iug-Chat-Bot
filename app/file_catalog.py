"""Managed file catalog and pre-retrieval access filtering.

Uploaded JSON remains in the existing uploaded-files database.  This module
adds the missing control plane: draft versions, processing state, published
version, classifications, and role/owner access.  The RAG layer asks this
catalog for the permitted collection names *before* ranking any chunks.
"""

from datetime import datetime, timezone
from typing import Iterable, Optional
from uuid import uuid4

from app import config, db
from app.chunking import build_uploaded_chunks
from app.rbac import Principal, Role


CATALOG = "file_catalog"
VERSIONS = "managed_file_versions"

# The MAXIMUM roles each classification may ever grant. A file's allowed_roles
# is intersected with this set, so an over-broad request (e.g. the schema's
# default "all roles" left on an admin_only file) can never widen access beyond
# what the classification means. Enforced on BOTH write and read.
_CLASSIFICATION_MAX_ROLES = {
    "university_public": {"guest", "student", "employee", "admin"},
    "student_records":   {"student", "admin"},
    "employee_internal": {"employee", "admin"},
    "employee_private":  {"employee", "admin"},
    "admin_only":        {"admin"},
}
CLASSIFICATIONS = set(_CLASSIFICATION_MAX_ROLES)

# Classifications that identify ONE person's record: a non-empty owner_id is
# mandatory, else the file would be readable by every same-role user.
_OWNER_REQUIRED = {"student_records", "employee_private"}


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


def recency_map() -> dict:
    """{collection: آخر تحديث ISO} لكل الملفات المسجّلة — تُستخدم لتفضيل الملف
    الأحدث عند تعارض المعلومات بين مصدرين في الإجابة."""
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
    now = _now()
    _versions().update_one(
        {"file_id": file_id, "version": version},
        {"$set": {"status": "ready", "chunks_count": len(chunks), "processed_at": now}},
    )
    _catalog().update_one(
        {"file_id": file_id},
        {"$set": {"status": "ready", "updated_at": now, "updated_by": actor_id}},
    )
    return get_file(file_id)


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
    bot.upload_json_file(entry["collection"], documents)
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
        if classification in _OWNER_REQUIRED and not _owner_present(owner_id):
            # Owner-scoped record with no owner → fail closed for everyone but
            # admin (who needs to see it to fix the misconfiguration).
            if principal.role != Role.ADMIN:
                continue
        elif _owner_present(owner_id) and principal.role != Role.ADMIN \
                and str(owner_id) != principal.subject:
            continue
        allowed.add(name)
    return allowed
