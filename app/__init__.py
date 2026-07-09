"""
IUG Chatbot — application package.

Feature map (each module owns exactly one responsibility):

    config          → all environment variables + tuning constants
    prompts         → Arabic system-prompt templates
    db              → single MongoDB connection manager (main + uploaded DBs)
    chunking        → document → chunk-text conversion + sensitivity detection
    text_norm       → Arabic normalization + tokenization (for lexical search)
    lexical         → Okapi-BM25 lexical scorer
    embeddings      → Jina embeddings client + semantic-index construction
    index_store     → on-disk persistence/cache for embedding indexes
    retrieval       → dense ranking + hybrid (dense + BM25 via RRF) ranking
    llm             → Groq chat-completion client (payload, retries, errors)
    sessions        → per-session chat history store
    privacy         → sensitive-record lookups + privacy guard
    knowledge_base  → main RAG corpus lifecycle (load → chunk → index → search)
    uploaded_files  → per-uploaded-file corpora lifecycle + search
    chatbot         → IUGChatbot facade orchestrating everything
"""

from app.chatbot import IUGChatbot

__all__ = ["IUGChatbot"]
