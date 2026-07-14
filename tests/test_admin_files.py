"""إدارة الملفات من لوحة الأدمن: حذف/تعديل صلاحيات (بما فيها الملفات القديمة
السابقة للسجل) + مذكرة «الأحدث يفوز» عند تعارض المصادر."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import create_app
from app.chatbot import IUGChatbot
from app.rbac import Role
from app.tokens import create_access_token
from tests.test_api import FakeBot


def _admin_headers():
    return {"Authorization": "Bearer " + create_access_token("ADMIN-1", Role.ADMIN)}


class TestAdminFileActions(unittest.TestCase):

    def setUp(self):
        from app.api import deps
        deps.reset_rate_limits()
        self.bot = FakeBot()
        self.client = TestClient(create_app(bot=self.bot))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_delete_managed_file_removes_content_and_archives(self):
        entry = {"file_id": "f1", "collection": "ملف_علامات"}
        with patch("app.file_catalog.get_file", return_value=entry), \
             patch("app.file_catalog.archive") as archive, \
             patch("app.audit.record"):
            r = self.client.delete("/api/admin/files/f1", headers=_admin_headers())
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("ملف_علامات", self.bot.files)      # حُذف من المحتوى/الفهرس
        archive.assert_called_once()

    def test_delete_legacy_file_adopts_then_deletes(self):
        adopted = {"file_id": "auto1", "collection": "ملف_علامات"}
        with patch("app.file_catalog.adopt_legacy", return_value=adopted) as adopt, \
             patch("app.file_catalog.archive") as archive, \
             patch("app.audit.record"):
            r = self.client.delete(
                "/api/admin/files/legacy:ملف_علامات", headers=_admin_headers()
            )
        self.assertEqual(r.status_code, 200)
        adopt.assert_called_once_with("ملف_علامات", "ADMIN-1")
        archive.assert_called_once_with("auto1", "ADMIN-1")
        self.assertNotIn("ملف_علامات", self.bot.files)

    def test_delete_requires_admin_role(self):
        student = {"Authorization": "Bearer " + create_access_token("12345", Role.STUDENT)}
        r = self.client.delete("/api/admin/files/f1", headers=student)
        self.assertEqual(r.status_code, 403)

    def test_access_edit_on_legacy_adopts_first(self):
        adopted = {"file_id": "auto2", "collection": "قديم"}
        with patch("app.file_catalog.adopt_legacy", return_value=adopted) as adopt, \
             patch("app.file_catalog.update_access", return_value={**adopted, "classification": "admin_only"}) as upd, \
             patch("app.audit.record"):
            r = self.client.patch(
                "/api/admin/files/legacy:قديم/access",
                headers=_admin_headers(),
                json={"classification": "admin_only", "allowed_roles": ["admin"], "owner_id": None},
            )
        self.assertEqual(r.status_code, 200)
        adopt.assert_called_once()
        self.assertEqual(upd.call_args[0][0], "auto2")   # عُدّل السجل المتبنّى

    def test_missing_file_is_404(self):
        with patch("app.file_catalog.get_file", return_value=None):
            r = self.client.delete("/api/admin/files/ghost", headers=_admin_headers())
        self.assertEqual(r.status_code, 404)


class TestSourceRecencyNote(unittest.TestCase):
    """مذكرة تفضيل الأحدث تُبنى فقط عند تعدد المصادر وتُرتّبها تنازلياً."""

    CHUNKS = [
        "[ملف: رسوم_2025]\nرسوم الساعة 20 دينار",
        "[ملف: رسوم_2026]\nرسوم الساعة 25 دينار",
        "[ملف: رسوم_2025]\nمقطع آخر",
    ]

    def test_note_lists_sources_newest_first_with_rule(self):
        dates = {"رسوم_2025": "2025-09-01T00:00:00", "رسوم_2026": "2026-07-01T00:00:00"}
        with patch("app.file_catalog.recency_map", return_value=dates):
            note = IUGChatbot._source_recency_note(self.CHUNKS)
        self.assertIn("رسوم_2026: 2026-07-01", note)
        self.assertIn("رسوم_2025: 2025-09-01", note)
        self.assertLess(note.index("رسوم_2026"), note.index("رسوم_2025"))  # الأحدث أولاً
        self.assertIn("اعتمد قيمة", note)

    def test_single_source_produces_no_note(self):
        with patch("app.file_catalog.recency_map", return_value={"رسوم_2025": "2025-01-01"}):
            note = IUGChatbot._source_recency_note([self.CHUNKS[0], self.CHUNKS[2]])
        self.assertEqual(note, "")

    def test_no_dates_at_all_produces_no_note(self):
        with patch("app.file_catalog.recency_map", return_value={}):
            note = IUGChatbot._source_recency_note(self.CHUNKS)
        self.assertEqual(note, "")

    def test_unknown_date_treated_as_oldest(self):
        dates = {"رسوم_2026": "2026-07-01T00:00:00"}   # رسوم_2025 بلا تاريخ
        with patch("app.file_catalog.recency_map", return_value=dates):
            note = IUGChatbot._source_recency_note(self.CHUNKS)
        self.assertIn("رسوم_2025: غير معروف", note)
        self.assertLess(note.index("رسوم_2026"), note.index("رسوم_2025"))


if __name__ == "__main__":
    unittest.main()
