"""
Behavioral tests for the new chat pipeline (access-filtered hybrid retrieval).
These assert the *guarantees* of chat — privacy shortcuts, identity handling,
history folding, uploaded-file flows — rather than diffing against the legacy
monolith (whose retrieval behavior we have intentionally superseded).
"""

import copy
import unittest
from unittest.mock import patch

from app import chunking, config, embeddings
from app.chatbot import IUGChatbot
from tests.test_equivalence import FIXTURE_DATA, UPLOADED_DOCS, fake_embed


class ChatBase(unittest.TestCase):

    def setUp(self):
        embeddings.reset_query_cache()  # isolate the module-level query cache
        self.llm_calls = []
        self.bot = IUGChatbot()

        data = copy.deepcopy(FIXTURE_DATA)
        self.bot._kb._data = data
        self.bot._kb._chunks, self.bot._kb._chunk_meta = chunking.build_chunks(data)
        with patch("app.embeddings.embed_texts", side_effect=fake_embed):
            self.bot._kb._index = embeddings.build_index(self.bot._kb._chunks)

        nchunks = chunking.build_uploaded_chunks(copy.deepcopy(UPLOADED_DOCS), "ملف_علامات")
        self.bot._uploaded._chunks["ملف_علامات"] = nchunks
        with patch("app.embeddings.embed_texts", side_effect=fake_embed):
            self.bot._uploaded._indexes["ملف_علامات"] = embeddings.build_index(nchunks)

    def _chat(self, method, *args):
        def fake_groq(headers, payload):
            self.llm_calls.append(payload)
            return "إجابة تجريبية من النموذج."

        with patch.object(config, "CHAT_API_KEY", "test-key"), \
             patch("app.embeddings.embed_texts", side_effect=fake_embed), \
             patch("app.llm._post_with_retry", side_effect=fake_groq):
            return getattr(self.bot, method)(*args)

    def _system_of_last_call(self) -> str:
        return self.llm_calls[-1]["messages"][0]["content"]


class TestChatFlows(ChatBase):

    def test_normal_question_guest_has_no_identity_note(self):
        res = self._chat("chat", "ما هي رسوم هندسة الحاسوب؟", "guest_session")
        self.assertIn("answer", res)
        self.assertEqual(len(self.llm_calls), 1)
        self.assertNotIn("الطالب الذي يحادثك الآن", self._system_of_last_call())

    def test_question_with_student_record_adds_identity_and_own_data(self):
        self._chat("chat", "متى يبدأ التسجيل؟", "12345")
        system = self._system_of_last_call()
        self.assertIn("محمد أحمد خالد", system)
        self.assertIn("بيانات الطالب الحالي", system)

    def test_academic_status_shortcut_skips_llm(self):
        res = self._chat("chat", "ما هي حالتي الأكاديمية؟", "12345")
        self.assertEqual(self.llm_calls, [])          # answered without the LLM
        self.assertIn("88.5", res["answer"])

    def test_privacy_guard_blocks_other_student_without_llm(self):
        res = self._chat("chat", "كم معدل سالم؟", "12345")
        self.assertEqual(self.llm_calls, [])
        self.assertIn("خاصة", res["answer"])

    def test_history_folds_into_next_prompt(self):
        self._chat("chat", "ما هي رسوم هندسة الحاسوب؟", "sess")
        self._chat("chat", "وماذا عن المساقات؟", "sess")
        user_msg = self.llm_calls[-1]["messages"][1]["content"]
        self.assertIn("سجل المحادثة السابقة", user_msg)


class TestUploadedChatFlows(ChatBase):

    def test_chat_with_file(self):
        res = self._chat("chat_with_file", "كم علامة الرياضيات؟", "ملف_علامات", "sess")
        self.assertEqual(res["source"], "uploaded_file")
        self.assertTrue(res["top_chunks"])

    def test_chat_with_missing_file_skips_llm(self):
        res = self._chat("chat_with_file", "سؤال", "غير_موجود", "sess")
        self.assertEqual(self.llm_calls, [])
        self.assertIn("غير موجود", res["answer"])

    def test_chat_with_all_files(self):
        res = self._chat("chat_with_all_files", "كم علامة الفيزياء؟", "sess")
        self.assertEqual(res["source"], "uploaded_files_all")
        self.assertTrue(res["top_chunks"])

    def test_chat_with_all_files_empty(self):
        self.bot._uploaded._chunks = {}
        self.bot._uploaded._indexes = {}
        res = self._chat("chat_with_all_files", "سؤال", "sess")
        self.assertEqual(res["answer"], "لا توجد ملفات مرفوعة حالياً.")


if __name__ == "__main__":
    unittest.main()
