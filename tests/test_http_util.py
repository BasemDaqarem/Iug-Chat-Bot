import unittest

from app.http_util import error_detail, provider_label, status_hint


class _Resp:
    def __init__(self, body=None, text=""):
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class TestProviderLabel(unittest.TestCase):

    def test_extracts_host(self):
        self.assertEqual(
            provider_label("https://openrouter.ai/api/v1/chat/completions", "x"),
            "openrouter.ai",
        )
        self.assertEqual(provider_label("https://api.jina.ai/v1/embeddings", "x"), "api.jina.ai")

    def test_falls_back_when_missing(self):
        self.assertEqual(provider_label("", "خدمة المحادثة"), "خدمة المحادثة")
        self.assertEqual(provider_label(None, "fallback"), "fallback")


class TestErrorDetail(unittest.TestCase):

    def test_nested_error_message(self):
        self.assertEqual(error_detail(_Resp({"error": {"message": "invalid api key"}})), "invalid api key")

    def test_string_error(self):
        self.assertEqual(error_detail(_Resp({"error": "rate limited"})), "rate limited")

    def test_top_level_message(self):
        self.assertEqual(error_detail(_Resp({"message": "bad request"})), "bad request")

    def test_falls_back_to_text(self):
        self.assertEqual(error_detail(_Resp(body=None, text="  <html>502</html>  ")), "<html>502</html>")

    def test_empty(self):
        self.assertEqual(error_detail(_Resp(body=None, text="")), "بلا تفاصيل من الخادم")


class TestStatusHint(unittest.TestCase):

    def test_hints_are_actionable(self):
        self.assertIn(".env", status_hint(401))
        self.assertIn("النموذج", status_hint(404))
        self.assertIn("مزوّد", status_hint(503))
        self.assertEqual(status_hint(418), "")  # no hint for unusual codes


if __name__ == "__main__":
    unittest.main()
