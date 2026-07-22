"""
IUGChatbot — thin facade that composes the feature services and keeps the
exact public API the rest of the system (console harness, future REST
layer) already relies on. All heavy lifting lives in the feature modules;
this class only orchestrates.
"""

import hashlib
import json
import re
import time
from typing import List
from uuid import uuid4

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
from app.conversation_frame import (
    ClaimRequirement,
    ConceptResolution,
    CONTEXT_ASSISTANT_REFERENCE,
    CONTEXT_CORRECTION,
    CONTEXT_FOLLOWUP,
    build_query_plan,
)
from app.data_quality import (
    deduplicate_evidence,
    suppress_rejected_conflict_values,
)
from app.evidence_contract import build_evidence_contract, missing_field_query
from app.domain_router import project_structured_evidence, route_query
from app.cache import TTLCache
from app.chunking import SENSITIVE_MARKER
from app.errors import ChatbotError, ServiceNotReadyError
from app.knowledge_base import KnowledgeBase
from app.llm import chat_completion
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
from app.rag_agent import plan_rag_actions
from app.semantic_rag import (
    run_semantic_planner,
    run_semantic_verifier,
    should_run_semantic_verifier,
)

log = get_logger("chatbot")


def _conflicts_relevant_to_plan(plan, conflicts: list[dict]) -> list[dict]:
    """Keep only conflicts for the requested field and bound entity.

    Retrieval deliberately keeps neighboring evidence for recall.  A conflict
    in another program or an unbound synthetic/global projection must not
    block a claim about a specific program.
    """
    relevant = []
    for conflict in conflicts:
        field_name = str(conflict.get("canonical_field") or "")
        conflict_entity = normalize_arabic(
            str(conflict.get("entity") or "")
        )
        for claim in getattr(plan, "claims", []) or []:
            if field_name != claim.canonical_field:
                continue
            claim_entity = normalize_arabic(str(claim.entity or ""))
            if not claim_entity:
                relevant.append(conflict)
                break
            if not conflict_entity or conflict_entity == "global":
                continue
            terms = []
            for token in claim_entity.split():
                token = token[1:] if token.startswith("و") and len(token) > 4 else token
                if token.startswith("ال") and len(token) > 4:
                    token = token[2:]
                if len(token) >= 3 and token not in terms:
                    terms.append(token)
            comparable_entity = conflict_entity
            if all(term in comparable_entity for term in terms):
                relevant.append(conflict)
                break
    return list({item["conflict_id"]: item for item in relevant}.values())


class IUGChatbot:

    SENSITIVE_MARKER = SENSITIVE_MARKER

    def __init__(self, sessions=None):
        self._kb = KnowledgeBase()
        self._uploaded = UploadedFilesStore()
        # persistent (Mongo) by default; tests inject an in-memory store
        self._sessions = sessions if sessions is not None else make_session_store()
        # Kept only for the existing cache-stats API. Product policy disables
        # all writes/reads of final answers; this cache therefore remains empty.
        self._answer_cache = TTLCache(
            "public_answers", config.CACHE_ANSWER_MAXSIZE, config.CACHE_ANSWER_TTL
        )
        self._readiness_state = "new"
        self._initialization_error: str | None = None
        self._initialization_started_at: float | None = None
        self._ready_at: float | None = None

    def cache_stats(self) -> dict:
        return {
            "public_answers": self._answer_cache.stats(),
            "query_embeddings": embeddings.query_cache_stats(),
        }

    def clear_caches(self) -> None:
        self._answer_cache.clear()
        embeddings.reset_query_cache()

    def initialize(self):
        """Build and validate every retrieval index before serving chat."""
        self._readiness_state = "starting"
        self._initialization_started_at = (
            self._initialization_started_at or time.time()
        )
        self._initialization_error = None
        try:
            self._kb.load()
            self._uploaded.load_all()
            if not self._kb.index_ready or not self._uploaded.index_ready:
                failed = ", ".join(self._uploaded.failed_sources) or "unknown"
                raise RuntimeError(
                    "index readiness verification failed; sources=" + failed
                )
        except Exception as exc:
            self._readiness_state = "failed"
            self._initialization_error = type(exc).__name__
            raise
        self._readiness_state = "ready"
        self._ready_at = time.time()

    def begin_initialization(self) -> None:
        """Close the tiny scheduling race before background initialization."""
        self._readiness_state = "starting"
        self._initialization_started_at = time.time()
        self._initialization_error = None

    @property
    def index_version(self) -> str:
        raw = "\x00".join((
            config.RAG_PIPELINE_VERSION,
            self._kb.index_version,
            self._uploaded.index_version,
        ))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def readiness(self) -> dict:
        index_ready = bool(
            self._readiness_state == "ready"
            and self._kb.index_ready
            and self._uploaded.index_ready
        )
        return {
            "status": "ready" if index_ready else (
                "failed" if self._readiness_state == "failed" else "starting"
            ),
            "index_ready": index_ready,
            "document_count": self._kb.document_count,
            "chunk_count": len(self._kb.chunks or []),
            "uploaded_chunk_count": sum(
                item["chunks_count"] for item in self._uploaded.list_files()
            ),
            "index_version": self.index_version,
            "failed_sources": self._uploaded.failed_sources,
            "failed_refresh_sources": self._uploaded.failed_refresh_sources,
            "initialization_error": self._initialization_error,
            "initialization_started_at": self._initialization_started_at,
            "ready_at": self._ready_at,
        }

    def ensure_ready(self) -> None:
        """Reject cold-start/failed requests before retrieval or LLM use.

        A freshly constructed test/local facade (state ``new``) remains usable
        for injected in-memory corpora.  Once production initialization starts,
        readiness is enforced strictly.
        """
        if self._readiness_state == "new":
            return
        if (
            self._readiness_state != "ready"
            or not self._kb.index_ready
            or not self._uploaded.index_ready
        ):
            raise ServiceNotReadyError(
                "فهرس المعرفة ما زال قيد التجهيز؛ أعد المحاولة بعد ظهور "
                "index_ready=true في /health.",
                details={"status": self._readiness_state},
            )

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

    def push_history(
        self, sid: str, user: str, assistant: str, embedding=None,
        *, status: str = sessions_mod.TurnStatus.VERIFIED,
        verification: dict | None = None,
    ):
        self._sessions.push(
            sid, user, assistant, embedding, status=status,
            verification=verification,
        )

    def clear_history(self, sid: str):
        return self._sessions.clear(sid)

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
    def _turn_ids(turns: list) -> list[str]:
        """Return non-secret stable identifiers for prompt-memory audit."""
        result = []
        for turn in turns:
            turn_id = str(turn.get("turn_id") or "").strip()
            if not turn_id:
                raw = f"{turn.get('at', '')}\x00{turn.get('user', '')}"
                turn_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
            result.append(turn_id)
        return result

    def _build_user_message(
        self,
        question: str,
        session_id: str,
        client_history=None,
        *,
        user_constraints_only: bool = False,
        context_mode: str = "independent",
    ):
        """(نص رسالة المستخدم مع الذاكرة المنتقاة، متجه السؤال). مشترك بين
        المسار العادي والبثّ، فالذاكرة تُبنى بنفس الطريقة في الحالتين.

        client_history: سياق يحمله متصفح الزائر (لا جلسات مخزّنة للزوار) —
        أدواره بلا متجهات محفوظة، فتُطوى نصاً كاملةً بلا انتقاء دلالي."""
        include_history = context_mode in {
            CONTEXT_FOLLOWUP,
            CONTEXT_CORRECTION,
            CONTEXT_ASSISTANT_REFERENCE,
        }
        if client_history is not None:
            prompt_history = client_history if include_history else []
            memory_text = (
                sessions_mod.format_assistant_reference(
                    prompt_history[-1] if prompt_history else None
                )
                if context_mode == CONTEXT_ASSISTANT_REFERENCE
                else sessions_mod.format_user_memory(prompt_history)
            )
            try:
                q_vec = embeddings.embed_query(question)
            except Exception:
                q_vec = None
            return (
                f"{memory_text}السؤال: {question}",
                q_vec,
                self._turn_ids(
                    prompt_history[-1:]
                    if context_mode == CONTEXT_ASSISTANT_REFERENCE
                    else prompt_history
                ),
            )
        history = self.get_history(session_id)
        memory_text, q_vec, history_turn_ids = self._memory_block(
            question, history,
            user_constraints_only=user_constraints_only,
            include_history=include_history,
            include_last_assistant=(
                context_mode == CONTEXT_ASSISTANT_REFERENCE
            ),
        )
        return f"{memory_text}السؤال: {question}", q_vec, history_turn_ids

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

    def _memory_block(
        self,
        question: str,
        history: list,
        *,
        user_constraints_only: bool = False,
        include_history: bool = True,
        include_last_assistant: bool = False,
    ):
        """(نص الذاكرة، متجه السؤال). المتجه يُحسب مرة واحدة هنا ويُخزَّن مع
        الدور عند الحفظ فلا يُعاد حسابه لاحقاً (embed_query نفسه مُكاش، فالنداء
        مجاني عندما سبق للاسترجاع تضمين السؤال ذاته). عند فشل التضمين نعود
        بأمان للسلوك القديم حرفياً: طيّ آخر الأدوار كلها بلا انتقاء."""
        try:
            vec = embeddings.embed_query(question)
        except Exception as exc:
            log.warning("⚠️ تعذّر تضمين السؤال للذاكرة — fallback نصي: %s", exc)
            prompt_history = history if include_history else []
            return (
                sessions_mod.format_assistant_reference(
                    prompt_history[-1] if prompt_history else None
                )
                if include_last_assistant
                else sessions_mod.format_user_memory(prompt_history),
                None,
                self._turn_ids(
                    prompt_history[-1:] if include_last_assistant
                    else prompt_history
                ),
            )
        if not include_history:
            return "", vec, []
        turns = sessions_mod.relevant_turns(history, vec)
        if include_last_assistant:
            selected = history[-1:] if history else []
            return (
                sessions_mod.format_assistant_reference(
                    selected[-1] if selected else None
                ),
                vec,
                self._turn_ids(selected),
            )
        return sessions_mod.format_user_memory(turns), vec, self._turn_ids(turns)

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
المصدر الأحدث فقط إذا كان تاريخا المصدرين المتعارضين معروفين وقابلين
للمقارنة. هذه تواريخ إدخال للنظام للترجيح الداخلي فقط — لا تذكرها في
إجابتك كأنها تاريخ إصدار المعلومة أو «آخر تحديث للنشرة». إذا كان أحد
التاريخين مجهولاً فلا تفترض أنه الأقدم؛ صرّح بوجود تعارض يحتاج حسم الإدارة.
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

    def _search_claims_for_plan(
        self,
        plan,
        top_k: int,
        allowed_collections: set[str] | None = None,
    ) -> List[str]:
        """Retrieve a reserved quota for every requested claim.

        One search *wave* may contain several claim queries; it still counts as
        one bounded retrieval attempt.  Round-robin merging prevents a strong
        fee match from evicting all admission-cutoff evidence (or vice versa).
        """
        claims = list(getattr(plan, "claims", []) or [])
        if len(claims) <= 1:
            query = claims[0].retrieval_query if claims else plan.standalone_query
            return self._search_all_for_question(
                query, top_k, allowed_collections
            )
        quota = max(2, (top_k + len(claims) - 1) // len(claims))
        candidate_k = max(quota, min(config.RERANK_CANDIDATES, quota * 2))
        batches = []
        for claim in claims:
            values = self._search_all_for_question(
                claim.retrieval_query,
                candidate_k,
                allowed_collections,
            )
            if (
                config.RERANK_ENABLED
                and len(values) > quota
                and query_rewrite.candidates_support_query(
                    claim.retrieval_query, values
                )
            ):
                reordered, status = rerank_mod.rerank_with_status(
                    claim.retrieval_query, values, quota
                )
                if status == "applied":
                    values = reordered
            batches.append(values[:quota])
        merged: List[str] = []
        seen = set()
        for position in range(max((len(batch) for batch in batches), default=0)):
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

    @staticmethod
    def _apply_semantic_plan(plan, frame, semantic: dict) -> None:
        """Merge a planner result without allowing it to drop deterministic claims."""
        if semantic.get("status") != "applied":
            return
        unresolved_norm = {
            normalize_arabic(value): value for value in plan.unresolved_clauses
        }
        existing_by_field = {
            claim.canonical_field: claim for claim in plan.claims
        }
        resolved_surfaces: set[str] = set()
        for item in semantic.get("claims", []):
            field_name = item.get("canonical_field")
            refined_query = str(item.get("refined_query") or "").strip()
            if field_name in existing_by_field:
                if refined_query:
                    existing = existing_by_field[field_name]
                    existing.retrieval_query = (
                        existing.retrieval_query
                        + " ("
                        + refined_query
                        + ")"
                    )
                continue
            surface = str(item.get("surface_text") or "").strip()
            normalized_surface = normalize_arabic(surface)
            if (
                not surface
                or normalized_surface not in unresolved_norm
                or float(item.get("confidence") or 0) < 0.75
            ):
                continue
            # A new-session bare «المفتاح» remains a clarification even if an
            # LLM guesses admission.  Semantic planning may resolve it only
            # when another safely planned claim/context already anchors it.
            if (
                field_name == "admission_cutoff"
                and plan.requires_clarification
                and not plan.claims
            ):
                continue
            claim = ClaimRequirement(
                claim_id=f"claim_{len(plan.claims) + 1}",
                surface_text=surface,
                canonical_field=field_name,
                entity=item.get("entity") or frame.reference,
                scope={
                    "degree_level": frame.degree_level,
                    "branch": frame.branch,
                    "rate": frame.rate,
                    "transfer_scope": frame.transfer_scope,
                },
                answer_type=str(item.get("answer_type") or "text"),
                time_state=(
                    "live" if plan.live_policy == "dated_caveat" else "indexed"
                ),
                retrieval_query=refined_query or surface,
                resolution_source="semantic",
                confidence=float(item.get("confidence") or 0),
            )
            plan.claims.append(claim)
            existing_by_field[field_name] = claim
            resolved_surfaces.add(normalized_surface)
        if resolved_surfaces:
            plan.unresolved_clauses = [
                value for value in plan.unresolved_clauses
                if normalize_arabic(value) not in resolved_surfaces
            ]
            plan.requires_clarification = bool(
                plan.unresolved_clauses and not plan.claims
            )
            for item in semantic.get("concept_resolutions", []):
                surface = str(item.get("surface_text") or "").strip()
                if normalize_arabic(surface) not in resolved_surfaces:
                    continue
                try:
                    confidence = float(item.get("confidence") or 0)
                except (TypeError, ValueError):
                    confidence = 0.0
                plan.concept_resolutions.append(ConceptResolution(
                    surface_text=surface,
                    canonical_concept=str(
                        item.get("canonical_concept") or "unknown"
                    ),
                    source="semantic",
                    confidence=max(0.0, min(1.0, confidence)),
                    context_used=(
                        str(item.get("context_used"))
                        if item.get("context_used") else None
                    ),
                ))
        field_domains = {
            "fee": "fees", "admission_cutoff": "admissions",
            "branch": "admissions", "requirements": "admissions",
            "scholarships": "scholarships", "programs": "programs",
            "procedures": "procedures", "date": "deadlines",
            "documents": "documents", "link": "contacts",
            "contact": "contacts", "people": "people",
        }
        fields = list(dict.fromkeys(
            claim.canonical_field for claim in plan.claims
            if claim.canonical_field not in {"general", "account_access"}
        ))
        if fields:
            frame.requested_fields = fields
            frame.domains = list(dict.fromkeys(
                field_domains[field_name]
                for field_name in fields if field_name in field_domains
            )) or frame.domains
            plan.domains = list(frame.domains)
            plan.intent = (
                plan.domains[0] if len(plan.domains) == 1 else "compound"
            )
        refined = [
            str(item.get("refined_query") or "").strip()
            for item in semantic.get("claims", [])
            if str(item.get("refined_query") or "").strip()
        ]
        if refined:
            plan.standalone_query += " (" + "؛ ".join(dict.fromkeys(refined)) + ")"
            plan.needs_query_expansion = True

    def chat(self, question: str, session_id: str) -> dict:
        """Trace-wrapped legacy main-corpus path."""
        trace_id = uuid4().hex
        token = retrieval.begin_trace()
        started = time.perf_counter()
        result = None
        try:
            result = self._chat_main(question, session_id)
        except Exception as exc:
            events = retrieval.end_trace(token)
            summary = self._build_trace_summary(
                trace_id=trace_id,
                question=question,
                session_id=session_id,
                allowed_collections=None,
                prepared=None,
                validation_metadata=None,
                events=events,
                retrieval_latency_ms=round(
                    (time.perf_counter() - started) * 1000
                ),
                total_latency_ms=round(
                    (time.perf_counter() - started) * 1000
                ),
                error_type=type(exc).__name__,
            )
            log.info("RAG_TRACE %s", json.dumps(
                summary, ensure_ascii=False, sort_keys=True
            ))
            raise
        events = retrieval.end_trace(token)
        elapsed = round((time.perf_counter() - started) * 1000)
        result_metadata = result.setdefault("retrieval_metadata", {})
        prepared = {
            "chunks": result.get("top_chunks", []),
            "retrieval_metadata": result_metadata,
        }
        summary = self._build_trace_summary(
            trace_id=trace_id,
            question=question,
            session_id=session_id,
            allowed_collections=None,
            prepared=prepared,
            validation_metadata=result_metadata,
            events=events,
            retrieval_latency_ms=max(
                0, elapsed - int(result_metadata.get("generation_latency_ms", 0))
            ),
            total_latency_ms=elapsed,
        )
        result_metadata["trace_id"] = trace_id
        result_metadata["diagnostic_trace"] = summary
        result["trace_id"] = trace_id
        log.info("RAG_TRACE %s", json.dumps(
            summary, ensure_ascii=False, sort_keys=True
        ))
        return result

    def _chat_main(self, question: str, session_id: str) -> dict:
        """Legacy main-corpus path; every final answer is generated by the LLM."""
        self.ensure_ready()
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
                plan.claims = []
                plan.unresolved_clauses = []

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
        prepared = {
            "system": system,
            "chunks": general_chunks,
            "validation_chunks": evidence,
            "excluded": frame.exclusions,
            "asked_level": frame.degree_level,
            "generation_max_tokens": None,
            "retrieval_metadata": {
                "query_plan": plan.as_metadata(),
                "conversation_frame": frame.as_metadata(),
                "evidence_contract": contract.as_metadata(),
                "active_academic_constraints": {
                    "branch": frame.branch,
                    "rate": frame.rate,
                    "degree": frame.degree_level,
                },
            },
        }
        answer, q_vec, validation_metadata = self._generate_validated_answer(
            prepared, question, session_id
        )
        self.push_history(
            session_id,
            question,
            answer,
            embedding=q_vec,
            status=validation_metadata["turn_status"],
        )
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
                **validation_metadata,
            },
        }

    def chat_with_file(self, question: str, collection_name: str, session_id: str) -> dict:
        """Single-file path; missing files are also explained by the LLM."""
        exists = self._uploaded.has(collection_name)
        authoritative = None if exists else [
            f"[حالة المصدر] الملف المحدد «{collection_name}» غير موجود؛ "
            "يجب ذكر ذلك بوضوح دون اختراع محتوى."
        ]
        result = self.chat_with_all_files(
            question,
            session_id,
            allowed_collections={collection_name} if exists else set(),
            authoritative_evidence=authoritative,
        )
        result["source"] = "uploaded_file_llm"
        return result

    def _safe_llm_answer(
        self,
        question: str,
        safe_evidence: str,
        *,
        max_tokens: int | None = None,
        strict: bool = False,
    ) -> str:
        """Ask the LLM to phrase a bounded safe answer.

        The deterministic pipeline may decide that only a conservative fact is
        supportable, but it never returns that Python string directly.  Even
        safety/absence/source-metadata responses are generated by the LLM, as
        required by the product contract.
        """
        system = (
            "أنت طبقة الصياغة النهائية لمساعد الجامعة. أجب بالعربية بإيجاز. "
            "المادة الموثوقة أدناه هي المصدر الوحيد المسموح؛ لا تضف رقماً أو "
            "اسماً أو رابطاً أو إجراءً غير موجود فيها. أجب عن المطلوب وحده "
            "ولا تضف خدمة أو نفيًا أو مثالاً لم يسأل عنه المستخدم. "
        )
        if strict:
            system += (
                "المحاولة السابقة لم تجتز الفحص، لذلك التزم بمعنى المادة "
                "حرفياً ولا توسّعها بأي معلومة أخرى."
            )
        user_message = (
            f"السؤال الأصلي: {question}\n\n"
            "المادة الوحيدة المسموح بصياغتها:\n"
            f"{safe_evidence}"
        )
        return self._complete_llm(
            system,
            user_message,
            max_tokens=max_tokens,
        )

    def _generate_validated_answer(
        self,
        prepared: dict,
        question: str,
        session_id: str,
        *,
        client_history: list | None = None,
    ) -> tuple[str, np.ndarray | None, dict]:
        """Generate, validate, and (when needed) repair one final LLM answer."""
        user_message, q_vec, history_turn_ids = self._build_user_message(
            question,
            session_id,
            client_history,
            user_constraints_only=bool(
                prepared.get("retrieval_metadata", {}).get("admission_intent")
            ),
            context_mode=(
                prepared.get("retrieval_metadata", {})
                .get("query_plan", {})
                .get("context_mode", "independent")
            ),
        )
        user_message = self._anchor_active_constraints(user_message, prepared)
        prompt_sha256 = hashlib.sha256(
            (prepared["system"] + "\x00" + user_message).encode("utf-8")
        ).hexdigest()
        generation_max_tokens = prepared.get("generation_max_tokens")
        generation_started = time.perf_counter()
        answer = self._complete_llm(
            prepared["system"],
            user_message,
            max_tokens=generation_max_tokens,
        )
        generation_count = 1
        max_generation_attempts = int(
            prepared.get("retrieval_metadata", {})
            .get("agentic_rag", {})
            .get("max_generation_attempts", 3)
        )
        max_generation_attempts = max(1, min(3, max_generation_attempts))

        validation_sources = list(
            prepared.get("validation_chunks", prepared["chunks"])
        ) + [question]
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
            validation_sources.append(
                f"معدل الثانوية الذي ذكره المستخدم: {active_rate:g}%"
            )

        contract_metadata = (
            prepared.get("retrieval_metadata", {}).get("evidence_contract", {})
        )
        unresolved_evidence_conflicts = list(
            prepared.get("retrieval_metadata", {}).get(
                "evidence_conflicts", []
            )
        )

        def unresolved_conflict_issues(value: str) -> list[str]:
            if not unresolved_evidence_conflicts:
                return []
            ascii_answer = value.translate(str.maketrans(
                "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
                "01234567890123456789",
            ))
            norm_answer = normalize_arabic(ascii_answer)
            acknowledges = any(
                marker in norm_answer
                for marker in (
                    "تعارض", "اختلاف بين المصدر", "قيمتان", "قيمتين",
                    "غير محسوم", "لا يمكن ترجيح", "يحتاج حسم",
                )
            )
            issues = []
            for conflict in unresolved_evidence_conflicts:
                mentioned = 0
                for raw in conflict.get("values", []):
                    candidate = str(raw).strip()
                    numeric = candidate.rstrip("%")
                    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", numeric):
                        pattern = rf"(?<![\d.]){re.escape(numeric)}(?!\d)"
                        present = bool(re.search(pattern, ascii_answer))
                    else:
                        present = bool(
                            normalize_arabic(candidate)
                            and normalize_arabic(candidate) in norm_answer
                        )
                    mentioned += int(present)
                # An unresolved conflict may be described (with both values)
                # or withheld, but one side may never be selected silently.
                if not acknowledges or mentioned == 1:
                    issues.append(
                        "يوجد تعارض أدلة غير محسوم؛ لا تختَر قيمة واحدة. "
                        "اذكر التعارض والقيمتين أو قل إن القرار يحتاج حسم الإدارة."
                    )
                    break
            return issues

        def answer_issues(value: str) -> list[str]:
            # A general catalogue can cover the topic while still being unable
            # to establish what is true "today".  In that case an explicit
            # absence/current-status caveat is correct and must not be rejected
            # as a false denial merely because the topic-level contract is full.
            live_caveat = (
                prepared.get("retrieval_metadata", {}).get("live_policy")
                == "dated_caveat"
            )
            refund_policy_gap = (
                "دفعت" in normalize_arabic(question)
                and "استرد" in normalize_arabic(question)
            )
            return [
                *answer_check.problems(
                value,
                sources=validation_sources,
                excluded=prepared.get("excluded", []),
                asked_level=prepared.get("asked_level"),
                question=question,
                entity_terms=contract_metadata.get("entity_terms", []),
                evidence_sufficient=(
                    False if live_caveat or refund_policy_gap
                    else contract_metadata.get("sufficient")
                ),
                retrieval_degraded=bool(
                    prepared.get("retrieval_metadata", {}).get(
                        "retrieval_degraded"
                    )
                ),
                ),
                *unresolved_conflict_issues(value),
            ]

        issues = answer_issues(answer)
        post_retry_issues: list[str] = []
        safe_llm_fallback = False
        if issues:
            unresolved_external_policy = any(
                "سياسة دخول/تأشيرة" in issue for issue in issues
            )
            unresolved_refund_policy = any(
                "حكم استرداد رسوم طلب الالتحاق" in issue for issue in issues
            )
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
                or "أضفت رابطاً/بريداً/هاتفاً/سنة" in issue
                for issue in issues
            )
            workflow_status_gap = any(
                "حالة واجهة" in issue for issue in issues
            )
            if unresolved_refund_policy:
                safe_evidence = (
                    "المستخدم دفع رسوم طلب الالتحاق بالفعل. المعنى الإلزامي "
                    "للجواب هو: لا يمكن تأكيد هل يسترد المبلغ أم لا، لأن "
                    "سياسة استرداد رسوم طلب الالتحاق غير موثقة في البيانات "
                    "الحالية. لا تقل إن المبلغ يُسترد، ولا تقل إنه لا يُسترد، "
                    "ولا تستخدم إعلان إعفاء لفترة أخرى كحكم استرداد. وجّه "
                    "المستخدم إلى عمادة القبول والتسجيل لحسم معاملته."
                )
                answer = self._safe_llm_answer(
                    question,
                    safe_evidence,
                    max_tokens=generation_max_tokens,
                    strict=True,
                )
                generation_count += 1
                safe_llm_fallback = True
                post_retry_issues = answer_issues(answer)
            elif unresolved_external_policy:
                safe_evidence = (
                    "المعلومة المطلوبة غير محسومة في بيانات الجامعة الحالية. "
                    "لا تؤكد نوع تأشيرة أو جهة إصدار أو ضمان دخول. صغ جواباً "
                    "موجزاً يقول إن ذلك يحتاج تحققاً من الجهات الرسمية، وإن "
                    "كان المستخدم يحتاج كتاب قبول فيراجع القبول والتسجيل."
                )
                answer = self._safe_llm_answer(
                    question,
                    safe_evidence,
                    max_tokens=generation_max_tokens,
                    strict=True,
                )
                generation_count += 1
                safe_llm_fallback = True
                post_retry_issues = answer_issues(answer)
            elif source_metadata_gap:
                safe_evidence = self._source_metadata_fallback(prepared["chunks"])
                answer = self._safe_llm_answer(
                    question,
                    safe_evidence,
                    max_tokens=generation_max_tokens,
                )
                generation_count += 1
                safe_llm_fallback = True
                post_retry_issues = answer_issues(answer)
            elif workflow_status_gap:
                safe_evidence = (
                    "لا تعرض الأدلة المسترجعة علامة واجهة محددة تثبت حفظ "
                    "أو إرسال الطلب. أجب عن هذه الفجوة فقط: يراجع المستخدم "
                    "طلبه في البوابة، وإذا بقي الأمر غير واضح يتواصل مع "
                    "عمادة القبول والتسجيل لتأكيد الاستلام. لا تذكر رسالة "
                    "نجاح أو زرًا أو حالة طلب بعينها."
                )
                answer = self._safe_llm_answer(
                    question,
                    safe_evidence,
                    max_tokens=generation_max_tokens,
                    strict=True,
                )
                generation_count += 1
                safe_llm_fallback = True
                post_retry_issues = answer_issues(answer)
            elif hard_exact_gap:
                safe_evidence = (
                    (
                        "الأدلة التالية كافية وتحتوي القيمة الدقيقة؛ انقلها "
                        "منها ولا تستبدلها بقيمة أو مسار آخر:\n"
                        + "\n\n".join(prepared["chunks"])
                    )
                    if contract_metadata.get("sufficient")
                    else (
                        "المعلومة أو المسار الدقيق المطلوب غير وارد بوضوح في "
                        "المقاطع المسترجعة؛ يجب التصريح بعدم القدرة على تأكيده "
                        "وعدم تخمين رابط أو اسم أو خطوات."
                    )
                )
                answer = self._safe_llm_answer(
                    question,
                    safe_evidence,
                    max_tokens=generation_max_tokens,
                )
                generation_count += 1
                safe_llm_fallback = True
                post_retry_issues = answer_issues(answer)
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
                generation_count += 1
                post_retry_issues = answer_issues(answer)

            unsafe_exact = query_rewrite.requires_direct_evidence(question) or any(
                "رابطاً/بريداً/هاتفاً/سنة" in issue
                or "ليس رابطاً" in issue
                or "رابط دليل/خطوات" in issue
                or "ادعيتَ أن المعلومة غير موجودة" in issue
                or "حالة واجهة" in issue
                for issue in post_retry_issues
            )
            if (
                post_retry_issues
                and unsafe_exact
                and not safe_llm_fallback
                and generation_count < max_generation_attempts
            ):
                post_workflow_status_gap = any(
                    "حالة واجهة" in issue for issue in post_retry_issues
                )
                safe_evidence = (
                    self._source_metadata_fallback(prepared["chunks"])
                    if query_rewrite.is_source_metadata_followup(question)
                    else (
                        "لا تعرض الأدلة المسترجعة علامة واجهة محددة تثبت "
                        "حفظ أو إرسال الطلب. يراجع المستخدم طلبه في البوابة، "
                        "وإذا بقي الأمر غير واضح يتواصل مع عمادة القبول "
                        "والتسجيل لتأكيد الاستلام. لا تذكر رسالة نجاح أو "
                        "زرًا أو حالة طلب بعينها."
                    )
                    if post_workflow_status_gap
                    else (
                        "الأدلة التالية مسندة؛ أجب بالمعلومة الموجودة فيها "
                        "ولا تقل إنها غير موجودة:\n"
                        + "\n\n".join(prepared["chunks"])
                    )
                    if contract_metadata.get("sufficient")
                    else (
                        "المعلومة أو المسار الدقيق المطلوب غير وارد بوضوح "
                        "في الأدلة المتاحة؛ يجب قول ذلك دون تخمين قيمة بديلة."
                    )
                )
                answer = self._safe_llm_answer(
                    question,
                    safe_evidence,
                    max_tokens=generation_max_tokens,
                )
                generation_count += 1
                safe_llm_fallback = True
                post_retry_issues = answer_issues(answer)

            # A safe LLM response is itself checked.  One strict LLM phrasing
            # attempt is allowed; no Python-authored final answer is returned.
            if (
                safe_llm_fallback
                and post_retry_issues
                and generation_count < max_generation_attempts
            ):
                strict_workflow_gap = workflow_status_gap or any(
                    "حالة واجهة" in issue for issue in post_retry_issues
                )
                safe_evidence = (
                    self._source_metadata_fallback(prepared["chunks"])
                    if query_rewrite.is_source_metadata_followup(question)
                    else (
                        "المتاح فقط: لا توجد علامة واجهة موثقة للحفظ أو "
                        "الإرسال. اطلب من المستخدم مراجعة طلبه في البوابة، "
                        "ثم التواصل مع عمادة القبول والتسجيل إذا بقي الأمر "
                        "غير واضح. لا تذكر أي رسالة أو شاشة أو حالة مفترضة."
                    )
                    if strict_workflow_gap
                    else (
                        (
                            "الأدلة التالية كافية ومسندة؛ أجب بما يظهر فيها "
                            "ولا تقل إن المعلومة غير موجودة:\n"
                            + "\n\n".join(prepared["chunks"])
                        )
                        if contract_metadata.get("sufficient")
                        else (
                            "لا يمكن تأكيد المعلومة الدقيقة المطلوبة من الأدلة "
                            "المتاحة، لذلك يجب الامتناع عن تخمينها."
                        )
                    )
                )
                answer = self._safe_llm_answer(
                    question,
                    safe_evidence,
                    max_tokens=generation_max_tokens,
                    strict=True,
                )
                generation_count += 1
                post_retry_issues = answer_issues(answer)

        semantic_verifier = {
            "called": False,
            "status": "not_needed",
            "decision": None,
            "supported_claims": [],
            "unsupported_claims": [],
            "missing_required": [],
            "contradictions": [],
            "repair_instructions": [],
        }
        semantic_repair = False
        semantic_verifier_call_count = 0
        verification_outcome = "deterministic_accept"
        uncertainty_evidence = any(
            any(marker in normalize_arabic(source) for marker in (
                "غير محسوم رسميا",
                "سياسه الاسترداد غير موثقه",
                "لا توجد لدينا سياسه رسميه موثقه",
                "غير متاح في البيانات الحاليه",
            ))
            for source in validation_sources
        )
        honest_uncertainty_answer = any(
            marker in normalize_arabic(answer) for marker in (
                "غير موثق", "لا يمكن تاكيد", "لا يمكن الجزم",
                "لا تتوفر", "لا تحسم", "يحتاج تاكيد",
            )
        )
        # When the authoritative record explicitly says the policy is
        # unresolved and the draft honestly preserves that limitation, the
        # deterministic exact-fact checks are the reliable gate.  Asking the
        # semantic verifier to "complete" an unknowable field can turn a safe
        # caveat into a repair loop or a fabricated policy.
        resolved_as_honest_caveat = (
            uncertainty_evidence and honest_uncertainty_answer
        )
        plan_metadata = prepared.get("retrieval_metadata", {}).get(
            "query_plan", {}
        )
        frame_metadata = prepared.get("retrieval_metadata", {}).get(
            "conversation_frame", {}
        )
        if (
            not post_retry_issues
            and not resolved_as_honest_caveat
            and config.SEMANTIC_RAG_ENABLED
            and should_run_semantic_verifier(plan_metadata, frame_metadata)
        ):
            semantic_verifier = run_semantic_verifier(
                question=question,
                answer=answer,
                evidence=validation_sources,
                claim_coverage=contract_metadata.get("claim_coverage", {}),
                live_policy=str(
                    prepared.get("retrieval_metadata", {}).get(
                        "live_policy", "indexed"
                    )
                ),
                completion=lambda system, user, max_tokens=None: self._complete_llm(
                    system, user, max_tokens=max_tokens
                ),
                max_tokens=config.SEMANTIC_VERIFIER_MAX_TOKENS,
                max_evidence_chars=(
                    config.SEMANTIC_VERIFIER_MAX_EVIDENCE_CHARS
                ),
            )
            semantic_verifier_call_count += 1
            if semantic_verifier.get("status") == "unavailable":
                verification_outcome = "verification_degraded"
            elif semantic_verifier.get("decision") == "accept":
                verification_outcome = "accept"
            else:
                decision = semantic_verifier.get("decision") or "repair"
                instructions = [
                    *semantic_verifier.get("repair_instructions", []),
                    *(
                        "احذف أو صحح الادعاء غير المسند: " + value
                        for value in semantic_verifier.get(
                            "unsupported_claims", []
                        )
                    ),
                    *(
                        "أكمل الحقل المطلوب أو صرّح بنقصه: " + value
                        for value in semantic_verifier.get(
                            "missing_required", []
                        )
                    ),
                    *(
                        "لا تمرر هذا التناقض: " + value
                        for value in semantic_verifier.get(
                            "contradictions", []
                        )
                    ),
                ]
                if decision == "dated_caveat":
                    instructions.append(
                        "صغ المعلومة كآخر معلومة مؤرخة، ولا تثبت الحالة الحالية."
                    )
                if decision == "clarify":
                    instructions.append(
                        "صغ سؤال توضيح واحداً قصيراً ولا تخمّن المرجع."
                    )
                if generation_count < max_generation_attempts:
                    corrective = (
                        prepared["system"]
                        + "\n\n⚠️ إصلاح دلالي إلزامي ونهائي:\n- "
                        + "\n- ".join(instructions or [
                            "أعد الصياغة من الأدلة وعقد الادعاءات فقط."
                        ])
                    )
                    answer = self._complete_llm(
                        corrective,
                        user_message,
                        max_tokens=generation_max_tokens,
                    )
                    generation_count += 1
                    semantic_repair = True
                    semantic_verifier["repair_applied"] = True
                    post_retry_issues = answer_issues(answer)
                    verification_outcome = decision
                    # A repaired draft must not become ``grounded`` merely
                    # because deterministic number/link checks pass.  Verify
                    # semantic claim coverage once more; this is a bounded
                    # confirmation, not an open repair loop.
                    if not post_retry_issues:
                        initial_decision = decision
                        repaired_verifier = run_semantic_verifier(
                            question=question,
                            answer=answer,
                            evidence=validation_sources,
                            claim_coverage=contract_metadata.get(
                                "claim_coverage", {}
                            ),
                            live_policy=str(
                                prepared.get("retrieval_metadata", {}).get(
                                    "live_policy", "indexed"
                                )
                            ),
                            completion=lambda system, user, max_tokens=None: self._complete_llm(
                                system, user, max_tokens=max_tokens
                            ),
                            max_tokens=config.SEMANTIC_VERIFIER_MAX_TOKENS,
                            max_evidence_chars=(
                                config.SEMANTIC_VERIFIER_MAX_EVIDENCE_CHARS
                            ),
                        )
                        semantic_verifier_call_count += 1
                        repaired_verifier["repair_applied"] = True
                        repaired_verifier["initial_decision"] = initial_decision
                        semantic_verifier = repaired_verifier
                        repaired_decision = repaired_verifier.get("decision")
                        if repaired_verifier.get("status") == "unavailable":
                            verification_outcome = "verification_degraded"
                        elif repaired_decision in {"accept", "dated_caveat"}:
                            verification_outcome = "accept_after_repair"
                        else:
                            missing_after_repair = list(
                                repaired_verifier.get("missing_required", [])
                            )
                            unsupported_after_repair = list(
                                repaired_verifier.get("unsupported_claims", [])
                            )
                            detail = ", ".join(
                                [*missing_after_repair, *unsupported_after_repair]
                            )
                            post_retry_issues.append(
                                "الإصلاح الدلالي لم يغطِّ جميع الادعاءات المطلوبة"
                                + (f": {detail}" if detail else ".")
                            )
                else:
                    post_retry_issues.append(
                        "رفض المدقق الدلالي الإجابة ولا توجد محاولة إصلاح متبقية."
                    )

        # A known deterministic failure is never delivered (and therefore can
        # never be streamed or written to conversation history).
        if post_retry_issues:
            raise ChatbotError(
                "تعذر اعتماد إجابة آمنة بعد التحقق؛ أعد صياغة السؤال أو حاول لاحقاً.",
                details={
                    "verification_outcome": "rejected",
                    "issues": list(post_retry_issues),
                },
            )

        resolved_fields = list(contract_metadata.get("resolved_fields", []))
        missing_fields = list(contract_metadata.get("missing_fields", []))
        if (
            plan_metadata.get("requires_clarification")
            or contract_metadata.get("unresolved_clauses")
            or semantic_verifier.get("decision") == "clarify"
        ):
            turn_status = sessions_mod.TurnStatus.NEEDS_CLARIFICATION
        elif (
            prepared.get("retrieval_metadata", {}).get("live_policy")
            == "dated_caveat"
            or semantic_verifier.get("decision") == "dated_caveat"
        ):
            turn_status = sessions_mod.TurnStatus.LIVE_VERIFICATION_REQUIRED
        elif verification_outcome == "verification_degraded":
            turn_status = sessions_mod.TurnStatus.VERIFICATION_DEGRADED
        elif contract_metadata.get("sufficient"):
            turn_status = sessions_mod.TurnStatus.VERIFIED
        elif resolved_fields and missing_fields:
            turn_status = sessions_mod.TurnStatus.PARTIAL
        elif not prepared.get("chunks"):
            turn_status = sessions_mod.TurnStatus.RETRIEVAL_FAILURE
        else:
            turn_status = sessions_mod.TurnStatus.INSUFFICIENT_EVIDENCE
        generation_outcome = (
            "safe_llm_fallback"
            if safe_llm_fallback
            else "semantic_repair"
            if semantic_repair
            else "corrected"
            if issues
            else "first_pass"
        )
        generation_latency_ms = round(
            (time.perf_counter() - generation_started) * 1000
        )
        retrieval.record_trace({
            "scope": "answer_validation",
            "strategy": "deterministic_claim_check_bounded_llm_repair",
            "initial_issue_count": len(issues),
            "final_issue_count": len(post_retry_issues),
            "generation_count": generation_count,
            "turn_status": turn_status,
            "generation_outcome": generation_outcome,
            "final_answer_origin": "llm",
            "semantic_verifier": semantic_verifier,
            "verification_outcome": verification_outcome,
        })
        planner_call_count = int(
            prepared.get("retrieval_metadata", {}).get(
                "semantic_planner_call_count", 0
            )
        )
        verifier_call_count = semantic_verifier_call_count
        return answer, q_vec, {
            "answer_check_retry": bool(issues),
            "answer_check_issues": list(issues),
            "answer_check_post_retry_issues": list(post_retry_issues),
            "answer_check_safety_fallback": safe_llm_fallback,
            "final_answer_origin": "llm",
            "llm_generation_count": generation_count,
            "llm_call_count": (
                generation_count + planner_call_count + verifier_call_count
            ),
            "semantic_planner_call_count": planner_call_count,
            "semantic_verifier_call_count": verifier_call_count,
            "llm_generation_limit": max_generation_attempts,
            "generation_outcome": generation_outcome,
            "turn_status": turn_status,
            "semantic_verifier": semantic_verifier,
            "verification_outcome": verification_outcome,
            "history_turn_ids_used": history_turn_ids,
            "prompt_sha256": prompt_sha256,
            "generation_latency_ms": generation_latency_ms,
        }

    def _build_trace_summary(
        self,
        *,
        trace_id: str,
        question: str,
        session_id: str,
        allowed_collections: set[str] | None,
        prepared: dict | None,
        validation_metadata: dict | None,
        events: list[dict],
        retrieval_latency_ms: int,
        total_latency_ms: int,
        error_type: str | None = None,
    ) -> dict:
        """Collapse detailed request-local events into one safe audit record."""
        prepared_metadata = (prepared or {}).get("retrieval_metadata", {})
        query_plan = dict(prepared_metadata.get("query_plan", {}))
        contract = prepared_metadata.get("evidence_contract", {})
        validation = validation_metadata or {}

        before_rerank: list[str] = []
        after_rerank: list[str] = []
        reranker_events = []
        for event in events:
            for candidate in event.get("candidates", []):
                candidate_id = candidate.get("candidate_id") or candidate.get(
                    "chunk_sha256"
                )
                if candidate_id and candidate_id not in before_rerank:
                    before_rerank.append(candidate_id)
            if event.get("scope") == "reranker":
                reranker_events.append({
                    "status": event.get("status"),
                    "latency_ms": event.get("latency_ms"),
                    "error_type": event.get("error_type"),
                })
                for candidate in event.get("selected", []):
                    candidate_id = candidate.get("chunk_sha256")
                    if candidate_id and candidate_id not in after_rerank:
                        after_rerank.append(candidate_id)

        evidence_ids = [
            hashlib.sha256(chunk.encode("utf-8")).hexdigest()
            for chunk in (prepared or {}).get("chunks", [])
        ]
        retry_reasons = []
        if prepared_metadata.get("coverage_retry_query"):
            retry_reasons.append("evidence_coverage")
        if validation.get("answer_check_retry"):
            retry_reasons.append("answer_validation")
        if any(
            item.get("status") not in {None, "applied"}
            for item in reranker_events
        ):
            retry_reasons.append("reranker_fallback")

        privacy_trace = query_plan.get("intent") == "privacy"
        standalone_query = query_plan.get("standalone_query")
        standalone_query_hash = (
            hashlib.sha256(str(standalone_query).encode("utf-8")).hexdigest()
            if standalone_query else None
        )
        if standalone_query and (
            privacy_trace or not config.TRACE_INCLUDE_QUERY_TEXT
        ):
            standalone_query = (
                "[redacted:privacy]" if privacy_trace else "[redacted:config]"
            )
            for key in (
                "original_question", "standalone_query", "retrieval_question"
            ):
                if key in query_plan:
                    query_plan[key] = standalone_query
            if privacy_trace:
                query_plan["entities"] = {}
        scope_value = (
            "*" if allowed_collections is None
            else "\x00".join(sorted(allowed_collections))
        )
        return {
            "trace_id": trace_id,
            "pipeline_version": config.RAG_PIPELINE_VERSION,
            "index_version": prepared_metadata.get(
                "index_version", self.index_version
            ),
            "session_id_hash": hashlib.sha256(
                str(session_id).encode("utf-8")
            ).hexdigest()[:16],
            "question_hash": hashlib.sha256(
                question.encode("utf-8")
            ).hexdigest(),
            "access_scope_hash": hashlib.sha256(
                scope_value.encode("utf-8")
            ).hexdigest()[:16],
            "context_mode": query_plan.get("context_mode"),
            "standalone_query": standalone_query,
            "standalone_query_hash": standalone_query_hash,
            "query_plan": query_plan,
            "retrieval_cache_hit": False,
            "candidate_ids_before_rerank": before_rerank,
            "candidate_ids_after_rerank": after_rerank,
            "selected_evidence_ids": evidence_ids,
            "structured_fields": contract.get("resolved_fields", []),
            "claim_coverage": contract.get("claim_coverage", {}),
            "evidence_sufficient": contract.get("sufficient", False),
            "reranker_status": prepared_metadata.get("rerank_status"),
            "reranker_events": reranker_events,
            "history_turn_ids_used": validation.get(
                "history_turn_ids_used", []
            ),
            "prompt_sha256": validation.get("prompt_sha256"),
            "llm_model": config.CHAT_API_MODEL,
            "answer_validation": {
                "initial_issues": validation.get("answer_check_issues", []),
                "final_issues": validation.get(
                    "answer_check_post_retry_issues", []
                ),
                "turn_status": validation.get("turn_status"),
                "generation_outcome": validation.get("generation_outcome"),
                "generation_count": validation.get("llm_generation_count", 0),
                "llm_call_count": validation.get("llm_call_count", 0),
                "final_answer_origin": validation.get("final_answer_origin"),
                "semantic_verifier": validation.get("semantic_verifier"),
                "verification_outcome": validation.get(
                    "verification_outcome"
                ),
            },
            "retry_reason": retry_reasons or None,
            "latency_ms": {
                "retrieval": retrieval_latency_ms,
                "generation": validation.get("generation_latency_ms", 0),
                "total": total_latency_ms,
            },
            "error_type": error_type,
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
        trace_id: str | None = None,
    ) -> dict:
        """
        Same idea as chat_with_file(), but searches across ALL currently
        uploaded files merged into a single global ranking — the LLM only
        ever sees the best top-K chunks overall.

        retrieval_question, when given, replaces the literal question FOR THE
        SEARCH ONLY (e.g. «رئيس قسمي» expanded with the student's major); the
        LLM, history, and cache always see what the student actually typed.
        """
        trace_id = trace_id or uuid4().hex
        trace_token = retrieval.begin_trace()
        turn_started = time.perf_counter()
        retrieval_started = time.perf_counter()
        prepared = None
        validation_metadata = None
        try:
            # A publication can complete while retrieval is in progress.  If
            # the generation changed, discard the mixed preparation and retry
            # once before any LLM call; a second change yields a clean 503.
            for preparation_attempt in range(2):
                version_before = self.index_version
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
                version_after = self.index_version
                if version_before == version_after:
                    prepared["retrieval_metadata"]["index_version"] = version_after
                    prepared["retrieval_metadata"]["index_refresh_retry"] = bool(
                        preparation_attempt
                    )
                    break
                retrieval.record_trace({
                    "scope": "index_consistency",
                    "status": "version_changed_retry",
                    "attempt": preparation_attempt + 1,
                })
            else:
                raise ServiceNotReadyError(
                    "تزامن السؤال مع تحديث متكرر للفهرس؛ أعد المحاولة بعد لحظة."
                )
            retrieval_latency_ms = round(
                (time.perf_counter() - retrieval_started) * 1000
            )
            answer, q_vec, validation_metadata = self._generate_validated_answer(
                prepared,
                question,
                session_id,
                client_history=client_history,
            )
        except Exception as exc:
            events = retrieval.end_trace(trace_token)
            trace_summary = self._build_trace_summary(
                trace_id=trace_id,
                question=question,
                session_id=session_id,
                allowed_collections=allowed_collections,
                prepared=prepared,
                validation_metadata=validation_metadata,
                events=events,
                retrieval_latency_ms=round(
                    (time.perf_counter() - retrieval_started) * 1000
                ),
                total_latency_ms=round(
                    (time.perf_counter() - turn_started) * 1000
                ),
                error_type=type(exc).__name__,
            )
            log.info("RAG_TRACE %s", json.dumps(
                trace_summary, ensure_ascii=False, sort_keys=True
            ))
            raise

        events = retrieval.end_trace(trace_token)
        total_latency_ms = round((time.perf_counter() - turn_started) * 1000)
        trace_summary = self._build_trace_summary(
            trace_id=trace_id,
            question=question,
            session_id=session_id,
            allowed_collections=allowed_collections,
            prepared=prepared,
            validation_metadata=validation_metadata,
            events=events,
            retrieval_latency_ms=retrieval_latency_ms,
            total_latency_ms=total_latency_ms,
        )
        log.info("RAG_TRACE %s", json.dumps(
            trace_summary, ensure_ascii=False, sort_keys=True
        ))

        # لا يدخل الذاكرة إلا الجواب النهائي الذي سيصل للمستخدم.
        self.push_history(
            session_id,
            question,
            answer,
            embedding=q_vec,
            status=validation_metadata["turn_status"],
            verification={
                "trace_id": trace_id,
                "index_version": trace_summary["index_version"],
                "evidence_ids": trace_summary["selected_evidence_ids"],
                "evidence_sufficient": trace_summary["evidence_sufficient"],
            },
        )

        retrieval_metadata = dict(prepared.get("retrieval_metadata", {}))
        retrieval_metadata.update(validation_metadata)
        retrieval_metadata["trace_id"] = trace_id
        retrieval_metadata["diagnostic_trace"] = trace_summary
        result = {
            "answer": answer,
            "top_chunks": prepared["chunks"],
            "source": prepared["source"],
            "trace_id": trace_id,
            "retrieval_metadata": retrieval_metadata,
        }
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
        self.ensure_ready()
        # Every route ends in one LLM generation.  Deterministic components
        # may prepare authoritative evidence, but never return the final answer.
        history = client_history if client_history is not None else self.get_history(session_id)
        frame, plan = build_query_plan(
            question,
            history,
            retrieval_question=retrieval_question,
        )
        semantic_planner = {
            "called": False,
            "status": "not_needed",
            "decision": None,
            "claims": [],
            "unresolved_clauses": list(plan.unresolved_clauses),
        }

        def _invoke_semantic_planner(evidence_gaps: list[str] | None = None):
            nonlocal semantic_planner
            if semantic_planner.get("called") or not config.SEMANTIC_RAG_ENABLED:
                if not config.SEMANTIC_RAG_ENABLED:
                    semantic_planner["status"] = "disabled"
                return
            recent_user_turns = [
                str(turn.get("user") or "").strip()
                for turn in history[-5:]
                if sessions_mod.is_fresh(turn)
                and str(turn.get("user") or "").strip()
            ]
            semantic_planner = run_semantic_planner(
                question=question,
                deterministic_plan=plan.as_metadata(),
                recent_user_turns=recent_user_turns,
                evidence_gaps=evidence_gaps,
                completion=lambda system, user, max_tokens=None: self._complete_llm(
                    system, user, max_tokens=max_tokens
                ),
                max_tokens=config.SEMANTIC_PLANNER_MAX_TOKENS,
            )
            self._apply_semantic_plan(plan, frame, semantic_planner)

        if plan.needs_semantic_planner and not safety_directive:
            _invoke_semantic_planner()
        previous_turn = history[-1] if history else {}
        repeated_after_low_confidence = bool(
            previous_turn.get("status") in sessions_mod.LOW_CONFIDENCE_STATUSES
            and normalize_arabic(str(previous_turn.get("user") or ""))
            == normalize_arabic(question)
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
            # Do not echo the requested person's name through claim surface
            # text/entity metadata into the system prompt.  The LLM needs the
            # policy directive only, never the private subject identifier.
            plan.claims = []
            plan.unresolved_clauses = []
        domain_route = route_query(plan, frame)
        authoritative = list(authoritative_evidence or [])
        if plan.requires_clarification:
            authoritative.append(
                "[حالة فهم السؤال] يوجد تعبير لا يملك مرجعاً جامعياً "
                "كافياً في السؤال أو في دور مستخدم حديث. لا تخمّن معناه؛ "
                "صغ سؤال توضيح واحداً قصيراً يذكر الخيار المقصود."
            )
        structured_chunks: list[str] = []
        structured_source: str | None = None

        trusted_answer = self._trusted_direct_answer(question)
        if trusted_answer:
            authoritative.append(
                "[حقيقة مرجعية موثوقة؛ صغها جواباً مناسباً للسؤال]\n"
                + trusted_answer
            )
            structured_source = "trusted_fact_llm"

        agent_plan = plan_rag_actions(
            plan,
            frame,
            domain_route,
            has_authoritative_evidence=bool(authoritative),
            safety_directive=bool(safety_directive),
        )
        if (
            agent_plan.use_structured_lookup
            and not safety_directive
            and not plan.requires_clarification
        ):
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
                # Exact structured evidence may let the bounded agent skip a
                # redundant general search for a single-field question.
                agent_plan = plan_rag_actions(
                    plan,
                    frame,
                    domain_route,
                    has_authoritative_evidence=True,
                    safety_directive=False,
                )

        # Final-answer caching is a product-level prohibition.  Only safe
        # embedding/index caches remain; retrieval itself is executed anew.
        cache_key = None

        base_question = query_rewrite.add_canonical_terms(
            query_rewrite.positive_query(retrieval_question or question)
        )
        search_question = plan.standalone_query
        if repeated_after_low_confidence:
            # The previous assistant text is already excluded from memory.
            # Widen the fresh retrieval deterministically as an additional
            # guard for a repeated question after a weak turn.
            search_question = query_rewrite.add_coverage_terms(search_question)
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
            or repeated_after_low_confidence
        )
        target_k = max(top_k, config.COVERAGE_TOP_K) if coverage_requested else top_k
        rerank_requested = (
            config.RERANK_ENABLED
            and agent_plan.use_reranker
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
        skip_general_retrieval = (
            not agent_plan.use_hybrid_retrieval
            or plan.requires_clarification
        )
        if safety_directive:
            relevant_chunks = []
        elif skip_general_retrieval:
            relevant_chunks = list(structured_chunks)
        elif self._uploaded.is_empty():
            relevant_chunks = []
        else:
            relevant_chunks = (
                self._search_claims_for_plan(
                    plan, fetch_k, allowed_collections
                )
                if plan.claims
                else self._search_all_for_question(
                    search_question, fetch_k, allowed_collections
                )
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
            if plan.is_followup and search_question != base_question and \
                    not query_rewrite.is_pure_reference(retrieval_question or question) and \
                    not plan.is_compound and \
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
                    all_focused = []
                    for name in sorted(focus):
                        all_focused.extend(self._uploaded.chunks_of(name))
                    focused = list(all_focused)
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
                    specific_admission_entities = list(dict.fromkeys(
                        claim.entity
                        for claim in plan.claims
                        if claim.entity
                        and claim.canonical_field in {
                            "fee", "branch", "admission_cutoff"
                        }
                    ))
                    targeted_digest = bool(
                        specific_admission_entities
                        and not complete_list_requested
                        and not plan.is_list_question
                    )
                    # A student's rate is an upper filter only for discovery
                    # questions ("what can accept me?").  Applying it to a
                    # named-program eligibility question hides the very cutoff
                    # that must be compared when the student does *not* qualify.
                    digest = self._uploaded.admission_context_lines(
                        None if allowed_collections is None
                        else set(allowed_collections),
                        branch=(
                            None if targeted_digest
                            else active_constraints.get("branch")
                        ),
                        max_percentage=(
                            None if targeted_digest
                            else active_constraints.get("rate")
                        ),
                    )

                    def _matches_specific_entity(value: str) -> bool:
                        normalized_value = normalize_arabic(value)
                        for entity in specific_admission_entities:
                            terms = []
                            for token in normalize_arabic(entity).split():
                                if token.startswith("ال") and len(token) > 4:
                                    token = token[2:]
                                if len(token) >= 3:
                                    terms.append(token)
                            if terms and all(
                                term in normalized_value for term in terms
                            ):
                                return True
                        return False

                    if digest and targeted_digest:
                        headers = [line for line in digest if line.startswith("[")]
                        matching_lines = [
                            line for line in digest
                            if not line.startswith("[")
                            and _matches_specific_entity(line)
                        ]
                        digest = [*headers, *matching_lines]
                        if not digest:
                            digest = [
                                "[لا يوجد مفتاح رقمي مطابق للبرنامج؛ "
                                "استخدم الشرط النصي من دليله الخام]"
                            ]
                    if digest:
                        admission_digest = True
                        if not targeted_digest:
                            for line in digest:
                                if line.startswith("[") or "|" not in line:
                                    continue
                                faculty = line.split("|", 1)[0].strip()
                                if faculty and faculty not in admission_faculties:
                                    admission_faculties.append(faculty)
                        digest_chunk = (
                            (
                                "دليل مفتاح القبول المرتبط بالبرنامج المطلوب "
                                "(مستخلص آلياً من ملفات الجامعة):\n"
                                if targeted_digest
                                else "جدول مفاتيح القبول (مستخلص آلياً من ملفات الجامعة):\n"
                            )
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
                        if targeted_digest:
                            textual_support = [
                                chunk for chunk in textual_support
                                if _matches_specific_entity(chunk)
                            ]
                        active_branch = active_constraints.get("branch")
                        if (
                            active_branch is not None
                            and normalize_arabic(active_branch) != "علمي"
                        ):
                            # الشرط النصي المتاح حالياً هو طب/علمي؛ لا نحقنه
                            # في سؤال أدبي فيعيد جذب النموذج إلى الفرع القديم.
                            textual_support = []
                        focused_set = set(all_focused)
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
                        # The admissions digest intentionally contains no
                        # tuition values.  For compound questions such as
                        # "price + admission cutoff", keep the already
                        # retrieved, entity-matching raw fee record even when
                        # it comes from the same admissions/fees collection.
                        focused_fee_evidence = []
                        if any(
                            claim.canonical_field == "fee"
                            for claim in plan.claims
                        ):
                            focused_fee_evidence = [
                                chunk for chunk in relevant_chunks
                                if (
                                    chunk in focused_set
                                    and _matches_specific_entity(chunk)
                                    and any(
                                        marker in normalize_arabic(chunk)
                                        for marker in (
                                            "credit_hour_fee", "سعر الساعه",
                                            "رسوم الساعه", "دينار",
                                        )
                                    )
                                )
                            ]
                        relevant_chunks = (
                            [digest_chunk]
                            + list(dict.fromkeys(textual_support))
                            + list(dict.fromkeys(focused_fee_evidence))
                            + other_evidence
                        )
                    else:
                        # When a large admission collection is bounded to a
                        # handful of focused hits, remove the rest of that
                        # same collection from the general retrieval window;
                        # otherwise it silently defeats the configured cap.
                        seen = set(all_focused)
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
        exclusion_markers = query_rewrite.exclusion_file_markers(excluded)

        def _filter_excluded_chunks(values: list[str]) -> list[str]:
            if not exclusion_markers:
                return list(values)
            kept = [c for c in values
                    if not (c.startswith("[ملف: ") and any(
                        marker in normalize_arabic(c[6:c.find("]")])
                        for marker in exclusion_markers))]
            return kept

        if excluded and relevant_chunks:
            relevant_chunks = _filter_excluded_chunks(relevant_chunks)

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

        parent_expansion_added = 0
        if agent_plan.use_parent_expansion and relevant_chunks:
            relevant_chunks, parent_expansion_added = (
                self._uploaded.expand_parent_chunks(
                    relevant_chunks,
                    max_additions=config.PARENT_EXPANSION_MAX_CHUNKS,
                )
            )
            # A parent/sibling expansion may cross back into a collection the
            # user explicitly excluded.  Enforce the same boundary again.
            relevant_chunks = _filter_excluded_chunks(relevant_chunks)
            # Parent/sibling expansion must not reintroduce adjacent roles
            # (for example a vice dean) after an exact institutional-role
            # query was already focused to deans only.
            if role_focus_applied:
                relevant_chunks, _ = query_rewrite.prefer_exact_role_chunks(
                    search_question, relevant_chunks
                )

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

        recency_resolutions: list[dict] = []

        def _deduplicate_runtime_evidence(values: list[str]):
            metadata = self._uploaded.candidate_metadata_for_chunks(values)
            source_recency = (
                {} if config.API_ENV == "testing"
                else file_catalog.recency_map()
            )
            conflict_overrides = (
                {} if config.API_ENV == "testing"
                else file_catalog.fact_resolution_map()
            )
            result = deduplicate_evidence(
                values,
                metadata,
                source_recency=source_recency,
                conflict_overrides=conflict_overrides,
            )
            result.conflicts = _conflicts_relevant_to_plan(
                plan, result.conflicts
            )
            if result.resolved_conflicts:
                lines = []
                for conflict in result.resolved_conflicts:
                    lines.append(
                        f"{conflict['canonical_field']} | {conflict['entity']} | "
                        f"القيمة المعتمدة: {conflict['selected_value']} | "
                        f"المصدر: {conflict['selected_source']} | "
                        f"تاريخ المصدر: {conflict['selected_date']}"
                    )
                resolution_chunk = (
                    "[حسم تعارض بالحداثة؛ تجاهل القيم الأقدم المخالفة]\n"
                    + "\n".join(lines)
                )
                if resolution_chunk not in authoritative:
                    authoritative.append(resolution_chunk)
                kept_metadata = [
                    metadata[index] if index < len(metadata) else {}
                    for index in result.kept_indexes
                ]
                result.chunks = suppress_rejected_conflict_values(
                    result.chunks,
                    result.resolved_conflicts,
                    kept_metadata,
                )
            recency_resolutions[:] = result.resolved_conflicts
            merged = list(dict.fromkeys([
                *authoritative,
                *result.chunks,
            ]))
            return merged, result

        relevant_chunks, deduplication = _deduplicate_runtime_evidence(
            relevant_chunks
        )
        contract = build_evidence_contract(
            plan,
            frame,
            relevant_chunks,
            authoritative_evidence=authoritative,
            evidence_conflicts=deduplication.conflicts,
        )
        if (
            config.SEMANTIC_RAG_ENABLED
            and not semantic_planner.get("called")
            and (
                contract.missing_fields
                or not contract.entity_supported
            )
        ):
            _invoke_semantic_planner([
                *contract.missing_fields,
                *( ["entity_binding"] if not contract.entity_supported else [] ),
            ])
            contract = build_evidence_contract(
                plan,
                frame,
                relevant_chunks,
                authoritative_evidence=authoritative,
                evidence_conflicts=deduplication.conflicts,
            )
        coverage_retry_query = None
        coverage_retry_added = 0
        reranker_failed = rerank_status in {
            "error_fallback", "empty_fallback", "circuit_open_fallback"
        }
        if (
            config.EVIDENCE_CONTRACT_ENABLED
            and agent_plan.allow_evidence_retry
            and (
                contract.missing_fields
                or contract.contradictions
                or not contract.entity_supported
                or reranker_failed
            )
            and not safety_directive
            and not skip_general_retrieval
        ):
            missing_claims = [
                claim for claim in plan.claims
                if not (
                    contract.claim_coverage.get(claim.claim_id, {})
                    .get("resolved", False)
                )
            ]
            claim_retry_queries = list(dict.fromkeys(
                claim.retrieval_query for claim in missing_claims
            ))
            coverage_retry_query = (
                " || ".join(claim_retry_queries)
                if claim_retry_queries
                else missing_field_query(plan, contract)
            )
            if not coverage_retry_query and reranker_failed:
                coverage_retry_query = (
                    plan.standalone_query
                    + " (توسيع RRF بعد تعذر إعادة الترتيب)"
                )
            if coverage_retry_query:
                retry_k = max(4, min(config.COVERAGE_TOP_K, top_k))
                if claim_retry_queries:
                    per_claim_k = max(
                        2,
                        (retry_k + len(claim_retry_queries) - 1)
                        // len(claim_retry_queries),
                    )
                    batches = [
                        self._search_all_for_question(
                            value,
                            per_claim_k,
                            allowed_collections,
                        )
                        for value in claim_retry_queries
                    ]
                    extra_chunks = []
                    seen_retry = set()
                    for position in range(max(
                        (len(batch) for batch in batches), default=0
                    )):
                        for batch in batches:
                            if position >= len(batch):
                                continue
                            chunk = batch[position]
                            if chunk in seen_retry:
                                continue
                            seen_retry.add(chunk)
                            extra_chunks.append(chunk)
                            if len(extra_chunks) >= retry_k:
                                break
                        if len(extra_chunks) >= retry_k:
                            break
                else:
                    extra_chunks = self._search_all_for_question(
                        coverage_retry_query,
                        retry_k,
                        allowed_collections,
                    )
                extra_chunks = _filter_excluded_chunks(extra_chunks)
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
                    relevant_chunks, deduplication = (
                        _deduplicate_runtime_evidence(relevant_chunks)
                    )
                    contract = build_evidence_contract(
                        plan,
                        frame,
                        relevant_chunks,
                        authoritative_evidence=authoritative,
                        evidence_conflicts=deduplication.conflicts,
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
        if plan.requires_clarification:
            if normalize_arabic(question).startswith("كم ساع"):
                dynamic_instructions.append(
                    "صغ سؤال توضيح واحداً فقط: هل تقصد سعر الساعة الدراسية "
                    "أم عدد الساعات المعتمدة في الخطة؟ لا تعرض رقماً قبل التحديد."
                )
            else:
                dynamic_instructions.append(
                    "توقف عن الاسترجاع لهذا الدور وصغ سؤال توضيح واحداً فقط؛ "
                    "لا تفترض أن «المفتاح» يعني مفتاح القبول بلا سياق أكاديمي."
                )
        if reranker_failed:
            dynamic_instructions.append(
                "تعذرت إعادة الترتيب واستخدم النظام RRF الموسع؛ لا تدّعِ أن "
                "المعلومة غير موجودة إلا إذا ظل عقد الأدلة ناقصاً بعد المحاولة الثانية."
            )
        if self._uploaded.failed_refresh_sources:
            dynamic_instructions.append(
                "تعذر تحديث مصدر واحد على الأقل وبقيت نسخته السابقة فعالة؛ "
                "لا تستخدم نفياً قطعياً لمعلومة غائبة، وقل إنك لا تستطيع "
                "تأكيدها من النسخة المتاحة حالياً."
            )
        if plan.live_policy == "dated_caveat":
            dynamic_instructions.append(
                "السؤال يطلب حالة الآن/اليوم، لكن هذا الإصدار لا يجلب الويب "
                "مباشرة. اذكر فقط آخر معلومة واردة وتاريخ تحققها إن وُجد، "
                "وقل إنها لا تثبت الحالة الحالية، ثم وجّه إلى الرابط الرسمي "
                "الموجود في الدليل. غياب تحديث ليس نفياً ولا إثباتاً."
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
- يجوز الإطالة بقدر إكمال قائمة شاملة، ودون أي سرد زائد خارج عناصرها.
- إن كانت المقاطع نفسها لا تسند قائمة كاملة، اذكر حدود المتاح بوضوح ولا تخترع.
"""
        if query_rewrite.is_multi_part_question(question):
            system += """

تعليمات خاصة بالسؤال المركب: جزّئ المطلوب إلى بنود قصيرة، وأجب كل جزء صراحةً
من الأدلة قبل إنهاء الإجابة، ويجوز الإطالة بقدر الحاجة لإكمال الأجزاء.
"""

        agent_metadata = agent_plan.as_metadata()
        agent_metadata["retrieval_attempts_used"] = (
            0 if skip_general_retrieval else 1 + int(bool(coverage_retry_query))
        )
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
            "retrieval_cache_hit": False,
            "force_refresh": repeated_after_low_confidence,
            "previous_turn_status": previous_turn.get("status"),
            "index_refresh_failures": self._uploaded.failed_refresh_sources,
            "retrieval_degraded": bool(
                reranker_failed or self._uploaded.failed_refresh_sources
            ),
            "active_academic_constraints": active_constraints,
            "query_plan": plan.as_metadata(),
            "claim_plan": [claim.as_metadata() for claim in plan.claims],
            "unresolved_clauses": list(plan.unresolved_clauses),
            "concept_resolutions": [
                item.as_metadata() for item in plan.concept_resolutions
            ],
            "semantic_planner": semantic_planner,
            "semantic_planner_call_count": int(bool(
                semantic_planner.get("called")
            )),
            "domain_route": domain_route.as_metadata(),
            "conversation_frame": frame.as_metadata(),
            "evidence_contract": contract.as_metadata(),
            "claim_coverage": contract.claim_coverage,
            "deduplicated_evidence_count": len(relevant_chunks),
            "deduplicated_fact_count": len(deduplication.items),
            "duplicate_evidence_removed": deduplication.duplicate_count,
            "evidence_conflicts": deduplication.conflicts,
            "recency_conflict_resolutions": recency_resolutions,
            "agentic_rag": agent_metadata,
            "retrieval_attempts": agent_metadata["retrieval_attempts_used"],
            "parent_expansion_added": parent_expansion_added,
            "coverage_retry_query": coverage_retry_query,
            "coverage_retry_added": coverage_retry_added,
            "candidate_metadata": self._uploaded.candidate_metadata_for_chunks(
                relevant_chunks
            ),
            "source_metadata_extracted": source_metadata_extracted,
            "authoritative_evidence_count": len(authoritative),
            "live_policy": plan.live_policy,
            "safety_directive_applied": bool(safety_directive),
            "pipeline_version": config.RAG_PIPELINE_VERSION,
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
            "strategy": "bounded_agentic_structured_hybrid_parent_rerank",
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

    def stream_answer(
        self,
        question: str,
        principal: Principal,
        *,
        allowed_collections,
        trace_id: str | None = None,
    ):
        """Validated streaming twin of ``chat_as_principal``.

        Generation is buffered server-side, checked, and corrected before the
        first answer byte is emitted.  The validated LLM answer is then sent in
        small chunks so the UI keeps its streaming behaviour without exposing a
        rejected first draft.

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

        try:
            result = self.chat_with_all_files(
                question,
                principal.subject,
                private_context=private_context,
                allowed_collections=allowed_collections,
                role_prompt=prompt_for(principal),
                retrieval_question=retrieval_question,
                safety_directive=safety_directive,
                authoritative_evidence=authoritative_evidence,
                trace_id=trace_id,
            )
        except ChatbotError as exc:
            yield "⚠️ " + exc.message
            return

        answer = result["answer"]
        if not answer:
            return
        for start in range(0, len(answer), 96):
            yield answer[start:start + 96]

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
