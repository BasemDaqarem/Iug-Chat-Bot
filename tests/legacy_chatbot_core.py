import os
import time
from typing import List, Optional
import numpy as np
import requests
from pymongo.errors import PyMongoError

from database import get_collection, list_collection_names

from uploaded_files_db import (
    get_uploaded_collection,
    list_uploaded_collections,
    drop_uploaded_collection,
)


CHAT_API_MODEL = os.getenv("CHAT_API_MODEL", "openai/gpt-oss-120b")
CHAT_API_KEY = os.getenv("CHAT_API_KEY", "")
CHAT_API_URL = os.getenv("CHAT_API_URL", "")

EMBED_MODEL = os.getenv("EMBED_MODEL", "jina-embeddings-v3")
EMBED_API_KEY = os.getenv("EMBED_API_KEY", "")
EMBED_API_URL = os.getenv("EMBED_API_URL", "")
TOP_K = 10
MAX_HISTORY = 20
SIM_THRESHOLD = 0.25
LLM_MAX_TOKENS = 450

# Collections that exist in the DB but should NOT be indexed as RAG content
# (e.g. internal/ops collections). Configurable via .env, no code change
# needed to add/remove a collection from the RAG pipeline.
RAG_EXCLUDE_COLLECTIONS = {
    c.strip() for c in os.getenv("RAG_EXCLUDE_COLLECTIONS", "").split(",") if c.strip()
}

SYSTEM_PROMPT_TEMPLATE = """\
أنت مساعد جامعي ذكي ومتخصص للجامعة الإسلامية بغزة.

فيما يلي المعلومات ذات الصلة بسؤال الطالب — مستخرجة من قاعدة بيانات الجامعة:
────────────────────────────────────────
{context}
────────────────────────────────────────

تعليمات صارمة يجب الالتزام بها:
1. أجب *فقط* بناءً على المعلومات الواردة أعلاه.
2. لا تخترع أي رقم أو معلومة غير موجودة في النص أعلاه.
3. إذا لم تجد الإجابة بوضوح في النص، أجب بما تعرفه بشكل عام
   واذكر أن المعلومة التفصيلية تحتاج تأكيد من الجامعة مباشرةً.
   لا تقل "لا أعلم" وتوقف — أضف سياقاً مفيداً دائماً.
4. أجب بالعربية فقط في جميع الأحوال.
5. ⚠️ خصوصية صارمة: بيانات الترتيب والمعدل التراكمي خاصة بكل طالب.
   - إذا سألك الطالب عن معدل أو ترتيب طالب آخر بالاسم أو برقم الهوية → أجب فوراً: "عذراً، هذه البيانات خاصة ولا يمكن الاطلاع عليها."
   - لا تذكر أي معدل أو ترتيب لأي شخص غير الطالب الحالي تحت أي ظرف.
   - حتى لو وُجدت البيانات في السياق أعلاه، لا تُفصح عنها إذا كانت لطالب آخر.
6. اجب على السؤال بشكل مباشر ولا تجيب بمواضيع اخرى لم تطرح
   فقط اجب بما يتم سؤالك اياه بطريقة مختصرة ومميزة.
7. الحد الأقصى للإجابة: 300 حرف — لخّص عند الحاجة.
8. اذا سالك الطالب عن حالته الاكاديمية او عن خطورته اجب عليه حسب اليوم الحالي، لا تجب بمعلومات مستقبلية
   واذا سالك الطالب عن سبب ضعف او تحسن مستواه اجبه، واذا سالك الطالب كيف يحسن من اداءه اعطيه اقتراحات لتحسين اداءه بناءاً على الضعف الذي يوجدلديه.
"""

UPLOADED_FILE_SYSTEM_PROMPT = """\
أنت مساعد ذكي متخصص في الإجابة على الأسئلة بناءً على محتوى الملف المُرفق فقط.

فيما يلي أكثر المقاطع ذات الصلة بسؤال الطالب من الملف المُرفق (وليس الملف كاملاً):
────────────────────────────────────────
{context}
────────────────────────────────────────

تعليمات صارمة يجب الالتزام بها:
1. أجب *فقط* بناءً على المعلومات الواردة أعلاه.
2. لا تخترع أي رقم أو معلومة غير موجودة في النص أعلاه.
3. إذا لم تجد الإجابة بوضوح في النص، أجب بما تعرفه بشكل عام
   واذكر أن المعلومة التفصيلية تحتاج تأكيد من الجامعة مباشرةً.
   لا تقل "لا أعلم" وتوقف — أضف سياقاً مفيداً دائماً.
4. أجب بالعربية فقط في جميع الأحوال.
5. ⚠️ خصوصية صارمة: بيانات الترتيب والمعدل التراكمي خاصة بكل طالب.
   - إذا سألك الطالب عن معدل أو ترتيب طالب آخر → أجب: "عذراً، هذه البيانات خاصة."
6. اجب على السؤال بشكل مباشر ولا تجيب بمواضيع اخرى لم تطرح
   فقط اجب بما يتم سؤالك اياه بطريقة مختصرة ومميزة.
7. الحد الأقصى للإجابة: 300 حرف — لخّص عند الحاجة.
8. اذا سالك الطالب عن حالته الاكاديمية او عن خطورته اجب عليه حسب اليوم الحالي، لا تجب بمعلومات مستقبلية
   واذا سالك الطالب عن سبب ضعف او تحسن مستواه اجبه، واذا سالك الطالب كيف يحسن من اداءه اعطيه اقتراحات لتحسين اداءه بناءاً على الضعف الذي يوجدلديه.
"""


# -> Core engine for IUG Chatbot.
# -> Encapsulates all business logic: dynamic data loading, chunk building,
# -> semantic indexing, session history, and LLM calls.
class IUGChatbot:

    # Marker used to tag chunks built from documents that declare an
    # access-control list (`privacy.allowed_users`). This is a STRUCTURAL
    # convention, not a hardcoded collection name — any collection whose
    # documents follow this shape is automatically privacy-protected.
    SENSITIVE_MARKER = "SENSITIVE"

    def __init__(self):
        self._data: dict = None              # {collection_name: [raw_docs...]}
        self._chunks: List[str] = None        # flat list of chunk texts (public, unchanged shape)
        self._chunk_meta: List[dict] = None   # parallel metadata, same length/order as _chunks
        self._index: np.ndarray = None
        self._sessions: dict = {}
        self._uploaded_chunks: dict = {}
        self._uploaded_indexes: dict = {}

    # Load data, build chunks, build semantic index.
    def initialize(self):
        print("⏳ Discovering & loading MongoDB collections …")
        self._data = self._load_data()
        self._chunks, self._chunk_meta = self._build_chunks(self._data)
        print(f"✅ Built {len(self._chunks)} chunks from {len(self._data)} collection(s).")

        print(f"⏳ Using Jina Embeddings API — model: '{EMBED_MODEL}' …")
        if not EMBED_API_KEY:
            raise RuntimeError("❌ EMBED_API_KEY غير موجود — أضفه في ملف .env")

        print("⏳ Building semantic index …")
        self._index = self._build_index(self._chunks)
        print(f"✅ Semantic index ready — shape: {self._index.shape}")

        self._load_all_uploaded_files()

    # ═════════════════════════════════════════════════════════════════════════
    #  PUBLIC PROPERTIES
    # ═════════════════════════════════════════════════════════════════════════

    @property
    def data(self) -> dict:
        """{collection_name: [raw documents]} — fully dynamic, mirrors MongoDB."""
        return self._data

    @property
    def chunks(self) -> List[str]:
        return self._chunks

    @property
    def index(self) -> np.ndarray:
        return self._index

    # ═════════════════════════════════════════════════════════════════════════
    #  SECTION 1 — DATA HELPERS (fully dynamic, no hardcoded collection names)
    # ═════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _load_data() -> dict:
        """
        Discover every collection in MongoDB and load ALL of its documents,
        with no assumption about field names or document shape. Adding a new
        collection to the database is enough for it to show up here — no
        code change required.
        """
        try:
            names = [
                n for n in list_collection_names()
                if n not in RAG_EXCLUDE_COLLECTIONS
            ]
            data: dict = {}
            for name in names:
                docs = list(get_collection(name).find({}))
                for doc in docs:
                    if "_id" in doc:
                        doc["_id"] = str(doc["_id"])
                data[name] = docs
        except PyMongoError as exc:
            raise RuntimeError(
                f"❌ MongoDB query failed: {exc}\n"
                "Check MONGO_URI in .env and Atlas network access."
            ) from exc

        total_docs = sum(len(v) for v in data.values())
        print(f"✅ Data loaded from MongoDB — {len(data)} collection(s), {total_docs} document(s) total.")
        return data

    # ── Generic, structural (not name-based) document introspection ───────
    #
    # These helpers let the privacy guard, ranking shortcut, etc. keep
    # working WITHOUT hardcoding a collection name like "students_rankings".
    # Any collection whose documents follow these conventions is handled
    # automatically:
    #   • sensitive record  → doc has a `privacy.allowed_users` list
    #   • owner id          → first of: student_id / id_student / user_id /
    #                          owner_id fields, else first allowed_users entry
    #   • display name      → first of: student_name / name / full_name / title

    @staticmethod
    def _is_sensitive_doc(doc: dict) -> bool:
        privacy = doc.get("privacy")
        return isinstance(privacy, dict) and bool(privacy.get("allowed_users"))

    @staticmethod
    def _extract_owner_id(doc: dict) -> Optional[str]:
        for key in ("student_id", "id_student", "user_id", "owner_id"):
            if doc.get(key) not in (None, ""):
                return str(doc[key])
        privacy = doc.get("privacy") if isinstance(doc.get("privacy"), dict) else None
        if privacy and privacy.get("allowed_users"):
            return str(privacy["allowed_users"][0])
        return None

    @staticmethod
    def _extract_display_name(doc: dict) -> Optional[str]:
        for key in ("student_name", "name", "full_name", "title"):
            if doc.get(key):
                return str(doc[key])
        return None

    # ═════════════════════════════════════════════════════════════════════════
    #  SECTION 2 — CHUNK BUILDER (generic — identical logic for every collection)
    # ═════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _flatten_json_to_text(obj, prefix: str = "") -> List[str]:
        """Recursively flatten any JSON-like structure into 'key: value' lines."""
        lines = []
        if isinstance(obj, dict):
            for key, value in obj.items():
                full_key = f"{prefix}.{key}" if prefix else key
                if isinstance(value, (dict, list)):
                    lines.extend(IUGChatbot._flatten_json_to_text(value, full_key))
                else:
                    lines.append(f"{full_key}: {value}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                full_key = f"{prefix}[{i}]"
                if isinstance(item, (dict, list)):
                    lines.extend(IUGChatbot._flatten_json_to_text(item, full_key))
                else:
                    lines.append(f"{prefix}: {item}")
        else:
            lines.append(f"{prefix}: {obj}")
        return lines

    @classmethod
    def _doc_to_chunk_texts(
        cls,
        collection_name: str,
        doc: dict,
        sensitive: bool,
        owner_id: Optional[str],
    ) -> List[str]:
        """
        Turn ONE document into one or more chunk texts.

        Generic rule, applied uniformly to every collection (no per-type
        logic): scalar fields become one "overview" chunk; any field that is
        a list of sub-objects is additionally split into one chunk per item
        (each carrying the parent's scalar fields as shared context). This
        keeps retrieval granularity comparable to hand-written chunking
        (e.g. one chunk per program/grant/faculty) without any bespoke code.
        """
        scalars, nested_lists = {}, {}
        for key, value in doc.items():
            if isinstance(value, list) and value and all(isinstance(i, dict) for i in value):
                nested_lists[key] = value
            else:
                scalars[key] = value

        header = (
            f"[{cls.SENSITIVE_MARKER}|collection={collection_name}|owner={owner_id}]"
            if sensitive else f"[{collection_name}]"
        )

        parent_lines = cls._flatten_json_to_text(scalars)
        parent_ctx = "\n".join(parent_lines)

        texts: List[str] = []
        if parent_lines or not nested_lists:
            texts.append(header + (f"\n{parent_ctx}" if parent_ctx else ""))

        for field_name, items in nested_lists.items():
            for item in items:
                item_lines = cls._flatten_json_to_text(item)
                if not item_lines:
                    continue
                piece = f"{header} :: {field_name}\n"
                if parent_ctx:
                    piece += parent_ctx + "\n"
                piece += "\n".join(item_lines)
                texts.append(piece)

        return texts

    def _build_chunks(self, data: dict):
        """
        Build chunks for ALL collections generically. Returns (chunks, meta)
        where meta[i] describes chunks[i] (same order/length) — used
        internally for the privacy guard / academic-status shortcut without
        ever hardcoding a collection name.
        """
        chunks: List[str] = []
        meta: List[dict] = []

        for collection_name, docs in data.items():
            for doc in docs:
                doc_copy = dict(doc)
                doc_id = doc_copy.pop("_id", None)
                doc_copy.pop("seeded_at", None)

                sensitive = self._is_sensitive_doc(doc_copy)
                owner_id = self._extract_owner_id(doc_copy) if sensitive else None
                display_name = self._extract_display_name(doc_copy)

                for text in self._doc_to_chunk_texts(collection_name, doc_copy, sensitive, owner_id):
                    chunks.append(text)
                    meta.append({
                        "collection": collection_name,
                        "doc_id": doc_id,
                        "sensitive": sensitive,
                        "owner_id": owner_id,
                        "display_name": display_name,
                        "raw": doc_copy,
                    })

        return chunks, meta

    # ═════════════════════════════════════════════════════════════════════════
    #  SECTION 3 — SEMANTIC INDEX
    # ═════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _jina_embed(texts: List[str]) -> np.ndarray:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {EMBED_API_KEY}",
        }
        data = {"model": EMBED_MODEL, "input": texts}
        resp = requests.post(EMBED_API_URL, headers=headers, json=data, timeout=120)
        resp.raise_for_status()
        embeddings = [item["embedding"] for item in resp.json()["data"]]
        return np.array(embeddings, dtype=np.float32)

    @staticmethod
    def _call_groq(headers: dict, payload: dict, max_retries: int = 4) -> str:
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(CHAT_API_URL, headers=headers, json=payload, timeout=60)

                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else (2 ** attempt)
                    print(f"⚠️  Groq 429 — المحاولة {attempt}/{max_retries}، انتظار {wait:.1f}s …")
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()

            except requests.exceptions.ConnectionError:
                raise RuntimeError("❌ تعذّر الاتصال بـ Groq API — تحقق من الاتصال بالإنترنت.")
            except requests.exceptions.Timeout:
                if attempt < max_retries:
                    wait = 2 ** attempt
                    print(f"⏱️  Groq Timeout — المحاولة {attempt}/{max_retries}، انتظار {wait}s …")
                    time.sleep(wait)
                    continue
                raise RuntimeError("❌ Groq API استغرق وقتاً طويلاً — حاول مرة أخرى.")
            except Exception as exc:
                raise RuntimeError(f"❌ خطأ في Groq: {exc}")

        raise RuntimeError(
            "❌ Groq API: تجاوزنا الحد المسموح به من الطلبات (429). "
            "حاول بعد لحظات أو تحقق من خطة Groq الخاصة بك."
        )

    @staticmethod
    def _build_index(chunks: List[str]) -> np.ndarray:
        if not chunks:
            return np.array([], dtype=np.float32)
        batch_size = 64
        all_embeddings = []
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            print(f"   Embedding batch {i // batch_size + 1} ({len(batch)} chunks) …")
            embeddings = IUGChatbot._jina_embed(batch)
            all_embeddings.append(embeddings)
        result = np.vstack(all_embeddings) if all_embeddings else np.array([], dtype=np.float32)
        norms = np.linalg.norm(result, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        return result / norms

    @staticmethod
    def _embed_query(question: str) -> np.ndarray:
        """Embed a single query and L2-normalize it into a column vector."""
        q_arr = IUGChatbot._jina_embed([question])
        norm = np.linalg.norm(q_arr)
        return (q_arr / norm if norm != 0 else q_arr).T

    @staticmethod
    def _rank_chunks(
        q_vec: np.ndarray,
        chunks: List[str],
        index: Optional[np.ndarray],
        top_k: int,
        threshold: float,
    ) -> List[str]:
        """Shared cosine-similarity ranking, used by both the main index and
        every per-uploaded-file index."""
        if index is None or len(chunks) == 0 or getattr(index, "size", 0) == 0:
            return []

        scores = (index @ q_vec).flatten()
        ranked = np.argsort(scores)[::-1]

        results = []
        for idx in ranked[:top_k]:
            if float(scores[idx]) >= threshold:
                results.append(chunks[int(idx)])

        if not results and len(ranked):
            results.append(chunks[int(ranked[0])])

        return results

    def semantic_search(
        self,
        question: str,
        top_k: int = TOP_K,
        threshold: float = SIM_THRESHOLD,
    ) -> List[str]:
        q_vec = self._embed_query(question)
        return self._rank_chunks(q_vec, self._chunks, self._index, top_k, threshold)

    # ═════════════════════════════════════════════════════════════════════════
    #  SECTION 4 — UPLOADED FILES (each file gets its own chunks + index)
    # ═════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _build_uploaded_chunks(docs: List[dict], collection_name: str) -> List[str]:
        chunks = []
        for doc in docs:
            doc = dict(doc)
            doc.pop("_id", None)
            doc.pop("__file_meta__", None)
            flat_lines = IUGChatbot._flatten_json_to_text(doc)
            if flat_lines:
                chunk_text = f"[ملف: {collection_name}]\n" + "\n".join(flat_lines)
                chunks.append(chunk_text)
        return chunks

    def _load_all_uploaded_files(self):
        try:
            collections = list_uploaded_collections()
            if not collections:
                print("ℹ️  No uploaded files found in MongoDB.")
                return
            for col_name in collections:
                self._load_uploaded_collection(col_name)
            print(f"✅ Loaded {len(self._uploaded_chunks)} uploaded file(s).")
        except Exception as exc:
            print(f"⚠️  Could not load uploaded files: {exc}")

    def _load_uploaded_collection(self, collection_name: str):
        """
        Load one uploaded file's documents, build its chunks, AND build a
        dedicated embeddings index for it, so chat_with_file() can run
        semantic search over just this file instead of sending it whole.
        """
        col = get_uploaded_collection(collection_name)
        docs = list(col.find({}))
        if not docs:
            self._uploaded_chunks.pop(collection_name, None)
            self._uploaded_indexes.pop(collection_name, None)
            return

        chunks = self._build_uploaded_chunks(docs, collection_name)
        if not chunks:
            return

        self._uploaded_chunks[collection_name] = chunks
        try:
            self._uploaded_indexes[collection_name] = self._build_index(chunks)
            print(f"   ✅ Indexed uploaded file '{collection_name}' ({len(chunks)} chunks).")
        except Exception as exc:
            # Keep the chunks even if embeddings fail — chat_with_file() has
            # a safe fallback — but drop any stale index.
            self._uploaded_indexes.pop(collection_name, None)
            print(f"   ⚠️  Failed to build embeddings for '{collection_name}': {exc}")

    def upload_json_file(self, collection_name: str, json_data: list) -> dict:
        col = get_uploaded_collection(collection_name)
        if isinstance(json_data, dict):
            json_data = [json_data]
        if not isinstance(json_data, list):
            raise ValueError("محتوى الملف يجب أن يكون JSON object أو array.")
        cleaned = []
        for item in json_data:
            if isinstance(item, dict):
                item.pop("_id", None)
                cleaned.append(item)
            else:
                cleaned.append({"value": item})
        col.drop()
        if cleaned:
            col.insert_many(cleaned)
        # Rebuilds chunks + embeddings + index for this file.
        self._load_uploaded_collection(collection_name)
        return {"inserted": len(cleaned), "collection": collection_name}

    def chat_with_file(self, question: str, collection_name: str, session_id: str) -> dict:
        if collection_name not in self._uploaded_chunks:
            return {
                "answer":     f"الملف '{collection_name}' غير موجود. يرجى رفع الملف أولاً.",
                "top_chunks": [],
                "source":     "uploaded_file",
            }

        all_chunks = self._uploaded_chunks[collection_name]
        if not all_chunks:
            return {
                "answer":     "لا تتوفر هذه المعلومة في الملف المُرفق.",
                "top_chunks": [],
                "source":     "uploaded_file",
            }

        index = self._uploaded_indexes.get(collection_name)
        if index is not None and getattr(index, "size", 0) and len(index) == len(all_chunks):
            # Normal path: semantic search restricted to THIS file only —
            # the LLM only ever sees the top-K relevant chunks, never the
            # whole file.
            q_vec = self._embed_query(question)
            relevant_chunks = self._rank_chunks(q_vec, all_chunks, index, TOP_K, SIM_THRESHOLD)
        else:
            # Degraded fallback (e.g. embeddings API was unreachable when
            # the file was indexed): bound the payload instead of sending
            # everything.
            relevant_chunks = all_chunks[:TOP_K]

        context      = "\n\n---\n\n".join(relevant_chunks)
        history      = self.get_history(session_id)
        history_text = self.fmt_history(history)
        system       = UPLOADED_FILE_SYSTEM_PROMPT.format(context=context)
        user_message = f"{history_text}السؤال: {question}"

        if not CHAT_API_KEY:
            raise RuntimeError("❌ CHAT_API_KEY غير موجود — أضفه في ملف .env")

        headers = {
            "Authorization": f"Bearer {CHAT_API_KEY}",
            "Content-Type":  "application/json",
        }
        payload = {
            "model":       CHAT_API_MODEL,
            "messages":    [
                {"role": "system", "content": system},
                {"role": "user",   "content": user_message},
            ],
            "temperature": 0.05,
            "max_tokens":  LLM_MAX_TOKENS,
        }

        answer = self._call_groq(headers, payload)
        self.push_history(session_id, question, answer)
        return {"answer": answer, "top_chunks": relevant_chunks, "source": "uploaded_file"}

    def chat_with_all_files(
        self,
        question: str,
        session_id: str,
        top_k: int = TOP_K,
    ) -> dict:
        """
        Same idea as chat_with_file(), but instead of restricting the search
        to ONE uploaded file, it searches across the indexes of ALL
        currently uploaded files and merges the results into a single
        global ranking — so the LLM only ever sees the best top-K chunks
        overall, regardless of which file they came from.
 
        Each chunk already carries a "[ملف: <اسم الملف>]" header (added in
        _build_uploaded_chunks), so both the LLM and the caller can tell
        which file a piece of context came from.
        """
        if not self._uploaded_chunks:
            return {
                "answer":     "لا توجد ملفات مرفوعة حالياً.",
                "top_chunks": [],
                "source":     "uploaded_files_all",
            }
 
        q_vec = self._embed_query(question)
 
        # ── Merge candidates from every file's own index into one pool ────
        scored: List[tuple] = []  # (score, chunk_text, collection_name)
        for collection_name, chunks in self._uploaded_chunks.items():
            index = self._uploaded_indexes.get(collection_name)
            if index is None or not getattr(index, "size", 0) or len(index) != len(chunks):
                continue  # this file has no usable embeddings yet — skip it
            scores = (index @ q_vec).flatten()
            for i, score in enumerate(scores):
                scored.append((float(score), chunks[i], collection_name))
 
        if scored:
            scored.sort(key=lambda t: t[0], reverse=True)
            top = [t for t in scored if t[0] >= SIM_THRESHOLD][:top_k]
            if not top:
                top = scored[:1]
            relevant_chunks = [text for _, text, _ in top]
        else:
            # Degraded fallback: no file has a usable index yet — take a
            # bounded sample across files instead of dumping everything.
            relevant_chunks = []
            per_file_quota = max(1, top_k // max(1, len(self._uploaded_chunks)))
            for chunks in self._uploaded_chunks.values():
                relevant_chunks.extend(chunks[:per_file_quota])
            relevant_chunks = relevant_chunks[:top_k]
 
        context      = "\n\n---\n\n".join(relevant_chunks)
        history      = self.get_history(session_id)
        history_text = self.fmt_history(history)
        system       = UPLOADED_FILE_SYSTEM_PROMPT.format(context=context)
        user_message = f"{history_text}السؤال: {question}"
 
        if not CHAT_API_KEY:
            raise RuntimeError("❌ CHAT_API_KEY غير موجود — أضفه في ملف .env")
 
        headers = {
            "Authorization": f"Bearer {CHAT_API_KEY}",
            "Content-Type":  "application/json",
        }
        payload = {
            "model":       CHAT_API_MODEL,
            "messages":    [
                {"role": "system", "content": system},
                {"role": "user",   "content": user_message},
            ],
            "temperature": 0.05,
            "max_tokens":  LLM_MAX_TOKENS,
        }
 
        answer = self._call_groq(headers, payload)
        self.push_history(session_id, question, answer)
        return {"answer": answer, "top_chunks": relevant_chunks, "source": "uploaded_files_all"}
 
    def get_uploaded_files_list(self) -> List[dict]:
        return [
            {
                "collection":   name,
                "chunks_count": len(chunks),
                "indexed":      name in self._uploaded_indexes,
            }
            for name, chunks in self._uploaded_chunks.items()
        ]

    def reload_uploaded_file(self, collection_name: str) -> bool:
        try:
            self._load_uploaded_collection(collection_name)
            return True
        except Exception:
            return False

    def delete_uploaded_file(self, collection_name: str) -> bool:
        drop_uploaded_collection(collection_name)
        self._uploaded_chunks.pop(collection_name, None)
        self._uploaded_indexes.pop(collection_name, None)
        return True

    # ═════════════════════════════════════════════════════════════════════════
    #  SECTION 5 — SESSION HISTORY
    # ═════════════════════════════════════════════════════════════════════════

    def get_history(self, sid: str) -> list:
        return self._sessions.setdefault(sid, [])

    def push_history(self, sid: str, user: str, assistant: str):
        h = self.get_history(sid)
        h.append({"user": user, "assistant": assistant})
        if len(h) > MAX_HISTORY:
            self._sessions[sid] = h[-MAX_HISTORY:]

    def clear_history(self, sid: str):
        self._sessions.pop(sid, None)

    @staticmethod
    def fmt_history(history: list) -> str:
        if not history:
            return ""
        turns = "\n".join(
            f"الطالب: {t['user']}\nالمساعد: {t['assistant']}"
            for t in history[-6:]
        )
        return f"سجل المحادثة السابقة:\n{turns}\n\n"

    # ═════════════════════════════════════════════════════════════════════════
    #  SECTION 6 — CHAT (main orchestration)
    # ═════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _is_academic_status_question(question: str) -> bool:
        status_keywords = [
            "حالتي الاكاديمية", "حالتي الأكاديمية", "حالة اكاديمية", "حالة أكاديمية",
            "وضعي الاكاديمي", "وضعي الأكاديمي", "الوضع الاكاديمي", "الوضع الأكاديمي",
            "انا في خطر", "أنا في خطر", "في خطر", "خطر", "تعثر", "متعثر",
            "at risk", "risk",
        ]
        return any(keyword in question for keyword in status_keywords)

    # ── Generic sensitive-record lookups (replace the old hardcoded
    # `students_rankings` collection access with structural detection over
    # ANY collection's chunk metadata) ─────────────────────────────────────

    def _find_sensitive_record(self, session_id: str) -> Optional[dict]:
        """Return the chunk-meta of the sensitive record owned by session_id,
        regardless of which collection it lives in."""
        sid = str(session_id)
        for m in self._chunk_meta:
            if m.get("sensitive") and str(m.get("owner_id")) == sid:
                return m
        return None

    def _other_sensitive_display_names(self, session_id: str) -> List[str]:
        sid = str(session_id)
        names = set()
        for m in self._chunk_meta:
            if m.get("sensitive") and str(m.get("owner_id")) != sid and m.get("display_name"):
                names.add(m["display_name"])
        return list(names)

    @staticmethod
    def _format_sensitive_record_context(meta: dict) -> str:
        raw = dict(meta.get("raw") or {})
        raw.pop("privacy", None)
        lines = IUGChatbot._flatten_json_to_text(raw)
        body = "\n".join(lines)
        return f"بيانات الطالب الحالي (سري — للطالب نفسه فقط):\n{body}"

    @staticmethod
    def _build_status_from_sensitive_record(raw: dict) -> str:
        gpa  = raw.get("gpa", "غير متوفر")
        rank = raw.get("rank", "غير متوفر")
        return f"حالتك الأكاديمية الحالية: المعدل التراكمي {gpa}، والترتيب على الدفعة {rank}."

    def chat(self, question: str, session_id: str) -> dict:
        """
        Full chat pipeline:
        1. Semantic retrieval over the fully-dynamic chunk index
        2. Academic status shortcut (any sensitive Mongo record owned by the student)
        3. Privacy guard for ranking-style queries
        4. LLM call (Groq)
        """
        # ── Step 1: semantic retrieval ────────────────────────────────────
        relevant_chunks = self.semantic_search(
            question  = question,
            top_k     = TOP_K,
            threshold = SIM_THRESHOLD,
        )

        # ── Step 2: separate general chunks from privacy-sensitive ones ───
        sensitive_prefix = f"[{self.SENSITIVE_MARKER}|"
        general_chunks = [c for c in relevant_chunks if not c.startswith(sensitive_prefix)]

        # ── Step 3: detect ranking/GPA intent ──────────────────────────────
        ranking_keywords = ["معدل", "ترتيب", "gpa", "معدله", "ترتيبه", "معدلها", "ترتيبها"]
        asking_about_ranking = any(kw in question for kw in ranking_keywords)

        current_record = self._find_sensitive_record(session_id)

        # ── Step 4: academic status shortcut ─────────────────────────────
        if self._is_academic_status_question(question):
            if current_record:
                status_answer = self._build_status_from_sensitive_record(current_record["raw"])
                self.push_history(session_id, question, status_answer)
                return {"answer": status_answer, "top_chunks": []}

        # ── Step 5: privacy guard ─────────────────────────────────────────
        if asking_about_ranking:
            other_names = self._other_sensitive_display_names(session_id)
            first_tokens = [n.split()[0] for n in other_names if n]
            if any(tok in question for tok in first_tokens):
                blocked_answer = "عذراً، بيانات الترتيب والمعدلات خاصة بكل طالب ولا يمكن الاطلاع عليها."
                self.push_history(session_id, question, blocked_answer)
                return {"answer": blocked_answer, "top_chunks": []}

        # ── Step 6: build context ─────────────────────────────────────────
        student_context_chunk = ""
        if current_record:
            student_context_chunk = self._format_sensitive_record_context(current_record)
            context = "\n\n---\n\n".join([student_context_chunk] + general_chunks)
        else:
            context = "\n\n---\n\n".join(general_chunks)

        # ── Step 7: build prompt ──────────────────────────────────────────
        identity_note = ""
        if current_record:
            name = current_record.get("display_name") or "الطالب"
            identity_note = (
                f"\n\nالطالب الذي يحادثك الآن: {name} "
                f"(رقم الهوية: {current_record.get('owner_id')}). "
                f"أجبه عن بياناته مباشرة دون تحفظ، ولا تكشف بيانات أي طالب آخر."
            )

        history      = self.get_history(session_id)
        history_text = self.fmt_history(history)
        system       = SYSTEM_PROMPT_TEMPLATE.format(context=context) + identity_note
        user_message = f"{history_text}السؤال: {question}"

        # ── Step 8: call LLM ──────────────────────────────────────────────
        if not CHAT_API_KEY:
            raise RuntimeError("❌ CHAT_API_KEY غير موجود — أضفه في ملف .env")

        headers = {
            "Authorization": f"Bearer {CHAT_API_KEY}",
            "Content-Type":  "application/json",
        }
        payload = {
            "model":       CHAT_API_MODEL,
            "messages":    [
                {"role": "system", "content": system},
                {"role": "user",   "content": user_message},
            ],
            "temperature": 0.05,
            "max_tokens":  LLM_MAX_TOKENS,
        }

        answer = self._call_groq(headers, payload)
        self.push_history(session_id, question, answer)

        return {"answer": answer, "top_chunks": general_chunks}


# ═════════════════════════════════════════════════════════════════════════
#  CONSOLE TEST HARNESS
# Commands inside the loop:
#   exit / quit / خروج   → stop
#   files                → list currently loaded uploaded files
#   clear                → clear this session's chat history
#   data                 → print which Mongo collections were loaded + counts
if __name__ == "__main__":
    print("🚀 تشغيل IUG Chatbot — وضع الاختبار من الـ console")
    print("═" * 60)

    bot = IUGChatbot()
    try:
        bot.initialize()
    except Exception as exc:
        print(f"❌ فشل التهيئة (initialize): {exc}")
        raise SystemExit(1)

    print("═" * 60)
    print(f"📦 عدد الـ Collections المحمّلة: {len(bot.data)}")
    for col_name, docs in bot.data.items():
        print(f"   - {col_name}: {len(docs)} وثيقة")
    print(f"🧩 عدد الـ Chunks الكلي: {len(bot.chunks)}")
    print(f"📁 عدد الملفات المرفوعة المفهرسة: {len(bot.get_uploaded_files_list())}")
    print("═" * 60)

    session_id = input("🆔 أدخل session_id / رقم الطالب للاختبار (Enter لجلسة تجريبية): ").strip()
    if not session_id:
        session_id = "console_test_session"

    print("\n✅ الشات جاهز. اكتب سؤالك (أو 'exit' للخروج، 'files' لعرض الملفات، 'clear' لمسح السجل، 'data' لعرض بيانات الـ Collections).\n")

    while True:
        try:
            question = input("🧑 أنت: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 وداعًا")
            break

        if not question:
            continue

        if question.lower() in ("exit", "quit", "خروج"):
            print("👋 وداعًا")
            break

        if question.lower() == "files":
            files = bot.get_uploaded_files_list()
            if not files:
                print("📁 لا توجد ملفات مرفوعة حالياً.")
            for f in files:
                print(f"   - {f['collection']}: {f['chunks_count']} مقطع | مفهرس: {f['indexed']}")
            continue

        if question.lower() == "clear":
            bot.clear_history(session_id)
            print("🧹 تم مسح سجل المحادثة لهذه الجلسة.")
            continue

        if question.lower() == "data":
            for col_name, docs in bot.data.items():
                print(f"   - {col_name}: {len(docs)} وثيقة")
            continue

        try:
            result = bot.chat_with_all_files(question, session_id)
            print(f"\n🤖 المساعد: {result['answer']}")
            print(f"   (عدد المقاطع المستخدمة كسياق: {len(result.get('top_chunks', []))})\n")
        except Exception as exc:
            print(f"❌ خطأ أثناء المحادثة: {exc}\n")