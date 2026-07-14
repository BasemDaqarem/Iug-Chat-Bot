"""
Regression tests closing the 9 findings of the 2026-07-13 security review.
Each test names the finding it locks down so a future change that reopens the
hole fails loudly here.
"""

import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import config, file_catalog
from app.api import create_app
from app.rbac import Principal, Role
from app.sessions import MongoSessionStore, SessionStore
from app.tokens import create_access_token
from tests.test_api import FakeBot


# ══════════════════════════════════════════════════════════════════════════
#  Findings 2 & 7 — classification and owner enforced at write AND read
# ══════════════════════════════════════════════════════════════════════════

class TestClassificationAndOwnerPolicy(unittest.TestCase):

    def test_sanitize_narrows_overbroad_roles_to_classification(self):
        # admin_only requested with "all roles" → collapses to admin only.
        roles = file_catalog._sanitize_policy(
            "admin_only", ["guest", "student", "employee", "admin"], None
        )
        self.assertEqual(roles, ["admin"])

    def test_sanitize_rejects_owner_scoped_without_owner(self):
        with self.assertRaises(ValueError):
            file_catalog._sanitize_policy("student_records", ["student"], None)
        with self.assertRaises(ValueError):
            file_catalog._sanitize_policy("employee_private", ["employee"], "  ")

    def test_sanitize_accepts_owner_scoped_with_owner(self):
        roles = file_catalog._sanitize_policy("student_records", ["student", "admin"], "12345")
        self.assertEqual(roles, ["admin", "student"])

    def test_sanitize_rejects_empty_after_intersection(self):
        # guest-only on admin_only leaves no valid role.
        with self.assertRaises(ValueError):
            file_catalog._sanitize_policy("admin_only", ["guest"], None)

    @patch("app.file_catalog._catalog")
    def test_read_gate_denies_guest_on_admin_only_despite_broad_roles(self, catalog):
        # A stored doc that (via an old bug) kept every role on an admin_only
        # file must still never reach a guest.
        catalog.return_value.find.return_value = [{
            "collection": "secret", "status": "published",
            "classification": "admin_only",
            "allowed_roles": ["guest", "student", "employee", "admin"],
            "owner_id": None,
        }]
        guest = Principal.guest("guest:x")
        allowed = file_catalog.allowed_collections(guest, {"secret"})
        self.assertEqual(allowed, set())

    @patch("app.file_catalog._catalog")
    def test_read_gate_denies_when_owner_scoped_but_owner_missing(self, catalog):
        catalog.return_value.find.return_value = [{
            "collection": "rec", "status": "published",
            "classification": "student_records",
            "allowed_roles": ["student", "admin"],
            "owner_id": None,          # misconfigured — no owner
        }]
        student = Principal("99999", Role.STUDENT)
        self.assertEqual(file_catalog.allowed_collections(student, {"rec"}), set())

    @patch("app.file_catalog._catalog")
    def test_read_gate_owner_match_still_scopes_to_the_owner(self, catalog):
        catalog.return_value.find.return_value = [{
            "collection": "rec", "status": "published",
            "classification": "student_records",
            "allowed_roles": ["student", "admin"],
            "owner_id": "12345",
        }]
        owner = Principal("12345", Role.STUDENT)
        other = Principal("67890", Role.STUDENT)
        self.assertEqual(file_catalog.allowed_collections(owner, {"rec"}), {"rec"})
        self.assertEqual(file_catalog.allowed_collections(other, {"rec"}), set())


# ══════════════════════════════════════════════════════════════════════════
#  Finding 3 — version stores its own policy; rollback restores it
# ══════════════════════════════════════════════════════════════════════════

class _FakeCol:
    """Tiny dict-backed Mongo collection (find_one/insert_one/update_one/find)."""
    def __init__(self):
        self.docs = []

    def _match(self, q):
        return [d for d in self.docs if all(d.get(k) == v for k, v in q.items())]

    def find_one(self, q):
        m = self._match(q)
        return dict(m[0]) if m else None

    def find(self, q=None):
        return [dict(d) for d in (self._match(q) if q else self.docs)]

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def update_one(self, q, update, upsert=False):
        m = self._match(q)
        if m:
            m[0].update(update.get("$set", {}))
        elif upsert:
            d = dict(q); d.update(update.get("$set", {})); self.docs.append(d)

    def update_many(self, q, update):
        for d in self._match(q):
            d.update(update.get("$set", {}))


class TestRollbackRestoresPolicy(unittest.TestCase):

    def setUp(self):
        self.cat, self.ver = _FakeCol(), _FakeCol()
        patch("app.file_catalog._catalog", return_value=self.cat).start()
        patch("app.file_catalog._versions", return_value=self.ver).start()
        self.addCleanup(patch.stopall)

    def test_draft_snapshots_policy_into_version(self):
        file_catalog.create_draft(
            "col", [{"a": 1}], "student_records", ["student", "admin"], "admin1", owner_id="12345"
        )
        v = self.ver.find_one({"version": 1})
        self.assertEqual(v["classification"], "student_records")
        self.assertEqual(v["owner_id"], "12345")
        self.assertEqual(v["allowed_roles"], ["admin", "student"])

    def test_rollback_restores_old_versions_narrow_policy(self):
        bot = MagicMock()
        bot.get_uploaded_files_list.return_value = [{"collection": "col", "indexed": True}]
        # v1: private student record. v2: broadened to public.
        file_catalog.create_draft("col", [{"v": 1}], "student_records",
                                  ["student", "admin"], "admin1", owner_id="12345")
        file_catalog.process(self._fid(), "admin1")
        file_catalog.publish(self._fid(), bot, "admin1", version=1)
        file_catalog.create_draft("col", [{"v": 2}], "university_public",
                                  ["guest", "student", "employee", "admin"], "admin1")
        file_catalog.process(self._fid(), "admin1")
        file_catalog.publish(self._fid(), bot, "admin1", version=2)
        self.assertEqual(self.cat.find_one({"collection": "col"})["classification"],
                         "university_public")
        # Roll back to v1 → its private policy comes back with its content.
        file_catalog.publish(self._fid(), bot, "admin1", version=1)
        entry = self.cat.find_one({"collection": "col"})
        self.assertEqual(entry["classification"], "student_records")
        self.assertEqual(entry["owner_id"], "12345")
        self.assertNotIn("guest", entry["allowed_roles"])

    def _fid(self):
        return self.cat.find_one({"collection": "col"})["file_id"]


# ══════════════════════════════════════════════════════════════════════════
#  Finding 5 — guest chat history is never persisted
# ══════════════════════════════════════════════════════════════════════════

class TestGuestSessionsNotPersisted(unittest.TestCase):

    def test_mongo_store_skips_guest_writes_and_reads(self):
        store = MongoSessionStore()
        col = MagicMock()
        with patch.object(store, "_col", return_value=col):
            store.push("guest:abc123", "س", "ج")
            self.assertEqual(store.get("guest:abc123"), [])
        col.update_one.assert_not_called()   # no Mongo document per guest
        col.find_one.assert_not_called()

    def test_real_student_still_persists(self):
        store = MongoSessionStore()
        col = MagicMock()
        col.find_one.return_value = {"_id": "12345", "turns": [{"user": "س", "assistant": "ج"}]}
        with patch.object(store, "_col", return_value=col):
            store.push("12345", "س", "ج")
            self.assertEqual(len(store.get("12345")), 1)
        col.update_one.assert_called_once()

    def test_memory_store_also_skips_guest(self):
        s = SessionStore()
        s.push("guest:xyz", "س", "ج")
        self.assertEqual(s.get("guest:xyz"), [])


# ══════════════════════════════════════════════════════════════════════════
#  Finding 9 — unique account indexes + duplicate-key → 409
# ══════════════════════════════════════════════════════════════════════════

class TestUniqueAccountIndexes(unittest.TestCase):

    def test_ensure_indexes_creates_unique_identifiers(self):
        from app import auth
        col = MagicMock()
        with patch.object(auth, "_col", return_value=col):
            auth.ensure_indexes()
        names = {c.args[0]: c.kwargs for c in col.create_index.call_args_list}
        self.assertTrue(names["user_id"]["unique"])
        self.assertTrue(names["student_id"]["unique"])
        self.assertTrue(names["student_id"]["sparse"])


# ══════════════════════════════════════════════════════════════════════════
#  Findings 1/6/9 (register) + 4/8 (rate limits) — HTTP surface
# ══════════════════════════════════════════════════════════════════════════

class TestAuthAndRateLimitSurface(unittest.TestCase):

    def setUp(self):
        from app.api import deps
        deps.reset_rate_limits()
        self.client = TestClient(create_app(bot=FakeBot()))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _reg_body(self, sid="120200999"):
        return {"student_id": sid, "password": "Passw0rd!", "name": "طالب",
                "major": "هندسة الحاسوب", "gpa": 80.0, "rank": 5,
                "academic_status": "regular"}

    def test_registration_gate_blocks_when_disabled(self):
        with patch.object(config, "ALLOW_PUBLIC_REGISTRATION", False):
            r = self.client.post("/api/auth/register", json=self._reg_body())
        self.assertEqual(r.status_code, 403)

    def test_registration_duplicate_key_race_becomes_409(self):
        from pymongo.errors import DuplicateKeyError
        with patch.object(config, "ALLOW_PUBLIC_REGISTRATION", True), \
             patch("app.auth.find_account", return_value=None), \
             patch("app.auth.create_account", side_effect=DuplicateKeyError("dup")):
            r = self.client.post("/api/auth/register", json=self._reg_body())
        self.assertEqual(r.status_code, 409)

    def test_guest_chat_is_rate_limited(self):
        # Finding 4: anonymous chat must hit a ceiling, not bill forever.
        statuses = [
            self.client.post("/api/chat/guest", json={"question": "س"}).status_code
            for _ in range(config.RATE_LIMIT_CHAT_PER_MIN + 1)
        ]
        self.assertIn(200, statuses)
        self.assertEqual(statuses[-1], 429)

    def test_login_is_rate_limited(self):
        # Finding 8: password guessing / bcrypt CPU is bounded.
        with patch("app.auth.authenticate", return_value=None):
            statuses = [
                self.client.post(
                    "/api/auth/login", json={"student_id": "1", "password": "x"}
                ).status_code
                for _ in range(config.RATE_LIMIT_LOGIN_PER_MIN + 1)
            ]
        self.assertEqual(statuses[-1], 429)


if __name__ == "__main__":
    unittest.main()
