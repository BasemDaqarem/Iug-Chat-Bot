"""query_rewrite — retrieval-query expansion is pure string logic, so every
behavior is unit-testable without any network or Mongo."""

from app import query_rewrite as qr


class TestExpandSelfReferences:
    def test_department_appended_for_qismi(self):
        out = qr.expand_self_references("كيف اتواصل مع رئيس قسمي", "هندسة الحاسوب")
        assert "هندسة الحاسوب" in out
        assert out.startswith("كيف اتواصل مع رئيس قسمي")  # original wording kept

    def test_takhassusi_and_kulliyati_trigger(self):
        assert "الطب" in qr.expand_self_references("ما مساقات تخصصي؟", "الطب")
        assert "التمريض" in qr.expand_self_references("رسوم كليتي", "التمريض")

    def test_no_profile_major_is_noop(self):
        q = "كيف اتواصل مع رئيس قسمي"
        assert qr.expand_self_references(q, None) == q
        assert qr.expand_self_references(q, "") == q

    def test_question_without_self_reference_is_noop(self):
        q = "ما معدل القبول في كلية الهندسة؟"
        assert qr.expand_self_references(q, "هندسة الحاسوب") == q

    def test_major_already_present_not_duplicated(self):
        q = "رئيس قسمي في هندسة الحاسوب"
        assert qr.expand_self_references(q, "هندسة الحاسوب") == q


class TestPersonalizeImplicitTopics:
    def test_field_training_gets_student_major(self):
        out = qr.personalize_implicit_topics("كيف ممكن انجز التدريب الميداني", "هندسة الحاسوب")
        assert "هندسة الحاسوب" in out

    def test_graduation_project_gets_student_major(self):
        out = qr.personalize_implicit_topics("ما متطلبات مشروع التخرج؟", "هندسة الحاسوب")
        assert "هندسة الحاسوب" in out

    def test_question_naming_another_faculty_untouched(self):
        q = "كيف التدريب الميداني في كلية الطب؟"
        assert qr.personalize_implicit_topics(q, "هندسة الحاسوب") == q

    def test_my_faculty_form_still_personalized(self):
        out = qr.personalize_implicit_topics("التدريب الخاص بقسمي", "هندسة الحاسوب")
        assert "هندسة الحاسوب" in out

    def test_unrelated_topic_untouched(self):
        q = "ما رسوم ساعة الماجستير؟"
        assert qr.personalize_implicit_topics(q, "هندسة الحاسوب") == q

    def test_no_major_untouched(self):
        q = "كيف ممكن انجز التدريب الميداني"
        assert qr.personalize_implicit_topics(q, None) == q

    def test_personalize_query_composes_without_duplication(self):
        # «قسمي» triggers expand_self_references first; the implicit-topic pass
        # must NOT append the major a second time.
        out = qr.personalize_query("التدريب الميداني الخاص بقسمي", "هندسة الحاسوب")
        assert out.count("هندسة الحاسوب") == 1


class TestNeedsHistoryContext:
    def test_demonstratives_trigger(self):
        assert qr.needs_history_context("كم هيكلفني رسوم هذا الطلب")
        assert qr.needs_history_context("وما شروط ذلك البرنامج؟ وهل يحتاج مستندات اضافية")

    def test_repair_phrases_trigger(self):
        assert qr.needs_history_context("اقصد رسوم التأجيل عند التقديم للتسجيل")

    def test_short_question_triggers(self):
        assert qr.needs_history_context("كم رسومه؟")

    def test_long_standalone_question_does_not_trigger(self):
        assert not qr.needs_history_context(
            "ما هي شروط القبول في كلية الطب للفرع العلمي للعام الحالي؟"
        )


class TestWithHistoryContext:
    HISTORY = [
        {"user": "ما معدل القبول في الهندسة؟", "assistant": "80%"},
        {"user": "كيف بدي اجل الفصل الحالي", "assistant": "تقدم بطلب لمكتب التسجيل."},
    ]

    def test_follow_up_inherits_last_user_turn(self):
        out = qr.with_history_context("كم هيكلفني رسوم هذا الطلب", self.HISTORY)
        assert "اجل الفصل" in out           # previous topic present for BM25
        assert "رسوم هذا الطلب" in out      # current question still present

    def test_only_last_turn_used_not_older_ones(self):
        out = qr.with_history_context("كم هيكلفني رسوم هذا الطلب", self.HISTORY)
        assert "معدل القبول" not in out

    def test_standalone_question_untouched(self):
        q = "ما هي شروط القبول في كلية الطب للفرع العلمي للعام الحالي؟"
        assert qr.with_history_context(q, self.HISTORY) == q

    def test_no_history_untouched(self):
        q = "كم رسومه؟"
        assert qr.with_history_context(q, []) == q

    def test_empty_last_user_turn_untouched(self):
        q = "كم رسومه؟"
        assert qr.with_history_context(q, [{"user": "", "assistant": "x"}]) == q
