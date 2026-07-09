"""
Central configuration — the ONLY place that reads .env / environment
variables. Every other module imports its settings from here, so config is
loaded exactly once and there is no hidden import-order coupling.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# ── Chat LLM (Groq-compatible endpoint) ──────────────────────────────────
CHAT_API_MODEL = os.getenv("CHAT_API_MODEL", "openai/gpt-oss-120b")
CHAT_API_KEY = os.getenv("CHAT_API_KEY", "")
CHAT_API_URL = os.getenv("CHAT_API_URL", "")

# ── Embeddings (Jina) ─────────────────────────────────────────────────────
EMBED_MODEL = os.getenv("EMBED_MODEL", "jina-embeddings-v3")
EMBED_API_KEY = os.getenv("EMBED_API_KEY", "")
EMBED_API_URL = os.getenv("EMBED_API_URL", "")

# ── Logging ───────────────────────────────────────────────────────────────
# DEBUG | INFO | WARNING | ERROR — consumed by app.log (one console handler).
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── API layer (FastAPI) ───────────────────────────────────────────────────
# ENV switches docs/CORS defaults: "development" | "testing" | "production".
API_ENV = os.getenv("API_ENV", "development")
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))
# Comma-separated allowed origins for the frontend, e.g. "http://localhost:3000".
# "*" (dev default) must be replaced with the real frontend origin in production.
API_CORS_ORIGINS = [
    o.strip() for o in os.getenv("API_CORS_ORIGINS", "*").split(",") if o.strip()
]

# ── MongoDB ───────────────────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "iug_chatbot")
UPLOADED_DB_NAME = os.getenv("UPLOADED_DB_NAME", "uploaded_files")

# ── RAG / chat tuning ─────────────────────────────────────────────────────
TOP_K = 10
MAX_HISTORY = 20
SIM_THRESHOLD = 0.25

# ── Hybrid retrieval (dense embeddings + lexical BM25, fused with RRF) ─────
# Lexical BM25 catches exact Arabic terms — fee numbers, course codes, names —
# that pure embedding similarity often misses. Reciprocal Rank Fusion merges
# the two rankings without fragile score normalization.
HYBRID_ENABLED = True
RRF_K = 60          # RRF damping constant (standard default)
BM25_K1 = 1.5       # term-frequency saturation
BM25_B = 0.75       # document-length normalization

# ── Embedding-index persistence ───────────────────────────────────────────
# Embeddings are expensive (Jina API call per chunk) and deterministic for a
# given (model, chunk-text). Caching them to disk turns an ~80s cold start
# into a near-instant load and stops re-billing Jina on every run. The cache
# is keyed by a fingerprint of (model + chunk texts), so it self-invalidates
# the moment any chunk changes.
INDEX_CACHE_DIR = os.getenv("INDEX_CACHE_DIR", ".index_cache")
LLM_MAX_TOKENS = 450
LLM_TEMPERATURE = 0.05
# Reasoning models (e.g. gpt-oss) spend part of max_tokens on hidden
# "reasoning" before any visible answer. "low" keeps that budget tiny so the
# answer is never starved (returned as null content) on complex questions.
LLM_REASONING_EFFORT = "low"
EMBED_BATCH_SIZE = 64
HISTORY_TURNS_IN_PROMPT = 6

# Collections that exist in the DB but should NOT be indexed as RAG content
# (e.g. internal/ops collections). Configurable via .env, no code change
# needed to add/remove a collection from the RAG pipeline.
RAG_EXCLUDE_COLLECTIONS = {
    c.strip() for c in os.getenv("RAG_EXCLUDE_COLLECTIONS", "").split(",") if c.strip()
}
