"""إدارة الملفات من لوحة الأدمن: حذف/تعديل صلاحيات (بما فيها الملفات القديمة
السابقة للسجل) + مذكرة «الأحدث يفوز» عند تعارض المصادر."""

import unittest
from unittest.mock import call, patch

from fastapi.testclient import TestClient

from app import file_catalog
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
             patch("app.file_catalog.purge_versions", return_value=2) as purge, \
             patch("app.audit.record"):
            r = self.client.delete("/api/admin/files/f1", headers=_admin_headers())
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("ملف_علامات", self.bot.files)      # حُذف من المحتوى/الفهرس
        archive.assert_called_once()
        purge.assert_called_once_with("f1")                 # نصوص النسخ حُذفت أيضاً

    def test_delete_legacy_file_adopts_then_deletes(self):
        adopted = {"file_id": "auto1", "collection": "ملف_علامات"}
        with patch("app.file_catalog.adopt_legacy", return_value=adopted) as adopt, \
             patch("app.file_catalog.archive") as archive, \
             patch("app.file_catalog.purge_versions", return_value=0), \
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

    def test_adopt_all_brings_uncatalogued_files_under_management(self):
        """التسوية الجماعية: الملفات القديمة تدخل السجل بضغطة — الشرط المسبق
        لإطفاء LEGACY_UNCATALOGUED_FILES_PUBLIC في الإنتاج بلا اختفاء ملفات."""
        with patch("app.file_catalog.find_by_collection", return_value=None), \
             patch("app.file_catalog.adopt_legacy") as adopt, \
             patch("app.audit.record"):
            r = self.client.post("/api/admin/files/adopt-all", headers=_admin_headers())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 1)                  # FakeBot فيه ملف واحد
        adopt.assert_called_once_with("ملف_علامات", "ADMIN-1")

    def test_adopt_all_skips_already_catalogued(self):
        with patch("app.file_catalog.find_by_collection", return_value={"file_id": "x"}), \
             patch("app.file_catalog.adopt_legacy") as adopt, \
             patch("app.audit.record"):
            r = self.client.post("/api/admin/files/adopt-all", headers=_admin_headers())
        self.assertEqual(r.json()["count"], 0)
        adopt.assert_not_called()

    def test_adopt_all_requires_admin(self):
        from app.rbac import Role
        from app.tokens import create_access_token
        token = create_access_token("12345", Role.STUDENT)
        r = self.client.post("/api/admin/files/adopt-all",
                             headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 403)

    def test_missing_file_is_404(self):
        with patch("app.file_catalog.get_file", return_value=None):
            r = self.client.delete("/api/admin/files/ghost", headers=_admin_headers())
        self.assertEqual(r.status_code, 404)

    def test_preflight_endpoint_surfaces_duplicates_and_conflicts(self):
        report = {
            "exact_duplicate_count": 2,
            "conflict_count": 1,
            "unresolved_conflict_count": 1,
            "can_publish": False,
            "conflicts": [{"conflict_id": "c1"}],
        }
        with patch("app.file_catalog.preflight", return_value=report), \
             patch("app.audit.record"):
            r = self.client.post(
                "/api/admin/files/preflight",
                headers=_admin_headers(),
                json={"collection": "رسوم_جديدة", "documents": [{"fee": 7}]},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["exact_duplicate_count"], 2)
        self.assertFalse(r.json()["can_publish"])

    def test_unresolved_conflict_blocks_publish_with_409(self):
        with patch(
            "app.file_catalog.publish",
            side_effect=file_catalog.UnresolvedDataConflictError("conflict"),
        ):
            r = self.client.post(
                "/api/admin/files/f1/publish", headers=_admin_headers()
            )
        self.assertEqual(r.status_code, 409)

    def test_admin_can_resolve_all_conflicts_with_audited_decision(self):
        resolved = {
            "file_id": "f1",
            "resolved_count": 2,
            "preflight": {"unresolved_conflict_count": 0, "can_publish": True},
        }
        with patch("app.file_catalog.resolve_conflicts", return_value=resolved) as fn, \
             patch("app.audit.record") as audit:
            r = self.client.post(
                "/api/admin/files/f1/resolve-conflicts",
                headers=_admin_headers(),
                json={"decision": "prefer_incoming", "conflict_ids": []},
            )
        self.assertEqual(r.status_code, 200)
        fn.assert_called_once_with(
            "f1", "ADMIN-1", decision="prefer_incoming", conflict_ids=[]
        )
        audit.assert_called_once()


class TestDeleteLeavesNoOrphans(unittest.TestCase):
    """حذف الملف يجب أن يمحو كل تبعاته: المتجهات المخزّنة (قرص وMongo)
    ونصوص النسخ — لا يتيمة تبقى بعد الحذف."""

    def test_index_store_delete_removes_disk_files(self):
        import os, tempfile
        import numpy as np
        from app import config, index_store
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(config, "INDEX_CACHE_DIR", tmp), \
             patch.object(index_store, "_index_col") as col:
            index_store._disk_save("ملف_تجريبي", ["مقطع"], np.zeros((1, 4)), "m")
            npy, meta = index_store._disk_paths("ملف_تجريبي")
            assert os.path.exists(npy) and os.path.exists(meta)
            index_store.delete("ملف_تجريبي")
            self.assertFalse(os.path.exists(npy))            # القرص نظيف
            self.assertFalse(os.path.exists(meta))
            col.return_value.delete_one.assert_called_once_with({"_id": "ملف_تجريبي"})  # وMongo

    def test_uploaded_store_delete_purges_persisted_index(self):
        from app.uploaded_files import UploadedFilesStore
        store = UploadedFilesStore()
        store._chunks["ملف"] = ["c"]
        with patch("app.uploaded_files.drop_uploaded_collection"), \
             patch("app.uploaded_files.index_store.delete") as purge:
            store.delete("ملف")
        # بنفس المفتاح المسبوق الذي خُزّن به — الاسم المجرد ترك يتيمة سابقاً
        self.assertEqual(
            purge.call_args_list,
            [call("uploaded::v1::ملف"), call("uploaded::v2::ملف")],
        )
        self.assertNotIn("ملف", store._chunks)

    def test_ensure_indexes_backfills_legacy_user_id_first(self):
        """حسابات قديمة بلا user_id أفشلت بناء الفهرس الفريد على القاعدة الحية
        (dup key: user_id null) — يجب التعبئة من student_id قبل البناء."""
        from app import auth
        col = unittest.mock.MagicMock()
        with patch.object(auth, "_col", return_value=col):
            auth.ensure_indexes()
        col.update_many.assert_called_once()          # backfill أولاً
        kwargs = {c.args[0]: c.kwargs for c in col.create_index.call_args_list}
        self.assertTrue(kwargs["user_id"]["unique"] and kwargs["user_id"]["sparse"])

    def test_purge_versions_deletes_all_version_docs(self):
        from app import file_catalog
        col = unittest.mock.MagicMock()
        col.delete_many.return_value.deleted_count = 3
        with patch("app.file_catalog._versions", return_value=col):
            n = file_catalog.purge_versions("f9")
        self.assertEqual(n, 3)
        col.delete_many.assert_called_once_with({"file_id": "f9"})


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
