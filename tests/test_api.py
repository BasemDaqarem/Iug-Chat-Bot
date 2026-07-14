"""
API-layer tests: routes, validation, status codes, and contracts — with a
fake bot injected through create_app(bot=...), so no Mongo/Jina/Groq is ever
touched. The API layer is pure delegation, so these tests pin exactly that:
correct wiring, correct schemas, correct error translation.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config
from app.api import create_app
from app.errors import UpstreamServiceError
from app.tokens import create_access_token


class FakeBot:
    """Stands in for IUGChatbot — same public surface the routes use."""

    def __init__(self):
        self.files = {"ملف_علامات": {"collection": "ملف_علامات", "chunks_count": 2, "indexed": True}}
        self.history = {}
        self.data = {"programs": [{"name": "هندسة"}]}
        self.chunks = ["c1", "c2", "c3"]
        self.fail_next_chat = False
        self.crash_next_chat = False

    # chat flows ----------------------------------------------------------
    def _answer(self, question, session_id, source):
        if self.crash_next_chat:
            raise KeyError("boom")  # an unexpected bug → must become a clean 500
        if self.fail_next_chat:
            raise UpstreamServiceError("❌ openrouter.ai رفض الطلب (HTTP 502): upstream down")
        self.history.setdefault(session_id, []).append(
            {"user": question, "assistant": "إجابة"})
        return {"answer": "إجابة", "top_chunks": ["مقطع"], "source": source}

    def chat(self, question, session_id):
        return self._answer(question, session_id, "knowledge_base")

    def chat_with_all_files(self, question, session_id):
        return self._answer(question, session_id, "uploaded_files_all")

    def chat_as_student(self, question, session_id):
        return self._answer(question, session_id, "student_profile")

    def chat_with_file(self, question, collection_name, session_id):
        return self._answer(question, session_id, "uploaded_file")

    def stream_answer(self, question, subject, *, allowed_collections=None):
        # `subject` is a plain student_id here (legacy branch of the route).
        sid = getattr(subject, "subject", subject)
        self.history.setdefault(sid, []).append({"user": question, "assistant": "إجابة"})
        for part in ["إ", "جا", "بة"]:   # three visible chunks
            yield part

    # files ----------------------------------------------------------------
    def get_uploaded_files_list(self):
        return list(self.files.values())

    def upload_json_file(self, name, data):
        if not isinstance(data, (list, dict)):
            raise ValueError("محتوى الملف يجب أن يكون JSON object أو array.")
        docs = data if isinstance(data, list) else [data]
        self.files[name] = {"collection": name, "chunks_count": len(docs), "indexed": True}
        return {"inserted": len(docs), "collection": name}

    def reload_uploaded_file(self, name):
        return True

    def delete_uploaded_file(self, name):
        self.files.pop(name, None)
        return True

    # sessions ---------------------------------------------------------------
    def get_history(self, sid):
        return self.history.get(sid, [])

    def clear_history(self, sid):
        self.history.pop(sid, None)


class ApiBase(unittest.TestCase):

    def setUp(self):
        from app.api import deps
        deps.reset_rate_limits()          # tests share a client IP
        self.bot = FakeBot()
        self._admin = patch.object(config, "ADMIN_API_KEY", "test-admin-key")
        self._admin.start()
        self.addCleanup(self._admin.stop)
        app = create_app(bot=self.bot)
        self.client = TestClient(app)
        self.client.__enter__()          # run lifespan (installs app.state.bot)
        self.addCleanup(self.client.__exit__, None, None, None)

    def auth(self, student_id="s1"):
        return {"Authorization": "Bearer " + create_access_token(student_id)}

    def admin(self, student_id="s1"):
        # admin ops also need a valid session token in the real flow; include both
        return {**self.auth(student_id), "X-Admin-Key": "test-admin-key"}


class TestHealth(ApiBase):

    def test_health_ok(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["knowledge_chunks"], 3)
        self.assertEqual(body["uploaded_files"], 1)

    def test_timing_header_present(self):
        r = self.client.get("/health")
        self.assertIn("X-Process-Time", r.headers)


class TestChat(ApiBase):

    def test_chat_happy_path(self):
        r = self.client.post("/api/chat", json={"question": "سؤال؟"}, headers=self.auth("s1"))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["answer"], "إجابة")
        self.assertEqual(body["source"], "knowledge_base")
        self.assertEqual(body["top_chunks"], ["مقطع"])

    def test_chat_requires_token(self):
        r = self.client.post("/api/chat", json={"question": "سؤال؟"})
        self.assertEqual(r.status_code, 401)

    def test_chat_validation_rejects_empty_question(self):
        r = self.client.post("/api/chat", json={"question": ""}, headers=self.auth("s1"))
        self.assertEqual(r.status_code, 422)

    def test_chat_all_files(self):
        r = self.client.post("/api/chat/files", json={"question": "سؤال؟"}, headers=self.auth("s1"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["source"], "uploaded_files_all")

    def test_chat_student_requires_token(self):
        # No Authorization header → 401, never processed.
        r = self.client.post("/api/chat/student", json={"question": "ما حالتي؟"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["code"], "UNAUTHORIZED")

    def test_chat_student_with_token(self):
        r = self.client.post("/api/chat/student", json={"question": "ما حالتي؟"},
                             headers=self.auth("12345"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["source"], "student_profile")

    def test_chat_student_identity_comes_from_token_not_body(self):
        # A spoofed session_id in the body is ignored; identity = token's sub.
        r = self.client.post("/api/chat/student",
                             json={"question": "ما حالتي؟", "session_id": "victim999"},
                             headers=self.auth("12345"))
        self.assertEqual(r.status_code, 200)
        # FakeBot echoes the session_id it was actually called with into history
        self.assertIn("12345", self.bot.history)
        self.assertNotIn("victim999", self.bot.history)

    def test_chat_student_rejects_garbage_token(self):
        r = self.client.post("/api/chat/student", json={"question": "س"},
                             headers={"Authorization": "Bearer not.a.jwt"})
        self.assertEqual(r.status_code, 401)

    def test_chat_student_stream_requires_token(self):
        r = self.client.post("/api/chat/student/stream", json={"question": "س"})
        self.assertEqual(r.status_code, 401)

    def test_chat_student_stream_yields_full_answer(self):
        r = self.client.post("/api/chat/student/stream", json={"question": "ما حالتي؟"},
                             headers=self.auth("12345"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
        self.assertEqual(r.text, "إجابة")            # three chunks reassembled
        self.assertIn("12345", self.bot.history)     # identity from token

    def test_chat_one_file(self):
        r = self.client.post("/api/chat/files/ملف_علامات",
                             json={"question": "سؤال؟"}, headers=self.auth("s1"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["source"], "uploaded_file")

    def test_chat_missing_file_404(self):
        r = self.client.post("/api/chat/files/غير_موجود",
                             json={"question": "سؤال؟"}, headers=self.auth("s1"))
        self.assertEqual(r.status_code, 404)

    def test_llm_failure_maps_to_502(self):
        self.bot.fail_next_chat = True
        r = self.client.post("/api/chat", json={"question": "سؤال؟"}, headers=self.auth("s1"))
        self.assertEqual(r.status_code, 502)
        body = r.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error"]["code"], "UPSTREAM_ERROR")
        self.assertIn("openrouter.ai", body["error"]["message"])


class TestFiles(ApiBase):

    def test_list_files_requires_token(self):
        self.assertEqual(self.client.get("/api/files").status_code, 401)

    def test_list_files(self):
        r = self.client.get("/api/files", headers=self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 1)
        self.assertEqual(r.json()["files"][0]["collection"], "ملف_علامات")

    def test_upload_requires_admin(self):
        # a normal student token is NOT enough to mutate the shared corpus
        r = self.client.put("/api/files/جديد", json={"documents": [{"a": 1}]},
                            headers=self.auth("student"))
        self.assertEqual(r.status_code, 403)

    def test_upload_then_visible(self):
        r = self.client.put("/api/files/جديد",
                            json={"documents": [{"a": 1}, {"b": 2}]}, headers=self.admin())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"inserted": 2, "collection": "جديد", "indexed": True})
        self.assertEqual(self.client.get("/api/files", headers=self.auth()).json()["count"], 2)

    def test_delete_requires_admin(self):
        self.assertEqual(
            self.client.delete("/api/files/ملف_علامات", headers=self.auth()).status_code, 403)

    def test_delete_file(self):
        r = self.client.delete("/api/files/ملف_علامات", headers=self.admin())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/files", headers=self.auth()).json()["count"], 0)

    def test_delete_missing_404(self):
        self.assertEqual(
            self.client.delete("/api/files/غير_موجود", headers=self.admin()).status_code, 404)

    def test_reload_missing_404(self):
        self.assertEqual(
            self.client.post("/api/files/غير_موجود/reload", headers=self.admin()).status_code, 404)

    def test_reload_ok(self):
        self.assertEqual(
            self.client.post("/api/files/ملف_علامات/reload", headers=self.admin()).status_code, 200)


class TestSessions(ApiBase):

    def test_history_requires_token(self):
        self.assertEqual(self.client.get("/api/sessions/me/history").status_code, 401)

    def test_empty_history(self):
        r = self.client.get("/api/sessions/me/history", headers=self.auth("s9"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"session_id": "s9", "turns": [], "count": 0})

    def test_history_accumulates_and_clears(self):
        h = self.auth("s1")
        self.client.post("/api/chat/student", json={"question": "أول"}, headers=h)
        self.client.post("/api/chat/student", json={"question": "ثاني"}, headers=h)
        r = self.client.get("/api/sessions/me/history", headers=h)
        self.assertEqual(r.json()["count"], 2)
        self.assertEqual(r.json()["turns"][0]["user"], "أول")

        self.client.delete("/api/sessions/me/history", headers=h)
        self.assertEqual(self.client.get("/api/sessions/me/history", headers=h).json()["count"], 0)

    def test_cannot_read_another_students_history(self):
        # student s1 builds history; student s2's token sees only their own (empty)
        self.client.post("/api/chat/student", json={"question": "سري"}, headers=self.auth("s1"))
        r = self.client.get("/api/sessions/me/history", headers=self.auth("s2"))
        self.assertEqual(r.json()["count"], 0)  # s2 never sees s1's history


class TestErrorEnvelope(ApiBase):
    """Every failing endpoint returns the SAME unified envelope."""

    def _assert_envelope(self, body, code, path):
        self.assertFalse(body["success"])
        err = body["error"]
        self.assertEqual(err["code"], code)
        self.assertTrue(err["message"])                 # non-empty, human-readable
        self.assertEqual(err["path"], path)
        self.assertIn("T", err["timestamp"])            # ISO-8601
        self.assertIn("code", err)                      # all keys present
        self.assertIn("details", err)

    def test_not_found_envelope(self):
        r = self.client.delete("/api/files/مفقود", headers=self.admin())
        self.assertEqual(r.status_code, 404)
        self._assert_envelope(r.json(), "NOT_FOUND", "/api/files/مفقود")

    def test_unauthorized_envelope(self):
        r = self.client.post("/api/chat", json={"question": "س"})  # no token
        self.assertEqual(r.status_code, 401)
        self._assert_envelope(r.json(), "UNAUTHORIZED", "/api/chat")

    def test_validation_envelope_lists_the_field(self):
        r = self.client.post("/api/chat", json={"question": ""}, headers=self.auth("s"))
        self.assertEqual(r.status_code, 422)
        body = r.json()
        self._assert_envelope(body, "VALIDATION_ERROR", "/api/chat")
        fields = [d["field"] for d in body["error"]["details"]]
        self.assertIn("question", fields)               # the offending field is named

    def test_upstream_envelope(self):
        self.bot.fail_next_chat = True
        r = self.client.post("/api/chat/files", json={"question": "q"}, headers=self.auth("s"))
        self.assertEqual(r.status_code, 502)
        self._assert_envelope(r.json(), "UPSTREAM_ERROR", "/api/chat/files")

    def test_unexpected_bug_becomes_clean_500(self):
        # An unforeseen exception must NOT leak a stack trace — it becomes a
        # uniform 500 envelope with a friendly message.
        self.bot.crash_next_chat = True
        client = TestClient(create_app(bot=self.bot), raise_server_exceptions=False)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        r = client.post("/api/chat", json={"question": "q"}, headers=self.auth("s"))
        self.assertEqual(r.status_code, 500)
        body = r.json()
        self._assert_envelope(body, "INTERNAL_ERROR", "/api/chat")
        self.assertNotIn("Traceback", body["error"]["message"])
        self.assertNotIn("KeyError", body["error"]["message"])  # message is friendly


class TestStaticFrontend(ApiBase):

    def test_frontend_served_with_no_store(self):
        # Guards the root cause of "login works but doesn't redirect": the
        # frontend must never be cached, so a JS update is always picked up.
        r = self.client.get("/app/index.html")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-store", r.headers.get("cache-control", ""))

    def test_frontend_assets_are_version_busted(self):
        html = self.client.get("/app/index.html").text
        self.assertIn("app.js?v=", html)  # cache-busting query present


class TestRateLimit(ApiBase):

    def test_chat_is_rate_limited_per_student(self):
        from app.api import deps
        with patch.object(deps._chat_limiter, "max", 2):
            deps._chat_limiter.reset()
            h = self.auth("rl_student")
            for _ in range(2):
                ok = self.client.post("/api/chat/student", json={"question": "س"}, headers=h)
                self.assertEqual(ok.status_code, 200)
            blocked = self.client.post("/api/chat/student", json={"question": "س"}, headers=h)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["error"]["code"], "RATE_LIMITED")
        self.assertIn("Retry-After", blocked.headers)

    def test_limit_is_per_student_not_global(self):
        from app.api import deps
        with patch.object(deps._chat_limiter, "max", 1):
            deps._chat_limiter.reset()
            self.client.post("/api/chat/student", json={"question": "س"}, headers=self.auth("A"))
            # a DIFFERENT student is unaffected by A's usage
            other = self.client.post("/api/chat/student", json={"question": "س"}, headers=self.auth("B"))
        self.assertEqual(other.status_code, 200)


class TestDocs(ApiBase):

    def test_openapi_schema_exposed(self):
        r = self.client.get("/openapi.json")
        self.assertEqual(r.status_code, 200)
        paths = r.json()["paths"]
        for expected in ("/health", "/api/chat", "/api/chat/student", "/api/auth/login",
                         "/api/files", "/api/sessions/me/history"):
            self.assertIn(expected, paths)


if __name__ == "__main__":
    unittest.main()
