"""Acceptance tests for the four-role authorization plan."""

import unittest
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from app.api import create_app
from app.rbac import Role
from app.tokens import create_access_token, decode_principal
from app.uploaded_files import UploadedFilesStore
from tests.test_api import FakeBot


class TestRoleClaims(unittest.TestCase):
    def test_role_and_version_round_trip_from_signed_token(self):
        token = create_access_token("EMP-1001", Role.EMPLOYEE, token_version=7)
        principal = decode_principal(token)
        self.assertEqual(principal.subject, "EMP-1001")
        self.assertEqual(principal.role, Role.EMPLOYEE)
        self.assertEqual(principal.token_version, 7)


class TestCrossWorkerRevocation(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(bot=FakeBot()))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.app.state.verify_account_tokens = True

    def test_protected_route_rejects_stale_token_version(self):
        token = create_access_token("EMP-1001", Role.EMPLOYEE, token_version=7)
        account = {
            "user_id": "EMP-1001",
            "role": "employee",
            "active": True,
            "token_version": 8,
        }
        with patch("app.auth.find_account", return_value=account):
            response = self.client.post(
                "/api/chat",
                json={"question": "سؤال"},
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(response.status_code, 401)

    def test_protected_route_rejects_role_changed_in_database(self):
        token = create_access_token("EMP-1001", Role.EMPLOYEE, token_version=7)
        account = {
            "user_id": "EMP-1001",
            "role": "student",
            "active": True,
            "token_version": 7,
        }
        with patch("app.auth.find_account", return_value=account):
            response = self.client.post(
                "/api/chat",
                json={"question": "سؤال"},
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(response.status_code, 401)


class TestRoleRoutes(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(bot=FakeBot()))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_student_cannot_use_admin_control_plane(self):
        token = create_access_token("12345", Role.STUDENT)
        response = self.client.get(
            "/api/admin/files", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 403)

    def test_guest_chat_requires_no_token(self):
        response = self.client.post("/api/chat/guest", json={"question": "ما هي الكليات؟"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "knowledge_base")

    def test_employee_token_is_accepted_by_unified_chat(self):
        token = create_access_token("EMP-1001", Role.EMPLOYEE)
        response = self.client.post(
            "/api/chat",
            json={"question": "ما تعليمات القبول؟", "role": "admin"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("EMP-1001", self.client.app.state.bot.history)


class TestPreRetrievalFileFilter(unittest.TestCase):
    def test_disallowed_collection_never_enters_candidate_pool(self):
        store = UploadedFilesStore()
        store._chunks = {
            "public": ["public-1", "public-2"],
            "admin_only": ["secret-1", "secret-2"],
        }
        with patch("app.uploaded_files.embed_query", return_value=np.array([1.0])):
            result = store.search_all(
                "سؤال", top_k=10, allowed_collections={"public"}
            )
        self.assertEqual(result, ["public-1", "public-2"])
        self.assertFalse(any("secret" in chunk for chunk in result))

    @patch("app.file_catalog._catalog")
    def test_uncatalogued_legacy_file_fails_closed_when_disabled(self, catalog):
        from app import file_catalog

        catalog.return_value.find.return_value = []
        principal = decode_principal(
            create_access_token("guest-check", Role.GUEST, token_version=1)
        )
        with patch("app.config.LEGACY_UNCATALOGUED_FILES_PUBLIC", False):
            allowed = file_catalog.allowed_collections(principal, {"legacy_secret"})
        self.assertEqual(allowed, set())


if __name__ == "__main__":
    unittest.main()
