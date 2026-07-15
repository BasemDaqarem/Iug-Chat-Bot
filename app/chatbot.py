"""
IUGChatbot — thin facade that composes the feature services and keeps the
exact public API the rest of the system (console harness, future REST
layer) already relies on. All heavy lifting lives in the feature modules;
this class only orchestrates.
"""

import hashlib
import re
from typing import List

import numpy as np

from app import auth, config, embeddings, file_catalog, privacy, query_rewrite
from app.cache import TTLCache
from app.chunking import SENSITIVE_MARKER
from app.errors import ChatbotError
from app.knowledge_base import KnowledgeBase
from app.llm import chat_completion, stream_completion
from app.log import get_logger
from app.prompts import SYSTEM_PROMPT_TEMPLATE, UPLOADED_FILE_SYSTEM_PROMPT
from app import sessions as sessions_mod
from app.sessions import SessionStore, make_session_store
from app.text_norm import normalize_arabic
from app.uploaded_files import UploadedFilesStore
from app.context_builder import build_private_context
from app.rbac import Principal, Role, prompt_for

log = get_logger("chatbot")


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

    def push_history(self, sid: str, user: str, assistant: str, embedding=None):
        self._sessions.push(sid, user, assistant, embedding)

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

    def _build_user_message(self, question: str, session_id: str):
        """(نص رسالة المستخدم مع الذاكرة المنتقاة، متجه السؤال). مشترك بين
        المسار العادي والبثّ، فالذاكرة تُبنى بنفس الطريقة في الحالتين."""
        history = self.get_history(session_id)
        memory_text, q_vec = self._memory_block(question, history)
        return f"{memory_text}السؤال: {question}", q_vec

    def _ask_llm(self, system: str, question: str, session_id: str) -> str:
        """Shared final step of every chat flow: inject only the RELEVANT
        previous turns (semantic short-term memory) before the question, call
        the LLM, record the turn with its reusable embedding."""
        user_message, q_vec = self._build_user_message(question, session_id)
        answer = self._strip_markdown_tables(chat_completion(system, user_message))
        self.push_history(session_id, question, answer, embedding=q_vec)
        return answer

    def _memory_block(self, question: str, history: list):
        """(نص الذاكرة، متجه السؤال). المتجه يُحسب مرة واحدة هنا ويُخزَّن مع
        الدور عند الحفظ فلا يُعاد حسابه لاحقاً (embed_query نفسه مُكاش، فالنداء
        مجاني عندما سبق للاسترجاع تضمين السؤال ذاته). عند فشل التضمين نعود
        بأمان للسلوك القديم حرفياً: طيّ آخر الأدوار كلها بلا انتقاء."""
        try:
            vec = embeddings.embed_query(question)
        except Exception as exc:
            log.warning("⚠️ تعذّر تضمين السؤال للذاكرة — fallback نصي: %s", exc)
            return self.fmt_history(history), None
        turns = sessions_mod.relevant_turns(history, vec)
        return sessions_mod.format_memory(turns), vec

    _TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")

    @classmethod
    def _strip_markdown_tables(cls, text: str) -> str:
        """واجهة الشات لا تعرض جداول Markdown فتظهر مشوّهة — أي جدول يفلت من
        تعليمات البرومت يُحوَّل هنا حتمياً إلى قائمة نقطية (رؤوس الأعمدة تُدمج
        مع قيم كل صف). تحويل نصي خالص — لا يغيّر أي معلومة."""
        if "|" not in text:
            return text
        lines, out, i = text.split("\n"), [], 0
        while i < len(lines):
            if not cls._TABLE_ROW_RE.match(lines[i]):
                out.append(lines[i]); i += 1; continue
            rows = []
            while i < len(lines) and cls._TABLE_ROW_RE.match(lines[i]):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(set(c) <= set(":- ") for c in cells):   # تجاهل صف الفواصل
                    rows.append(cells)
                i += 1
            if not rows:
                continue
            headers, data = rows[0], rows[1:]
            if not data:                       # جدول بصف واحد → سطر مفرد
                out.append("- " + " — ".join(c for c in headers if c))
                continue
            for row in data:
                first = row[0] if row else ""
                rest = "، ".join(
                    f"{headers[j]}: {row[j]}"
                    for j in range(1, min(len(headers), len(row))) if row[j]
                )
                out.append(f"- **{first}**" + (f" — {rest}" if rest else ""))
        return "\n".join(out)

    _CHUNK_SOURCE_RE = re.compile(r"^\[ملف: (.+?)\]")

    @classmethod
    def _source_recency_note(cls, chunks: List[str]) -> str:
        """عند استرجاع مقاطع من أكثر من ملف، نُلحق بالبرومت تواريخ آخر تحديث
        للمصادر وقاعدة «الأحدث يفوز» — فيُحسم تعارض المعلومات (رسوم قديمة
        مقابل جديدة) لصالح الملف الأحدث. مصدر واحد ⇒ لا تعارض ⇒ لا إضافة."""
        names: List[str] = []
        for chunk in chunks:
            m = cls._CHUNK_SOURCE_RE.match(chunk)
            if m and m.group(1) not in names:
                names.append(m.group(1))
        if len(names) < 2:
            return ""
        dates = file_catalog.recency_map()
        dated = [(name, (dates.get(name) or "")[:10]) for name in names]
        if not any(date for _, date in dated):
            return ""  # لا تواريخ مسجّلة إطلاقاً — لا أساس للتفضيل
        dated.sort(key=lambda item: item[1], reverse=True)
        lines = "\n".join(f"- {name}: {date or 'غير معروف'}" for name, date in dated)
        return f"""

تواريخ إدخال المصادر المسترجعة إلى النظام (الأحدث أولاً):
{lines}

⚠️ عند تعارض معلومة (رقم أو رسوم أو شرط) بين مصدرين مما سبق، اعتمد قيمة
المصدر الأحدث إدخالاً. هذه تواريخ إدخال للنظام للترجيح الداخلي فقط — لا
تذكرها في إجابتك كأنها تاريخ إصدار المعلومة أو «آخر تحديث للنشرة».
المصدر مجهول التاريخ يُعامل كالأقدم.
"""

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

    def _search_all_for_question(
        self,
        question: str,
        top_k: int,
        allowed_collections: set[str] | None = None,
    ) -> List[str]:
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
        if allowed_collections is None:
            batches = [self._uploaded.search_all(query, per_query_k) for query in queries]
        else:
            batches = [
                self._uploaded.search_all(
                    query,
                    per_query_k,
                    allowed_collections=allowed_collections,
                )
                for query in queries
            ]

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
        *,
        private_context: str | None = None,
        allowed_collections: set[str] | None = None,
        role_prompt: str | None = None,
        retrieval_question: str | None = None,
    ) -> dict:
        """
        Same idea as chat_with_file(), but searches across ALL currently
        uploaded files merged into a single global ranking — the LLM only
        ever sees the best top-K chunks overall.

        retrieval_question, when given, replaces the literal question FOR THE
        SEARCH ONLY (e.g. «رئيس قسمي» expanded with the student's major); the
        LLM, history, and cache always see what the student actually typed.
        """
        prepared = self._prepare_all_files(
            question, session_id, top_k,
            private_context=private_context,
            allowed_collections=allowed_collections,
            role_prompt=role_prompt,
            retrieval_question=retrieval_question,
        )
        if prepared["kind"] == "instant":
            return prepared["result"]

        answer = self._ask_llm(prepared["system"], question, session_id)
        result = {"answer": answer, "top_chunks": prepared["chunks"], "source": prepared["source"]}
        if prepared["cache_key"]:
            self._answer_cache.set(
                prepared["cache_key"],
                {"answer": answer, "top_chunks": list(prepared["chunks"]),
                 "source": "uploaded_files_all"},
            )
        return result

    def _prepare_all_files(
        self,
        question: str,
        session_id: str,
        top_k: int = config.TOP_K,
        *,
        private_context: str | None = None,
        allowed_collections: set[str] | None = None,
        role_prompt: str | None = None,
        retrieval_question: str | None = None,
    ) -> dict:
        """Everything an all-files turn needs BEFORE the LLM call, shared by the
        blocking (`chat_with_all_files`) and streaming (`stream_answer`) paths so
        the two can never drift. Returns either:
          {"kind": "instant", "result": {...}}     — no LLM needed (trusted /
                                                      admission / cache hit)
          {"kind": "llm", "system", "chunks", "source", "cache_key"}"""
        trusted_answer = self._trusted_direct_answer(question)
        if trusted_answer:
            self.push_history(session_id, question, trusted_answer)
            return {"kind": "instant", "result": {
                "answer": trusted_answer, "top_chunks": [], "source": "trusted_fact"}}

        admission = self._uploaded.resolve_admission(question, allowed_collections)
        if admission is not None:
            self.push_history(session_id, question, admission.answer)
            return {"kind": "instant", "result": {
                "answer": admission.answer, "top_chunks": admission.top_chunks,
                "source": "structured_admission"}}

        # A student-specific prompt must never read from or write to the
        # answer cache shared by public users.
        history = self.get_history(session_id)
        cacheable = (
            config.CACHE_ENABLED
            and private_context is None
            and not history
        )
        access_key = "*" if allowed_collections is None else ",".join(sorted(allowed_collections))
        cache_key = self._cache_key("all_files", question, access_key) if cacheable else None
        if cacheable:
            cached = self._answer_cache.get(cache_key)
            if cached is not None:
                self.push_history(session_id, question, cached["answer"])
                return {"kind": "instant", "result": dict(cached)}

        # Retrieval sees a context-aware query (follow-ups inherit the previous
        # turn's topic, colloquial verbs gain their canonical data nouns);
        # the LLM still receives the literal question.
        search_question = query_rewrite.add_canonical_terms(
            query_rewrite.with_history_context(retrieval_question or question, history)
        )

        generic_engineering_fee = self._is_generic_engineering_hourly_fee(search_question)
        if self._uploaded.is_empty():
            relevant_chunks = []
        else:
            relevant_chunks = self._search_all_for_question(
                search_question, top_k, allowed_collections
            )
            # مقارنة معدل الثانوية بمفاتيح القبول سؤال تجميعي: يحتاج جدول
            # المفاتيح كاملاً لا أقرب مقاطعه فقط. نوجّه حصة استرجاع إضافية
            # للملفات التي اسمها يدل على القبول/المعدلات (توجيه معتمد على
            # البيانات — أي ملف يسمّيه الأدمن كذلك يُلتقط تلقائياً).
            if query_rewrite.has_admission_intent(search_question):
                names = {f["collection"] for f in self._uploaded.list_files()}
                if allowed_collections is not None:
                    names &= set(allowed_collections)
                focus = {n for n in names
                         if "قبول" in normalize_arabic(n) or "معدلات" in normalize_arabic(n)}
                if focus:
                    focused = self._uploaded.search_all(
                        search_question, config.TOP_K, allowed_collections=focus
                    )
                    seen = set(focused)
                    relevant_chunks = focused + [c for c in relevant_chunks if c not in seen]
        context = "\n\n---\n\n".join(relevant_chunks)
        system  = UPLOADED_FILE_SYSTEM_PROMPT.format(context=context)
        system += self._source_recency_note(relevant_chunks)  # «الأحدث يفوز» عند التعارض
        if role_prompt:
            system += "\n\nسياسة الصلاحية الملزمة:\n" + role_prompt
        if private_context is not None:
            system += f"""

{private_context}

تعليمات السياق الخاص:
- افهم سؤال الطالب كاملاً؛ لا تتوقف عند كلمة مثل «معدلي» أو «ترتيبي».
- استخدم بيانات الطالب فقط عندما تكون مرتبطة بالسؤال، ولا تسرد الملف كاملاً إلا إذا طلبه.
- إذا جمع السؤال بين بياناته وموضوع جامعي كالمنح، فادمج بياناته مع المقاطع العامة المسترجعة للإجابة عن المقصود.
- ⚠️ قاعدة ملزمة: إذا سأل الطالب عن إجراء يخص كليته (كالتدريب الميداني أو مشروع التخرج)
  ولم تجد في المقاطع معلومات لكليته هو تحديداً (قارن مع تخصصه في بياناته أعلاه)، فيجب أن
  تبدأ إجابتك حرفياً بتوضيح ذلك، مثل: «لا تتوفر لدي تفاصيل التدريب الميداني لكليتك
  (الهندسة — قسم X)، ويُنصح بمراجعة كليتك مباشرة.» وبعدها فقط يجوز أن تعرض المتوفر عن
  كلية أخرى موصوفاً صراحة بأنه يخص تلك الكلية الأخرى كمرجع عام — لا كإجراءات كليته.
- لا تذكر أو تستنتج بيانات طالب آخر.
"""
        if generic_engineering_fee:
            system += """

تعليمات خاصة بهذا السؤال: لم يحدد الطالب المرحلة الدراسية لسعر الساعة في
الهندسة. اعرض بشكل منفصل سعر البكالوريوس وسعر الماجستير/الدراسات العليا إذا
وُجدا في المقاطع أعلاه. لا تخلط بينهما، ولا تخترع قيمة لم ترد في المقاطع.
"""
        source = "student_context_rag" if private_context is not None else "uploaded_files_all"
        return {"kind": "llm", "system": system, "chunks": relevant_chunks,
                "source": source, "cache_key": cache_key}

    def chat_as_student(self, question: str, student_id: str) -> dict:
        """
        Chat for an authenticated student. Their approved profile fields are
        fetched server-side and supplied as private LLM context alongside the
        public university retrieval. There is no keyword shortcut, so compound
        questions are processed in full.

        SECURITY: callers pass the student_id extracted from the verified JWT
        (see app.api.deps.get_current_student), never a raw client field — so
        a student can only ever read their OWN profile.
        """
        verdict, payload = self._student_turn(question, student_id)
        if verdict == "block":
            self.push_history(student_id, question, payload)
            return {"answer": payload, "top_chunks": [], "source": "privacy_block"}
        return self.chat_with_all_files(question, student_id, **payload)

    def _student_turn(self, question: str, student_id: str):
        """Shared student setup for both blocking and streaming replies.

        Returns ("block", refusal_text) when the question targets another
        student's private record, else ("ok", kwargs) with the private profile
        context, role policy, and major-expanded retrieval query.

        SECURITY: student_id comes from the verified JWT (app.api.deps), never a
        client field — a student can only ever read their OWN profile."""
        if privacy.asks_about_other_student(question, student_id):
            return "block", privacy.BLOCKED_ANSWER
        account = auth.find_account(student_id)
        profile = (account or {}).get("profile") or {}
        private_context = (
            privacy.format_authenticated_profile_context(profile) if profile else ""
        )
        return "ok", {
            "private_context": private_context,
            "role_prompt": prompt_for(Principal(student_id, Role.STUDENT)),
            # «رئيس قسمي» / «التدريب الميداني» → the search also sees the
            # student's actual major.
            "retrieval_question": query_rewrite.personalize_query(
                question, profile.get("major")
            ),
        }

    def stream_answer(self, question: str, principal: Principal, *, allowed_collections):
        """Streaming twin of chat_as_principal: yields the answer token-by-token.

        Mirrors chat_as_principal EXACTLY — same privacy refusal, same
        RBAC-filtered `allowed_collections` (so streaming can't reach a
        collection the blocking path would hide), same private context, same
        major-expanded retrieval — then streams the LLM content instead of
        blocking on it. Instant paths (privacy block / trusted fact / structured
        admission / cache hit) yield their whole answer in one chunk. Once the
        stream completes it records history (with the reusable question
        embedding) and fills the cache just like the blocking path, so memory
        and behavior are identical; only delivery differs.

        Errors after the first byte surface as visible text — the HTTP status
        was already sent and can no longer become a clean 502."""
        if principal.role == Role.STUDENT and privacy.asks_about_other_student(
            question, principal.subject
        ):
            self.push_history(principal.subject, question, privacy.BLOCKED_ANSWER)
            yield privacy.BLOCKED_ANSWER
            return

        private_context = None
        retrieval_question = question
        if principal.role != Role.GUEST:
            account = auth.find_account(principal.subject)
            private_context = build_private_context(principal, question, account=account)
            if principal.role == Role.STUDENT:
                major = ((account or {}).get("profile") or {}).get("major")
                retrieval_question = query_rewrite.personalize_query(question, major)

        prepared = self._prepare_all_files(
            question, principal.subject,
            private_context=private_context,
            allowed_collections=allowed_collections,
            role_prompt=prompt_for(principal),
            retrieval_question=retrieval_question,
        )
        if prepared["kind"] == "instant":
            yield prepared["result"]["answer"]
            return

        user_message, q_vec = self._build_user_message(question, principal.subject)
        parts: List[str] = []
        try:
            for chunk in stream_completion(prepared["system"], user_message):
                parts.append(chunk)
                yield chunk
        except ChatbotError as exc:
            yield ("\n\n⚠️ " + exc.message) if parts else ("⚠️ " + exc.message)
            return

        # ما بُثّ للعميل يعالجه تحويل الواجهة؛ هنا ننظّف النسخة المحفوظة
        # (سجل + كاش) من أي جدول كي لا يُعاد تقديمه لاحقاً كما هو.
        answer = self._strip_markdown_tables("".join(parts).strip())
        if answer:
            self.push_history(principal.subject, question, answer, embedding=q_vec)
            if prepared["cache_key"]:
                self._answer_cache.set(
                    prepared["cache_key"],
                    {"answer": answer, "top_chunks": list(prepared["chunks"]),
                     "source": "uploaded_files_all"},
                )

    def chat_as_principal(
        self,
        question: str,
        principal: Principal,
        *,
        allowed_collections: set[str],
    ) -> dict:
        """Unified role-aware chat path used by the new API endpoints."""
        if principal.role == Role.STUDENT and privacy.asks_about_other_student(
            question, principal.subject
        ):
            self.push_history(principal.subject, question, privacy.BLOCKED_ANSWER)
            return {
                "answer": privacy.BLOCKED_ANSWER,
                "top_chunks": [],
                "source": "privacy_block",
            }

        private_context = None
        retrieval_question = question
        if principal.role != Role.GUEST:
            account = auth.find_account(principal.subject)
            private_context = build_private_context(principal, question, account=account)
            if principal.role == Role.STUDENT:
                major = ((account or {}).get("profile") or {}).get("major")
                retrieval_question = query_rewrite.personalize_query(question, major)
        return self.chat_with_all_files(
            question,
            principal.subject,
            private_context=private_context,
            allowed_collections=allowed_collections,
            role_prompt=prompt_for(principal),
            retrieval_question=retrieval_question,
        )
