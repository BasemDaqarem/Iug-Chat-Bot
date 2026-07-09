import unittest
from unittest.mock import patch

from app.sessions import MongoSessionStore, SessionStore, make_session_store


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
