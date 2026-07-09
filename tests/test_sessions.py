import unittest

from app.sessions import SessionStore


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


if __name__ == "__main__":
    unittest.main()
