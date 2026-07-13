"""
Per-session chat history.

Two interchangeable backends behind the same tiny interface (get / push /
clear / format_for_prompt):

  • SessionStore       — in-process dict; lost on restart (used by tests).
  • MongoSessionStore  — a `chat_sessions` collection; survives restarts and
                         is shared across processes (production default).

make_session_store() picks one from config.SESSION_BACKEND.
"""

from typing import List, Optional

import numpy as np

from app import config
from app.config import HISTORY_TURNS_IN_PROMPT, MAX_HISTORY
from app.log import get_logger

log = get_logger("sessions")

SESSIONS_COLLECTION = "chat_sessions"

# التعليمة المطلوبة قبل بيانات الذاكرة المحقونة (تسبق السياق الرئيسي في الرسالة).
MEMORY_INSTRUCTION = (
    "قد يكون سؤال المستخدم مرتبطاً ببيانات المحادثة السابقة أدناه. "
    "راجعها أولاً واستخدمها فقط عند وجود صلة، ثم تابع إلى السياق الرئيسي."
)


def _format_for_prompt(history: list) -> str:
    if not history:
        return ""
    turns = "\n".join(
        f"الطالب: {t['user']}\nالمساعد: {t['assistant']}"
        for t in history[-HISTORY_TURNS_IN_PROMPT:]
    )
    return f"سجل المحادثة السابقة:\n{turns}\n\n"


def relevant_turns(
    history: list, query_vec: Optional[np.ndarray], min_sim: float = None
) -> list:
    """الأدوار السابقة ذات الصلة بالسؤال الحالي.

    القاعدة: الدور الأحدث يُبقى دائماً (استمرارية الحوار — أسئلة «اقصد…»
    و«هذا الطلب» تعتمد عليه حتى لو تباعد متجهاها)، والأقدم منه يُبقى فقط إذا
    تجاوز تشابه متجه سؤاله المخزَّن العتبة. دور بلا متجه مخزَّن (مسار فوري
    قديم أو فشل تضمين سابق) لا يمكن تقييمه فيُستبعد إلا إذا كان الأحدث."""
    if not history:
        return []
    if query_vec is None:
        return history[-HISTORY_TURNS_IN_PROMPT:]
    if min_sim is None:
        min_sim = config.MEMORY_MIN_SIM
    q = np.asarray(query_vec).ravel()

    selected = []
    for turn in history[:-1]:
        stored = turn.get("embedding")
        if not stored:
            continue
        if float(np.dot(np.asarray(stored, dtype=np.float32), q)) >= min_sim:
            selected.append(turn)
    selected.append(history[-1])
    return selected


def format_memory(turns: list) -> str:
    """كتلة الذاكرة المحقونة قبل السياق الرئيسي، مسبوقة بالتعليمة الملزمة."""
    if not turns:
        return ""
    body = "\n".join(
        f"الطالب: {t['user']}\nالمساعد: {t['assistant']}" for t in turns
    )
    return f"{MEMORY_INSTRUCTION}\nسجل المحادثة السابقة:\n{body}\n\n"


class SessionStore:
    """In-memory history (lost on restart)."""

    def __init__(self, max_history: int = MAX_HISTORY):
        self._sessions: dict = {}
        self._max_history = max_history

    def get(self, sid: str) -> list:
        return self._sessions.setdefault(sid, [])

    def push(self, sid: str, user: str, assistant: str, embedding=None):
        h = self.get(sid)
        turn = {"user": user, "assistant": assistant}
        if embedding is not None:  # يُخزَّن مرة واحدة — لا يُعاد حسابه أبداً
            turn["embedding"] = np.asarray(embedding).ravel().tolist()
        h.append(turn)
        if len(h) > self._max_history:
            self._sessions[sid] = h[-self._max_history:]

    def clear(self, sid: str):
        self._sessions.pop(sid, None)

    format_for_prompt = staticmethod(_format_for_prompt)


class MongoSessionStore:
    """History persisted in MongoDB — survives restarts and is shared across
    workers. One document per session: {_id: session_id, turns: [...]}, with
    turns atomically appended and capped to max_history."""

    def __init__(self, max_history: int = MAX_HISTORY):
        self._max_history = max_history

    def _col(self):
        from app import db  # lazy: avoid importing the Mongo client at load time
        return db.get_collection(SESSIONS_COLLECTION)

    def get(self, sid: str) -> list:
        try:
            doc = self._col().find_one({"_id": str(sid)})
        except Exception as exc:
            log.warning("⚠️ تعذّر قراءة سجل الجلسة '%s': %s", sid, exc)
            return []
        return (doc or {}).get("turns", [])

    def push(self, sid: str, user: str, assistant: str, embedding=None):
        turn = {"user": user, "assistant": assistant}
        if embedding is not None:  # يُخزَّن مرة واحدة — لا يُعاد حسابه أبداً
            turn["embedding"] = np.asarray(embedding).ravel().tolist()
        try:
            self._col().update_one(
                {"_id": str(sid)},
                {"$push": {"turns": {
                    "$each": [turn],
                    "$slice": -self._max_history,  # FIFO: يُطرد الأقدم تلقائياً
                }}},
                upsert=True,
            )
        except Exception as exc:
            log.warning("⚠️ تعذّر حفظ سجل الجلسة '%s': %s", sid, exc)

    def clear(self, sid: str):
        try:
            self._col().delete_one({"_id": str(sid)})
        except Exception as exc:
            log.warning("⚠️ تعذّر مسح سجل الجلسة '%s': %s", sid, exc)

    format_for_prompt = staticmethod(_format_for_prompt)


def make_session_store():
    """The session store selected by config.SESSION_BACKEND."""
    if config.SESSION_BACKEND == "mongo":
        return MongoSessionStore()
    return SessionStore()
