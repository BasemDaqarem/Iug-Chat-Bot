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

from app import (
    answer_check,
    auth,
    config,
    embeddings,
    file_catalog,
    privacy,
    query_rewrite,
    retrieval,
)
from app import rerank as rerank_mod
from app.conversation_frame import build_query_plan
from app.evidence_contract import build_evidence_contract, missing_field_query
from app.domain_router import project_structured_evidence, route_query
from app.cache import TTLCache
from app.chunking import SENSITIVE_MARKER
from app.errors import ChatbotError
from app.knowledge_base import KnowledgeBase
from app.llm import chat_completion, stream_completion
from app.log import get_logger
from app.prompts import (
    PromptContext,
    PromptRoute,
    build_system_prompt,
)
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

    @staticmethod
    def _history_without_assistant_claims(history: list) -> list:
        """في حوارات القبول، القيود تأتي من أسئلة المستخدم لا من جواب سابق.

        إعادة حقن جواب ناقص («الأدبي = الآداب فقط») جعل النموذج يكرره رغم
        وجود جدول أحدث كامل. نبقي كل سؤال وتوقيته ومتجهه، ونستبدل نص المساعد
        بتنبيه محايد؛ الاسترجاع والسياق الحواري لا يضيعان، والهلوسة لا تتكاثر.
        """
        return [
            {
                **turn,
                "assistant": (
                    "[لا تعتمد هذه الإجابة السابقة كدليل؛ أعد الاستخراج من "
                    "سياق الجامعة الحالي.]"
                ),
            }
            for turn in history
        ]

    def _build_user_message(
        self,
        question: str,
        session_id: str,
        client_history=None,
        *,
        user_constraints_only: bool = False,
    ):
        """(نص رسالة المستخدم مع الذاكرة المنتقاة، متجه السؤال). مشترك بين
        المسار العادي والبثّ، فالذاكرة تُبنى بنفس الطريقة في الحالتين.

        client_history: سياق يحمله متصفح الزائر (لا جلسات مخزّنة للزوار) —
        أدواره بلا متجهات محفوظة، فتُطوى نصاً كاملةً بلا انتقاء دلالي."""
        if client_history is not None:
            prompt_history = (
                self._history_without_assistant_claims(client_history)
                if user_constraints_only else client_history
            )
            memory_text = sessions_mod.format_memory(prompt_history)
            try:
                q_vec = embeddings.embed_query(question)
            except Exception:
                q_vec = None
            return f"{memory_text}السؤال: {question}", q_vec
        history = self.get_history(session_id)
        memory_text, q_vec = self._memory_block(
            question, history,
            user_constraints_only=user_constraints_only,
        )
        return f"{memory_text}السؤال: {question}", q_vec

    def _complete_llm(
        self,
        system: str,
        user_message: str,
        *,
        max_tokens: int | None = None,
    ) -> str:
        """Generate and normalize an answer without mutating chat history."""
        return self._strip_markdown_tables(
            chat_completion(system, user_message, max_tokens=max_tokens)
        )

    def _ask_llm(self, system: str, question: str, session_id: str, client_history=None) -> str:
        """Shared final step of every chat flow: inject only the RELEVANT
        previous turns (semantic short-term memory) before the question, call
        the LLM, record the turn with its reusable embedding."""
        user_message, q_vec = self._build_user_message(question, session_id, client_history)
        answer = self._complete_llm(system, user_message)
        self.push_history(session_id, question, answer, embedding=q_vec)
        return answer

    def _memory_block(
        self,
        question: str,
        history: list,
        *,
        user_constraints_only: bool = False,
    ):
        """(نص الذاكرة، متجه السؤال). المتجه يُحسب مرة واحدة هنا ويُخزَّن مع
        الدور عند الحفظ فلا يُعاد حسابه لاحقاً (embed_query نفسه مُكاش، فالنداء
        مجاني عندما سبق للاسترجاع تضمين السؤال ذاته). عند فشل التضمين نعود
        بأمان للسلوك القديم حرفياً: طيّ آخر الأدوار كلها بلا انتقاء."""
        try:
            vec = embeddings.embed_query(question)
        except Exception as exc:
            log.warning("⚠️ تعذّر تضمين السؤال للذاكرة — fallback نصي: %s", exc)
            prompt_history = (
                self._history_without_assistant_claims(history)
                if user_constraints_only else history
            )
            return self.fmt_history(prompt_history), None
        turns = sessions_mod.relevant_turns(history, vec)
        if user_constraints_only:
            turns = self._history_without_assistant_claims(turns)
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
    _METADATA_DATE_RE = re.compile(
        r"(?:last_verified|آخر[_\s]+تحقق|تاريخ[_\s]+التحقق)"
        r"\s*[:：]\s*((?:19|20)\d{2}(?:[-/]\d{1,2}[-/]\d{1,2})?)",
        re.IGNORECASE,
    )

    @staticmethod
    def _anchor_active_constraints(user_message: str, prepared: dict) -> str:
        """ضع حالة القبول الأحدث قرب السؤال كي لا تطغى إجابة قديمة عليها."""
        metadata = prepared.get("retrieval_metadata", {})
        if not metadata.get("admission_intent"):
            return user_message
        constraints = metadata.get("active_academic_constraints") or {}
        lines = []
        if constraints.get("branch") is not None:
            lines.append(f"- الفرع الحالي: {constraints['branch']}")
        if constraints.get("rate") is not None:
            lines.append(f"- معدل الثانوية الحالي: {constraints['rate']:g}%")
        if constraints.get("degree") is not None:
            lines.append(f"- المرحلة الحالية: {constraints['degree']}")
        if not lines:
            return user_message
        return (
            user_message
            + "\n\nحالة المستخدم الأحدث والملزمة لهذا السؤال:\n"
            + "\n".join(lines)
            + "\nأي فرع أو معدل أقدم في سجل المحادثة ملغى عند التعارض."
            + "\nقوائم المساعد السابقة ليست دليلاً وقد تكون ناقصة؛ أعد بناء "
            "القائمة من جدول الجامعة الموجود في سياق النظام."
        )

    @classmethod
    def _source_metadata_fallback(cls, chunks: List[str]) -> str:
        """استخراج اسم أول سجل مسترجع وتاريخه من السجل نفسه فقط."""
        for chunk in chunks:
            source_match = cls._CHUNK_SOURCE_RE.match(chunk)
            if not source_match:
                continue
            source = source_match.group(1).strip()
            date_match = cls._METADATA_DATE_RE.search(chunk)
            if date_match:
                return (
                    f"المصدر المسترجع المرتبط بالإجابة هو «{source}»، "
                    f"وتاريخ التحقق المذكور في السجل نفسه هو "
                    f"{date_match.group(1)}."
                )
            return (
                f"المصدر المسترجع المرتبط بالإجابة هو «{source}»، "
                "لكن تاريخ التحقق غير مذكور في المقطع نفسه."
            )
        return (
            "اسم المصدر وتاريخ التحقق غير واردين بوضوح في المقاطع "
            "المسترجعة، لذلك لا أريد تخمينهما."
        )

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
            # «الموقع الجغرافي/العنوان/وين مكانها» سؤال مكان لا رابط — يُترك
            # للاسترجاع (كان الاختصار يخطفه ويجيب بالرابط الإلكتروني).
            and not any(term in normalized for term in ("جغراف", "عنوان", "مكان", "وين"))
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
        """Legacy main-corpus path; every final answer is generated by the LLM."""
        chunk_meta = self._kb.chunk_meta
        current_record = privacy.find_sensitive_record(chunk_meta, session_id)
        relevant_chunks = self._kb.search_for(
            question=question,
            session_id=session_id,
            top_k=config.TOP_K,
            threshold=config.SIM_THRESHOLD,
        )
        sensitive_prefix = f"[{self.SENSITIVE_MARKER}|"
        general_chunks = [
            chunk for chunk in relevant_chunks
            if not chunk.startswith(sensitive_prefix)
        ]
        history = self.get_history(session_id)
        frame, plan = build_query_plan(question, history)
        authoritative: list[str] = []
        dynamic: list[str] = []
        route = PromptRoute.GENERAL

        if privacy.is_academic_status_question(question) and current_record:
            authoritative.append(
                "[حقيقة خاصة مصرح بها للمستخدم الحالي]\n"
                + privacy.build_status_from_sensitive_record(current_record["raw"])
            )
            route = PromptRoute.PRIVATE_STUDENT
        elif privacy.is_ranking_question(question):
            other_names = privacy.other_sensitive_display_names(chunk_meta, session_id)
            if privacy.mentions_other_student(question, other_names):
                # Do not expose or retrieve the other student's record. The LLM
                # receives only a policy directive and must phrase the refusal.
                general_chunks = []
                authoritative.append(
                    "[سياسة خصوصية ملزمة] الطلب يتعلق ببيانات طالب آخر؛ "
                    "يجب رفض كشف المعدل أو الترتيب أو أي بيانات خاصة."
                )
                dynamic.append("صغ رفضاً موجزاً دون ذكر أي بيانات شخصية.")
                route = PromptRoute.PRIVACY_REFUSAL
                frame.intent = "privacy"
                frame.domains = ["privacy"]
                frame.requested_fields = ["privacy"]
                frame.rate_type = None
                plan.intent = "privacy"
                plan.domains = ["privacy"]
                plan.expected_answer_type = "safe_refusal"

        private_text = ""
        if current_record and route != PromptRoute.PRIVACY_REFUSAL:
            private_text = privacy.format_sensitive_record_context(current_record)

        evidence = authoritative + general_chunks
        contract = build_evidence_contract(
            plan, frame, general_chunks, authoritative_evidence=authoritative
        )
        system = build_system_prompt(PromptContext(
            route=route,
            evidence="\n\n---\n\n".join(evidence),
            private_context=private_text,
            conversation_frame=frame.prompt_block(),
            evidence_contract=contract.prompt_block(),
            dynamic_instructions=dynamic,
        ))
        answer = self._ask_llm(system, question, session_id)
        source = (
            "privacy_policy_llm" if route == PromptRoute.PRIVACY_REFUSAL
            else "main_corpus_llm"
        )
        return {
            "answer": answer,
            "top_chunks": general_chunks,
            "source": source,
            "retrieval_metadata": {
                "llm_always_answer": True,
                "query_plan": plan.as_metadata(),
                "conversation_frame": frame.as_metadata(),
                "evidence_contract": contract.as_metadata(),
                "answer_cache_bypassed": True,
            },
        }

    def chat_with_file(self, question: str, collection_name: str, session_id: str) -> dict:
        """Single-file path; missing files are also explained by the LLM."""
        history = self.get_history(session_id)
        frame, plan = build_query_plan(question, history)
        dynamic: list[str] = []
        if not self._uploaded.has(collection_name):
            relevant_chunks: list[str] = []
            dynamic.append(
                f"الملف المحدد «{collection_name}» غير موجود؛ اذكر ذلك بوضوح ولا تخترع محتواه."
            )
        else:
            relevant_chunks = self._uploaded.search_one(question, collection_name)
            if not relevant_chunks:
                dynamic.append(
                    "الملف موجود لكن لم يظهر فيه دليل مباشر؛ صرّح بنقص المعلومة دون تخمين."
                )
        contract = build_evidence_contract(plan, frame, relevant_chunks)
        system = build_system_prompt(PromptContext(
            route=PromptRoute.UPLOADED_FILES,
            evidence="\n\n---\n\n".join(relevant_chunks),
            conversation_frame=frame.prompt_block(),
            evidence_contract=contract.prompt_block(),
            dynamic_instructions=dynamic,
        ))
        answer = self._ask_llm(system, question, session_id)
        return {
            "answer": answer,
            "top_chunks": relevant_chunks,
            "source": "uploaded_file_llm",
            "retrieval_metadata": {
                "llm_always_answer": True,
                "query_plan": plan.as_metadata(),
                "conversation_frame": frame.as_metadata(),
                "evidence_contract": contract.as_metadata(),
                "answer_cache_bypassed": True,
            },
        }

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
        client_history: list | None = None,
        safety_directive: str | None = None,
        authoritative_evidence: list[str] | None = None,
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
            client_history=client_history,
            safety_directive=safety_directive,
            authoritative_evidence=authoritative_evidence,
        )
        # ابنِ رسالة الذاكرة مرة واحدة، ولا تحفظ المحاولة الأولى قبل أن يجتاز
        # الجواب الفاحص. سابقاً كانت الإجابة المرفوضة والتصحيح كلاهما يدخلان
        # السجل فتلوّث الأولى فهم المتابعات التالية.
        user_message, q_vec = self._build_user_message(
            question,
            session_id,
            client_history,
            user_constraints_only=bool(
                prepared.get("retrieval_metadata", {}).get("admission_intent")
            ),
        )
        user_message = self._anchor_active_constraints(user_message, prepared)
        generation_max_tokens = prepared.get("generation_max_tokens")
        answer = self._complete_llm(
            prepared["system"],
            user_message,
            max_tokens=generation_max_tokens,
        )

        # الفاحص الحتمي (خطة التحسين م3): حقائق دقيقة غير مسندة/خرق
        # استبعاد/خرق مرحلة/ادعاء تنفيذ فعل غير متاح.
        # ← إعادة توليد واحدة بتعليمة تصحيحية. صفر كلفة في المسار السليم؛
        # يغطي المسار المحجوب فقط (البث أرسل حروفه فلا يُسحب).
        validation_sources = list(prepared["chunks"]) + [question]
        active_constraints = (
            prepared.get("retrieval_metadata", {})
            .get("active_academic_constraints", {})
        )
        active_rate = active_constraints.get("rate")
        active_branch = active_constraints.get("branch")
        if active_branch is not None:
            validation_sources.append(
                f"الفرع الحالي الذي ذكره المستخدم: {active_branch}"
            )
        if active_rate is not None:
            # قيود مذكورة في دور سابق (مثل «معدلي 85») موجودة في رسالة
            # الذاكرة، ويجب أن تُعد دليلاً رقمياً على تكرار رقم المستخدم.
            # نضيف الرقم فقط لا نص الاستعلام، كي لا يتحول سؤال المستخدم نفسه
            # إلى «دليل» على رابط أو مسار إجرائي.
            validation_sources.append(f"معدل الثانوية الذي ذكره المستخدم: {active_rate:g}%")

        def _answer_issues(value: str) -> list[str]:
            return answer_check.problems(
                value,
                sources=validation_sources,
                excluded=prepared.get("excluded", []),
                asked_level=prepared.get("asked_level"),
                question=question,
            )

        issues = _answer_issues(answer)
        post_retry_issues: list[str] = []
        safety_fallback = False
        if issues:
            source_metadata_gap = (
                query_rewrite.is_source_metadata_followup(question)
                and any(
                    "ربطتَ مصدراً بتاريخ" in issue
                    or "سنة مؤرخة غير موجودة" in issue
                    for issue in issues
                )
            )
            hard_exact_gap = any(
                "ليس رابطاً" in issue
                or "اسم المورد نفسه غير موجود" in issue
                or "رابط دليل/خطوات" in issue
                for issue in issues
            )
            if source_metadata_gap:
                # سؤال metadata يمكن حسمه من ترويسة أول سجل مسترجع: لا
                # نستهلك محاولة ثانية كي يعيد النموذج استعارة تاريخ من سجل آخر.
                post_retry_issues = list(issues)
                answer = self._source_metadata_fallback(prepared["chunks"])
                safety_fallback = True
            elif hard_exact_gap:
                # لا معنى لطلب توليد ثانٍ حين أثبت الفاحص أن «الرابط» مجرد
                # معرّف داخلي أو أن المورد نفسه غائب من الدليل. جواب صادق
                # فوري أوفر وأأمن من محاولة أخرى قد تعيد التخمين.
                post_retry_issues = list(issues)
                answer = (
                    "المعلومة أو المسار الدقيق المطلوب غير وارد بوضوح في "
                    "المقاطع المسترجعة، لذلك لا أريد تخمينه. تحقّق من المصدر "
                    "الرسمي أو الجهة الجامعية المختصة."
                )
                safety_fallback = True
            else:
                log.info(
                    "🛡️ الفاحص رفض الجواب (%d مشكلة) — إعادة توليد واحدة.",
                    len(issues),
                )
                corrective = (
                    prepared["system"]
                    + "\n\n⚠️ تصحيح إلزامي لمحاولتك السابقة:\n- "
                    + "\n- ".join(issues)
                )
                answer = self._complete_llm(
                    corrective,
                    user_message,
                    max_tokens=generation_max_tokens,
                )
                post_retry_issues = _answer_issues(answer)
            unsafe_exact = query_rewrite.requires_direct_evidence(question) or any(
                "رابطاً/بريداً/هاتفاً/سنة" in issue
                or "ليس رابطاً" in issue
                or "رابط دليل/خطوات" in issue
                for issue in post_retry_issues
            )
            if post_retry_issues and unsafe_exact and not safety_fallback:
                # لا نستهلك نداءً ثالثاً ولا نسمح لمحاولة تصحيح فاشلة أن
                # تُرسل رابطاً/مساراً مخمناً. هذا fallback عام لأي مورد دقيق،
                # لا يعرف إجابة السؤال ولا اسماً أو ملفاً بعينه.
                answer = (
                    self._source_metadata_fallback(prepared["chunks"])
                    if query_rewrite.is_source_metadata_followup(question)
                    else (
                        "المعلومة أو المسار الدقيق المطلوب غير وارد بوضوح في "
                        "المقاطع المسترجعة، لذلك لا أريد تخمينه. تحقّق من المصدر "
                        "الرسمي أو الجهة الجامعية المختصة."
                    )
                )
                safety_fallback = True

        # لا يدخل الذاكرة إلا الجواب النهائي الذي سيصل للمستخدم.
        self.push_history(session_id, question, answer, embedding=q_vec)

        retrieval_metadata = dict(prepared.get("retrieval_metadata", {}))
        retrieval_metadata["answer_check_retry"] = bool(issues)
        retrieval_metadata["answer_check_issues"] = list(issues)
        retrieval_metadata["answer_check_post_retry_issues"] = list(
            post_retry_issues
        )
        retrieval_metadata["answer_check_safety_fallback"] = safety_fallback
        result = {
            "answer": answer,
            "top_chunks": prepared["chunks"],
            "source": prepared["source"],
            "retrieval_metadata": retrieval_metadata,
        }
        if prepared["cache_key"]:
            self._answer_cache.set(
                prepared["cache_key"],
                {"answer": answer, "top_chunks": list(prepared["chunks"]),
                 "source": "uploaded_files_all",
                 "retrieval_metadata": retrieval_metadata},
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
        client_history: list | None = None,
        safety_directive: str | None = None,
        authoritative_evidence: list[str] | None = None,
    ) -> dict:
        """Everything an all-files turn needs BEFORE the LLM call, shared by the
        blocking (`chat_with_all_files`) and streaming (`stream_answer`) paths so
        the two can never drift. Deterministic services prepare evidence only;
        the returned plan always has kind="llm" so every user turn reaches the LLM."""
        # Every route ends in one LLM generation.  Deterministic components
        # may prepare authoritative evidence, but never return the final answer.
        history = client_history if client_history is not None else self.get_history(session_id)
        frame, plan = build_query_plan(
            question,
            history,
            retrieval_question=retrieval_question,
        )
        if safety_directive:
            frame.intent = "privacy"
            frame.domains = ["privacy"]
            frame.requested_fields = ["privacy"]
            frame.rate_type = None
            plan.intent = "privacy"
            plan.domains = ["privacy"]
            plan.expected_answer_type = "safe_refusal"
            plan.needs_reranking = False
            plan.needs_query_expansion = False
        domain_route = route_query(plan, frame)
        authoritative = list(authoritative_evidence or [])
        structured_chunks: list[str] = []
        structured_source: str | None = None

        trusted_answer = self._trusted_direct_answer(question)
        if trusted_answer:
            authoritative.append(
                "[حقيقة مرجعية موثوقة؛ صغها جواباً مناسباً للسؤال]\n"
                + trusted_answer
            )
            structured_source = "trusted_fact_llm"

        admission = self._uploaded.resolve_admission(
            plan.standalone_query, allowed_collections
        )
        if admission is not None:
            authoritative.append(
                "[نتيجة قبول منظمة محسوبة من حقائق الجامعة؛ لا تعِد حسابها من نص مجاور]\n"
                + admission.answer
            )
            structured_chunks.extend(admission.top_chunks)
            structured_source = "structured_admission_llm"

        # The embedding/retrieval caches remain useful, but serving a cached
        # final answer would violate the all-questions-to-LLM requirement.
        cacheable = (
            config.CACHE_ENABLED
            and config.ANSWER_CACHE_ENABLED
            and not config.LLM_ALWAYS_ANSWER
            and private_context is None
            and not history
            and not query_rewrite.has_reference_tokens(question)
        )
        access_key = "*" if allowed_collections is None else ",".join(sorted(allowed_collections))
        cache_key = self._cache_key("all_files", question, access_key) if cacheable else None

        base_question = query_rewrite.add_canonical_terms(
            query_rewrite.positive_query(retrieval_question or question)
        )
        search_question = plan.standalone_query
        active_constraints = {
            "branch": frame.branch,
            "rate": frame.rate,
            "degree": frame.degree_level,
        }
        complete_list_requested = plan.is_list_question
        if complete_list_requested:
            search_question = query_rewrite.add_coverage_terms(search_question)
        admission_intent = query_rewrite.inherits_admission_intent(
            retrieval_question or question, base_question, search_question
        )
        coverage_requested = (
            admission_intent
            or complete_list_requested
            or domain_route.use_wide_retrieval
        )
        target_k = max(top_k, config.COVERAGE_TOP_K) if coverage_requested else top_k
        rerank_requested = (
            config.RERANK_ENABLED
            and domain_route.use_reranker
            and not complete_list_requested
        )

        generic_engineering_fee = self._is_generic_engineering_hourly_fee(search_question)
        admission_table = False
        admission_digest = False
        admission_faculties: list[str] = []
        rerank_guard_passed = False
        rerank_attempted = False
        rerank_status = "not_requested"
        role_focus_applied = False
        # لا نوسّع كل سؤال عند تفعيل الفلاغ: القوائم تأخذ ميزانية تغطية،
        # والأسئلة الدقيقة المختارة وحدها تأخذ مرشحي الـreranker.
        fetch_k = (
            max(top_k, config.RERANK_CANDIDATES)
            if rerank_requested
            else target_k
        )
        skip_general_retrieval = bool(authoritative) and not plan.is_compound
        if safety_directive:
            relevant_chunks = []
        elif skip_general_retrieval:
            relevant_chunks = list(structured_chunks)
        elif self._uploaded.is_empty():
            relevant_chunks = []
        else:
            relevant_chunks = self._search_all_for_question(
                search_question, fetch_k, allowed_collections
            )
            # سلسلة السياق سلاح ذو حدين: تُنقذ المتابعات الحقيقية («كم هيكلف؟»)
            # لكنها عند تغيير الموضوع تُغرق البحث بموضوع الدور السابق — ثبت
            # حياً: «مين رئيس الجامعة؟» بعد سؤال رسوم أعادت مقاطع رسوم فقط
            # فأنكر البوت معلومة موجودة. لذا يُبحث بالسؤال الخام أيضاً وتُقدَّم
            # نتائجه (موضوع السائل الحالي لا يُزاحَم أبداً)، وتبقى نتائج
            # السياق بعدها للمتابعات. (كلفة إضافية شبه معدومة: embed_query
            # مُكاش، والسؤال بلا مرادفات عامية يطابق متجه الذاكرة المحسوب أصلاً.)
            # استثناء: سؤال الإحالة الخالص («اذكرهم») بحثه الخام ضجيج —
            # نتائج السياق وحدها هي الصواب، فلا بحث مزدوجاً له.
            if search_question != base_question and \
                    not query_rewrite.is_pure_reference(retrieval_question or question) and \
                    not complete_list_requested and \
                    not query_rewrite.is_source_metadata_followup(
                        retrieval_question or question
                    ):
                contextual = list(relevant_chunks)
                primary = self._search_all_for_question(
                    base_question, fetch_k, allowed_collections
                )
                # سقف لإضافات السياق: البرومت المتضخم في المحادثات الطويلة
                # ثبت أنه يفكك أمانة الخطوات الإجرائية في إجابات الموديل.
                extra_cap = max(2, target_k // 2)
                # احجز دائماً جزءاً من نافذة المرشحين للاستعلام السياقي،
                # سواء استُخدم الـreranker أم لا. الخطأ السابق كان يعتبر
                # المقطع السياقي «مكرراً» إذا ظهر عميقاً في نتائج السؤال
                # الخام، ثم يقص الخام قبل موضعه؛ فتضيع الوثيقة الصحيحة من
                # النافذتين معاً (ثبت Q097/Q262/Q398).
                reserve = min(extra_cap, max(2, fetch_k // 4))
                primary_limit = max(0, fetch_k - reserve)
                selected = list(primary[:primary_limit])
                chosen = set(selected)
                for chunk in contextual:
                    if chunk in chosen:
                        continue
                    selected.append(chunk)
                    chosen.add(chunk)
                    if len(selected) >= fetch_k:
                        break
                if len(selected) < fetch_k:
                    for chunk in primary[primary_limit:]:
                        if chunk in chosen:
                            continue
                        selected.append(chunk)
                        chosen.add(chunk)
                        if len(selected) >= fetch_k:
                            break
                relevant_chunks = selected
            # مقارنة معدل الثانوية بمفاتيح القبول سؤال تجميعي: يحتاج جدول
            # المفاتيح كاملاً لا أقرب مقاطعه فقط — top-K التشابهي كان يعيد
            # 8 نسخ لبرامج كلية واحدة (العلوم) ويُسقط مفاتيح بقية الكليات،
            # فيجيب الموديل بكليتين ويصمت عن التسع الباقيات (ثبت حياً).
            # نلتقط الملفات التي اسمها يدل على القبول/المعدلات (توجيه معتمد
            # على البيانات — أي ملف يسمّيه الأدمن كذلك يُلتقط تلقائياً)
            # ونرسل جدولها كاملاً ما دام تحت السقف.
            # نية القبول: بنية السؤال الذاتية أولاً، والوراثة من السياق فقط
            # للمتابعات القصيرة/الإحالات وللأسئلة ذات الموضوع الأكاديمي
            # (المنطق موثق ومختبر في query_rewrite.inherits_admission_intent).
            if admission_intent:
                names = {f["collection"] for f in self._uploaded.list_files()}
                if allowed_collections is not None:
                    names &= set(allowed_collections)
                focus = {n for n in names
                         if "قبول" in normalize_arabic(n) or "معدلات" in normalize_arabic(n)}
                if focus:
                    focused = []
                    for name in sorted(focus):
                        focused.extend(self._uploaded.chunks_of(name))
                    if len(focused) > config.ADMISSION_TABLE_MAX_CHUNKS:
                        focused = self._uploaded.search_all(
                            search_question, config.ADMISSION_TABLE_MAX_CHUNKS,
                            allowed_collections=focus,
                        )
                    else:
                        admission_table = True
                    # المقاطع الخام تخلط الرسوم بالمفاتيح في القراءة (ثبت حياً:
                    # سعر ساعة الآداب 20 ديناراً قُرئ «مفتاح 20%») — نُصدّر
                    # المفاتيح الرقمية كسطور مستخلصة آلياً لا لبس فيها، وتبقى
                    # الخام للمفاتيح النصية (الطب «تنافسية») والرسوم.
                    digest = self._uploaded.admission_context_lines(
                        None if allowed_collections is None
                        else set(allowed_collections),
                        branch=active_constraints.get("branch"),
                        max_percentage=active_constraints.get("rate"),
                    )
                    if digest:
                        admission_digest = True
                        for line in digest:
                            if line.startswith("[") or "|" not in line:
                                continue
                            faculty = line.split("|", 1)[0].strip()
                            if faculty and faculty not in admission_faculties:
                                admission_faculties.append(faculty)
                        digest_chunk = (
                            "جدول مفاتيح القبول (مستخلص آلياً من ملفات الجامعة):\n"
                            + "\n".join(dict.fromkeys(digest))
                        )
                        # النسخة المنظمة تحمل كل الأرقام والفروع بلا رسوم.
                        # لا نكرر معها عشرات المقاطع الخام نفسها؛ نحتفظ فقط
                        # بالسجلات ذات الشرط النصي غير الرقمي (مثل «تنافسية»)
                        # وبالمقاطع الأخرى التي استرجعها السؤال المركب.
                        textual_markers = (
                            "تنافسي", "تنافسيه", "غير رقمي",
                            "لا يوجد معدل ثابت", "حسب المنافسه",
                        )
                        textual_support = [
                            chunk for chunk in focused
                            if any(
                                marker in normalize_arabic(chunk)
                                for marker in textual_markers
                            )
                        ]
                        active_branch = active_constraints.get("branch")
                        if (
                            active_branch is not None
                            and normalize_arabic(active_branch) != "علمي"
                        ):
                            # الشرط النصي المتاح حالياً هو طب/علمي؛ لا نحقنه
                            # في سؤال أدبي فيعيد جذب النموذج إلى الفرع القديم.
                            textual_support = []
                        focused_set = set(focused)
                        list_from_digest = (
                            query_rewrite.has_admission_intent(base_question)
                            or complete_list_requested
                            or query_rewrite.wants_academic_programs(base_question)
                        )
                        other_evidence = (
                            [
                                chunk for chunk in relevant_chunks
                                if chunk not in focused_set
                            ]
                            if (
                                not list_from_digest
                                or query_rewrite.is_multi_part_question(question)
                            )
                            else []
                        )
                        relevant_chunks = (
                            [digest_chunk]
                            + list(dict.fromkeys(textual_support))
                            + other_evidence
                        )
                    else:
                        seen = set(focused)
                        relevant_chunks = (
                            focused
                            + [c for c in relevant_chunks if c not in seen]
                        )

        # ── وعي المرحلة الأكاديمية ──
        # سجلات رسوم الدراسات العليا (65 سجلاً غنياً بكلمات «برامج/تخصصات»)
        # كانت تبتلع أسئلة البكالوريوس: «ما برامج كلية العلوم؟» أجاب بماجستير،
        # وسؤال الدرجات نفى الدكتوراه (ثبت في تقييم الـ90). مرحلة مذكورة
        # صراحةً = إسقاط مقاطع المراحل الأخرى؛ غير مذكورة = تقديم البكالوريوس
        # (ترتيباً لا إسقاطاً) لأنه سؤال الزائر/الطالب الافتراضي.
        asked_level = (query_rewrite.detect_degree_level(base_question)
                       or active_constraints.get("degree")
                       or query_rewrite.detect_degree_level(search_question))

        def _chunk_level(chunk: str) -> str | None:
            if chunk.startswith("[ملف: "):
                return query_rewrite.file_degree_level(chunk[6:chunk.find("]")])
            return None

        if relevant_chunks:
            if asked_level:
                kept = [c for c in relevant_chunks
                        if _chunk_level(c) in (None, asked_level)]
                if len(kept) >= 3:  # لا نفرغ السياق إن كان الترشيح قاسياً
                    relevant_chunks = kept
            elif query_rewrite.wants_academic_programs(base_question):
                # «اعطيني خيارات أكاديمية / ما التخصصات» بلا ذكر دراسات عليا =
                # سؤال بكالوريوس؛ إعادة الترتيب وحدها لا تكفي إن لم يُسترجع أصلاً
                # مقطع بكالوريوس (بعد استبعاد المنح تصدّرت الماجستير) — نُسقط
                # الدراسات العليا صراحةً مع حارس ألا نُفرّغ السياق (ثبت Q097).
                kept = [c for c in relevant_chunks
                        if _chunk_level(c) in (None, "bachelor")]
                relevant_chunks = kept if len(kept) >= 2 else relevant_chunks
                asked_level = asked_level or "bachelor"  # للفاحص الحتمي أيضاً
            else:
                relevant_chunks = (
                    [c for c in relevant_chunks if _chunk_level(c) in (None, "bachelor")]
                    + [c for c in relevant_chunks if _chunk_level(c) in ("masters", "phd")]
                )

        # ── احترام الاستبعاد الصريح («مش منح»، «خلينا من الهندسة») ──
        excluded = query_rewrite.extract_exclusions(question)
        if excluded and relevant_chunks:
            markers = query_rewrite.exclusion_file_markers(excluded)
            kept = [c for c in relevant_chunks
                    if not (c.startswith("[ملف: ") and any(
                        m in normalize_arabic(c[6:c.find("]")]) for m in markers))]
            if kept:
                relevant_chunks = kept

        # دليل الأشخاص المختلط: إذا طلب المستخدم دوراً مؤسسياً محدداً
        # ووجدت عدة سجلات تطابقه حرفياً، احذف النواب/الأدوار الإدارية
        # المجاورة قبل التوليد. هذا يقلل السياق والهلوسة والزمن معاً.
        if relevant_chunks:
            relevant_chunks, role_focus_applied = (
                query_rewrite.prefer_exact_role_chunks(
                    search_question, relevant_chunks
                )
            )
            if role_focus_applied and complete_list_requested:
                # top-K قد يحتوي ثمانية عمداء صحيحين لكنه يترك سجلات مطابقة
                # أخرى أعمق في الملف. بعد إثبات توقيع الحقل المطلوب، امسح
                # المقاطع المحلية المسموح بها واجمع كل السجلات المطابقة؛
                # لا API ولا توليد إضافياً، ولا اعتماد على اسم ملف بعينه.
                role_pool: list[str] = []
                for file_info in self._uploaded.list_files():
                    collection = file_info["collection"]
                    if (
                        allowed_collections is not None
                        and collection not in allowed_collections
                    ):
                        continue
                    role_pool.extend(self._uploaded.chunks_of(collection))
                expanded_roles, expanded = (
                    query_rewrite.prefer_exact_role_chunks(
                        search_question, list(dict.fromkeys(role_pool))
                    )
                )
                if expanded and len(expanded_roles) > len(relevant_chunks):
                    relevant_chunks = expanded_roles

        # ── Reranker انتقائي ──
        # لا يستطيع استحضار وثيقة غير موجودة؛ لذلك لا ندفع كلفته إلا إذا كان
        # المرشحون أنفسهم يحملون إشارات كافية للاستعلام السياقي. القوائم
        # الشاملة تُحفظ بتغطية أوسع بدلاً من قصّها إلى top-K.
        if rerank_requested and not admission_table and len(relevant_chunks) > top_k:
            rerank_guard_passed = query_rewrite.candidates_support_query(
                search_question, relevant_chunks
            )
            if rerank_guard_passed:
                rerank_attempted = True
                relevant_chunks, rerank_status = rerank_mod.rerank_with_status(
                    search_question, relevant_chunks, top_k
                )
                if rerank_status != "applied":
                    # فشل الـranker لا يعيدنا إلى مشكلة recall: احتفظ بسياق
                    # أوسع قليلاً؛ Q067 كان دليله الصحيح في المرتبة 13.
                    relevant_chunks = relevant_chunks[
                        :max(top_k, config.COVERAGE_TOP_K)
                    ]
            else:
                rerank_status = "candidate_guard_skipped"
                relevant_chunks = relevant_chunks[:top_k]
        elif rerank_requested:
            rerank_status = "insufficient_candidates"
        elif coverage_requested and not admission_table:
            relevant_chunks = relevant_chunks[:target_k]

        # ── ميزانية سياق تكيفية (المفاتيح المجانية تسقط 402 فوق سقفها) ──
        if config.MAX_CONTEXT_CHARS > 0:
            budget, trimmed = config.MAX_CONTEXT_CHARS, []
            for c in relevant_chunks:
                if budget - len(c) < 0 and trimmed:
                    break
                trimmed.append(c)
                budget -= len(c)
            relevant_chunks = trimmed

        structured_projection = project_structured_evidence(
            domain_route, relevant_chunks
        )
        if structured_projection:
            authoritative.append(
                "[إسقاط حقلي من المقاطع؛ القيم منقولة حرفياً]\n"
                + "\n\n".join(structured_projection)
            )

        source_metadata_extracted = bool(
            history
            and query_rewrite.is_source_metadata_followup(question)
            and relevant_chunks
        )
        if source_metadata_extracted:
            authoritative.append(
                "[بيانات المصدر المطابق للدور السابق؛ صغها جواباً ولا تستعر تاريخاً من مصدر آخر]\n"
                + self._source_metadata_fallback(relevant_chunks)
            )

        if authoritative:
            seen_evidence = set(authoritative)
            relevant_chunks = authoritative + [
                chunk for chunk in relevant_chunks if chunk not in seen_evidence
            ]

        contract = build_evidence_contract(
            plan,
            frame,
            relevant_chunks,
            authoritative_evidence=authoritative,
        )
        coverage_retry_query = None
        coverage_retry_added = 0
        if (
            config.EVIDENCE_CONTRACT_ENABLED
            and plan.route == "advanced_rag"
            and contract.missing_fields
            and not safety_directive
            and not skip_general_retrieval
        ):
            coverage_retry_query = missing_field_query(plan, contract)
            if coverage_retry_query:
                extra_chunks = self._search_all_for_question(
                    coverage_retry_query,
                    max(4, min(config.COVERAGE_TOP_K, top_k)),
                    allowed_collections,
                )
                known = set(relevant_chunks)
                additions = [chunk for chunk in extra_chunks if chunk not in known]
                if additions:
                    body = [chunk for chunk in relevant_chunks if chunk not in authoritative]
                    merged_body = body + additions
                    retry_limit = target_k if coverage_requested else top_k
                    if (
                        config.RERANK_ENABLED
                        and len(merged_body) > retry_limit
                        and query_rewrite.candidates_support_query(
                            coverage_retry_query, merged_body
                        )
                    ):
                        reordered, retry_status = rerank_mod.rerank_with_status(
                            coverage_retry_query, merged_body, retry_limit
                        )
                        if retry_status == "applied":
                            merged_body = reordered
                    merged_body = merged_body[:retry_limit]
                    relevant_chunks = list(authoritative) + merged_body
                    coverage_retry_added = max(0, len(merged_body) - len(body))
                    contract = build_evidence_contract(
                        plan,
                        frame,
                        relevant_chunks,
                        authoritative_evidence=authoritative,
                    )
        dynamic_instructions: list[str] = []
        if safety_directive:
            dynamic_instructions.extend([
                safety_directive,
                "لا تعرض أي سجل خاص أو قيمة شخصية في سياق الرفض.",
            ])
        if private_context is not None:
            dynamic_instructions.extend([
                "استخدم بيانات المستخدم الخاصة فقط عندما تكون مرتبطة بالسؤال.",
                "إذا جمع السؤال بياناته مع موضوع جامعي فادمجها مع الأدلة العامة دون سرد ملفه كاملاً.",
                "إذا غابت تفاصيل الإجراء الخاصة بكلية المستخدم فابدأ بتوضيح هذا النقص قبل عرض مثال من كلية أخرى.",
            ])
        if frame.ambiguous and not frame.reference:
            dynamic_instructions.append(
                "السؤال ما زال يحتمل أكثر من مرجع؛ اطلب تحديداً قصيراً بدل اختيار موضوع عشوائي."
            )

        prompt_route = (
            PromptRoute.PRIVACY_REFUSAL if safety_directive
            else PromptRoute.PRIVATE_STUDENT if private_context is not None
            else PromptRoute.UPLOADED_FILES
        )
        context = "\n\n---\n\n".join(relevant_chunks)
        system = build_system_prompt(PromptContext(
            route=prompt_route,
            evidence=context,
            role_policy=role_prompt or "",
            private_context=private_context or "",
            conversation_frame=frame.prompt_block(),
            evidence_contract=contract.prompt_block(),
            dynamic_instructions=dynamic_instructions,
        ))
        system += self._source_recency_note(relevant_chunks)
        if generic_engineering_fee:
            system += """

تعليمات خاصة بهذا السؤال: لم يحدد الطالب المرحلة الدراسية لسعر الساعة في
الهندسة. اعرض بشكل منفصل سعر البكالوريوس وسعر الماجستير/الدراسات العليا إذا
وُجدا في المقاطع أعلاه. لا تخلط بينهما، ولا تخترع قيمة لم ترد في المقاطع.
"""
        if admission_table:
            system += """

تعليمات خاصة بهذا السؤال: جدول مفاتيح القبول متوفر أعلاه كاملاً، فلا تجتزئ:
- إن سأل «ما التخصصات/الكليات التي تقبلني بمعدلي؟» فاسرد جميع الكليات
  والتخصصات التي يحققها معدله وفرعه (علمي/أدبي/…) مصنفة حسب الكلية —
  القائمة الكاملة، لا أمثلة ولا «راجع عمادة القبول» بديلاً عن السرد.
- مفتاح القبول هو نسبة الثانوية الدنيا فقط (حقل مثل min_high_school_percentage).
  لا تخلط بينه وبين رسوم الساعة (credit_hour_fee — مبلغ بالدينار وليس نسبة).
- اتجاه المقارنة: التخصص يقبل السائل إذا كان مفتاحه أصغر من معدله أو
  يساويه (مثال: مفتاح 70 ومعدله 81 → مقبول لأن 70 ≤ 81).
- في سؤال «ما الذي يقبلني/ما الخيارات المتاحة؟» لا تضع برنامجاً مفتاحه أعلى
  من معدل السائل ضمن القائمة، ولا تضف قائمة «غير متاح» إلا إذا طلبها صراحة.
- إن لم يُعرف فرعه في التوجيهي فاطلبه، أو قدّم القائمتين مفصولتين بوضوح.
- إن عُرف الفرع ولم يذكر السائل معدله، فلا توقف الجواب بطلب المعدل: اسرد كل
  البرامج التي تقبل ذلك الفرع مع مفتاح كل مجموعة، ليقارن السائل لاحقاً.
- للقوائم الطويلة استخدم سطراً واحداً لكل كلية، وأسماء التخصصات مفصولة
  بفواصل؛ الإيجاز هنا لضمان عدم إسقاط كلية بسبب طول الجواب.
- لا تستبدل الأسماء بعبارة «جميع البرامج» أو «وغيرها»: أسماء البرامج مضغوطة
  بعد حقل «البرامج:» في كل سطر، فانقلها كما هي مع حذف التكرار.
- «يقبل الفرع العلمي» لا يعني «علمي فقط»: لا تقل «فقط» إلا إذا نص سطر
  البرنامج نفسه على حصرية الفرع. راجع الكليات الإحدى عشرة قبل إنهاء القائمة.
"""
            constraint_lines = []
            if active_constraints["branch"] is not None:
                constraint_lines.append(
                    f"- الفرع الأحدث في الحوار: {active_constraints['branch']}"
                )
            if active_constraints["rate"] is not None:
                constraint_lines.append(
                    f"- معدل الثانوية الأحدث في الحوار: "
                    f"{active_constraints['rate']:g}%"
                )
            if active_constraints["degree"] is not None:
                constraint_lines.append(
                    f"- المرحلة الأحدث في الحوار: {active_constraints['degree']}"
                )
            if constraint_lines:
                system += (
                    "\nحالة القبول النشطة (الأحدث يلغي أي قيد أقدم مخالف):\n"
                    + "\n".join(constraint_lines)
                    + "\nطبّق هذه الحالة حصراً؛ لا ترجع إلى فرع أو معدل أقدم "
                    "ورد في سجل المحادثة.\n"
                )
        if admission_digest:
            system += """
- كتلة «جدول مفاتيح القبول (مستخلص آلياً من ملفات الجامعة)» أعلاه هي المرجع
  الحصري للمفاتيح الرقمية — خذ الحد الأدنى منها حصراً ولا تستخرجه من المقاطع
  الخام. البرامج ذات الشرط غير الرقمي (مثل الطب «تنافسية») ليست في الكتلة:
  خذ شرطها النصي من المقاطع الخام واذكرها منفصلة بشرطها كما ورد؛ لا تسقطها
  لمجرد أن شرطها لا يمكن مقارنته رقمياً.
"""
            if admission_faculties:
                system += (
                    "\nقائمة تدقيق ديناميكية مستخرجة من الجدول نفسه (لا تضف "
                    "اسماً من خارج الدليل):\n- "
                    + "\n- ".join(admission_faculties)
                    + "\nقبل إنهاء الجواب مرّ على كل اسم في قائمة التدقيق: "
                    "اذكره إن كان فرع السائل ومعدله يحققان أحد برامجه، ولا "
                    "تسقط كلية مؤهلة بسبب طول القائمة.\n"
                )
        if query_rewrite.requires_direct_evidence(question):
            system += """

تعليمات دقة لهذا الطلب:
- المطلوب مورد/حقل محدد؛ لا تستنتجه من معلومة قريبة منه.
- سمِّ كل رابط أو صفحة أو نظام بالوظيفة والعنوان المكتوبين له في المقطع حرفياً.
  رابط «دليل/خطوات» ليس تلقائياً رابط النموذج أو البوابة نفسها، ووجود محتوى
  مساق على نظام لا يثبت أن «وصف المساق» موجود فيه.
- حقول مثل link_id أو كلمات snake_case (مثال: resource_name) معرّفات داخلية
  وليست روابط؛ لا تعرضها للمستخدم كرابط. الرابط القابل للفتح يبدأ بـ https://.
- عند طلب المصدر وتاريخ التحقق، اربطهما بالمقطع الذي يسند المعلومة نفسها؛
  لا تستعر تاريخاً من مقطع آخر لمجرد أنه ظهر في السياق.
- إذا لم يعرض الدليل القيمة أو المسار المطلوب مباشرةً، قل بوضوح إن المطلوب
  الدقيق غير وارد، ثم قدّم البديل الموجود فقط مع تسميته الصحيحة.
"""
        if role_focus_applied:
            system += """

تم ترشيح دليل الأشخاص إلى سجلات الدور المطلوب حرفياً. انقل الأسماء والجهات
من هذه السجلات فقط، ولا تضف نائباً أو عمادة إدارية أو جهة غير مذكورة بينها.
إذا كانت كلية من القائمة السابقة بلا سجل مطابق فاذكر أن اسم عميدها غير وارد.
"""
        if not safety_directive and asked_level:
            _level_names = {"bachelor": "البكالوريوس", "masters": "الماجستير",
                            "phd": "الدكتوراه"}
            system += (f"\nالسائل يسأل عن مرحلة {_level_names[asked_level]} تحديداً —"
                       " لا تجب عن مرحلة أكاديمية أخرى إلا إن طلبها صراحة.\n")
        elif (
            not safety_directive
            and relevant_chunks
            and bool(set(plan.domains) & {"fees", "admissions", "programs"})
        ):
            system += ("\nلم يحدد السائل مرحلة أكاديمية — الافتراض أنه يسأل عن"
                       " البكالوريوس؛ قدّمه أولاً ولا تجب بالدراسات العليا وحدها.\n")
        if excluded:
            system += ("\n⚠️ السائل استبعد صراحةً: " + "، ".join(excluded)
                       + " — يُمنع ذكرها أو بناء الإجابة عليها.\n")
        if complete_list_requested:
            system += """

تعليمات تغطية خاصة بهذا الطلب:
- المطلوب قائمة شاملة مما تدعمه كل المقاطع أعلاه، لا أمثلة ولا «من أبرز».
- راجع المقاطع كلها قبل الصياغة وادمج العناصر المتكررة دون إسقاط عناصر مختلفة.
- لهذا الطلب يجوز الإطالة بقدر ما يلزم لإكمال القائمة دون إسقاط عناصر.
- إن كانت المقاطع نفسها لا تسند قائمة كاملة، اذكر حدود المتاح بوضوح ولا تخترع.
"""
        if query_rewrite.is_multi_part_question(question):
            system += """

تعليمات خاصة بالسؤال المركب: جزّئ المطلوب إلى بنود قصيرة، وأجب كل جزء صراحةً
من الأدلة قبل إنهاء الإجابة، ويجوز الإطالة بقدر الحاجة لإكمال الأجزاء.
"""

        retrieval_metadata = {
            "base_query": base_question,
            "search_query": search_question,
            "admission_intent": admission_intent,
            "coverage_requested": coverage_requested,
            "target_k": target_k,
            "fetch_k": fetch_k,
            "rerank_requested": rerank_requested,
            "rerank_guard_passed": rerank_guard_passed,
            "rerank_attempted": rerank_attempted,
            "rerank_status": rerank_status,
            "exact_role_focus_applied": role_focus_applied,
            "context_chunk_count": len(relevant_chunks),
            "cache_hit": False,
            "answer_cache_bypassed": bool(config.LLM_ALWAYS_ANSWER),
            "llm_always_answer": True,
            "active_academic_constraints": active_constraints,
            "query_plan": plan.as_metadata(),
            "domain_route": domain_route.as_metadata(),
            "conversation_frame": frame.as_metadata(),
            "evidence_contract": contract.as_metadata(),
            "coverage_retry_query": coverage_retry_query,
            "coverage_retry_added": coverage_retry_added,
            "candidate_metadata": self._uploaded.candidate_metadata_for_chunks(
                relevant_chunks
            ),
            "source_metadata_extracted": source_metadata_extracted,
            "authoritative_evidence_count": len(authoritative),
            "safety_directive_applied": bool(safety_directive),
            "pipeline_version": "adaptive-rag-v2",
        }
        if admission_table or complete_list_requested:
            generation_max_tokens = config.LLM_COVERAGE_MAX_TOKENS
        elif plan.is_compound:
            generation_max_tokens = config.LLM_MULTIPART_MAX_TOKENS
        else:
            generation_max_tokens = None
        retrieval_metadata["generation_max_tokens"] = (
            generation_max_tokens or config.LLM_MAX_TOKENS
        )
        retrieval.record_trace({
            "scope": "retrieval_plan",
            "strategy": "adaptive_query_plan_structured_hybrid_selective_rerank",
            **retrieval_metadata,
        })
        if safety_directive:
            source = "privacy_policy_llm"
        elif structured_source:
            source = structured_source
        elif private_context is not None:
            source = "student_context_rag_llm"
        else:
            source = "uploaded_files_all_llm"
        return {
            "kind": "llm",
            "system": system,
            "chunks": relevant_chunks,
            "source": source,
            "cache_key": cache_key,
            "asked_level": asked_level,
            "excluded": excluded,
            "retrieval_metadata": retrieval_metadata,
            "generation_max_tokens": generation_max_tokens,
        }

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
            return self.chat_with_all_files(
                question,
                student_id,
                safety_directive=(
                    "هذا السؤال يطلب بيانات خاصة لطالب آخر. ارفض كشف المعدل "
                    "أو الترتيب أو أي بيانات شخصية بصياغة عربية موجزة."
                ),
                authoritative_evidence=[
                    "[سياسة خصوصية] بيانات الطلاب الآخرين غير متاحة للمستخدم الحالي."
                ],
            )
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
        major-expanded evidence selection — then streams the LLM content instead
        of blocking on it. Privacy policies, trusted facts, and structured
        resolutions are evidence for this same LLM call rather than final-answer
        shortcuts. Once the stream completes it records history; answer caching
        remains bypassed when LLM_ALWAYS_ANSWER is enabled.

        Errors after the first byte surface as visible text — the HTTP status
        was already sent and can no longer become a clean 502."""
        safety_directive = None
        authoritative_evidence = None
        if principal.role == Role.STUDENT and privacy.asks_about_other_student(
            question, principal.subject
        ):
            safety_directive = (
                "هذا السؤال يطلب بيانات خاصة لطالب آخر. ارفض كشف المعدل "
                "أو الترتيب أو أي بيانات شخصية بصياغة عربية موجزة."
            )
            authoritative_evidence = [
                "[سياسة خصوصية] بيانات الطلاب الآخرين غير متاحة للمستخدم الحالي."
            ]

        private_context = None
        retrieval_question = question
        if principal.role != Role.GUEST and not safety_directive:
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
            safety_directive=safety_directive,
            authoritative_evidence=authoritative_evidence,
        )
        user_message, q_vec = self._build_user_message(
            question,
            principal.subject,
            user_constraints_only=bool(
                prepared.get("retrieval_metadata", {}).get("admission_intent")
            ),
        )
        user_message = self._anchor_active_constraints(user_message, prepared)
        parts: List[str] = []
        try:
            generation_max_tokens = prepared.get("generation_max_tokens")
            stream = (
                stream_completion(
                    prepared["system"],
                    user_message,
                    max_tokens=generation_max_tokens,
                )
                if generation_max_tokens
                else stream_completion(prepared["system"], user_message)
            )
            for chunk in stream:
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
        client_history: list | None = None,
    ) -> dict:
        """Unified role-aware chat path used by the new API endpoints.

        client_history: سياق محادثة الزائر من متصفحه (الزوار بلا جلسات مخزّنة
        على الخادم) — يُستخدم للفهم فقط ولا يُخزَّن؛ يُقبل لدور الزائر حصراً."""
        if principal.role != Role.GUEST:
            client_history = None  # الموثّقون: سجل الخادم أوثق من أي مدخل عميل
        safety_directive = None
        authoritative_evidence = None
        if principal.role == Role.STUDENT and privacy.asks_about_other_student(
            question, principal.subject
        ):
            safety_directive = (
                "هذا السؤال يطلب بيانات خاصة لطالب آخر. ارفض كشف المعدل "
                "أو الترتيب أو أي بيانات شخصية بصياغة عربية موجزة."
            )
            authoritative_evidence = [
                "[سياسة خصوصية] بيانات الطلاب الآخرين غير متاحة للمستخدم الحالي."
            ]

        private_context = None
        retrieval_question = question
        if principal.role != Role.GUEST and not safety_directive:
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
            client_history=client_history,
            safety_directive=safety_directive,
            authoritative_evidence=authoritative_evidence,
        )
