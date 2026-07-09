"""
Single MongoDB connection manager for BOTH logical databases:

  • main DB     (MONGO_DB_NAME, default "iug_chatbot")      → RAG source collections
  • uploaded DB (UPLOADED_DB_NAME, default "uploaded_files") → one collection per uploaded JSON file

Both databases live on the same cluster (same MONGO_URI), so ONE shared
MongoClient serves them — previously database.py and uploaded_files_db.py
each opened their own client to the same server.
"""

from typing import Iterable, Optional

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from app.config import MONGO_DB_NAME, MONGO_URI, UPLOADED_DB_NAME

# Module-level singleton client, opened lazily on first use.
_client: Optional[MongoClient] = None


def _get_client() -> MongoClient:
    global _client
    if _client is None:
        if not MONGO_URI:
            raise RuntimeError(
                "❌ Error: MONGO_URI is not set in .env file — check MONGO_URI inside .env."
            )
        _client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=5_000,
            connectTimeoutMS=10_000,
            socketTimeoutMS=30_000,
            retryWrites=True,
            w="majority",
        )
    return _client


def _get_db() -> Database:
    return _get_client()[MONGO_DB_NAME]


def _get_uploaded_db() -> Database:
    return _get_client()[UPLOADED_DB_NAME]


# ═════════════════════════════════════════════════════════════════════════
#  Main DB — RAG source collections
# ═════════════════════════════════════════════════════════════════════════

def get_collection(name: str) -> Collection:
    return _get_db()[name]


_SYSTEM_PREFIXES = ("system.",)


# Single source of truth used by the RAG pipeline: whatever collection
# exists here (today or added tomorrow) is picked up automatically, with
# zero code changes elsewhere.
def list_collection_names(exclude: Optional[Iterable[str]] = None) -> list[str]:
    db = _get_db()
    exclude_set = set(exclude or ())
    return [
        name
        for name in db.list_collection_names()
        if name not in exclude_set and not name.startswith(_SYSTEM_PREFIXES)
    ]


def get_all_documents(collection_name: str) -> list[dict]:
    return list(get_collection(collection_name).find({}))


def get_all_collections() -> dict[str, Collection]:
    db = _get_db()
    return {name: db[name] for name in list_collection_names()}


def ping() -> bool:
    try:
        _get_client().admin.command("ping")
        return True
    except (ConnectionFailure, ServerSelectionTimeoutError) as exc:
        print(f"❌ Error: [db] ping failed: {exc}")
        return False


def close() -> None:
    """Close the shared client. Safe to call more than once — any later DB
    access lazily reopens the connection."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
        print("🔌 [db] MongoDB connection closed.")


# ═════════════════════════════════════════════════════════════════════════
#  Uploaded-files DB — one collection per uploaded JSON file
# ═════════════════════════════════════════════════════════════════════════

def get_uploaded_collection(collection_name: str) -> Collection:
    return _get_uploaded_db()[collection_name]


def list_uploaded_collections() -> list:
    return _get_uploaded_db().list_collection_names()


def drop_uploaded_collection(collection_name: str) -> bool:
    _get_uploaded_db().drop_collection(collection_name)
    return True


def ping_uploaded() -> bool:
    return ping()


def close_uploaded() -> None:
    close()
