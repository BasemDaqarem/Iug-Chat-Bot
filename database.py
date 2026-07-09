"""
Backward-compatibility shim — MongoDB access now lives in app/db.py
(one shared MongoClient for both databases instead of two separate ones).
Existing imports like `from database import get_collection` keep working.
"""

from app.config import MONGO_DB_NAME as DB_NAME
from app.config import MONGO_URI
from app.db import (
    close,
    get_all_collections,
    get_all_documents,
    get_collection,
    list_collection_names,
    ping,
)

__all__ = [
    "MONGO_URI", "DB_NAME",
    "get_collection", "list_collection_names",
    "get_all_documents", "get_all_collections",
    "ping", "close",
]

if __name__ == "__main__":
    names = list_collection_names()
    print(f"✅ [database] MongoDB connection successful. Collections found: {names}")
