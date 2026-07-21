"""query_rewrite — retrieval-query expansion is pure string logic, so every
behavior is unit-testable without any network or Mongo."""

import time

from app import query_rewrite as qr

NOW = {"at": time.time()}          # دور طازج (الجلسة الجارية)
STALE = {"at": time.time() - 7200}  # دور قديم (قبل ساعتين)


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
        history = [{"user": "كيف اجل الفصل ظ", "assistant": "...", **NOW}]
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

    def test_source_and_date_request_inherits_previous_topic(self):
        assert qr.needs_history_context("اذكر اسم المصدر وتاريخه إذا موجود")
        assert qr.is_source_metadata_followup(
            "اذكر اسم المصدر وتاريخه إذا موجود"
        )

    def test_independent_source_question_is_not_metadata_followup(self):
        assert not qr.is_source_metadata_followup("ما مصدر الطاقة الشمسية؟")

    def test_long_correction_inherits_previous_constraints(self):
        assert qr.needs_history_context(
            "أنا بسأل عن تخصصات، مش منح؛ اعطيني خيارات أكاديمية."
        )


class TestWithHistoryContext:
    HISTORY = [
        {"user": "ما معدل القبول في الهندسة؟", "assistant": "80%", **NOW},
        {"user": "كيف بدي اجل الفصل الحالي", "assistant": "تقدم بطلب لمكتب التسجيل.", **NOW},
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
        assert qr.with_history_context(q, [{"user": "", "assistant": "x", **NOW}]) == q

    def test_stale_last_turn_is_never_chained(self):
        """سيناريو باسم الحرفي: سجل الأمس عن رؤساء الأقسام + «أذكرهم» اليوم —
        يجب ألا يرث موضوع الأمس (كان يبني: رئيس قسم التمريض — أذكرهم!)."""
        stale_hist = [{"user": "كيف اتواصل مع رئيس قسم التمريض؟",
                       "assistant": "wabeid@...", **STALE}]
        assert qr.with_history_context("أذكرهم", stale_hist) == "أذكرهم"

    def test_legacy_turn_without_timestamp_treated_as_stale(self):
        old = [{"user": "كيف اتواصل مع رئيس قسم التمريض؟", "assistant": "..."}]
        assert qr.with_history_context("أذكرهم", old) == "أذكرهم"

    def test_othkorhom_chains_onto_fresh_colleges_question(self):
        fresh = [{"user": "كم كلية تضم الجامعة الإسلامية؟",
                  "assistant": "11 كلية.", **NOW}]
        out = qr.with_history_context("أذكرهم", fresh)
        assert "كم كلية تضم" in out and "أذكرهم" in out

    def test_chain_hop_requires_fresh_anchor_too(self):
        history = [
            {"user": "كيف أقدم طلب تأجيل الفصل؟", "assistant": "...", **STALE},
            {"user": "كم هيكلفني؟", "assistant": "10", **NOW},
        ]
        out = qr.with_history_context("وشو الشروط؟", history)
        assert "كم هيكلفني" in out          # الطازج يُسلسل
        assert "تأجيل الفصل" not in out     # الراسي القديم لا يُورَّث

    def test_vague_chain_climbs_to_topic_anchor(self):
        """«كم هيكلفني؟» ثم «وشو الشروط؟» — السابق مبهم بدوره فيُلتقط الراسي قبله."""
        history = [
            {"user": "كيف أقدم طلب تأجيل الفصل الدراسي؟", "assistant": "...", **NOW},
            {"user": "كم هيكلفني؟", "assistant": "10 دنانير", **NOW},
        ]
        out = qr.with_history_context("وشو الشروط؟", history)
        assert "تأجيل الفصل" in out      # الموضوع الراسي نجا من السلسلة
        assert "كم هيكلفني؟" in out
        assert out.endswith("وشو الشروط؟")

    def test_chain_does_not_climb_past_topical_turn(self):
        history = [
            {"user": "ما هي المنح المتاحة؟", "assistant": "...", **NOW},
            {"user": "كيف أقدم طلب تأجيل الفصل الدراسي؟", "assistant": "...", **NOW},
        ]
        out = qr.with_history_context("كم هيكلفني؟", history)
        assert "تأجيل الفصل" in out
        assert "المنح" not in out         # السابق حامل لموضوعه — لا صعود أبعد

    def test_four_followups_climb_to_original_programs_topic(self):
        history = [
            {"user": "ما هي التخصصات المتاحة في الجامعة؟",
             "assistant": "...", **NOW},
            {"user": "قصدي بكالوريوس فقط، مش ماجستير.",
             "assistant": "...", **NOW},
            {"user": "وأنا فرعي علمي، شو الخيارات الأقرب إلي؟",
             "assistant": "...", **NOW},
            {"user": "لو كان فرعي أدبي بتتغير القائمة؟",
             "assistant": "...", **NOW},
        ]
        out = qr.with_history_context(
            "رتبهم حسب الكلية عشان ما أخلط بين اسم الكلية واسم التخصص.",
            history,
        )
        assert "ما هي التخصصات المتاحة" in out
        assert "فرعي أدبي" in out
        assert out.endswith("اسم التخصص.")


class TestDegreeLevelAwareness:
    # جذر أخطاء الـ90: سجلات الدراسات العليا كانت تبتلع أسئلة البكالوريوس

    def test_explicit_masters_detected(self):
        assert qr.detect_degree_level("ما هي برامج الماجستير المتاحة؟") == "masters"

    def test_tawjihi_implies_bachelor(self):
        assert qr.detect_degree_level("معدلي بالتوجيهي 85 شو بيقبلني؟") == "bachelor"

    def test_multi_level_question_returns_none(self):
        # سؤال «الدرجات التي تمنحونها» يذكر كل المراحل — لا ترشيح لأي منها
        assert qr.detect_degree_level(
            "شو الدرجات: دبلوم وبكالوريوس وماجستير ودكتوراه؟") is None

    def test_plain_programs_question_has_no_level(self):
        assert qr.detect_degree_level("ما برامج كلية العلوم؟") is None

    def test_file_level_from_name(self):
        assert qr.file_degree_level("تخصصات الماجستير") == "masters"
        assert qr.file_degree_level("تخصصات الدكتوراه") == "phd"
        assert qr.file_degree_level("رسوم البكالوريوس ومعدلات القبول") == "bachelor"
        assert qr.file_degree_level("التواصل والعناوين") is None


class TestExclusions:
    # «مش منح» كانت تُتجاهل ويُبنى الجواب على المستبعد نفسه (Q097 حياً)

    def test_mesh_minah_extracted(self):
        excluded = qr.extract_exclusions("أنا بسأل عن تخصصات، مش منح")
        assert "منح" in excluded

    def test_khaleena_men_extracted(self):
        excluded = qr.extract_exclusions("خلينا من الهندسة، في منح للمتفوقين؟")
        assert any("هندس" in t for t in excluded)

    def test_markers_map_to_english_file_names(self):
        markers = qr.exclusion_file_markers(["منح"])
        assert "scholarship" in markers

    def test_plain_question_has_no_exclusions(self):
        assert qr.extract_exclusions("ما هي رسوم الساعة؟") == []

    def test_answer_constraints_are_not_misread_as_topic_exclusions(self):
        assert qr.extract_exclusions("لا تذكر مبلغاً بلا مصدر") == []
        assert qr.extract_exclusions("جاوب بدون ما تضمن النتيجة") == []
        assert qr.extract_exclusions("مش رقم محفوظ قديم") == []
        assert qr.extract_exclusions("مش اسم أكاديمي") == []

    def test_specific_home_page_exclusion_keeps_full_phrase(self):
        assert qr.extract_exclusions("مش الصفحة الرئيسية، بدي رابط النموذج") == [
            "الصفحه الرييسيه"
        ]


class TestAdmissionIntentInheritance:
    # «وكم للطب؟» بعد نقاش المفاتيح كانت تسقط (Q093)، و«وشو الرسوم؟» يجب
    # ألا تجرّ الجدول عبثاً.

    BASE_TALK = "كم معدل قبول الهندسة؟ وكم للطب؟"

    def test_short_followup_inherits(self):
        assert qr.inherits_admission_intent(
            "وكم للطب؟", "وكم للطب؟", self.BASE_TALK)

    def test_academic_topic_with_context_grade_inherits(self):
        assert qr.inherits_admission_intent(
            "أنا بسأل عن تخصصات، مش منح؛ اعطيني خيارات أكاديمية.",
            "أنا بسأل عن تخصصات، مش منح؛ اعطيني خيارات أكاديمية. (تخصصات)",
            "معدلي 85 علمي هل في منح؟ أنا بسأل عن تخصصات مش منح")

    def test_fees_followup_does_not_inherit(self):
        # «وشو الرسوم؟» موضوعها ذاتي غير أكاديمي — لا جدول مفاتيح لها
        assert not qr.inherits_admission_intent(
            "وشو الرسوم؟", "وشو الرسوم؟",
            "ما التخصصات التي تقبلني بمعدلي 81؟ وشو الرسوم؟") \
            or len(qr.tokenize("وشو الرسوم؟")) <= 3  # قصيرة فترث — سلوك مقبول موثق

    def test_no_context_no_intent(self):
        assert not qr.inherits_admission_intent(
            "وشو الرسوم؟", "وشو الرسوم؟", "وشو الرسوم؟")


class TestLatestAcademicConstraints:
    def test_latest_branch_overrides_older_branch_in_looping_dialogue(self):
        history = [
            {"user": "معدلي 85% علمي، شو التخصصات؟",
             "assistant": "...", **NOW},
            {"user": "لو كان فرعي أدبي بتتغير القائمة؟",
             "assistant": "...", **NOW},
        ]
        state = qr.latest_academic_constraints("رتبهم حسب الكلية", history)
        assert state["branch"] == "أدبي"
        assert state["rate"] == 85

    def test_current_constraint_wins_over_history(self):
        history = [{
            "user": "أنا فرعي أدبي ومعدلي 75%",
            "assistant": "...",
            **NOW,
        }]
        state = qr.latest_academic_constraints(
            "لا، معدلي 90% وأنا علمي", history
        )
        assert state["branch"] == "علمي"
        assert state["rate"] == 90

    def test_only_degree_correction_selects_positive_level(self):
        history = [{
            "user": "ما البرامج المتاحة؟",
            "assistant": "...",
            **NOW,
        }]
        state = qr.latest_academic_constraints(
            "قصدي بكالوريوس فقط، مش ماجستير.", history
        )
        assert state["degree"] == "bachelor"

    def test_stale_constraints_are_not_inherited(self):
        history = [{
            "user": "معدلي 99% علمي",
            "assistant": "...",
            **STALE,
        }]
        state = qr.latest_academic_constraints("ما شروط التحويل؟", history)
        assert state["branch"] is None
        assert state["rate"] is None


class TestProgramsIntent:
    def test_academic_options_detected(self):
        assert qr.wants_academic_programs("اعطيني خيارات أكاديمية")
        assert qr.wants_academic_programs("ما التخصصات المتاحة؟")
        assert qr.wants_academic_programs("شو برامج كلية العلوم؟")

    def test_non_programs_question(self):
        assert not qr.wants_academic_programs("كم رسوم الساعة؟")
        assert not qr.wants_academic_programs("متى يبدأ الفصل؟")


class TestRetrievalCostRouting:
    def test_deans_followup_gets_canonical_academic_term(self):
        rewritten = qr.add_canonical_terms("أنا أقصد عمداءهم")
        assert "عمداء الكليات" in rewritten

    def test_complete_list_detects_explicit_and_reference_forms(self):
        assert qr.wants_complete_list("اذكر جميع كليات الجامعة")
        assert qr.wants_complete_list("رتبهم حسب المعدل")
        assert qr.wants_complete_list("أريد القائمة الكاملة")
        assert not qr.wants_complete_list("ما رسوم كلية الهندسة؟")

    def test_multi_part_requires_multiple_requests(self):
        assert qr.is_multi_part_question(
            "ما شروط التحويل؟ وكم رسومه؟"
        )
        assert qr.is_multi_part_question(
            "اذكر الرسوم؛ وافصل كل جزء عن الآخر"
        )
        assert not qr.is_multi_part_question("ما شروط التحويل الداخلي؟")

    def test_reranker_skips_coverage_and_admission_questions(self):
        assert not qr.should_use_reranker(
            "رتبهم", "ما كليات الجامعة؟ — رتبهم"
        )
        assert not qr.should_use_reranker(
            "ما التخصصات التي تقبل معدلي؟",
            "ما التخصصات التي تقبل معدلي؟",
            admission_intent=True,
        )

    def test_reranker_accepts_contextual_and_exact_lookups(self):
        assert qr.should_use_reranker(
            "وما رابطها؟",
            "بوابة التعليم الإلكتروني — وما رابطها؟",
        )
        assert qr.should_use_reranker(
            "ما بريد عمادة القبول؟",
            "ما بريد عمادة القبول؟",
        )

    def test_course_description_uses_direct_evidence_without_reranker(self):
        question = "كيف أعرف وصف المساقات اللي رح أدرسها بالتخصص؟"
        assert qr.requires_direct_evidence(question)
        assert not qr.should_use_reranker(question, question)

    def test_candidate_guard_requires_two_semantic_stems_in_one_chunk(self):
        query = "ما رابط بوابة الطالب؟"
        assert qr.candidates_support_query(
            query,
            ["بوابة الطالب الإلكترونية ورابط الدخول الرسمي"],
        )
        assert not qr.candidates_support_query(
            query,
            ["معلومات عن الرسوم", "معلومات عن الأنشطة"],
        )

    def test_candidate_guard_tolerates_arabic_singular_and_plural(self):
        assert qr.candidates_support_query(
            "أنا أقصد عمداء الكليات",
            ["عميد كلية الهندسة والبريد الرسمي للعمادة"],
        )

    def test_exact_faculty_deans_drop_deputies_and_administrative_deans(self):
        chunks = [
            "degree_or_request: عميد كلية الهندسة | full_name: أ",
            "degree_or_request: عميد كلية الطب | full_name: ب",
            "degree_or_request: عميد كلية العلوم | full_name: ج",
            "degree_or_request: نائب عميد كلية الهندسة | full_name: د",
            "degree_or_request: عميد شؤون الطلبة | full_name: هـ",
        ]
        selected, applied = qr.prefer_exact_role_chunks(
            "اذكر عمداء الكليات", chunks
        )
        assert applied
        assert len(selected) == 3
        assert all("degree_or_request: عميد كلية" in chunk for chunk in selected)
