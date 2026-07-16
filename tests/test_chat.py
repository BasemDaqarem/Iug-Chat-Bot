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

    def test_own_record_reaches_llm_with_private_profile_context(self):
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}):
            res = self._chat("chat_as_student", "ما هي حالتي الأكاديمية؟", "12345")
        self.assertEqual(len(self.llm_calls), 1)
        self.assertEqual(res["source"], "student_context_rag")
        system = self._system_of_last_call()
        self.assertIn("بيانات الطالب الحالي", system)
        self.assertIn("88.5", system)
        self.assertIn("هندسة حاسوب", system)

    def test_my_gpa_question_uses_llm_with_profile_context(self):
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}):
            res = self._chat("chat_as_student", "كم معدلي؟", "12345")
        self.assertEqual(len(self.llm_calls), 1)
        self.assertEqual(res["source"], "student_context_rag")
        self.assertIn("88.5", self._system_of_last_call())

    def test_my_name_question_uses_llm_with_profile_context(self):
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}):
            res = self._chat("chat_as_student", "ما هو اسمي؟", "12345")
        self.assertEqual(len(self.llm_calls), 1)
        self.assertEqual(res["source"], "student_context_rag")
        self.assertIn("محمد أحمد", self._system_of_last_call())

    def test_scholarship_question_with_gpa_processes_the_full_question(self):
        scholarship_chunk = "منحة التفوق: يمكن للطالب التقدم وفق شروط المنح المنشورة."
        question = "معدلي 90 ايش في منح متاحة الي"
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}), \
             patch.object(self.bot, "_search_all_for_question",
                          return_value=[scholarship_chunk]) as search:
            res = self._chat("chat_as_student", question, "12345")

        search.assert_called_once_with(question, config.TOP_K, None)
        self.assertEqual(len(self.llm_calls), 1)
        self.assertEqual(res["source"], "student_context_rag")
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
        self.assertIn("هندسة حاسوب", searched)
        self.assertIn("رئيس قسمي", searched)
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
        from app.sessions import MEMORY_INSTRUCTION

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
        self.assertIn(MEMORY_INSTRUCTION, user_msg)
        self.assertIn("سؤال التأجيل القديم", user_msg)   # ذو صلة (cos=1)
        self.assertNotIn("سؤال المنح البعيد", user_msg)  # متعامد → مستبعد
        self.assertIn("أحدث سؤال", user_msg)             # الأحدث دائماً

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
             patch.object(self.bot, "_search_all_for_question", return_value=[]), \
             patch("app.chatbot.stream_completion",
                   side_effect=lambda s, u: iter(["الرسو", "م ", "10 دنانير"])):
            chunks = list(self.bot.stream_answer(
                "كم رسوم التأجيل؟", principal, allowed_collections=None))

        self.assertEqual("".join(chunks), "الرسوم 10 دنانير")
        # the streamed answer is persisted as one history turn
        self.assertEqual(self.bot.get_history("12345")[-1]["assistant"], "الرسوم 10 دنانير")

    def test_stream_answer_blocks_question_about_other_student(self):
        from app.rbac import Principal, Role
        principal = Principal("12345", Role.STUDENT)
        chunks = list(self.bot.stream_answer(
            "كم معدل الطالب 67890؟", principal, allowed_collections=None))
        self.assertEqual(len(self.llm_calls), 0)          # never reached the LLM
        self.assertIn("خاصة", "".join(chunks))

    def test_memory_embedding_failure_falls_back_and_still_answers(self):
        """فشل توليد المتجه لا يكسر الشات: يعود لطيّ آخر الأدوار كما قبل."""
        self.bot.push_history("12345", "سؤال سابق", "جواب سابق")
        with patch("app.auth.find_account",
                   return_value={"student_id": "12345", "profile": self.PROFILE}), \
             patch.object(self.bot, "_search_all_for_question", return_value=[]), \
             patch("app.embeddings.embed_query", side_effect=RuntimeError("jina down")):
            res = self._chat("chat_as_student", "كم رسوم الساعة؟", "12345")

        self.assertEqual(res["answer"], "إجابة تجريبية من النموذج.")  # الشات يعمل
        user_msg = self.llm_calls[-1]["messages"][1]["content"]
        self.assertIn("سؤال سابق", user_msg)             # fallback يشمل السجل
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
        self.assertEqual(res["source"], "student_context_rag")

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
        self.assertEqual(res["source"], "student_context_rag")
        self.assertNotIn("المعدل التراكمي:", self._system_of_last_call())


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

        self.assertTrue(any("الهندسة" in q for q in searches))   # بحث السياق جرى
        self.assertTrue(any("الهندسة" not in q for q in searches))  # والخام أيضاً
        self.assertEqual(res["top_chunks"][0], "مقطع رئيس الجامعة")  # الخام يتقدم

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

        digest = res["top_chunks"][0]
        self.assertIn("جدول مفاتيح القبول (مستخلص آلياً", digest)
        self.assertIn("اللغة الإنجليزية", digest)
        self.assertIn("65%", digest)
        self.assertNotIn("20", digest.split("\n", 1)[1])  # لا رسوم داخل السطور
        self.assertIn("المرجع\n  الحصري للمفاتيح الرقمية", self._system_of_last_call())

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

        self.assertEqual(res["source"], "uploaded_files_all")
        self.assertEqual(len(self.llm_calls), 1)
        system = self._system_of_last_call()
        self.assertIn("معرفتك العامة الموثوقة", system)
        self.assertIn("عاصمة فلسطين هي القدس", system)


if __name__ == "__main__":
    unittest.main()
