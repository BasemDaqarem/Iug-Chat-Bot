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

    def test_bare_definite_alqism_means_my_department(self):
        """صيغة باسم الحرفية التي فشلت: «رئيس القسم» بأل التعريف = قسمه هو."""
        out = qr.expand_self_references(
            "كيف ممكن اتواصل مع رئيس القسم؟", "هندسة الحاسوب"
        )
        assert "هندسة الحاسوب" in out

    def test_named_faculty_does_not_trigger_bare_forms(self):
        # «قسم/كلية + اسم» يُقطَّع بلا أل التعريف على العلامة → لا توسيع
        q = "كم سعر ساعة كلية التمريض؟"
        assert qr.expand_self_references(q, "هندسة الحاسوب") == q
        q2 = "مين رئيس قسم التمريض؟"
        assert qr.expand_self_references(q2, "هندسة الحاسوب") == q2

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


class TestAddCanonicalTerms:
    def test_colloquial_deferral_verb_gains_canonical_noun(self):
        out = qr.add_canonical_terms("كيف اجل الفصل ظ")
        assert "تأجيل الدراسة" in out
        assert out.startswith("كيف اجل الفصل ظ")  # الأصل محفوظ

    def test_hamza_variant_matches_after_normalization(self):
        assert "تأجيل الدراسة" in qr.add_canonical_terms("بدي أجل الفصل")

    def test_registration_verb(self):
        assert "تسجيل المساقات" in qr.add_canonical_terms("كيف اسجل المواد؟")

    def test_already_canonical_untouched(self):
        q = "كم رسوم تأجيل الدراسة؟"
        assert qr.add_canonical_terms(q) == q

    def test_unrelated_question_untouched(self):
        q = "كم سعر ساعة كلية الطب؟"
        assert qr.add_canonical_terms(q) == q

    def test_admission_intent_verb_gains_cutoff_terms(self):
        """سيناريو الزائر الحرفي: «ما هي التخصصات التي يمكن ان تقبلني اذا معدلي 85%»."""
        out = qr.add_canonical_terms("ما هي التخصصات التي يمكن ان تقبلني اذا معدلي 85%")
        assert "معدلات القبول" in out

    def test_admission_intent_pair_without_verb(self):
        out = qr.add_canonical_terms("شو التخصصات المتاحة لمعدلي 85؟")
        assert "معدلات القبول" in out

    def test_gpa_question_without_major_context_untouched(self):
        q = "كم معدلي التراكمي؟"
        assert qr.add_canonical_terms(q) == q

    def test_explicit_admission_rates_question_not_duplicated(self):
        q = "ما هي معدلات القبول في التخصصات؟"
        assert qr.add_canonical_terms(q) == q

    def test_composes_with_history_chain(self):
        """سيناريو باسم الحرفي: «كيف اجل الفصل ظ» ثم «كم هيكلف ؟»."""
        history = [{"user": "كيف اجل الفصل ظ", "assistant": "..."}]
        combined = qr.with_history_context("كم هيكلف ؟", history)
        out = qr.add_canonical_terms(combined)
        assert "تأجيل الدراسة" in out   # المصطلح القانوني وصل للبحث
        assert "كم هيكلف" in out


class TestNeedsHistoryContext:
    def test_demonstratives_trigger(self):
        assert qr.needs_history_context("كم هيكلفني رسوم هذا الطلب")
        assert qr.needs_history_context("وما شروط ذلك البرنامج؟ وهل يحتاج مستندات اضافية")

    def test_repair_phrases_trigger(self):
        assert qr.needs_history_context("اقصد رسوم التأجيل عند التقديم للتسجيل")

    def test_short_question_triggers(self):
        assert qr.needs_history_context("كم رسومه؟")
        assert qr.needs_history_context("وللماجستير؟")

    def test_four_token_topical_question_does_not_trigger(self):
        # حاملة لموضوعها — إلحاق السابق بها يلوّث الاسترجاع (ثبت حياً)
        assert not qr.needs_history_context("كيف بدي اجل الفصل")

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

    def test_vague_chain_climbs_to_topic_anchor(self):
        """«كم هيكلفني؟» ثم «وشو الشروط؟» — السابق مبهم بدوره فيُلتقط الراسي قبله."""
        history = [
            {"user": "كيف أقدم طلب تأجيل الفصل الدراسي؟", "assistant": "..."},
            {"user": "كم هيكلفني؟", "assistant": "10 دنانير"},
        ]
        out = qr.with_history_context("وشو الشروط؟", history)
        assert "تأجيل الفصل" in out      # الموضوع الراسي نجا من السلسلة
        assert "كم هيكلفني؟" in out
        assert out.endswith("وشو الشروط؟")

    def test_chain_does_not_climb_past_topical_turn(self):
        history = [
            {"user": "ما هي المنح المتاحة؟", "assistant": "..."},
            {"user": "كيف أقدم طلب تأجيل الفصل الدراسي؟", "assistant": "..."},
        ]
        out = qr.with_history_context("كم هيكلفني؟", history)
        assert "تأجيل الفصل" in out
        assert "المنح" not in out         # السابق حامل لموضوعه — لا صعود أبعد
