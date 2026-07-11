"""
IUGChatbot — thin facade that composes the feature services and keeps the
exact public API the rest of the system (console harness, future REST
layer) already relies on. All heavy lifting lives in the feature modules;
this class only orchestrates.
"""

import hashlib
from typing import List

import numpy as np

from app import auth, config, embeddings, privacy
from app.cache import TTLCache
from app.chunking import SENSITIVE_MARKER
from app.knowledge_base import KnowledgeBase
from app.llm import chat_completion
from app.prompts import SYSTEM_PROMPT_TEMPLATE, UPLOADED_FILE_SYSTEM_PROMPT
from app.sessions import SessionStore, make_session_store
from app.text_norm import normalize_arabic
from app.uploaded_files import UploadedFilesStore


class IUGChatbot:

    SENSITIVE_MARKER = SENSITIVE_MARKER

    def __init__(self, sessions=None):
        self._kb = KnowledgeBase()
        self._uploaded = UploadedFilesStore()
        # persistent (Mongo) by default; tests inject an in-memory store
        self._sessions = sessions if sessions is not None else make_session_store()
        # Shared across users ON PURPOSE — only ever holds PUBLIC answers
        # (see the public-turn gate in chat/chat_with_*). No student-specific
        # response is ever written here, so cross-user isolation is guaranteed.
        self._answer_cache = TTLCache(
            "public_answers", config.CACHE_ANSWER_MAXSIZE, config.CACHE_ANSWER_TTL
        )

    @staticmethod
    def _cache_key(kind: str, question: str, extra: str = "") -> str:
        raw = f"{kind}\x00{extra}\x00{question}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def cache_stats(self) -> dict:
        return {
            "public_answers": self._answer_cache.stats(),
            "query_embeddings": embeddings.query_cache_stats(),
        }

    def clear_caches(self) -> None:
        self._answer_cache.clear()
        embeddings.reset_query_cache()

    def initialize(self):
        """Load data, build chunks, build semantic index."""
        self._kb.load()
        self._uploaded.load_all()

    # ═════════════════════════════════════════════════════════════════════
    #  PUBLIC PROPERTIES
    # ═════════════════════════════════════════════════════════════════════

    @property
    def data(self) -> dict:
        """{collection_name: [raw documents]} — fully dynamic, mirrors MongoDB."""
        return self._kb.data

    @property
    def chunks(self) -> List[str]:
        return self._kb.chunks

    @property
    def index(self) -> np.ndarray:
        return self._kb.index

    # ═════════════════════════════════════════════════════════════════════
    #  SEARCH
    # ═════════════════════════════════════════════════════════════════════

    def semantic_search(
        self,
        question: str,
        top_k: int = config.TOP_K,
        threshold: float = config.SIM_THRESHOLD,
    ) -> List[str]:
        return self._kb.semantic_search(question, top_k, threshold)

    # ═════════════════════════════════════════════════════════════════════
    #  SESSION HISTORY
    # ═════════════════════════════════════════════════════════════════════

    def get_history(self, sid: str) -> list:
        return self._sessions.get(sid)

    def push_history(self, sid: str, user: str, assistant: str):
        self._sessions.push(sid, user, assistant)

    def clear_history(self, sid: str):
        self._sessions.clear(sid)

    @staticmethod
    def fmt_history(history: list) -> str:
        return SessionStore.format_for_prompt(history)

    # ═════════════════════════════════════════════════════════════════════
    #  UPLOADED FILES
    # ═════════════════════════════════════════════════════════════════════

    def upload_json_file(self, collection_name: str, json_data: list) -> dict:
        result = self._uploaded.upload_json(collection_name, json_data)
        self._answer_cache.clear()  # content changed → drop stale public answers
        return result

    def get_uploaded_files_list(self) -> List[dict]:
        return self._uploaded.list_files()

    def reload_uploaded_file(self, collection_name: str) -> bool:
        ok = self._uploaded.reload(collection_name)
        if ok:
            self._answer_cache.clear()
        return ok

    def delete_uploaded_file(self, collection_name: str) -> bool:
        ok = self._uploaded.delete(collection_name)
        self._answer_cache.clear()
        return ok

    # ═════════════════════════════════════════════════════════════════════
    #  CHAT ORCHESTRATION
    # ═════════════════════════════════════════════════════════════════════

    def _ask_llm(self, system: str, question: str, session_id: str) -> str:
        """Shared final step of every chat flow: fold history into the user
        message, call the LLM, record the turn."""
        history_text = self.fmt_history(self.get_history(session_id))
        user_message = f"{history_text}السؤال: {question}"
        answer = chat_completion(system, user_message)
        self.push_history(session_id, question, answer)
        return answer

    @staticmethod
    def _is_generic_engineering_hourly_fee(question: str) -> bool:
        """Whether a fee question names engineering but omits degree level.

        These questions need evidence for both undergraduate and graduate
        programmes; otherwise a single high-ranking chunk can make the model
        present one level's price as if it applied to every programme.
        """
        normalized = normalize_arabic(question).lower()
        asks_hourly_fee = (
            "هندس" in normalized
            and "ساع" in normalized
            and any(term in normalized for term in ("سعر", "رسوم", "تكلف"))
        )
        names_level = any(
            term in normalized
            for term in (
                "بكالوريوس",
                "بكلوريوس",
                "بكالوريس",
                "ماجستير",
                "دراسات عليا",
                "دراسات العليا",
                "دكتوراه",
            )
        )
        return asks_hourly_fee and not names_level

    @staticmethod
    def _trusted_direct_answer(question: str) -> str:
        """Return canonical answers for a tiny set of verified core facts.

        These are intentionally deterministic because confusing Ramallah with
        Palestine's capital, or failing to provide IUG's own official URL,
        would make the student assistant confidently misleading.
        """
        normalized = normalize_arabic(question).lower()
        if "عاصم" in normalized and "فلسطين" in normalized:
            return "عاصمة فلسطين هي القدس."
        if (
            any(term in normalized for term in ("رابط", "موقع"))
            and "جامع" in normalized
            and any(term in normalized for term in ("اسلام", "غزه", "الجامعه"))
        ):
            return (
                "رابط الموقع الرسمي للجامعة الإسلامية بغزة هو: "
                "https://www.iugaza.edu.ps/"
            )
        return ""

    def _search_all_for_question(self, question: str, top_k: int) -> List[str]:
        """Retrieve uploaded-file evidence, expanding ambiguous fee queries.

        Values remain fully data-driven: the expansion only asks the existing
        uploaded-files index for each degree level, then interleaves and
        deduplicates its results so both levels fit in the final context.
        """
        queries = [question]
        if self._is_generic_engineering_hourly_fee(question):
            queries.extend((
                f"{question} بكالوريوس",
                f"{question} ماجستير دراسات عليا",
            ))

        per_query_k = max(2, (top_k + len(queries) - 1) // len(queries))
        batches = [self._uploaded.search_all(query, per_query_k) for query in queries]

        merged: List[str] = []
        seen = set()
        max_batch_size = max((len(batch) for batch in batches), default=0)
        for position in range(max_batch_size):
            for batch in batches:
                if position >= len(batch):
                    continue
                chunk = batch[position]
                if chunk in seen:
                    continue
                seen.add(chunk)
                merged.append(chunk)
                if len(merged) >= top_k:
                    return merged
        return merged

    def chat(self, question: str, session_id: str) -> dict:
        """
        Full chat pipeline:
        1. Semantic retrieval over the fully-dynamic chunk index
        2. Academic status shortcut (any sensitive Mongo record owned by the student)
        3. Privacy guard for ranking-style queries
        4. LLM call
        """
        chunk_meta = self._kb.chunk_meta
        current_record = privacy.find_sensitive_record(chunk_meta, session_id)

        # ── Cache gate: a turn is PUBLIC (shareable) only if the session owns
        # no sensitive record AND has no prior history — then the answer
        # depends solely on the question + public corpus. Any student with a
        # record, or any follow-up turn, is answered in real time and is never
        # read from or written to the shared cache. (Privacy > performance.)
        public_turn = (
            config.CACHE_ENABLED
            and current_record is None
            and not self.get_history(session_id)
        )
        cache_key = self._cache_key("chat", question) if public_turn else None
        if public_turn:
            cached = self._answer_cache.get(cache_key)
            if cached is not None:
                self.push_history(session_id, question, cached["answer"])
                return dict(cached)

        # ── Step 1: access-filtered hybrid retrieval ──────────────────────
        # Only public chunks + this session's own sensitive record are
        # candidates (structural isolation), fused dense + lexical ranking.
        relevant_chunks = self._kb.search_for(
            question   = question,
            session_id = session_id,
            top_k      = config.TOP_K,
            threshold  = config.SIM_THRESHOLD,
        )

        # ── Step 2: separate general chunks from privacy-sensitive ones ───
        sensitive_prefix = f"[{self.SENSITIVE_MARKER}|"
        general_chunks = [c for c in relevant_chunks if not c.startswith(sensitive_prefix)]

        # ── Step 3: academic status shortcut ──────────────────────────────
        if privacy.is_academic_status_question(question) and current_record:
            status_answer = privacy.build_status_from_sensitive_record(current_record["raw"])
            self.push_history(session_id, question, status_answer)
            return {"answer": status_answer, "top_chunks": []}

        # ── Step 4: privacy guard for ranking/GPA questions ───────────────
        if privacy.is_ranking_question(question):
            other_names = privacy.other_sensitive_display_names(chunk_meta, session_id)
            if privacy.mentions_other_student(question, other_names):
                self.push_history(session_id, question, privacy.BLOCKED_ANSWER)
                return {"answer": privacy.BLOCKED_ANSWER, "top_chunks": []}

        # ── Step 5: build context (student's own record first, if any) ────
        if current_record:
            student_context_chunk = privacy.format_sensitive_record_context(current_record)
            context = "\n\n---\n\n".join([student_context_chunk] + general_chunks)
        else:
            context = "\n\n---\n\n".join(general_chunks)

        # ── Step 6: identity note + system prompt ─────────────────────────
        identity_note = ""
        if current_record:
            name = current_record.get("display_name") or "الطالب"
            identity_note = (
                f"\n\nالطالب الذي يحادثك الآن: {name} "
                f"(رقم الهوية: {current_record.get('owner_id')}). "
                f"أجبه عن بياناته مباشرة دون تحفظ، ولا تكشف بيانات أي طالب آخر."
            )

        system = SYSTEM_PROMPT_TEMPLATE.format(context=context) + identity_note

        # ── Step 7: call LLM ──────────────────────────────────────────────
        answer = self._ask_llm(system, question, session_id)
        result = {"answer": answer, "top_chunks": general_chunks}

        # Only reached on the public path (current_record is None); the
        # private-record and privacy-blocked paths return earlier and are
        # never cached.
        if public_turn:
            self._answer_cache.set(cache_key, {"answer": answer, "top_chunks": list(general_chunks)})
        return result

    def chat_with_file(self, question: str, collection_name: str, session_id: str) -> dict:
        if not self._uploaded.has(collection_name):
            return {
                "answer":     f"الملف '{collection_name}' غير موجود. يرجى رفع الملف أولاً.",
                "top_chunks": [],
                "source":     "uploaded_file",
            }

        if not self._uploaded.chunks_of(collection_name):
            return {
                "answer":     "لا تتوفر هذه المعلومة في الملف المُرفق.",
                "top_chunks": [],
                "source":     "uploaded_file",
            }

        # Uploaded files carry no per-student data, so answers are public;
        # cache only when there is no history to fold in (else the answer is
        # conversation-specific).
        cacheable = config.CACHE_ENABLED and not self.get_history(session_id)
        cache_key = self._cache_key("file", question, collection_name) if cacheable else None
        if cacheable:
            cached = self._answer_cache.get(cache_key)
            if cached is not None:
                self.push_history(session_id, question, cached["answer"])
                return dict(cached)

        relevant_chunks = self._uploaded.search_one(question, collection_name)
        context = "\n\n---\n\n".join(relevant_chunks)
        system  = UPLOADED_FILE_SYSTEM_PROMPT.format(context=context)
        answer  = self._ask_llm(system, question, session_id)
        result = {"answer": answer, "top_chunks": relevant_chunks, "source": "uploaded_file"}
        if cacheable:
            self._answer_cache.set(
                cache_key,
                {"answer": answer, "top_chunks": list(relevant_chunks), "source": "uploaded_file"},
            )
        return result

    def chat_with_all_files(
        self,
        question: str,
        session_id: str,
        top_k: int = config.TOP_K,
    ) -> dict:
        """
        Same idea as chat_with_file(), but searches across ALL currently
        uploaded files merged into a single global ranking — the LLM only
        ever sees the best top-K chunks overall.
        """
        trusted_answer = self._trusted_direct_answer(question)
        if trusted_answer:
            self.push_history(session_id, question, trusted_answer)
            return {
                "answer": trusted_answer,
                "top_chunks": [],
                "source": "trusted_fact",
            }

        cacheable = config.CACHE_ENABLED and not self.get_history(session_id)
        cache_key = self._cache_key("all_files", question) if cacheable else None
        if cacheable:
            cached = self._answer_cache.get(cache_key)
            if cached is not None:
                self.push_history(session_id, question, cached["answer"])
                return dict(cached)

        generic_engineering_fee = self._is_generic_engineering_hourly_fee(question)
        relevant_chunks = (
            []
            if self._uploaded.is_empty()
            else self._search_all_for_question(question, top_k)
        )
        context = "\n\n---\n\n".join(relevant_chunks)
        system  = UPLOADED_FILE_SYSTEM_PROMPT.format(context=context)
        if generic_engineering_fee:
            system += """

تعليمات خاصة بهذا السؤال: لم يحدد الطالب المرحلة الدراسية لسعر الساعة في
الهندسة. اعرض بشكل منفصل سعر البكالوريوس وسعر الماجستير/الدراسات العليا إذا
وُجدا في المقاطع أعلاه. لا تخلط بينهما، ولا تخترع قيمة لم ترد في المقاطع.
"""
        answer  = self._ask_llm(system, question, session_id)
        result = {"answer": answer, "top_chunks": relevant_chunks, "source": "uploaded_files_all"}
        if cacheable:
            self._answer_cache.set(
                cache_key,
                {"answer": answer, "top_chunks": list(relevant_chunks), "source": "uploaded_files_all"},
            )
        return result

    def chat_as_student(self, question: str, student_id: str) -> dict:
        """
        Chat for an authenticated student. If they ask about THEIR OWN record
        (status / gpa / rank), answer directly from their profile — looked up
        server-side by student_id, so a student only ever sees their own data,
        and the profile never rides the shared answer cache. Any other question
        goes to the normal university-content search.

        SECURITY: callers pass the student_id extracted from the verified JWT
        (see app.api.deps.get_current_student), never a raw client field — so
        a student can only ever read their OWN profile.
        """
        # Asking about ANOTHER student's private record → refuse outright.
        if privacy.asks_about_other_student(question, student_id):
            self.push_history(student_id, question, privacy.BLOCKED_ANSWER)
            return {"answer": privacy.BLOCKED_ANSWER, "top_chunks": [], "source": "privacy_block"}

        if privacy.wants_own_academic_record(question):
            account = auth.find_account(student_id)
            profile = (account or {}).get("profile") or {}
            if profile:
                answer = privacy.build_status_from_profile(profile)
                self.push_history(student_id, question, answer)
                return {"answer": answer, "top_chunks": [], "source": "student_profile"}

        # Not a personal-record question (or no profile) → university content.
        return self.chat_with_all_files(question, student_id)
