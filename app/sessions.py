"""
Per-session chat history (in-memory).

NOTE: history lives in process memory — it is lost on restart and is not
shared between processes. Swapping in a Mongo/Redis-backed store later only
requires re-implementing this one class.
"""

from app.config import HISTORY_TURNS_IN_PROMPT, MAX_HISTORY


class SessionStore:

    def __init__(self, max_history: int = MAX_HISTORY):
        self._sessions: dict = {}
        self._max_history = max_history

    def get(self, sid: str) -> list:
        return self._sessions.setdefault(sid, [])

    def push(self, sid: str, user: str, assistant: str):
        h = self.get(sid)
        h.append({"user": user, "assistant": assistant})
        if len(h) > self._max_history:
            self._sessions[sid] = h[-self._max_history:]

    def clear(self, sid: str):
        self._sessions.pop(sid, None)

    @staticmethod
    def format_for_prompt(history: list) -> str:
        if not history:
            return ""
        turns = "\n".join(
            f"الطالب: {t['user']}\nالمساعد: {t['assistant']}"
            for t in history[-HISTORY_TURNS_IN_PROMPT:]
        )
        return f"سجل المحادثة السابقة:\n{turns}\n\n"
