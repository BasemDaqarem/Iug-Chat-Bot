"""
Auth tests — the service (bcrypt hashing/verification) and the endpoints
(login/register with the unified error envelope), all against a fake in-memory
collection so no real MongoDB is touched.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth
from app.api import create_app
from tests.test_api import FakeBot


class FakeCollection:
    """Minimal Mongo-collection stand-in for students_auth."""

    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find_one(self, query):
        sid = str(query.get("student_id"))
        return next((d for d in self.docs if str(d.get("student_id")) == sid), None)

    def insert_one(self, doc):
        self.docs.append(dict(doc))


# ═════════════════════════════════════════════════════════════════════════
#  Service layer (real bcrypt)
# ═════════════════════════════════════════════════════════════════════════


class TestAuthService(unittest.TestCase):

    def test_hash_is_bcrypt_and_verifies(self):
        h = auth.hash_password("s3cret")
        self.assertTrue(h.startswith("$2"))          # bcrypt
        self.assertTrue(auth.verify_password("s3cret", h))
        self.assertFalse(auth.verify_password("wrong", h))

    def test_verify_bad_hash_is_false_not_error(self):
        self.assertFalse(auth.verify_password("x", ""))
        self.assertFalse(auth.verify_password("x", "not-a-hash"))

    def test_authenticate_flow(self):
        col = FakeCollection([{"student_id": "12345",
                               "password_hash": auth.hash_password("pin1234"),
                               "profile": {"name": "طالب"}}])
        with patch.object(auth, "_col", return_value=col):
            self.assertIsNotNone(auth.authenticate("12345", "pin1234"))
            self.assertIsNone(auth.authenticate("12345", "bad"))     # wrong pass
            self.assertIsNone(auth.authenticate("00000", "pin1234"))  # no account


# ═════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═════════════════════════════════════════════════════════════════════════


class AuthApiBase(unittest.TestCase):

    def setUp(self):
        from app.api import deps
        deps.reset_rate_limits()          # tests share a client IP
        self.col = FakeCollection([{
            "student_id": "12345",
            "password_hash": auth.hash_password("pin1234"),
            "profile": {"name": "محمد أحمد", "major": "هندسة", "gpa": 88.5, "rank": 3},
        }])
        self._patch = patch.object(auth, "_col", return_value=self.col)
        self._patch.start()
        self.addCleanup(self._patch.stop)

        self.client = TestClient(create_app(bot=FakeBot()))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)


class TestLogin(AuthApiBase):

    def test_login_success_returns_profile(self):
        r = self.client.post("/api/auth/login", json={"student_id": "12345", "password": "pin1234"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["student_id"], "12345")
        self.assertEqual(body["profile"]["name"], "محمد أحمد")
        self.assertEqual(body["profile"]["gpa"], 88.5)

    def test_login_issues_valid_token(self):
        from app.tokens import decode_token
        r = self.client.post("/api/auth/login", json={"student_id": "12345", "password": "pin1234"})
        body = r.json()
        self.assertEqual(body["token_type"], "bearer")
        self.assertEqual(decode_token(body["access_token"]), "12345")  # token proves identity

    def test_wrong_password_is_401_envelope(self):
        r = self.client.post("/api/auth/login", json={"student_id": "12345", "password": "nope"})
        self.assertEqual(r.status_code, 401)
        body = r.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")
        # generic message — must NOT reveal which field was wrong
        self.assertNotIn("كلمة المرور فقط", body["error"]["message"])

    def test_unknown_id_is_401(self):
        r = self.client.post("/api/auth/login", json={"student_id": "99999", "password": "pin1234"})
        self.assertEqual(r.status_code, 401)

    def test_non_numeric_id_is_422_with_field(self):
        r = self.client.post("/api/auth/login", json={"student_id": "abc", "password": "pin1234"})
        self.assertEqual(r.status_code, 422)
        fields = [d["field"] for d in r.json()["error"]["details"]]
        self.assertIn("student_id", fields)

    def test_short_password_is_422(self):
        r = self.client.post("/api/auth/login", json={"student_id": "12345", "password": "1"})
        self.assertEqual(r.status_code, 422)


class TestLoginRateLimit(AuthApiBase):

    def test_login_is_throttled_after_too_many_attempts(self):
        from app.api import deps
        with patch.object(deps._login_limiter, "max", 3):
            deps._login_limiter.reset()
            for _ in range(3):
                self.client.post("/api/auth/login", json={"student_id": "12345", "password": "wrong"})
            r = self.client.post("/api/auth/login", json={"student_id": "12345", "password": "wrong"})
        self.assertEqual(r.status_code, 429)          # brute-force blunted
        self.assertEqual(r.json()["error"]["code"], "RATE_LIMITED")


class TestRegister(AuthApiBase):

    def test_register_creates_and_hashes(self):
        r = self.client.post("/api/auth/register",
                             json={"student_id": "67890", "password": "pin1234", "name": "سالم"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["student_id"], "67890")
        # stored with a bcrypt hash, never plaintext
        stored = self.col.find_one({"student_id": "67890"})
        self.assertTrue(stored["password_hash"].startswith("$2"))
        self.assertNotIn("pin1234", stored["password_hash"])

    def test_duplicate_id_is_409(self):
        r = self.client.post("/api/auth/register",
                             json={"student_id": "12345", "password": "pin1234", "name": "أحد"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "CONFLICT")

    def test_registered_user_can_login(self):
        self.client.post("/api/auth/register",
                        json={"student_id": "55555", "password": "mypass", "name": "خالد"})
        r = self.client.post("/api/auth/login", json={"student_id": "55555", "password": "mypass"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["profile"]["name"], "خالد")


if __name__ == "__main__":
    unittest.main()
