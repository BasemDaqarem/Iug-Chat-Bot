"""Hermetic test defaults.

The suite must not depend on a developer's local .env or production credentials.
Individual tests still patch these values when checking configuration errors.
"""

import os

os.environ.setdefault("CHAT_API_URL", "https://example.invalid/v1/chat/completions")
os.environ.setdefault("CHAT_API_KEY", "test-key")
os.environ.setdefault("EMBED_API_URL", "https://example.invalid/v1/embeddings")
os.environ.setdefault("EMBED_API_KEY", "test-key")
os.environ.setdefault("API_ENV", "testing")
os.environ.setdefault("SESSION_BACKEND", "memory")
os.environ.setdefault("INDEX_BACKEND", "disk")
# Semantic planner/verifier calls are opt-in in unit tests so existing tests
# remain hermetic; dedicated tests enable and mock them explicitly.
os.environ.setdefault("SEMANTIC_RAG_ENABLED", "false")
