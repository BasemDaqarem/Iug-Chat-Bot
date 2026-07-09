import unittest
from unittest.mock import patch

import requests

from app import config, llm


class FakeResponse:

    def __init__(self, status_code=200, content="جواب تجريبي", headers=None,
                 finish_reason="stop"):
        self.status_code = status_code
        self._content = content
        self.headers = headers or {}
        self._finish_reason = finish_reason

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")

    def json(self):
        return {"choices": [{
            "message": {"content": self._content},
            "finish_reason": self._finish_reason,
        }]}


class TestChatCompletion(unittest.TestCase):

    def test_missing_api_key_raises(self):
        with patch.object(config, "CHAT_API_KEY", ""):
            with self.assertRaises(RuntimeError) as ctx:
                llm.chat_completion("system", "user")
            self.assertIn("CHAT_API_KEY", str(ctx.exception))

    def test_payload_shape(self):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json
            return FakeResponse(content="  الإجابة  ")

        with patch.object(config, "CHAT_API_KEY", "test-key"), \
             patch.object(llm.requests, "post", side_effect=fake_post):
            answer = llm.chat_completion("النظام", "الرسالة")

        self.assertEqual(answer, "الإجابة")  # stripped
        p = captured["payload"]
        self.assertEqual(p["model"], config.CHAT_API_MODEL)
        self.assertEqual(p["temperature"], 0.05)
        self.assertEqual(p["max_tokens"], 450)
        self.assertEqual(p["messages"], [
            {"role": "system", "content": "النظام"},
            {"role": "user",   "content": "الرسالة"},
        ])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key")

    def test_retries_on_429_then_succeeds(self):
        responses = [FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(content="تم")]

        with patch.object(config, "CHAT_API_KEY", "k"), \
             patch.object(llm.requests, "post", side_effect=responses), \
             patch.object(llm.time, "sleep") as fake_sleep:
            answer = llm.chat_completion("s", "u")

        self.assertEqual(answer, "تم")
        fake_sleep.assert_called_once_with(0.0)

    def test_429_exhaustion_raises(self):
        responses = [FakeResponse(429, headers={"Retry-After": "0"})] * 4

        with patch.object(config, "CHAT_API_KEY", "k"), \
             patch.object(llm.requests, "post", side_effect=responses), \
             patch.object(llm.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                llm.chat_completion("s", "u")
        self.assertIn("429", str(ctx.exception))

    def test_timeout_retries_then_raises(self):
        with patch.object(config, "CHAT_API_KEY", "k"), \
             patch.object(llm.requests, "post", side_effect=requests.exceptions.Timeout()), \
             patch.object(llm.time, "sleep") as fake_sleep:
            with self.assertRaises(RuntimeError) as ctx:
                llm.chat_completion("s", "u")
        self.assertIn("وقتاً طويلاً", str(ctx.exception))
        self.assertEqual(fake_sleep.call_count, 3)  # retries between 4 attempts

    def test_connection_error_fails_fast(self):
        with patch.object(config, "CHAT_API_KEY", "k"), \
             patch.object(llm.requests, "post", side_effect=requests.exceptions.ConnectionError()):
            with self.assertRaises(RuntimeError) as ctx:
                llm.chat_completion("s", "u")
        self.assertIn("تعذّر الاتصال", str(ctx.exception))

    def test_null_content_length_retries_with_bigger_budget(self):
        # Reasoning model ate the whole budget → null content, finish "length".
        # It must retry (not crash) and the retry gets a larger max_tokens.
        seen_max_tokens = []

        def fake_post(url, headers=None, json=None, timeout=None):
            seen_max_tokens.append(json["max_tokens"])
            if len(seen_max_tokens) == 1:
                return FakeResponse(content=None, finish_reason="length")
            return FakeResponse(content="الإجابة الكاملة")

        with patch.object(config, "CHAT_API_KEY", "k"), \
             patch.object(llm.requests, "post", side_effect=fake_post):
            answer = llm.chat_completion("s", "u")

        self.assertEqual(answer, "الإجابة الكاملة")
        self.assertEqual(seen_max_tokens[1], seen_max_tokens[0] * 2)

    def test_null_content_exhaustion_raises_clean_error(self):
        with patch.object(config, "CHAT_API_KEY", "k"), \
             patch.object(llm.requests, "post",
                          return_value=FakeResponse(content=None, finish_reason="length")):
            with self.assertRaises(RuntimeError) as ctx:
                llm.chat_completion("s", "u")
        # Clean domain error, NOT a cryptic AttributeError, and not double-wrapped.
        self.assertIn("لم يُرجع", str(ctx.exception))
        self.assertNotIn("خطأ في Groq", str(ctx.exception))

    def test_http_error_wrapped(self):
        with patch.object(config, "CHAT_API_KEY", "k"), \
             patch.object(llm.requests, "post", return_value=FakeResponse(500)):
            with self.assertRaises(RuntimeError) as ctx:
                llm.chat_completion("s", "u")
        self.assertIn("خطأ في Groq", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
