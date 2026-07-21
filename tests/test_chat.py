"""
Behavioral tests for the new chat pipeline (access-filtered hybrid retrieval).
These assert the *guarantees* of chat — private context, identity handling,
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
from app.text_norm import normalize_arabic
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

    def _chat(self, method, *args, **kwargs):
        def fake_groq(headers, payload):
            self.llm_calls.append(payload)
            return "إجابة تجريبية من النموذج."

        with patch.object(config, "CHAT_API_KEY", "test-key"), \
             patch("app.embeddings.embed_texts", side_effect=fake_embed), \
             patch("app.chatbot.file_catalog.recency_map", return_value={}), \
             patch("app.llm._post_with_retry", side_effect=fake_groq):
            return getattr(self.bot, method)(*args, **kwargs)

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

    def test_academic_status_evidence_still_reaches_llm(self):
        res = self._chat("chat", "ما هي حالتي الأكاديمية؟", "12345")
        self.assertEqual(len(self.llm_calls), 1)
        self.assertIn("88.5", self._system_of_last_call())
        self.assertTrue(res["retrieval_metadata"]["llm_always_answer"])

    def test_privacy_guard_is_policy_evidence_for_llm(self):
        res = self._chat("chat", "كم معدل سالم؟", "12345")
        self.assertEqual(len(self.llm_calls), 1)
        system = self._system_of_last_call()
        self.assertIn("سياسة خصوصية ملزمة", system)
        self.assertNotIn("سالم", system)
        self.assertEqual(res["source"], "privacy_policy_llm")
        self.assertNotIn(
            "سالم",
            __import__("json").dumps(
                res["retrieval_metadata"]["diagnostic_trace"],
                ensure_ascii=False,
            ),
        )

    def test_history_folds_into_next_prompt(self):
        self._chat("chat", "ما هي رسوم هندسة الحاسوب؟", "sess")
        self._chat("chat", "وماذا عن المساقات؟", "sess")
        user_msg = self.llm_calls[-1]["messages"][1]["content"]
        self.assertIn("سياق المستخدم السابق", user_msg)
        self.assertIn("ما هي رسوم هندسة الحاسوب؟", user_msg)
        self.assertNotIn("المساعد:", user_msg)

    def test_explicit_answer_explanation_sees_only_latest_answer_as_untrusted(self):
        self._chat("chat", "ما هي رسوم هندسة الحاسوب؟", "explain-sess")
        self._chat("chat", "وضح إجابتك السابقة", "explain-sess")
        user_msg = self.llm_calls[-1]["messages"][1]["content"]
        self.assertIn("جواب المساعد السابق غير الموثق", user_msg)
        self.assertIn("إجابة تجريبية من النموذج.", user_msg)
        self.assertIn("ليس دليلاً جامعياً", user_msg)


class TestStudentChat(ChatBase):

    PROFILE = {"name": "محمد أحمد", "major": "هندسة حاسوب", "gpa": 88.5,
               "rank": 3, "academic_status": "regular"}

    def test_own_record_reaches_llm_with_private_profile_context(self):
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}):
            res = self._chat("chat_as_student", "ما هي حالتي الأكاديمية؟", "12345")
        self.assertEqual(len(self.llm_calls), 1)
        self.assertEqual(res["source"], "student_context_rag_llm")
        system = self._system_of_last_call()
        self.assertIn("بيانات الطالب الحالي", system)
        self.assertIn("88.5", system)
        self.assertIn("هندسة حاسوب", system)

    def test_my_gpa_question_uses_llm_with_profile_context(self):
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}):
            res = self._chat("chat_as_student", "كم معدلي؟", "12345")
        self.assertEqual(len(self.llm_calls), 1)
        self.assertEqual(res["source"], "student_context_rag_llm")
        self.assertIn("88.5", self._system_of_last_call())

    def test_my_name_question_uses_llm_with_profile_context(self):
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}):
            res = self._chat("chat_as_student", "ما هو اسمي؟", "12345")
        self.assertEqual(len(self.llm_calls), 1)
        self.assertEqual(res["source"], "student_context_rag_llm")
        self.assertIn("محمد أحمد", self._system_of_last_call())

    def test_scholarship_question_with_gpa_processes_the_full_question(self):
        scholarship_chunk = "منحة التفوق: يمكن للطالب التقدم وفق شروط المنح المنشورة."
        question = "معدلي 90 ايش في منح متاحة الي"
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}), \
             patch.object(self.bot, "_search_all_for_question",
                          return_value=[scholarship_chunk]) as search:
            res = self._chat("chat_as_student", question, "12345")

        self.assertGreaterEqual(search.call_count, 1)
        queries = [call.args[0] for call in search.call_args_list]
        self.assertTrue(any("منح" in query for query in queries))
        self.assertFalse(any("معدل القبول" in query for query in queries))
        self.assertEqual(len(self.llm_calls), 1)
        self.assertEqual(res["source"], "student_context_rag_llm")
        system = self._system_of_last_call()
        self.assertIn("88.5", system)
        self.assertIn(scholarship_chunk, system)
        self.assertIn(question, self.llm_calls[-1]["messages"][1]["content"])
        self.assertEqual(res["top_chunks"], [scholarship_chunk])
        self.assertNotIn("محمد أحمد", "\n".join(res["top_chunks"]))

    def test_my_department_question_expands_search_with_major(self):
        """«رئيس قسمي» — the retrieval query must carry the student's major so
        the right department's chunk can rank; the LLM question stays literal."""
        question = "كيف اتواصل مع رئيس قسمي؟"
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}), \
             patch.object(self.bot, "_search_all_for_question",
                          return_value=[]) as search:
            self._chat("chat_as_student", question, "12345")

        searched = search.call_args[0][0]
        from app.text_norm import normalize_arabic
        self.assertIn(normalize_arabic("هندسة حاسوب"), normalize_arabic(searched))
        self.assertIn(normalize_arabic("رئيس قسمي"), normalize_arabic(searched))
        # the LLM still sees exactly what the student typed
        self.assertIn(question, self.llm_calls[-1]["messages"][1]["content"])

    def test_follow_up_question_searches_with_previous_topic(self):
        """«كم هيكلفني رسوم هذا الطلب» after a deferral question — retrieval
        must inherit the previous turn's topic, not guess a fresh one."""
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}):
            self._chat("chat_as_student", "كيف بدي اجل الفصل الحالي؟", "12345")
            with patch.object(self.bot, "_search_all_for_question",
                              return_value=[]) as search:
                self._chat("chat_as_student", "كم هيكلفني رسوم هذا الطلب؟", "12345")

        # صار البحث مزدوجاً (موسّع بالسياق + خام) — الموسّع يجب أن يرث الموضوع
        queries = [call.args[0] for call in search.call_args_list]
        expanded = [q for q in queries if "اجل الفصل" in q]
        self.assertTrue(expanded, f"لا استعلام ورث الموضوع السابق: {queries}")
        self.assertIn("رسوم هذا الطلب", expanded[0])   # current question
        self.assertTrue(any("رسوم هذا الطلب" in q and "اجل الفصل" not in q
                            for q in queries))          # والخام جرى أيضاً

    def test_memory_injects_relevant_turn_and_drops_unrelated(self):
        """الذاكرة الدلالية: الدور القديم ذو الصلة يُحقن، وغير المرتبط يُستبعد،
        والدور الأحدث يُحقن دائماً، مع التعليمة الملزمة قبل البيانات."""
        import numpy as np
        from app.sessions import USER_MEMORY_INSTRUCTION

        def unit(*xs):
            v = np.asarray(xs, dtype=np.float32)
            return (v / np.linalg.norm(v)).reshape(-1, 1)

        self.bot.push_history("12345", "سؤال التأجيل القديم", "a1", embedding=unit(1, 0))
        self.bot.push_history("12345", "سؤال المنح البعيد", "a2", embedding=unit(0, 1))
        self.bot.push_history("12345", "أحدث سؤال", "a3", embedding=unit(0, 1))

        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}), \
             patch.object(self.bot, "_search_all_for_question", return_value=[]), \
             patch("app.embeddings.embed_query", return_value=unit(1, 0)):
            self._chat("chat_as_student", "كم رسوم هذا الطلب؟", "12345")

        user_msg = self.llm_calls[-1]["messages"][1]["content"]
        self.assertIn(USER_MEMORY_INSTRUCTION, user_msg)
        self.assertIn("سؤال التأجيل القديم", user_msg)   # ذو صلة (cos=1)
        self.assertNotIn("سؤال المنح البعيد", user_msg)  # متعامد → مستبعد
        self.assertIn("أحدث سؤال", user_msg)             # الأحدث دائماً
        self.assertNotIn("a1", user_msg)                 # أجوبة المساعد لا تعود كدليل
        self.assertNotIn("a2", user_msg)
        self.assertNotIn("a3", user_msg)

    def test_memory_embedding_stored_once_with_turn(self):
        """متجه السؤال يُخزَّن مع الدور عند الحفظ — لا يُعاد توليده."""
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}), \
             patch.object(self.bot, "_search_all_for_question", return_value=[]):
            self._chat("chat_as_student", "ما رسوم الماجستير؟", "12345")
        last_turn = self.bot.get_history("12345")[-1]
        self.assertIn("embedding", last_turn)
        self.assertIsInstance(last_turn["embedding"], list)

    def test_stream_answer_streams_content_and_saves_history(self):
        from app.rbac import Principal, Role

        principal = Principal("12345", Role.STUDENT)
        with patch.object(config, "CHAT_API_KEY", "test-key"), \
             patch("app.embeddings.embed_texts", side_effect=fake_embed), \
             patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}), \
             patch.object(
                 self.bot,
                 "_search_all_for_question",
                 return_value=["[ملف: الرسوم] رسوم التأجيل 10 دنانير."],
             ), \
             patch("app.llm._post_with_retry", return_value="الرسوم 10 دنانير"):
            chunks = list(self.bot.stream_answer(
                "كم رسوم التأجيل؟", principal, allowed_collections=None))

        self.assertEqual("".join(chunks), "الرسوم 10 دنانير")
        # the streamed answer is persisted as one history turn
        self.assertEqual(self.bot.get_history("12345")[-1]["assistant"], "الرسوم 10 دنانير")

    def test_stream_answer_privacy_policy_still_reaches_llm(self):
        from app.rbac import Principal, Role
        principal = Principal("12345", Role.STUDENT)
        calls = []

        def fake_llm(headers, payload):
            calls.append(payload)
            return "عذراً، هذه البيانات خاصة."

        with patch("app.embeddings.embed_texts", side_effect=fake_embed), \
             patch.object(config, "CHAT_API_KEY", "test-key"), \
             patch("app.llm._post_with_retry", side_effect=fake_llm):
            chunks = list(self.bot.stream_answer(
                "كم معدل الطالب 67890؟", principal, allowed_collections=None))
        self.assertEqual(len(calls), 1)
        self.assertIn("سياسة خصوصية", calls[0]["messages"][0]["content"])
        self.assertIn("خاصة", "".join(chunks))

    def test_memory_embedding_failure_falls_back_and_still_answers(self):
        """فشل التضمين لا يكسر المتابعة ويستخدم كلام المستخدم فقط."""
        self.bot.push_history(
            "12345", "ما رسوم تأجيل الفصل؟", "جواب سابق غير موثوق"
        )
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}), \
             patch.object(self.bot, "_search_all_for_question", return_value=[]), \
             patch("app.embeddings.embed_query", side_effect=RuntimeError("jina down")):
            res = self._chat("chat_as_student", "وما شروطه؟", "12345")

        self.assertEqual(res["answer"], "إجابة تجريبية من النموذج.")  # الشات يعمل
        user_msg = self.llm_calls[-1]["messages"][1]["content"]
        self.assertIn("ما رسوم تأجيل الفصل؟", user_msg)  # fallback يشمل سؤال المستخدم
        self.assertNotIn("جواب سابق غير موثوق", user_msg)
        # ولا يُخزَّن متجه لهذا الدور (يُحسب لاحقاً عند أول سؤال ناجح تالٍ)
        self.assertNotIn("embedding", self.bot.get_history("12345")[-1])

    def test_markdown_tables_are_converted_to_lists(self):
        """واجهة الشات لا تعرض جداول Markdown — تُحوَّل حتمياً لقوائم."""
        table = ("مقدمة\n"
                 "| الكلية | السعر | ملاحظة |\n"
                 "|--------|-------|--------|\n"
                 "| الطب | 100 دينار | بكالوريوس |\n"
                 "| الهندسة | 30 دينار | بكالوريوس |\n"
                 "خاتمة")
        out = IUGChatbot._strip_markdown_tables(table)
        self.assertNotIn("|", out)                      # لا أعمدة إطلاقاً
        self.assertIn("**الطب** — السعر: 100 دينار، ملاحظة: بكالوريوس", out)
        self.assertIn("**الهندسة** — السعر: 30 دينار", out)
        self.assertIn("مقدمة", out); self.assertIn("خاتمة", out)

    def test_text_without_tables_passes_through_unchanged(self):
        text = "سعر الساعة 100 دينار.\n- بند أول\n- بند ثانٍ"
        self.assertEqual(IUGChatbot._strip_markdown_tables(text), text)

    def test_llm_answer_with_table_reaches_user_as_list(self):
        """التكامل: جواب النموذج بجدولٍ يصل الطالب قائمةً."""
        tabled = "| التخصص | الرسوم |\n|---|---|\n| الطب | 100 |"
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}), \
             patch.object(self.bot, "_search_all_for_question", return_value=[]), \
             patch("app.llm._post_with_retry", return_value=tabled), \
             patch.object(config, "CHAT_API_KEY", "k"), \
             patch("app.embeddings.embed_texts", side_effect=lambda t: __import__("numpy").zeros((len(t), 4))):
            res = self.bot.chat_as_student("قارن الرسوم بجدول", "12345")
        self.assertNotIn("|", res["answer"])
        self.assertIn("الطب", res["answer"])

    def test_private_profile_answers_are_never_shared_through_cache(self):
        profiles = {
            "11111": {**self.PROFILE, "name": "الطالب الأول", "gpa": 91.0},
            "22222": {**self.PROFILE, "name": "الطالب الثاني", "gpa": 72.0},
        }

        def find_account(student_id):
            return {"student_id": student_id, "profile": profiles[student_id]}

        with patch("app.auth.find_account", side_effect=find_account):
            self._chat("chat_as_student", "كم معدلي؟", "11111")
            self._chat("chat_as_student", "كم معدلي؟", "22222")

        self.assertEqual(len(self.llm_calls), 2)
        first_system = self.llm_calls[0]["messages"][0]["content"]
        second_system = self.llm_calls[1]["messages"][0]["content"]
        self.assertIn("91.0", first_system)
        self.assertNotIn("72.0", first_system)
        self.assertIn("72.0", second_system)
        self.assertNotIn("91.0", second_system)

    def test_content_question_routes_to_files(self):
        with patch("app.auth.find_account", return_value=None):
            res = self._chat("chat_as_student", "كم رسوم كلية الطب؟", "12345")
        self.assertEqual(res["source"], "student_context_rag_llm")

    def test_asking_about_another_student_is_refused(self):
        # third-person ranking question → policy-only evidence, still reaches LLM
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}):
            res = self._chat("chat_as_student", "ما معدل الطالب أحمد؟", "12345")
        self.assertEqual(res["source"], "privacy_policy_llm")
        self.assertEqual(len(self.llm_calls), 1)
        system = self._system_of_last_call()
        self.assertIn("سياسة خصوصية", system)
        self.assertNotIn("88.5", system)

    def test_unknown_student_cannot_get_a_profile(self):
        # No account for this id → nothing to expose → falls through to content.
        with patch("app.auth.find_account", return_value=None):
            res = self._chat("chat_as_student", "ما معدلي؟", "99999")
        self.assertEqual(res["source"], "student_context_rag_llm")
        self.assertNotIn("المعدل التراكمي:", self._system_of_last_call())


class TestUploadedChatFlows(ChatBase):

    def test_structured_admission_answer_skips_rag_but_reaches_llm(self):
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

        self.assertEqual(len(self.llm_calls), 1)
        self.assertIn("نتيجة قبول منظمة", self._system_of_last_call())
        self.assertEqual(res["source"], "structured_admission_llm")
        self.assertIn("65%", self._system_of_last_call())

    def test_palestine_capital_uses_trusted_fact_through_llm(self):
        res = self._chat("chat_with_all_files", "ما هي عاصمة فلسطين؟", "capital-sess")

        self.assertEqual(len(self.llm_calls), 1)
        self.assertIn("عاصمة فلسطين هي القدس", self._system_of_last_call())
        self.assertEqual(res["source"], "trusted_fact_llm")
        self.assertIn("عاصمة فلسطين هي القدس", self._system_of_last_call())

    def test_official_university_url_uses_trusted_fact_through_llm(self):
        res = self._chat("chat_with_all_files", "ما هو رابط موقع الجامعة؟", "url-sess")

        self.assertEqual(len(self.llm_calls), 1)
        self.assertIn("https://www.iugaza.edu.ps/", self._system_of_last_call())
        self.assertEqual(res["source"], "trusted_fact_llm")
        self.assertIn("https://www.iugaza.edu.ps/", self._system_of_last_call())

    def test_chat_with_file(self):
        res = self._chat("chat_with_file", "كم علامة الرياضيات؟", "ملف_علامات", "sess")
        self.assertEqual(res["source"], "uploaded_file_llm")
        self.assertTrue(res["top_chunks"])

    def test_chat_with_missing_file_still_reaches_llm(self):
        res = self._chat("chat_with_file", "سؤال", "غير_موجود", "sess")
        self.assertEqual(len(self.llm_calls), 1)
        self.assertIn("غير موجود", self._system_of_last_call())
        self.assertEqual(res["source"], "uploaded_file_llm")

    def test_chat_with_all_files(self):
        res = self._chat("chat_with_all_files", "كم علامة الفيزياء؟", "sess")
        self.assertEqual(res["source"], "uploaded_files_all_llm")
        self.assertTrue(res["top_chunks"])

    def test_reference_question_is_never_cached(self):
        # «اذكرهم» بلا سجل: جوابها عشوائي — تخزينها كان يقدّم العشوائية
        # نفسها لكل زائر لاحق (اكتشاف sol، الحل الرشيق بدل طبقة تصنيف كاملة).
        for sess in ("ref-a", "ref-b"):
            self._chat("chat_with_all_files", "اذكرهم", sess)
        self.assertEqual(len(self.llm_calls), 2)

    def test_plain_question_bypasses_final_answer_cache(self):
        for sess in ("plain-a", "plain-b"):
            self._chat("chat_with_all_files", "ما هي أهداف الجامعة التعليمية؟", sess)
        self.assertEqual(len(self.llm_calls), 2)
        self.assertTrue(config.LLM_ALWAYS_ANSWER)
        self.assertFalse(config.ANSWER_CACHE_ENABLED)

    def test_pure_reference_skips_raw_search(self):
        # «اذكرهم» بحثها الخام ضجيج — يُبحث بالاستعلام الموسّع بالسياق فقط.
        import time as _time
        searches = []

        def fake_search(q, top_k, threshold=None, allowed_collections=None):
            searches.append(q)
            return ["مقطع سياق"]

        history = [{"user": "ما هي كليات الجامعة؟",
                    "assistant": "11 كلية.", "at": _time.time()}]
        with patch.object(self.bot._uploaded, "search_all", side_effect=fake_search):
            self._chat("chat_with_all_files", "اذكرهم", "pureref-sess",
                       client_history=history)

        self.assertEqual(len(searches), 1)
        self.assertIn("كليات", searches[0])

    def test_topic_switch_after_history_still_searches_raw_question(self):
        # سلسلة السياق كانت تُغرق البحث بموضوع الدور السابق عند تغيير الموضوع
        # (ثبت حياً: «مين رئيس الجامعة؟» بعد سؤال رسوم → مقاطع رسوم فقط →
        # إنكار معلومة موجودة). البحث الخام يجري دائماً وتتقدم نتائجه.
        import time as _time
        searches = []

        def fake_search(q, top_k, threshold=None, allowed_collections=None):
            searches.append(q)
            if "الهندسة" in q:
                return ["مقطع رسوم الهندسة"]
            return ["مقطع رئيس الجامعة"]

        history = [{"user": "كم رسوم ساعة الهندسة؟",
                    "assistant": "28 ديناراً.", "at": _time.time()}]
        with patch.object(self.bot._uploaded, "search_all", side_effect=fake_search):
            res = self._chat(
                "chat_with_all_files", "من رئيس الجامعة؟", "switch-sess",
                client_history=history,
            )

        self.assertTrue(searches)
        self.assertTrue(all("الهندسه" not in normalize_arabic(q) for q in searches))
        self.assertEqual(
            res["retrieval_metadata"]["query_plan"]["context_mode"],
            "independent",
        )
        self.assertEqual(res["top_chunks"][0], "مقطع رئيس الجامعة")  # الخام يتقدم

    def test_context_candidate_survives_when_duplicate_is_deep_in_raw_results(self):
        # كان الدمج يعدّ المقطع «مكرراً» لمجرد ظهوره عميقاً في نتائج الخام،
        # ثم يقص الخام قبل موضعه؛ فيضيع من النافذتين (Q097/Q262/Q398).
        import time as _time

        target = "[ملف: المنح] المصدر الرسمي وتاريخ آخر تحقق"
        raw = [f"[ملف: عام] نتيجة خام {i}" for i in range(config.TOP_K - 1)]
        raw.append(target)  # أعمق من الحصة المحجوزة للخام
        contextual = [target] + [
            f"[ملف: المنح] نتيجة سياقية {i}" for i in range(config.TOP_K - 1)
        ]
        history = [{
            "user": "ما شروط منحة الامتياز؟",
            "assistant": "الشروط كذا.",
            "at": _time.time(),
        }]
        searches = []

        def fake_search(query, top_k, allowed_collections=None):
            searches.append(query)
            return list(contextual if "شروط منحة الامتياز" in query else raw)

        with patch.object(config, "CACHE_ENABLED", False), \
             patch.object(config, "RERANK_ENABLED", False), \
             patch.object(self.bot, "_search_all_for_question",
                          side_effect=fake_search):
            res = self._chat(
                "chat_with_all_files",
                "اذكر اسم المصدر وتاريخه إذا موجود",
                "context-reserve-sess",
                client_history=history,
            )

        self.assertIn(target, res["top_chunks"])
        # structured/source evidence may be prepended without displacing the
        # bounded retrieval window.
        self.assertLessEqual(
            len(res["top_chunks"]),
            config.TOP_K + res["retrieval_metadata"]["authoritative_evidence_count"],
        )
        self.assertIn("شروط منحه الامتياز", normalize_arabic(res["retrieval_metadata"]["search_query"]))
        # استرجاع سياقي أساسي، وقد يتبعه retry واحد محدود لاستكمال حقل المصدر/التاريخ.
        self.assertLessEqual(len(searches), 2)
        self.assertIn("شروط منحه الامتياز", normalize_arabic(searches[0]))

    def test_validator_regenerates_on_orphan_percentage(self):
        # جواب أول بنسبة يتيمة (80%) → الفاحص يرفضه ويعيد التوليد مرة
        # واحدة بتعليمة تصحيحية — الثاني النظيف يمر.
        answers = iter(["معدل قبول الطب 80%.", "قبول الطب تنافسي (91% في 2025)."])

        def fake_llm(headers, payload):
            self.llm_calls.append(payload)
            return next(answers)

        chunks = ["[ملف: رسوم] الطب تنافسي كان 91% لعام 2025"]
        with patch.object(config, "CHAT_API_KEY", "test-key"), \
             patch("app.embeddings.embed_texts", side_effect=fake_embed), \
             patch.object(self.bot._uploaded, "search_all", return_value=chunks), \
             patch("app.llm._post_with_retry", side_effect=fake_llm):
            res = self.bot.chat_with_all_files("كم معدل قبول الطب؟", "valid-sess")

        self.assertEqual(len(self.llm_calls), 2)          # توليد + إعادة واحدة
        self.assertIn("91", res["answer"])
        self.assertIn("تصحيح إلزامي", self.llm_calls[-1]["messages"][0]["content"])
        self.assertTrue(res["retrieval_metadata"]["answer_check_retry"])
        self.assertTrue(res["retrieval_metadata"]["answer_check_issues"])
        history = self.bot.get_history("valid-sess")
        self.assertEqual(len(history), 1)                 # لا تُحفظ المحاولة المرفوضة
        self.assertIn("91", history[0]["assistant"])
        self.assertNotIn("80", history[0]["assistant"])

    def test_failed_exact_answer_retry_uses_safe_llm_fallback(self):
        answers = iter([
            "رابط النموذج هو admission_application",
            "الرابط المباشر غير وارد في الأدلة المتاحة، لذلك لا يمكنني تأكيده.",
        ])

        def fake_llm(headers, payload):
            self.llm_calls.append(payload)
            return next(answers)

        chunks = ["[ملف: الدليل] لا يتوفر رابط النموذج المباشر."]
        with patch.object(config, "CHAT_API_KEY", "test-key"), \
             patch("app.embeddings.embed_texts", side_effect=fake_embed), \
             patch.object(self.bot._uploaded, "search_all", return_value=chunks), \
             patch("app.llm._post_with_retry", side_effect=fake_llm):
            res = self.bot.chat_with_all_files(
                "أعطني رابط النموذج نفسه", "exact-fallback-sess"
            )

        self.assertEqual(len(self.llm_calls), 2)
        self.assertNotIn("admission_application", res["answer"])
        self.assertIn("غير وارد", res["answer"])
        self.assertTrue(
            res["retrieval_metadata"]["answer_check_safety_fallback"]
        )
        self.assertFalse(
            res["retrieval_metadata"]["answer_check_post_retry_issues"]
        )
        self.assertEqual(res["retrieval_metadata"]["final_answer_origin"], "llm")
        self.assertEqual(res["retrieval_metadata"]["llm_generation_count"], 2)

    def test_source_metadata_gap_names_source_without_borrowing_date(self):
        import time as _time

        answers = iter([
            "المصدر: internal_scholarships، التاريخ: 2026-07-15.",
            "المصدر هو internal_scholarships، وتاريخ التحقق غير مذكور في المقطع.",
        ])

        def fake_llm(headers, payload):
            self.llm_calls.append(payload)
            return next(answers)

        chunks = [
            "[ملف: internal_scholarships]\n"
            "scholarship_name: منحة الامتياز\n"
            "conditions: معدل فصلي"
        ]
        history = [{
            "user": "ما شروط منحة الامتياز؟",
            "assistant": "الشروط...",
            "at": _time.time(),
        }]
        with patch.object(config, "CHAT_API_KEY", "test-key"), \
             patch.object(config, "CACHE_ENABLED", False), \
             patch("app.embeddings.embed_texts", side_effect=fake_embed), \
             patch.object(self.bot, "_search_all_for_question",
                          return_value=chunks), \
             patch("app.llm._post_with_retry", side_effect=fake_llm):
            res = self.bot.chat_with_all_files(
                "اذكر اسم المصدر وتاريخه إذا موجود",
                "source-metadata-gap-sess",
                client_history=history,
            )

        self.assertEqual(len(self.llm_calls), 2)
        self.assertIn("internal_scholarships", res["answer"])
        self.assertIn("تاريخ التحقق غير مذكور", res["answer"])
        self.assertNotIn("2026-07-15", res["answer"])
        self.assertTrue(
            res["retrieval_metadata"]["answer_check_safety_fallback"]
        )
        self.assertEqual(res["retrieval_metadata"]["final_answer_origin"], "llm")
        self.assertTrue(
            res["retrieval_metadata"]["source_metadata_extracted"]
        )

    def test_complete_list_expands_context_without_calling_reranker(self):
        chunks = [f"[ملف: الكليات] كلية {i}" for i in range(config.COVERAGE_TOP_K)]
        with patch.object(config, "CACHE_ENABLED", False), \
             patch.object(config, "RERANK_ENABLED", True), \
             patch.object(self.bot, "_search_all_for_question",
                          return_value=chunks) as search, \
             patch("app.chatbot.rerank_mod.rerank_with_status") as rerank_call:
            res = self._chat(
                "chat_with_all_files",
                "اذكر جميع كليات الجامعة",
                "coverage-sess",
            )

        self.assertEqual(search.call_args.args[1], config.COVERAGE_TOP_K)
        rerank_call.assert_not_called()
        self.assertEqual(len(res["top_chunks"]), config.COVERAGE_TOP_K)
        self.assertTrue(res["retrieval_metadata"]["coverage_requested"])
        self.assertIn("يجوز الإطالة بقدر إكمال قائمة شاملة", self._system_of_last_call())

    def test_faculty_deans_use_only_exact_role_records(self):
        candidates = [
            "degree_or_request: عميد كلية الهندسة | full_name: أ",
            "degree_or_request: عميد كلية الطب | full_name: ب",
            "degree_or_request: عميد كلية العلوم | full_name: ج",
            "degree_or_request: نائب عميد كلية الهندسة | full_name: د",
            "degree_or_request: عميد شؤون الطلبة | full_name: هـ",
        ]
        extra = (
            "degree_or_request: عميد كلية تكنولوجيا المعلومات | full_name: و"
        )
        self.bot._uploaded._chunks["دليل الأشخاص"] = candidates + [extra]
        with patch.object(config, "CACHE_ENABLED", False), \
             patch.object(config, "RERANK_ENABLED", True), \
             patch.object(self.bot, "_search_all_for_question",
                          return_value=candidates), \
             patch("app.chatbot.rerank_mod.rerank_with_status") as rerank_call:
            res = self._chat(
                "chat_with_all_files",
                "اذكر جميع عمداء الكليات",
                "deans-role-focus-sess",
            )

        rerank_call.assert_not_called()
        self.assertTrue(
            res["retrieval_metadata"]["exact_role_focus_applied"]
        )
        self.assertEqual(len(res["top_chunks"]), 4)
        self.assertIn(extra, res["top_chunks"])
        self.assertTrue(all(
            "degree_or_request: عميد كلية" in chunk
            for chunk in res["top_chunks"]
        ))
        self.assertIn(
            "لا تضف نائباً أو عمادة إدارية",
            self._system_of_last_call(),
        )

    def test_admission_prompt_anchors_latest_branch_after_old_answers(self):
        import time as _time

        history = [
            {
                "user": "معدلي 85% علمي، شو الخيارات؟",
                "assistant": "الهندسة والعلوم.",
                "at": _time.time(),
            },
            {
                "user": "لو كان فرعي أدبي بتتغير القائمة؟",
                "assistant": "نعم.",
                "at": _time.time(),
            },
        ]
        with patch.object(config, "CACHE_ENABLED", False), \
             patch.object(self.bot, "_search_all_for_question",
                          return_value=["[ملف: قبول] برامج البكالوريوس"]):
            self._chat(
                "chat_with_all_files",
                "رتبهم حسب الكلية",
                "latest-branch-anchor-sess",
                client_history=history,
            )

        user_message = self.llm_calls[-1]["messages"][-1]["content"]
        self.assertIn("الفرع الحالي: أدبي", user_message)
        self.assertIn("أي فرع أو معدل أقدم", user_message)
        self.assertNotIn("الهندسة والعلوم.", user_message)
        self.assertIn("سياق المستخدم السابق", user_message)
        self.assertNotIn("المساعد:", user_message)

    def test_contextual_exact_lookup_reranks_with_expanded_query(self):
        import time as _time

        candidates = [
            f"[ملف: البوابة] بوابة التعليم الإلكتروني رابط الدخول "
            f"https://elearning.example/{i}"
            for i in range(config.RERANK_CANDIDATES)
        ]
        history = [{
            "user": "ما هي بوابة التعليم الإلكتروني؟",
            "assistant": "هي خدمة للطلاب.",
            "at": _time.time(),
        }]
        with patch.object(config, "CACHE_ENABLED", False), \
             patch.object(config, "RERANK_ENABLED", True), \
             patch.object(self.bot, "_search_all_for_question",
                          return_value=candidates), \
             patch("app.chatbot.rerank_mod.rerank_with_status",
                   side_effect=lambda query, chunks, top_n:
                   (chunks[:top_n], "applied")) as rerank_call:
            res = self._chat(
                "chat_with_all_files",
                "وما رابطها؟",
                "rerank-context-sess",
                client_history=history,
            )

        rerank_call.assert_called_once()
        query = rerank_call.call_args.args[0]
        from app.text_norm import normalize_arabic
        self.assertIn(normalize_arabic("بوابة التعليم الإلكتروني"), normalize_arabic(query))
        self.assertIn(normalize_arabic("رابطها"), normalize_arabic(query))
        self.assertEqual(len(res["top_chunks"]), config.TOP_K)
        self.assertTrue(res["retrieval_metadata"]["rerank_attempted"])

    def test_inherited_complete_list_uses_coverage_not_reranker(self):
        import time as _time

        candidates = [
            f"[ملف: deans] عميد كلية {i}"
            for i in range(config.COVERAGE_TOP_K)
        ]
        history = [
            {"user": "اعطيني أسماء كليات الجامعة",
             "assistant": "الكليات...", "at": _time.time()},
            {"user": "اذكرهم", "assistant": "الطب، الهندسة...",
             "at": _time.time()},
        ]
        with patch.object(config, "CACHE_ENABLED", False), \
             patch.object(config, "RERANK_ENABLED", True), \
             patch.object(self.bot, "_search_all_for_question",
                          return_value=candidates) as search, \
             patch("app.chatbot.rerank_mod.rerank_with_status") as rerank_call:
            res = self._chat(
                "chat_with_all_files",
                "أنا أقصد عمداءهم، مش الكليات نفسها",
                "inherited-list-sess",
                client_history=history,
            )

        rerank_call.assert_not_called()
        self.assertEqual(search.call_args.args[1], config.COVERAGE_TOP_K)
        self.assertTrue(res["retrieval_metadata"]["coverage_requested"])
        self.assertIn("قائمة شاملة", self._system_of_last_call())

    def test_reranker_guard_skips_when_candidates_do_not_support_query(self):
        candidates = [
            f"[ملف: عام] معلومات الرسوم والأنشطة {i}"
            for i in range(config.RERANK_CANDIDATES)
        ]
        with patch.object(config, "CACHE_ENABLED", False), \
             patch.object(config, "RERANK_ENABLED", True), \
             patch.object(self.bot, "_search_all_for_question",
                          return_value=candidates), \
             patch("app.chatbot.rerank_mod.rerank_with_status") as rerank_call:
            res = self._chat(
                "chat_with_all_files",
                "ما رابط بوابة الطالب؟",
                "rerank-guard-sess",
            )

        rerank_call.assert_not_called()
        self.assertEqual(len(res["top_chunks"]), config.TOP_K)
        self.assertFalse(res["retrieval_metadata"]["rerank_guard_passed"])

    def test_routine_question_keeps_baseline_top_k_without_reranker(self):
        chunks = [f"[ملف: عام] شروط الانسحاب {i}" for i in range(config.TOP_K)]
        with patch.object(config, "CACHE_ENABLED", False), \
             patch.object(config, "RERANK_ENABLED", True), \
             patch.object(self.bot, "_search_all_for_question",
                          return_value=chunks) as search, \
             patch("app.chatbot.rerank_mod.rerank_with_status") as rerank_call:
            res = self._chat(
                "chat_with_all_files",
                "ما شروط الانسحاب من المساق؟",
                "routine-sess",
            )

        self.assertEqual(search.call_args.args[1], config.TOP_K)
        rerank_call.assert_not_called()
        self.assertFalse(res["retrieval_metadata"]["rerank_requested"])

    def test_programs_question_drops_graduate_chunks(self):
        # «ما برامج كلية العلوم؟» بلا مرحلة كان يُجاب بماجستير (ثبت في الـ90):
        # سؤال البرامج/التخصصات افتراضه البكالوريوس، فتُسقط مقاطع الدراسات
        # العليا صراحةً لا تُؤخَّر فقط (Q097: الإعادة وحدها لم تكفِ).
        mixed = [
            "[ملف: تخصصات الماجستير] العلوم الحياتية ماجستير",
            "[ملف: تخصصات البكالوريوس لكل كلية] الكيمياء والفيزياء بكالوريوس",
            "[ملف: نشرة كلية العلوم] معلومات عامة",
            "[ملف: تخصصات الدكتوراه] الرياضيات دكتوراه",
        ]
        with patch.object(self.bot._uploaded, "search_all", return_value=list(mixed)):
            res = self._chat("chat_with_all_files", "ما برامج كلية العلوم؟", "lvl-sess")
        self.assertTrue(all("ماجستير" not in c and "دكتوراه" not in c
                            for c in res["top_chunks"]))
        self.assertIn("مرحلة البكالوريوس", self._system_of_last_call())

    def test_bachelor_default_demotes_when_not_programs_question(self):
        # سؤال عام بلا نية برامج: إعادة ترتيب فقط (لا إسقاط) — البكالوريوس أولاً.
        mixed = [
            "[ملف: تخصصات الماجستير] رسوم ماجستير",
            "[ملف: رسوم البكالوريوس ومعدلات القبول] رسوم بكالوريوس",
        ]
        with patch.object(self.bot._uploaded, "search_all", return_value=list(mixed)):
            res = self._chat("chat_with_all_files", "كم تكلفة الدراسة عندكم؟", "lvl0-sess")
        self.assertIn("بكالوريوس", res["top_chunks"][0])
        self.assertEqual(len(res["top_chunks"]), 2)  # لم يُسقط شيء
        self.assertIn("لم يحدد السائل مرحلة أكاديمية", self._system_of_last_call())

    def test_explicit_masters_drops_other_levels(self):
        mixed = [
            "[ملف: رسوم البكالوريوس ومعدلات القبول] الكيمياء بكالوريوس",
            "[ملف: تخصصات الماجستير] العلوم الحياتية ماجستير",
            "[ملف: تخصصات الماجستير] الرياضيات ماجستير",
            "[ملف: تخصصات الماجستير] الفيزياء ماجستير",
            "[ملف: تخصصات الدكتوراه] الرياضيات دكتوراه",
        ]
        with patch.object(self.bot._uploaded, "search_all", return_value=list(mixed)):
            res = self._chat("chat_with_all_files", "شو برامج الماجستير في العلوم؟", "lvl2-sess")
        self.assertTrue(all("بكالوريوس" not in c and "دكتوراه" not in c
                            for c in res["top_chunks"]))
        self.assertIn("مرحلة الماجستير", self._system_of_last_call())

    def test_exclusion_drops_marked_files_and_instructs(self):
        mixed = [
            "[ملف: internal_scholarships] منحة الامتياز 70%",
            "[ملف: تخصصات البكالوريوس لكل كلية] الهندسة والعلوم",
        ]
        with patch.object(self.bot._uploaded, "search_all", return_value=list(mixed)):
            res = self._chat("chat_with_all_files",
                             "أنا بسأل عن تخصصات، مش منح؛ اعطيني خيارات أكاديمية.",
                             "excl-sess")
        self.assertTrue(all("internal_scholarships" not in c for c in res["top_chunks"]))
        self.assertIn("استبعد صراحة", self._system_of_last_call().replace("ً", ""))

    def test_context_budget_trims_chunks(self):
        big = [f"[ملف: عام] مقطع {i} " + "س" * 500 for i in range(10)]
        with patch.object(self.bot._uploaded, "search_all", return_value=big), \
             patch.object(config, "MAX_CONTEXT_CHARS", 1200):
            res = self._chat("chat_with_all_files", "سؤال عام للتجربة؟", "budget-sess")
        self.assertLessEqual(len(res["top_chunks"]), 3)
        self.assertGreaterEqual(len(res["top_chunks"]), 1)

    def test_admission_question_gets_full_cutoff_table(self):
        # «من يقبلني بمعدلي؟» تجميعي: top-K التشابهي كان يعيد نسخ برامج كلية
        # واحدة ويُسقط بقية الكليات — يجب أن يصل جدول المفاتيح كاملاً للموديل.
        name = "رسوم البكالوريوس ومعدلات القبول"
        table = [
            f"[ملف: {name}] faculty_name: كلية-{i} min_rate: {60 + i}%"
            for i in range(config.TOP_K + 5)  # أكبر من top-K عمداً
        ]
        self.bot._uploaded._chunks[name] = list(table)

        res = self._chat(
            "chat_with_all_files",
            "ما هي التخصصات التي يمكن أن تقبلني إذا كان معدلي 81؟",
            "admission-sess",
        )

        for chunk in table:
            self.assertIn(chunk, res["top_chunks"])
        self.assertIn("جدول مفاتيح القبول متوفر أعلاه كاملاً", self._system_of_last_call())

    def test_admission_digest_lines_prepended_when_catalog_has_facts(self):
        # المفاتيح الرقمية تصل كسطور مستخلصة لا لبس فيها (بلا رسوم) — المقاطع
        # الخام خلطت سعر الساعة (20 ديناراً) بالمفتاح (65%) في القراءة الحية.
        name = "رسوم البكالوريوس ومعدلات القبول"
        self.bot._uploaded._chunks[name] = [
            f"[ملف: {name}] faculty_name: الآداب program_name: اللغة الإنجليزية "
            "credit_hour_fee: 20 admission_criteria: {'min_high_school_percentage': 65}"
        ]
        self.bot._uploaded._admissions.replace_collection(name, [{
            "faculty_name": "الآداب", "degree": "بكالوريوس",
            "program_name": "اللغة الإنجليزية", "credit_hour_fee": 20,
            "admission_criteria": {
                "min_high_school_percentage": 65,
                "allowed_high_school_branches": ["علمي", "أدبي"],
            },
        }], rebuild=False)
        self.bot._uploaded._admissions.rebuild(lambda fact: None)

        res = self._chat(
            "chat_with_all_files",
            "ما هي التخصصات التي يمكن أن تقبلني إذا كان معدلي 81؟",
            "admission-digest-sess",
        )

        digest = "\n".join(res["top_chunks"])
        self.assertIn("اللغة الإنجليزية", digest)
        self.assertIn("65%", digest)
        projection = next(
            chunk for chunk in res["top_chunks"]
            if "إسقاط حقلي" in chunk
        )
        self.assertNotIn("credit_hour_fee", projection)
        self.assertIn("المرجع\n  الحصري للمفاتيح الرقمية", self._system_of_last_call())

    def test_admission_digest_filters_by_active_branch_and_rate(self):
        name = "رسوم البكالوريوس ومعدلات القبول"
        self.bot._uploaded._admissions.replace_collection(name, [
            {
                "faculty_name": "كلية الآداب",
                "degree": "بكالوريوس",
                "program_name": "اللغة العربية",
                "admission_criteria": {
                    "min_high_school_percentage": 65,
                    "allowed_high_school_branches": ["علمي", "أدبي"],
                },
            },
            {
                "faculty_name": "كلية الاقتصاد",
                "degree": "بكالوريوس",
                "program_name": "المحاسبة",
                "admission_criteria": {
                    "min_high_school_percentage": 70,
                    "allowed_high_school_branches": ["أدبي"],
                },
            },
            {
                "faculty_name": "كلية الهندسة",
                "degree": "بكالوريوس",
                "program_name": "الهندسة المدنية",
                "admission_criteria": {
                    "min_high_school_percentage": 80,
                    "allowed_high_school_branches": ["علمي"],
                },
            },
            {
                "faculty_name": "كلية الطب",
                "degree": "بكالوريوس",
                "program_name": "الطب",
                "admission_criteria": {
                    "min_high_school_percentage": 90,
                    "allowed_high_school_branches": ["أدبي"],
                },
            },
        ], rebuild=False)
        self.bot._uploaded._admissions.rebuild(lambda fact: None)

        lines = self.bot._uploaded.admission_context_lines(
            branch="أدبي",
            max_percentage=85,
        )
        text = "\n".join(lines)
        self.assertIn("كلية الآداب", text)
        self.assertIn("كلية الاقتصاد", text)
        self.assertNotIn("كلية الهندسة", text)
        self.assertNotIn("كلية الطب", text)

    def test_oversized_cutoff_table_falls_back_to_bounded_search(self):
        name = "معدلات القبول الضخمة"
        self.bot._uploaded._chunks[name] = [f"مفتاح {i}" for i in range(30)]

        with patch.object(config, "ADMISSION_TABLE_MAX_CHUNKS", 3):
            res = self._chat(
                "chat_with_all_files",
                "ما هي التخصصات التي يمكن أن تقبلني إذا كان معدلي 81؟",
                "admission-big-sess",
            )

        from_table = [c for c in res["top_chunks"] if c.startswith("مفتاح ")]
        self.assertLessEqual(len(from_table), 3)
        self.assertNotIn("جدول مفاتيح القبول متوفر أعلاه كاملاً", self._system_of_last_call())

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

        self.assertEqual(res["source"], "uploaded_files_all_llm")
        self.assertEqual(len(self.llm_calls), 1)
        system = self._system_of_last_call()
        self.assertIn("المعرفة العامة الموثوقة", system)


if __name__ == "__main__":
    unittest.main()
