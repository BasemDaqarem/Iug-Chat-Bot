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
CLASSIFICATIONS = {
    "university_public",
    "student_records",
    "employee_internal",
    "employee_private",
    "admin_only",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    if classification not in CLASSIFICATIONS:
        raise ValueError("تصنيف الملف غير صالح.")
    docs = documents if isinstance(documents, list) else [documents]
    if not docs or not all(isinstance(item, dict) for item in docs):
        raise ValueError("المحتوى يجب أن يكون JSON object أو قائمة objects غير فارغة.")
    roles = sorted({Role(str(role)).value for role in allowed_roles})
    if not roles:
        raise ValueError("اختر دوراً واحداً على الأقل لقراءة الملف.")

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
        "status": "draft",
        "created_at": now,
        "created_by": actor_id,
    })
    return get_file(file_id) or {}


def get_file(file_id: str) -> Optional[dict]:
    return _clean_doc(_catalog().find_one({"file_id": str(file_id)}))


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
    if classification not in CLASSIFICATIONS:
        raise ValueError("تصنيف الملف غير صالح.")
    roles = sorted({Role(str(role)).value for role in allowed_roles})
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
    _catalog().update_one(
        {"file_id": file_id},
        {"$set": {
            "status": "published",
            "published_version": target,
            "updated_at": now,
            "updated_by": actor_id,
        }},
    )
    return get_file(file_id)


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
        if principal.role.value not in set(entry.get("allowed_roles") or []):
            continue
        owner_id = entry.get("owner_id")
        if owner_id and principal.role not in {Role.ADMIN} and str(owner_id) != principal.subject:
            continue
        allowed.add(name)
    return allowed
