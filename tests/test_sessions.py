import unittest
from unittest.mock import patch

import numpy as np

from app.sessions import (
    MEMORY_INSTRUCTION,
    MongoSessionStore,
    SessionStore,
    format_memory,
    make_session_store,
    relevant_turns,
)


class TestSessionStore(unittest.TestCase):

    def test_get_creates_empty(self):
        s = SessionStore()
        self.assertEqual(s.get("sid"), [])

    def test_push_and_get(self):
        s = SessionStore()
        s.push("sid", "سؤال", "جواب")
        self.assertEqual(s.get("sid"), [{"user": "سؤال", "assistant": "جواب"}])

    def test_trims_to_max_history(self):
        s = SessionStore(max_history=20)
        for i in range(25):
            s.push("sid", f"q{i}", f"a{i}")
        h = s.get("sid")
        self.assertEqual(len(h), 20)
        self.assertEqual(h[0]["user"], "q5")
        self.assertEqual(h[-1]["user"], "q24")

    def test_clear(self):
        s = SessionStore()
        s.push("sid", "q", "a")
        s.clear("sid")
        self.assertEqual(s.get("sid"), [])
        s.clear("unknown")  # no error

    def test_sessions_are_isolated(self):
        s = SessionStore()
        s.push("a", "q", "a")
        self.assertEqual(s.get("b"), [])

    def test_format_empty(self):
        self.assertEqual(SessionStore.format_for_prompt([]), "")

    def test_format_last_six_turns(self):
        history = [{"user": f"q{i}", "assistant": f"a{i}"} for i in range(8)]
        text = SessionStore.format_for_prompt(history)
        self.assertTrue(text.startswith("سجل المحادثة السابقة:\n"))
        self.assertNotIn("q1", text)
        self.assertIn("q2", text)
        self.assertIn("q7", text)
        self.assertTrue(text.endswith("\n\n"))


def _vec(*xs):
    """متجه اختباري مطبّع L2 (كما تعيد embed_query تماماً)."""
    v = np.asarray(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


class TestSemanticMemory(unittest.TestCase):
    """الذاكرة القصيرة الدلالية: 5 أدوار FIFO بمتجهات محفوظة + انتقاء بالصلة."""

    def test_five_item_limit_fifo(self):
        s = SessionStore(max_history=5)
        for i in range(7):
            s.push("sid", f"q{i}", f"a{i}", embedding=_vec(1, 0))
        h = s.get("sid")
        self.assertEqual(len(h), 5)                     # الحد = 5
        self.assertEqual(h[0]["user"], "q2")            # الأقدم أُزيل (FIFO)
        self.assertEqual(h[-1]["user"], "q6")           # الأحدث محفوظ

    def test_embedding_stored_once_as_plain_list(self):
        s = SessionStore(max_history=5)
        s.push("sid", "q", "a", embedding=_vec(1, 0))
        turn = s.get("sid")[0]
        self.assertIsInstance(turn["embedding"], list)  # قابل للتخزين في Mongo
        self.assertAlmostEqual(turn["embedding"][0], 1.0, places=5)

    def test_push_without_embedding_keeps_old_shape(self):
        s = SessionStore(max_history=5)
        s.push("sid", "q", "a")                          # المسارات الفورية
        self.assertNotIn("embedding", s.get("sid")[0])

    def test_relevance_selection_keeps_only_related_older_turns(self):
        history = [
            {"user": "سؤال تأجيل", "assistant": "a", "embedding": _vec(1, 0).tolist()},
            {"user": "سؤال منح", "assistant": "b", "embedding": _vec(0, 1).tolist()},
            {"user": "الأحدث", "assistant": "c", "embedding": _vec(0.6, 0.8).tolist()},
        ]
        picked = relevant_turns(history, _vec(1, 0), min_sim=0.45)
        users = [t["user"] for t in picked]
        self.assertIn("سؤال تأجيل", users)      # متشابه (cos=1) → يُحقن
        self.assertNotIn("سؤال منح", users)     # متعامد (cos=0) → يُستبعد
        self.assertEqual(users[-1], "الأحدث")   # الأحدث دائماً محقون

    def test_older_turn_without_embedding_is_skipped(self):
        history = [
            {"user": "قديم بلا متجه", "assistant": "a"},
            {"user": "الأحدث", "assistant": "b", "embedding": _vec(1, 0).tolist()},
        ]
        picked = relevant_turns(history, _vec(1, 0), min_sim=0.45)
        self.assertEqual([t["user"] for t in picked], ["الأحدث"])

    def test_no_query_vector_falls_back_to_recent_turns(self):
        history = [{"user": f"q{i}", "assistant": f"a{i}"} for i in range(8)]
        picked = relevant_turns(history, None)
        self.assertEqual(picked, history[-6:])          # السلوك القديم حرفياً

    def test_format_memory_has_mandated_instruction_before_data(self):
        text = format_memory([{"user": "q", "assistant": "a"}])
        self.assertTrue(text.startswith(MEMORY_INSTRUCTION))
        self.assertLess(text.index(MEMORY_INSTRUCTION), text.index("q"))

    def test_format_memory_empty(self):
        self.assertEqual(format_memory([]), "")

    def test_mongo_store_persists_embedding_fifo(self):
        col = FakeMongoSessions()
        store = MongoSessionStore(max_history=5)
        with patch.object(store, "_col", return_value=col):
            for i in range(6):
                store.push("sid", f"q{i}", f"a{i}", embedding=_vec(1, 0))
            h = store.get("sid")
        self.assertEqual(len(h), 5)
        self.assertEqual(h[0]["user"], "q1")            # FIFO في Mongo أيضاً
        self.assertIsInstance(h[0]["embedding"], list)


class FakeMongoSessions:
    """Minimal chat_sessions collection stand-in (supports $push/$slice)."""

    def __init__(self):
        self.docs = {}

    def find_one(self, query):
        return self.docs.get(query["_id"])

    def update_one(self, query, update, upsert=False):
        sid = query["_id"]
        doc = self.docs.setdefault(sid, {"_id": sid, "turns": []})
        push = update["$push"]["turns"]
        doc["turns"].extend(push["$each"])
        sl = push.get("$slice")
        if sl:
            doc["turns"] = doc["turns"][sl:]  # negative slice = keep last N

    def delete_one(self, query):
        self.docs.pop(query["_id"], None)


class TestMongoSessionStore(unittest.TestCase):

    def setUp(self):
        self.col = FakeMongoSessions()
        self.store = MongoSessionStore(max_history=20)
        self._p = patch.object(self.store, "_col", return_value=self.col)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_push_persists_and_reads_back(self):
        self.store.push("12345", "سؤال", "جواب")
        self.assertEqual(self.store.get("12345"), [{"user": "سؤال", "assistant": "جواب"}])

    def test_caps_to_max_history(self):
        for i in range(25):
            self.store.push("12345", f"q{i}", f"a{i}")
        h = self.store.get("12345")
        self.assertEqual(len(h), 20)
        self.assertEqual(h[0]["user"], "q5")

    def test_clear(self):
        self.store.push("12345", "q", "a")
        self.store.clear("12345")
        self.assertEqual(self.store.get("12345"), [])

    def test_read_failure_returns_empty_not_error(self):
        with patch.object(self.store, "_col", side_effect=RuntimeError("mongo down")):
            self.assertEqual(self.store.get("x"), [])  # degrades gracefully


class TestFactory(unittest.TestCase):

    def test_factory_selects_backend(self):
        with patch("app.config.SESSION_BACKEND", "memory"):
            self.assertIsInstance(make_session_store(), SessionStore)
        with patch("app.config.SESSION_BACKEND", "mongo"):
            self.assertIsInstance(make_session_store(), MongoSessionStore)


if __name__ == "__main__":
    unittest.main()
