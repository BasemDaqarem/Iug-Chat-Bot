"""
Backward-compatibility shim — uploaded-files MongoDB access now lives in
app/db.py (one shared MongoClient for both databases instead of two).
Existing imports like `from uploaded_files_db import get_uploaded_collection`
keep working.
"""

from app.config import MONGO_URI, UPLOADED_DB_NAME
from app.db import (
    close_uploaded,
    drop_uploaded_collection,
    get_uploaded_collection,
    list_uploaded_collections,
    ping_uploaded,
)

__all__ = [
    "MONGO_URI", "UPLOADED_DB_NAME",
    "get_uploaded_collection", "list_uploaded_collections",
    "drop_uploaded_collection", "ping_uploaded", "close_uploaded",
]
