import unittest

from app import privacy

META = [
    {"sensitive": True, "owner_id": "123", "display_name": "محمد أحمد خالد",
     "raw": {"student_id": "123", "student_name": "محمد أحمد خالد", "gpa": 88.5,
             "rank": 3, "privacy": {"allowed_users": ["123"]}}},
    {"sensitive": True, "owner_id": "456", "display_name": "سالم يوسف",
     "raw": {"student_id": "456", "gpa": 70, "rank": 40}},
    {"sensitive": False, "owner_id": None, "display_name": "برنامج الهندسة", "raw": {}},
]


class TestQuestionClassifiers(unittest.TestCase):

    def test_academic_status_detection(self):
        self.assertTrue(privacy.is_academic_status_question("ما هي حالتي الأكاديمية؟"))
        self.assertTrue(privacy.is_academic_status_question("هل أنا في خطر؟"))
        self.assertFalse(privacy.is_academic_status_question("متى يبدأ الفصل؟"))

    def test_wants_own_record(self):
        for q in ("ما هي حالتي الأكاديمية؟", "كم معدلي؟", "ما ترتيبي على الدفعة؟", "ما تخصصي؟"):
            self.assertTrue(privacy.wants_own_academic_record(q), q)
        # public/other questions must NOT be treated as a personal-record query
        for q in ("ما معدل القبول في الهندسة؟", "كم رسوم الطب؟", "ما معدل سالم؟"):
            self.assertFalse(privacy.wants_own_academic_record(q), q)

    def test_build_status_from_profile(self):
        out = privacy.build_status_from_profile(
            {"name": "محمد", "major": "هندسة حاسوب", "gpa": 88.5, "rank": 3,
             "academic_status": "regular"})
        self.assertIn("88.5", out)
        self.assertIn("3", out)
        self.assertIn("هندسة حاسوب", out)
        self.assertIn("منتظم", out)

    def test_build_status_flags_at_risk(self):
        out = privacy.build_status_from_profile({"gpa": 60, "rank": 200, "academic_status": "at_risk"})
        self.assertIn("خطر", out)
        self.assertIn("مرشدك", out)  # advice line for at-risk students

    def test_asks_about_other_student_third_person(self):
        for q in ("ما معدله؟", "كم ترتيبها على الدفعة؟", "ما معدل الطالب أحمد؟", "ترتيب زميلي"):
            self.assertTrue(privacy.asks_about_other_student(q), q)

    def test_asks_about_other_student_by_foreign_id(self):
        # a ranking question naming a DIFFERENT student id → about someone else
        self.assertTrue(privacy.asks_about_other_student("كم معدل الطالب 987654؟", own_student_id="12345"))
        # the caller's OWN id is fine
        self.assertFalse(privacy.asks_about_other_student("ما معدلي 12345؟", own_student_id="12345"))

    def test_own_and_public_questions_not_flagged_as_other(self):
        for q in ("ما هي حالتي الأكاديمية؟", "كم معدلي؟", "ما معدل القبول في الهندسة؟", "كم رسوم الطب؟"):
            self.assertFalse(privacy.asks_about_other_student(q, own_student_id="12345"), q)

    def test_ranking_detection(self):
        self.assertTrue(privacy.is_ranking_question("كم معدلي التراكمي؟"))
        self.assertTrue(privacy.is_ranking_question("what is my gpa"))
        self.assertFalse(privacy.is_ranking_question("متى الامتحانات؟"))


class TestSensitiveLookups(unittest.TestCase):

    def test_find_own_record(self):
        rec = privacy.find_sensitive_record(META, "123")
        self.assertEqual(rec["display_name"], "محمد أحمد خالد")
        self.assertIsNone(privacy.find_sensitive_record(META, "999"))

    def test_session_id_coerced_to_str(self):
        self.assertIsNotNone(privacy.find_sensitive_record(META, 123))

    def test_other_names_excludes_self_and_non_sensitive(self):
        names = privacy.other_sensitive_display_names(META, "123")
        self.assertEqual(names, ["سالم يوسف"])

    def test_mentions_other_student_first_token(self):
        self.assertTrue(privacy.mentions_other_student("كم معدل سالم؟", ["سالم يوسف"]))
        self.assertFalse(privacy.mentions_other_student("كم معدلي أنا؟", ["سالم يوسف"]))
        self.assertFalse(privacy.mentions_other_student("كم معدل سالم؟", []))


class TestFormatting(unittest.TestCase):

    def test_context_strips_privacy_field(self):
        text = privacy.format_sensitive_record_context(META[0])
        self.assertTrue(text.startswith("بيانات الطالب الحالي (سري — للطالب نفسه فقط):"))
        self.assertIn("gpa: 88.5", text)
        self.assertNotIn("privacy", text)

    def test_status_answer(self):
        self.assertEqual(
            privacy.build_status_from_sensitive_record({"gpa": 88.5, "rank": 3}),
            "حالتك الأكاديمية الحالية: المعدل التراكمي 88.5، والترتيب على الدفعة 3.",
        )

    def test_status_answer_defaults(self):
        self.assertEqual(
            privacy.build_status_from_sensitive_record({}),
            "حالتك الأكاديمية الحالية: المعدل التراكمي غير متوفر، والترتيب على الدفعة غير متوفر.",
        )


if __name__ == "__main__":
    unittest.main()
