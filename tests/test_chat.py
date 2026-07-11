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
from app.admissions import AdmissionFact, AdmissionResolution
from app.sessions import SessionStore
from tests.test_equivalence import FIXTURE_DATA, UPLOADED_DOCS, fake_embed


class ChatBase(unittest.TestCase):

    def setUp(self):
        embeddings.reset_query_cache()  # isolate the module-level query cache
        self.llm_calls = []
        self.bot = IUGChatbot(sessions=SessionStore())  # in-memory, no Mongo

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


class TestStudentChat(ChatBase):

    PROFILE = {"name": "محمد أحمد", "major": "هندسة حاسوب", "gpa": 88.5,
               "rank": 3, "academic_status": "regular"}

    def test_own_record_answered_from_profile_without_llm(self):
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}):
            res = self._chat("chat_as_student", "ما هي حالتي الأكاديمية؟", "12345")
        self.assertEqual(self.llm_calls, [])          # answered from profile, no LLM
        self.assertEqual(res["source"], "student_profile")
        self.assertIn("88.5", res["answer"])
        self.assertIn("هندسة حاسوب", res["answer"])

    def test_my_gpa_question_uses_profile(self):
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}):
            res = self._chat("chat_as_student", "كم معدلي؟", "12345")
        self.assertEqual(res["source"], "student_profile")

    def test_my_name_question_uses_profile_without_llm(self):
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}):
            res = self._chat("chat_as_student", "ما هو اسمي؟", "12345")
        self.assertEqual(self.llm_calls, [])
        self.assertEqual(res["source"], "student_profile")
        self.assertIn("محمد أحمد", res["answer"])

    def test_content_question_routes_to_files(self):
        with patch("app.auth.find_account", return_value=None):
            res = self._chat("chat_as_student", "كم رسوم كلية الطب؟", "12345")
        self.assertEqual(res["source"], "uploaded_files_all")

    def test_asking_about_another_student_is_refused(self):
        # third-person ranking question → blocked, never reaches profile or LLM
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}):
            res = self._chat("chat_as_student", "ما معدل الطالب أحمد؟", "12345")
        self.assertEqual(res["source"], "privacy_block")
        self.assertEqual(self.llm_calls, [])
        self.assertIn("خاصة", res["answer"])

    def test_unknown_student_cannot_get_a_profile(self):
        # No account for this id → nothing to expose → falls through to content.
        with patch("app.auth.find_account", return_value=None):
            res = self._chat("chat_as_student", "ما معدلي؟", "99999")
        self.assertEqual(res["source"], "uploaded_files_all")


class TestUploadedChatFlows(ChatBase):

    def test_structured_admission_answer_skips_rag_and_llm(self):
        fact = AdmissionFact(
            faculty="تكنولوجيا المعلومات",
            program="تكنولوجيا المعلومات",
            degree="بكالوريوس",
            branches=("علمي",),
            min_percentage=65,
            source="ملف القبول",
            path="doc[0]",
        )
        resolution = AdmissionResolution(
            "برنامج تكنولوجيا المعلومات: 65% للفرع العلمي.",
            (fact,),
        )
        with patch.object(
            self.bot._uploaded, "resolve_admission", return_value=resolution
        ), patch.object(
            self.bot._uploaded, "search_all",
            side_effect=AssertionError("RAG must not run"),
        ):
            res = self._chat(
                "chat_with_all_files",
                "ما معدل قبول تكنولوجيا المعلومات؟",
                "admission-sess",
            )

        self.assertEqual(self.llm_calls, [])
        self.assertEqual(res["source"], "structured_admission")
        self.assertIn("65%", res["answer"])

    def test_palestine_capital_uses_trusted_fact_without_llm(self):
        res = self._chat("chat_with_all_files", "ما هي عاصمة فلسطين؟", "capital-sess")

        self.assertEqual(self.llm_calls, [])
        self.assertEqual(res["source"], "trusted_fact")
        self.assertIn("القدس", res["answer"])
        self.assertNotIn("رام الله", res["answer"])

    def test_official_university_url_uses_trusted_fact_without_llm(self):
        res = self._chat("chat_with_all_files", "ما هو رابط موقع الجامعة؟", "url-sess")

        self.assertEqual(self.llm_calls, [])
        self.assertEqual(res["source"], "trusted_fact")
        self.assertIn("https://www.iugaza.edu.ps/", res["answer"])

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

    def test_generic_engineering_hourly_fee_retrieves_both_levels(self):
        searches = []

        def fake_search(question, top_k):
            searches.append(question)
            if "بكالوريوس" in question:
                return ["بكالوريوس الهندسة: سعر الساعة 30 دينار"]
            if "ماجستير" in question:
                return ["ماجستير الهندسة: سعر الساعة 70 دينار"]
            return ["معلومات عامة عن كلية الهندسة"]

        with patch.object(self.bot._uploaded, "search_all", side_effect=fake_search):
            res = self._chat("chat_with_all_files", "كم سعر ساعة الهندسة؟", "fee-sess")

        self.assertEqual(len(searches), 3)
        self.assertTrue(any("بكالوريوس" in query for query in searches))
        self.assertTrue(any("ماجستير" in query for query in searches))
        self.assertEqual(len(res["top_chunks"]), 3)
        system = self._system_of_last_call()
        self.assertIn("30 دينار", system)
        self.assertIn("70 دينار", system)
        self.assertIn("اعرض بشكل منفصل", system)

    def test_explicit_engineering_degree_does_not_expand_search(self):
        with patch.object(
            self.bot._uploaded,
            "search_all",
            return_value=["بكالوريوس الهندسة: سعر الساعة 30 دينار"],
        ) as search:
            self._chat(
                "chat_with_all_files",
                "كم سعر ساعة الهندسة للبكالوريوس؟",
                "explicit-fee-sess",
            )

        search.assert_called_once()

    def test_general_question_still_reaches_llm_when_files_are_empty(self):
        self.bot._uploaded._chunks = {}
        self.bot._uploaded._indexes = {}
        res = self._chat("chat_with_all_files", "ما هي عاصمة مصر؟", "sess")

        self.assertEqual(res["source"], "uploaded_files_all")
        self.assertEqual(len(self.llm_calls), 1)
        system = self._system_of_last_call()
        self.assertIn("معرفتك العامة الموثوقة", system)
        self.assertIn("عاصمة فلسطين هي القدس", system)


if __name__ == "__main__":
    unittest.main()
