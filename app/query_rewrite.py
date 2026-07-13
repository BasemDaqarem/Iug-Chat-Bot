"""
Retrieval-query rewriting — pure string operations, zero extra LLM calls.

The retrieval layer only ever sees the literal question text, which fails in
two recurring ways:

  1. Self-references: «رئيس قسمي» carries no department name, so the search
     can't rank the student's own department above any other.
  2. Anaphora / follow-ups: «كم رسوم هذا الطلب» carries none of the words of
     the previous turn («تأجيل الفصل»), so the search fetches the wrong topic.

Both are fixed by REWRITING THE RETRIEVAL QUERY ONLY — the question shown to
the LLM (and stored in history) stays exactly what the student typed.
"""

import re
from typing import Optional

from app.text_norm import tokenize

# ── 1. self-references → student profile ─────────────────────────────────────
# First-person possessives whose referent lives in the student's profile.
# Matched on NORMALIZED tokens (see text_norm: ة→ه, أ/إ→ا ...).
_SELF_REF_TOKENS = {
    "قسمي", "تخصصي", "كليتي", "برنامجي", "دراستي", "مساقاتي", "موادي",
}


def expand_self_references(question: str, major: Optional[str]) -> str:
    """Append the student's major when the question points at «my …» so the
    hybrid search can rank the right department's chunks. Appending (not
    substituting) keeps the original wording for dense retrieval."""
    if not major:
        return question
    tokens = set(tokenize(question))
    if tokens & _SELF_REF_TOKENS and not set(tokenize(major)) <= tokens:
        return f"{question} — {major}"
    return question


# ── 2. anaphora / follow-ups → previous turn ─────────────────────────────────
# Standalone tokens that signal the question leans on the previous turn.
_ANAPHORA_TOKENS = {
    "هذا", "هذه", "ذلك", "تلك", "هاي", "هيك",          # demonstratives
    "نفس", "المذكور", "السابق", "سابقا",                # same/aforementioned
    "اقصد", "قصدي", "قصدت", "يعني",                     # "I mean …" repairs
    "كمان", "ايضا", "برضه", "برضو",                     # "also …" continuations
}
# A follow-up is usually short; long questions restate their own context.
_SHORT_QUESTION_TOKENS = 4


def needs_history_context(question: str) -> bool:
    tokens = tokenize(question)
    if not tokens:
        return False
    if set(tokens) & _ANAPHORA_TOKENS:
        return True
    return len(tokens) <= _SHORT_QUESTION_TOKENS


def with_history_context(question: str, history: list) -> str:
    """Prefix the previous user turn onto the retrieval query when the current
    question can't stand alone. BM25+RRF then ranks the shared topic («تأجيل»)
    above lexical noise, and dense retrieval sees the full intent."""
    if not history or not needs_history_context(question):
        return question
    last_user = str(history[-1].get("user") or "").strip()
    if not last_user:
        return question
    return f"{last_user} — {question}"
