"""
Central configuration — the ONLY place that reads .env / environment
variables. Every other module imports its settings from here, so config is
loaded exactly once and there is no hidden import-order coupling.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# ── Chat LLM (any OpenAI-compatible endpoint: OpenRouter / Groq / NVIDIA …) ─
CHAT_API_MODEL = os.getenv("CHAT_API_MODEL", "openai/gpt-oss-120b")
CHAT_API_KEY = os.getenv("CHAT_API_KEY", "")
CHAT_API_URL = os.getenv("CHAT_API_URL", "")

# ── Embeddings (Jina) ─────────────────────────────────────────────────────
EMBED_MODEL = os.getenv("EMBED_MODEL", "jina-embeddings-v3")
EMBED_API_KEY = os.getenv("EMBED_API_KEY", "")
EMBED_API_URL = os.getenv("EMBED_API_URL", "")

# ── Caching (in-process TTL + LRU) ────────────────────────────────────────
# Two safe caches (see app.cache / app.chatbot):
#   • query-embedding cache: question text → its Jina vector (deterministic,
#     no PII in the value). Saves an embeddings API call on repeated questions.
#   • public-answer cache:  full answer for a PUBLIC turn only (no owned
#     student record + no prior history). NEVER stores a response built from
#     any student's private data — those are always generated in real time.
CACHE_ENABLED = os.getenv("CACHE_ENABLED", "true").lower() == "true"
CACHE_ANSWER_TTL = int(os.getenv("CACHE_ANSWER_TTL", "3600"))       # 1 hour
CACHE_ANSWER_MAXSIZE = int(os.getenv("CACHE_ANSWER_MAXSIZE", "512"))
CACHE_EMBED_TTL = int(os.getenv("CACHE_EMBED_TTL", "86400"))         # 24 hours
CACHE_EMBED_MAXSIZE = int(os.getenv("CACHE_EMBED_MAXSIZE", "2048"))

# ── Rate limiting (per fixed window) ──────────────────────────────────────
# Protects the LLM/embeddings from bursts and login from brute force. In-memory
# (per-process) for now; move to Redis when running multiple instances.
RATE_LIMIT_CHAT_PER_MIN = int(os.getenv("RATE_LIMIT_CHAT_PER_MIN", "30"))
RATE_LIMIT_LOGIN_PER_MIN = int(os.getenv("RATE_LIMIT_LOGIN_PER_MIN", "10"))

# ── Logging ───────────────────────────────────────────────────────────────
# DEBUG | INFO | WARNING | ERROR — consumed by app.log (one console handler).
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── Authentication (JWT) ──────────────────────────────────────────────────
# Signs the session token issued at login. MUST be a strong secret set via
# .env in production — the default below is for local dev only (app.tokens
# logs a warning if it is still in use outside development).
JWT_SECRET = os.getenv("JWT_SECRET", "dev-insecure-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "12"))

# Admin key for corpus-mutating ops (file upload/delete/reload, cache clear).
# Empty by default → those endpoints are DENIED until an admin key is set.
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")


def assert_secure_for_production() -> None:
    """Fail closed: refuse to run in production with a weak/default JWT secret,
    instead of quietly signing forgeable tokens. Dev/testing keep the default."""
    if API_ENV == "production" and (
        JWT_SECRET == "dev-insecure-change-me" or len(JWT_SECRET) < 32
    ):
        raise RuntimeError(
            "❌ JWT_SECRET ضعيف أو افتراضي في الإنتاج — عيّن سراً قوياً (≥32 حرفاً) في .env قبل التشغيل."
        )

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
# given (model, chunk-text). Persisting them turns an ~80s cold start into a
# near-instant load and stops re-billing Jina on every run. Keyed by a
# fingerprint of (model + chunk texts), so it self-invalidates on any change.
#   INDEX_BACKEND = "disk"  → .index_cache/ files (great locally)
#                 = "mongo" → embedding_index collection (survives ephemeral
#                             disks, e.g. Render redeploys)
INDEX_BACKEND = os.getenv("INDEX_BACKEND", "disk")
INDEX_CACHE_DIR = os.getenv("INDEX_CACHE_DIR", ".index_cache")

# ── Session (chat-history) persistence ────────────────────────────────────
#   SESSION_BACKEND = "mongo"  → chat_sessions collection (survives restarts)
#                   = "memory" → in-process (lost on restart; used by tests)
SESSION_BACKEND = os.getenv("SESSION_BACKEND", "mongo")
LLM_MAX_TOKENS = 450
LLM_TEMPERATURE = 0.05
# Reasoning models (e.g. gpt-oss) spend part of max_tokens on hidden
# "reasoning" before any visible answer. "low" keeps that budget tiny so the
# answer is never starved (returned as null content) on complex questions.
LLM_REASONING_EFFORT = "low"
EMBED_BATCH_SIZE = 64
HISTORY_TURNS_IN_PROMPT = 6

# Collections that must NEVER be indexed as RAG content. Auth/identity/PII
# collections (password hashes, tokens, user records) would otherwise become
# searchable chunks and could leak into an LLM answer — so they are ALWAYS
# excluded, on top of anything added via .env.
_ALWAYS_EXCLUDE_FROM_RAG = {
    "students_auth", "refresh_tokens", "users",  # auth/identity/PII
    "chat_sessions", "embedding_index",           # our own persistence collections
}
RAG_EXCLUDE_COLLECTIONS = _ALWAYS_EXCLUDE_FROM_RAG | {
    c.strip() for c in os.getenv("RAG_EXCLUDE_COLLECTIONS", "").split(",") if c.strip()
}
