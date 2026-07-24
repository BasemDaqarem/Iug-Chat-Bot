# -*- coding: utf-8 -*-
"""فاحص الإجابة الحتمي (م3) + الـ Reranker (م2 — بمحاكاة النداء)."""
import unittest
from unittest.mock import patch

from app import answer_check, config, rerank


class TestAnswerCheck(unittest.TestCase):

    def test_internal_metadata_and_invented_ui_status_are_rejected(self):
        issues = answer_check.problems(
            "افتح الصفحة (link_id: get_student_number) ثم ستظهر حالة «مُرسل».",
            sources=["راجع بوابة طلب الالتحاق أو القبول والتسجيل."],
            excluded=[], asked_level=None,
            question="كيف أتأكد أن طلب الالتحاق انرسل؟",
        )
        self.assertTrue(any("ميتاداتا" in issue for issue in issues))
        self.assertTrue(any("حالة واجهة" in issue for issue in issues))

    def test_supported_quoted_procedure_label_passes(self):
        issues = answer_check.problems(
            "اختر «طباعة وثائق الطالب».",
            sources=["من القبول والتسجيل اختر طباعة وثائق الطالب."],
            excluded=[], asked_level=None,
            question="كيف أستخرج الشهادة؟",
        )
        self.assertEqual(issues, [])

        invented = answer_check.problems(
            "راجع شاشة «تأكيد الطلب» أو «النتيجة».",
            sources=["راجع الطلب في البوابة أو تواصل مع القبول والتسجيل."],
            excluded=[], asked_level=None,
            question="كيف أتأكد أن الطلب انحفظ؟",
        )
        self.assertTrue(any("حالة واجهة" in issue for issue in invented))

    def test_unsubstantiated_application_status_is_rejected(self):
        issues = answer_check.problems(
            "ستظهر رسالة تأكيد ثم تصبح الحالة مقبول أو قيد المراجعة.",
            sources=["يمكن تعبئة طلب الالتحاق إلكترونياً."],
            excluded=[], asked_level=None,
            question="كيف أتأكد أن الطلب انحفظ وانرسل إذا لم يظهر تأكيد واضح؟",
        )
        self.assertTrue(any("حالة واجهة غير موثقة" in issue for issue in issues))

        safe = answer_check.problems(
            "لا يمكن تأكيد أن الطلب تم حفظه من علامة واجهة موثقة؛ راجعه في البوابة.",
            sources=["يمكن تعبئة طلب الالتحاق إلكترونياً."],
            excluded=[], asked_level=None,
            question="كيف أتأكد أن الطلب انحفظ وانرسل إذا لم يظهر تأكيد واضح؟",
        )
        self.assertEqual(safe, [])

    def test_program_cutoff_answer_must_include_known_branch_scope(self):
        source = (
            "علم الحاسوب | البرامج: علم الحاسوب | الفروع: علمي | "
            "الحد الأدنى: 65%"
        )
        missing = answer_check.problems(
            "الحد الأدنى لعلم الحاسوب 65%.",
            sources=[source], excluded=[], asked_level=None,
            question="والحد الأدنى؟", entity_terms=["علم", "الحاسوب"],
        )
        self.assertTrue(any("نطاق فرع" in issue for issue in missing))
        complete = answer_check.problems(
            "الحد الأدنى لعلم الحاسوب 65%، والفرع العلمي فقط.",
            sources=[source], excluded=[], asked_level=None,
            question="والحد الأدنى؟", entity_terms=["علم", "الحاسوب"],
        )
        self.assertEqual(complete, [])

        eligibility = answer_check.problems(
            "لا، معدلك 79% أقل من مفتاح الهندسة 80%.",
            sources=["الهندسة | الفروع: علمي | الحد الأدنى: 80%"],
            excluded=[], asked_level=None,
            question="معدلي 79 علمي؛ هل أحقق مفتاح الهندسة؟",
            entity_terms=["الهندسة"],
        )
        self.assertFalse(any("نطاق فرع" in issue for issue in eligibility))

    def test_direct_programs_accept_you_phrase_is_not_preliminary_eligibility(self):
        issues = answer_check.problems(
            "معدلك 89% أعلى من المفتاح 80%، وكل هذه البرامج تقبلك.",
            sources=["الهندسة | الفروع: علمي | الحد الأدنى: 80%"],
            excluded=[], asked_level=None,
            question="معدلي 89 علمي؛ شو وضعي للهندسة؟",
            entity_terms=["الهندسة"],
        )
        self.assertTrue(any("ضمان قبول" in issue for issue in issues))

    def test_orphan_percentage_detected(self):
        # النمط القاتل: «الطب 80%» ولا وجود لـ80 في المصادر
        issues = answer_check.problems(
            "معدل قبول الطب هو 80% من الثانوية.",
            sources=["[ملف: رسوم] الطب تنافسي 91% لعام 2025-2026"],
            excluded=[], asked_level=None)
        self.assertEqual(len(issues), 1)
        self.assertIn("80", issues[0])

    def test_supported_percentage_passes(self):
        issues = answer_check.problems(
            "مفتاح الهندسة 80% للفرع العلمي.",
            sources=["[ملف: رسوم] الهندسة الحد الأدنى: 80% علمي"],
            excluded=[], asked_level=None)
        self.assertEqual(issues, [])

    def test_computed_fee_totals_are_not_flagged(self):
        # مجموع محسوب (283 ديناراً) مشروع بقاعدة البرومت — الفحص للنسب فقط
        issues = answer_check.problems(
            "التكلفة = 13 + 18×15 = 283 ديناراً.",
            sources=["الثوابت 13 ديناراً وسعر الساعة 18"],
            excluded=[], asked_level=None)
        self.assertEqual(issues, [])

    def test_fee_for_wrong_entity_is_rejected(self):
        issues = answer_check.problems(
            "رسوم ساعة الطب 22 ديناراً.",
            sources=["رسوم ساعة التمريض 22 ديناراً، والطب 100 دينار."],
            excluded=[], asked_level=None,
            question="كم رسوم ساعة الطب؟", entity_terms=["الطب"])
        self.assertTrue(any("مبلغاً" in issue for issue in issues))

    def test_supported_entity_bound_fee_passes(self):
        issues = answer_check.problems(
            "رسوم ساعة الطب 100 دينار.",
            sources=["رسوم ساعة التمريض 22 ديناراً، والطب 100 دينار."],
            excluded=[], asked_level=None,
            question="كم رسوم ساعة الطب؟", entity_terms=["الطب"])
        self.assertEqual(issues, [])

    def test_structured_amount_is_accepted_only_for_matching_fee_entity(self):
        source = (
            "service_type: رسوم التخرج\n"
            "degree_or_request: دبلوم عالي\namount: 75.0"
        )
        issues = answer_check.problems(
            "رسوم تخرج الدبلوم العالي 75 ديناراً.",
            sources=[source], excluded=[], asked_level=None,
            question="كم رسوم تخرج الدبلوم العالي؟",
            entity_terms=["تخرج", "الدبلوم", "العالي"],
        )
        self.assertEqual(issues, [])

        wrong = answer_check.problems(
            "رسوم تخرج الدبلوم المهني 75 ديناراً.",
            sources=[source], excluded=[], asked_level=None,
            question="كم رسوم تخرج الدبلوم المهني؟",
            entity_terms=["المهني"],
        )
        self.assertTrue(any("مبلغاً" in issue for issue in wrong))

        card = answer_check.problems(
            "رسم البطاقة الجامعية 5 دنانير.",
            sources=[
                "service_type: طلبات الطلبة\n"
                "degree_or_request: رسوم بطاقة جامعية\namount: 5.0"
            ],
            excluded=[], asked_level=None,
            question="كم رسم البطاقة الجامعية؟",
            entity_terms=["البطاقة", "الجامعية"],
        )
        self.assertEqual(card, [])

        nested = answer_check.problems(
            "سعر ساعة المرحلة الأساسية 8 دنانير.",
            sources=[
                "programs[3].program_name: المرحلة الأساسية\n"
                "programs[3].credit_hour_fee: 8"
            ],
            excluded=[], asked_level=None,
            question="كم سعر ساعة المرحلة الأساسية؟",
            entity_terms=["المرحلة", "الأساسية"],
        )
        self.assertEqual(nested, [])

        multi_record = (
            "programs[0].program_name: تعليم اللغة العربية\n"
            "programs[0].credit_hour_fee: 13\n"
            "programs[3].program_name: المرحلة الأساسية\n"
            "programs[3].credit_hour_fee: 8"
        )
        nested_multi = answer_check.problems(
            "سعر ساعة المرحلة الأساسية 8 دنانير.",
            sources=[multi_record], excluded=[], asked_level=None,
            question="كم سعر ساعة المرحلة الأساسية؟",
            entity_terms=["المرحلة", "الأساسية"],
        )
        self.assertEqual(nested_multi, [])
        wrong_nested_multi = answer_check.problems(
            "سعر ساعة المرحلة الأساسية 13 ديناراً.",
            sources=[multi_record], excluded=[], asked_level=None,
            question="كم سعر ساعة المرحلة الأساسية؟",
            entity_terms=["المرحلة", "الأساسية"],
        )
        self.assertTrue(
            any("مبلغاً" in issue for issue in wrong_nested_multi)
        )

    def test_wrong_explicit_faculty_count_is_rejected(self):
        issues = answer_check.problems(
            "تضم الجامعة 9 كليات.",
            sources=["تضم الجامعة 11 كلية أكاديمية."],
            excluded=[], asked_level=None,
            question="كم عدد كليات الجامعة؟")
        self.assertTrue(any("العدد" in issue for issue in issues))

    def test_false_missing_claim_is_rejected_when_contract_is_sufficient(self):
        issues = answer_check.problems(
            "لا توجد رسوم منشورة لهذا البرنامج.",
            sources=["رسوم ساعة هندسة الحاسوب 28 ديناراً."],
            excluded=[], asked_level=None,
            question="كم رسوم هندسة الحاسوب؟",
            entity_terms=["هندسه", "الحاسوب"],
            evidence_sufficient=True)
        self.assertTrue(any("غير موجودة" in issue for issue in issues))

    def test_missing_claim_is_rejected_when_retrieval_is_degraded(self):
        issues = answer_check.problems(
            "المعلومة غير موجودة.",
            sources=[], excluded=[], asked_level=None,
            question="كم كلية؟",
            evidence_sufficient=False,
            retrieval_degraded=True,
        )
        self.assertTrue(any("استرجاع متدهورة" in issue for issue in issues))

    def test_violated_exclusion_detected(self):
        issues = answer_check.problems(
            "أنصحك بمنحة الامتياز فهي الأفضل.",
            sources=["نص"], excluded=["منح"], asked_level=None)
        self.assertTrue(any("استبعد" in i for i in issues))

    def test_bachelor_question_graduate_answer_detected(self):
        issues = answer_check.problems(
            "برامج الماجستير: الرياضيات، ورسوم أطروحة الماجستير 1500.",
            sources=["نص"], excluded=[], asked_level="bachelor")
        self.assertTrue(any("البكالوريوس" in i for i in issues))

    def test_single_negating_mention_passes(self):
        # «لا يوجد ماجستير لهذا التخصص» ذكر عابر مشروع
        issues = answer_check.problems(
            "هذا تخصص بكالوريوس ولا يوجد له ماجستير حالياً.",
            sources=["نص"], excluded=[], asked_level="bachelor")
        self.assertEqual(issues, [])

    def test_unsupported_url_is_rejected_but_supported_url_passes(self):
        bad = answer_check.problems(
            "الرابط هو https://fake.example/login",
            sources=["[ملف: البوابات] https://portal.iugaza.edu.ps/"],
            excluded=[], asked_level=None, question="ما رابط البوابة؟")
        self.assertTrue(any("fake.example" in issue for issue in bad))

        good = answer_check.problems(
            "الرابط هو https://portal.iugaza.edu.ps/",
            sources=["[ملف: البوابات] https://portal.iugaza.edu.ps/"],
            excluded=[], asked_level=None, question="ما رابط البوابة؟")
        self.assertEqual(good, [])

    def test_trusted_official_contacts_pass_without_chunk_duplicates(self):
        issues = answer_check.problems(
            "هاتف الجامعة +970 8 2644400 والبريد regist@iugaza.edu.ps.",
            sources=["[ملف: أمان] تواصل عبر القنوات الرسمية"],
            excluded=[], asked_level=None, question="كيف أتحقق؟")
        self.assertEqual(issues, [])

    def test_unsupported_email_and_phone_are_rejected(self):
        issues = answer_check.problems(
            "البريد wrong@iugaza.edu.ps وهاتف الجامعة 0599999999.",
            sources=[
                "[ملف: تواصل] البريد dean@iugaza.edu.ps، "
                "هاتف الجامعة +970-8-2644400"
            ],
            excluded=[], asked_level=None, question="كيف أتواصل؟")
        joined = "\n".join(issues)
        self.assertIn("wrong@iugaza.edu.ps", joined)
        self.assertIn("0599999999", joined)

    def test_labelled_year_must_exist_in_evidence(self):
        issues = answer_check.problems(
            "آخر تحديث للمعلومة عام 2026.",
            sources=["[ملف: سياسة] آخر تحقق عام 2025"],
            excluded=[], asked_level=None, question="هل هي محدثة؟")
        self.assertTrue(any("2026" in issue for issue in issues))

    def test_labelled_full_date_cannot_borrow_same_year_from_another_record(self):
        issues = answer_check.problems(
            "المصدر: ملف المنح، التاريخ: 2026‑07‑15.",
            sources=["[ملف: المنح] المصدر الرسمي، آخر_تحقق: 2026-07-18"],
            excluded=[], asked_level=None,
            question="اذكر المصدر وتاريخه")
        self.assertTrue(any("2026" in issue and "07" in issue for issue in issues))

    def test_supported_full_date_passes(self):
        issues = answer_check.problems(
            "المصدر: ملف المنح، التاريخ: 2026‑07‑18.",
            sources=["[ملف: المنح] المصدر الرسمي، آخر_تحقق: 2026-07-18"],
            excluded=[], asked_level=None,
            question="اذكر المصدر وتاريخه")
        self.assertEqual(issues, [])

    def test_source_cannot_borrow_date_from_another_evidence_record(self):
        issues = answer_check.problems(
            "المصدر: internal_scholarships، التاريخ: 2026-07-15.",
            sources=[
                "[ملف: internal_scholarships] بيانات المنح بلا تاريخ",
                "[ملف: دليل آخر] آخر_تحقق: 2026-07-15",
            ],
            excluded=[], asked_level=None,
            question="اذكر المصدر وتاريخه")
        self.assertTrue(any("سجل آخر" in issue for issue in issues))

    def test_chat_cannot_claim_backend_deletion(self):
        issues = answer_check.problems(
            "تم حذف بياناتك وسجل المحادثة نهائياً.",
            sources=[], excluded=[], asked_level=None,
            question="احذف بياناتي وسجل محادثتي")
        self.assertTrue(any("لا ينفذ عمليات" in issue for issue in issues))

    def test_complete_list_cannot_be_only_examples(self):
        issues = answer_check.problems(
            "من أبرز الكليات: الهندسة والطب.",
            sources=["الهندسة", "الطب", "العلوم"],
            excluded=[], asked_level=None,
            question="اذكر جميع كليات الجامعة")
        self.assertTrue(any("قائمة كاملة" in issue for issue in issues))

    def test_internal_link_identifier_is_not_a_user_url(self):
        issues = answer_check.problems(
            "رابط صفحة الطلب هو admission_application.",
            sources=["link_id: admission_application"],
            excluded=[], asked_level=None,
            question="أعطني رابط طلب الالتحاق نفسه")
        self.assertTrue(any("ليس رابطاً" in issue for issue in issues))

    def test_admission_digest_cannot_be_replaced_by_all_programs(self):
        issues = answer_check.problems(
            "الهندسة: جميع البرامج. الآداب: وغيرها.",
            sources=[
                "جدول مفاتيح القبول\n"
                "الهندسة | البرامج: مدنية، معمارية | الفروع: علمي",
                "معدل الثانوية الذي ذكره المستخدم: 85%",
            ],
            excluded=[], asked_level=None,
            question="معدلي 85% علمي، ما الخيارات؟")
        self.assertTrue(any("جميع البرامج" in issue for issue in issues))

    def test_filtered_admission_digest_requires_every_remaining_faculty(self):
        issues = answer_check.problems(
            "كلية الآداب: اللغة العربية.",
            sources=[
                "جدول مفاتيح القبول\n"
                "كلية الآداب | البرامج: اللغة العربية | الفروع: أدبي | "
                "الحد الأدنى: 65%\n"
                "كلية الاقتصاد | البرامج: المحاسبة | الفروع: أدبي | "
                "الحد الأدنى: 70%\n"
                "كلية التربية | البرامج: تعليم العربية | الفروع: أدبي | "
                "الحد الأدنى: 65%"
            ],
            excluded=[], asked_level=None,
            question="معدلي 85% أدبي، ما الخيارات؟")
        self.assertTrue(any("كلية الاقتصاد" in issue for issue in issues))

    def test_single_program_eligibility_does_not_require_all_faculties(self):
        issues = answer_check.problems(
            "لا، معدلك 79% أقل من مفتاح هندسة الحاسوب 80%.",
            sources=[
                "جدول مفاتيح القبول\n"
                "الهندسة | البرامج: هندسة الحاسوب | الفروع: علمي | "
                "الحد الأدنى: 80%\n"
                "الآداب | البرامج: اللغة العربية | الفروع: أدبي | "
                "الحد الأدنى: 65%",
                "معدل الثانوية الذي ذكره المستخدم: 79%",
            ],
            excluded=[], asked_level=None,
            question="معدلي 79 علمي، هل أحقق مفتاح هندسة الحاسوب؟",
        )
        self.assertFalse(any("أسقطتَ كليات" in issue for issue in issues))

    def test_named_program_eligibility_must_apply_branch_and_rate_together(self):
        sources = [
            "التربية | البرامج: تعليم العلوم، الحاسوب وأساليب تدريسه | "
            "الفروع: علمي، أدبي، شرعي | الحد الأدنى: 65%",
            "الهندسة | البرامج: هندسة الحاسوب | الفروع: علمي | "
            "الحد الأدنى: 80%",
            "الفرع الحالي الذي ذكره المستخدم: أدبي",
        ]
        kwargs = dict(
            sources=sources,
            excluded=[],
            asked_level=None,
            question="معدلي 85 أدبي، هل يتيح لي التقديم لهندسة الحاسوب؟",
            entity_terms=["هندسة", "الحاسوب"],
        )
        wrong = answer_check.problems(
            "نعم، يسمح لك لأن معدلك 85% أعلى من المفتاح 80%.", **kwargs
        )
        correct = answer_check.problems(
            "البرنامج للفرع العلمي فقط؛ لذلك لا يمكنك التقديم وفرعك أدبي.",
            **kwargs,
        )
        cautious = answer_check.problems(
            "البيانات لا تذكر الفرع الأدبي ضمن الفروع المسموحة؛ المذكور علمي فقط.",
            **kwargs,
        )
        self.assertTrue(any("قيد فرع الثانوية" in issue for issue in wrong))
        self.assertFalse(any("قيد فرع الثانوية" in issue for issue in correct))
        self.assertFalse(any("قيد فرع الثانوية" in issue for issue in cautious))

    def test_eligibility_answer_cannot_mix_yes_with_failed_condition(self):
        issues = answer_check.problems(
            "نعم، يحق لك التقديم، لكن معدلك لا يحقق شرط القبول.",
            sources=["التمريض | الفروع: أدبي | الحد الأدنى: 80%"],
            excluded=[], asked_level=None,
            question="معدلي 79 أدبي، هل أحقق شرط التمريض؟",
            entity_terms=["التمريض"],
        )
        self.assertTrue(any("قراراً واحداً" in issue for issue in issues))

    def test_branch_check_does_not_merge_words_from_different_programs(self):
        issues = answer_check.problems(
            "نعم، معدلك أعلى من 65% ولذلك يمكنك التقديم لعلم الحاسوب.",
            sources=[
                "التربية | البرامج: تعليم العلوم، الحاسوب وأساليب تدريسه | "
                "الفروع: علمي، أدبي، شرعي | الحد الأدنى: 65%",
                "تكنولوجيا المعلومات | البرامج: علم الحاسوب، تطوير البرمجيات | "
                "الفروع: علمي | الحد الأدنى: 65%",
            ],
            excluded=[], asked_level=None,
            question="معدلي 80 أدبي، هل يمكنني التقديم لعلم الحاسوب؟",
            entity_terms=["علم", "حاسوب"],
        )
        self.assertTrue(any("قيد فرع الثانوية" in issue for issue in issues))

    def test_cutoff_eligibility_cannot_guarantee_final_admission(self):
        kwargs = dict(
            sources=["التمريض | الفروع: أدبي | الحد الأدنى: 80%"],
            excluded=[], asked_level=None,
            question="معدلي 80 أدبي، هل أحقق شرط التمريض مبدئياً؟",
            entity_terms=["التمريض"],
        )
        wrong = answer_check.problems(
            "نعم مبدئياً، معدلك يساوي 80% وبالتالي يقبلك في البرنامج.",
            **kwargs,
        )
        correct = answer_check.problems(
            "نعم، تحقق الشرط المبدئي 80%، ولا يضمن ذلك القبول النهائي.",
            **kwargs,
        )
        self.assertTrue(any("ضمان قبول نهائي" in issue for issue in wrong))
        self.assertFalse(any("ضمان قبول نهائي" in issue for issue in correct))

    def test_competitive_medicine_cutoff_requires_date_and_variability(self):
        kwargs = dict(
            sources=[
                "قبول الطب تنافسي ويتغير كل عام؛ في 2025/2026 كان 91%."
            ],
            excluded=[], asked_level=None,
            question="معدلي 90 علمي، هل الطب مضمون؟",
            entity_terms=["الطب"],
        )
        wrong = answer_check.problems(
            "مفتاح الطب هو 91%، لذلك القبول غير مضمون.", **kwargs
        )
        correct = answer_check.problems(
            "لا؛ كان المرجع 91% في 2025/2026، وهو تنافسي ومتغير.", **kwargs
        )
        self.assertTrue(any("رقم ثابت" in issue for issue in wrong))
        self.assertFalse(any("رقم ثابت" in issue for issue in correct))

    def test_one_missing_admission_faculty_is_accepted_as_good_not_perfect(self):
        issues = answer_check.problems(
            "كلية الآداب: العربية. كلية الاقتصاد: المحاسبة.",
            sources=[
                "جدول مفاتيح القبول\n"
                "كلية الآداب | البرامج: العربية | الفروع: أدبي\n"
                "كلية الاقتصاد | البرامج: المحاسبة | الفروع: أدبي\n"
                "كلية التربية | البرامج: تعليم العربية | الفروع: أدبي"
            ],
            excluded=[], asked_level=None,
            question="ما الخيارات الأدبية؟")
        self.assertFalse(any("أسقطتَ كليات" in issue for issue in issues))

    def test_complete_admission_footer_is_not_vague_grouping(self):
        issues = answer_check.problems(
            "الهندسة: مدنية، معمارية.\n"
            "هذه القائمة تشمل جميع البرامج التي تحقق شرط المعدل.",
            sources=[
                "جدول مفاتيح القبول\n"
                "الهندسة | البرامج: مدنية، معمارية | الفروع: علمي",
                "معدل الثانوية الذي ذكره المستخدم: 85%",
            ],
            excluded=[], asked_level=None,
            question="معدلي 85% علمي، ما الخيارات؟")
        self.assertFalse(any("عبارات مبهمة" in issue for issue in issues))

    def test_direct_application_link_cannot_be_replaced_by_guide_url(self):
        guide = "https://admission.iugaza.edu.ps/guide/خطوات-تسجيل-طالب-جديد/"
        issues = answer_check.problems(
            f"رابط طلب الالتحاق نفسه هو {guide}",
            sources=[
                "steps[2].action: فتح صفحة طلب الالتحاق\n"
                "steps[2].link_id: admission_application\n"
                "official_sources[0].title: خطوات قبول والتحاق طالب جديد\n"
                f"official_sources[0].url: {guide}"
            ],
            excluded=[], asked_level=None,
            question="أعطني رابط طلب الالتحاق نفسه")
        self.assertTrue(any("دليل/خطوات" in issue for issue in issues))

    def test_procedure_answer_cannot_call_guide_url_a_portal(self):
        url = "https://admission.iugaza.edu.ps/guide/خطوات-تسجيل-طالب-جديد/"
        issues = answer_check.problems(
            f"ادخل إلى بوابة طلب الالتحاق عبر الرابط: {url}",
            sources=[f"title: خطوات تسجيل طالب جديد\nurl: {url}"],
            excluded=[], asked_level=None,
            question="اشرح بداية طلب الالتحاق الإلكتروني.",
        )
        self.assertTrue(any("دليل/خطوات" in issue for issue in issues))

    def test_foreign_certificate_must_precede_university_number(self):
        kwargs = dict(
            sources=[
                "إذا كانت الشهادة من الخارج أرسلها أولاً لإدخال البيانات، "
                "وبعدها يحصل الطالب على الرقم الجامعي."
            ],
            excluded=[], asked_level=None,
            question="أنا خارج غزة والمعبر مغلق؛ اشرح بداية طلب الالتحاق الإلكتروني.",
        )
        wrong = answer_check.problems(
            "1. الحصول على الرقم الجامعي. 2. إرسال الشهادة إذا كانت من الخارج.",
            **kwargs,
        )
        correct = answer_check.problems(
            "إذا كانت الشهادة صادرة من الخارج: أرسل الشهادة أولاً، ثم الحصول على الرقم الجامعي.",
            **kwargs,
        )
        self.assertTrue(any("عكستَ ترتيب" in issue for issue in wrong))
        self.assertFalse(any("عكستَ ترتيب" in issue for issue in correct))

    def test_answer_must_use_latest_active_branch(self):
        issues = answer_check.problems(
            "التخصصات التي تقبل الفرع العلمي: الهندسة والعلوم.",
            sources=[
                "الفرع الحالي الذي ذكره المستخدم: أدبي",
                "جدول مفاتيح القبول\n"
                "الآداب | البرامج: العربية | الفروع: أدبي",
            ],
            excluded=[], asked_level=None,
            question="رتبهم حسب الكلية")
        self.assertTrue(any("فرع قديم" in issue for issue in issues))

    def test_course_description_path_needs_direct_evidence(self):
        issues = answer_check.problems(
            "ادخل المودل واضغط المساق وستجد وصف المساق.",
            sources=["المودل يحتوي على الكتب والملفات التعليمية."],
            excluded=[], asked_level=None,
            question="كيف أعرف وصف المساقات التي سأدرسها؟")
        self.assertTrue(any("المورد نفسه غير موجود" in issue for issue in issues))

    def test_honest_course_description_gap_passes(self):
        issues = answer_check.problems(
            "المسار الدقيق لوصف المساق غير وارد في المقاطع المتاحة.",
            sources=["المودل يحتوي على الكتب والملفات التعليمية."],
            excluded=[], asked_level=None,
            question="كيف أعرف وصف المساقات التي سأدرسها؟")
        self.assertEqual(issues, [])

    def test_admission_comparison_direction_is_checked(self):
        issues = answer_check.problems(
            "التمريض 70% (مفتاح أعلى من معدلك).",
            sources=["التمريض | علمي | 70%"],
            excluded=[], asked_level=None,
            question="معدلي 85% علمي، شو التخصصات المتاحة؟")
        self.assertTrue(any("عكست" in issue for issue in issues))

    def test_correct_admission_comparison_passes(self):
        issues = answer_check.problems(
            "التمريض 70%، وهو متاح لأن معدلك 85%.",
            sources=["التمريض | علمي | 70%"],
            excluded=[], asked_level=None,
            question="معدلي 85% علمي، شو التخصصات المتاحة؟")
        self.assertEqual(issues, [])

    def test_mixed_branches_reject_global_science_only_claim(self):
        issues = answer_check.problems(
            "جميع هذه التخصصات تتطلب فرعاً علمياً فقط.",
            sources=["الآداب | اللغة العربية | علمي، أدبي | 65%"],
            excluded=[], asked_level=None,
            question="أنا علمي، شو التخصصات المتاحة؟")
        self.assertTrue(any("تقبل العلمي مع فروع أخرى" in issue for issue in issues))

    def test_unresolved_visa_policy_rejects_model_memory(self):
        issues = answer_check.problems(
            "تحتاج إلى تأشيرة طالب تصدرها وزارة الخارجية.",
            sources=[
                "التصنيف: غير محسوم رسمياً\n"
                "الإجابة: لا توجد معلومة موثقة عن نوع التأشيرة."
            ],
            excluded=[], asked_level=None,
            question="أي نوع تأشيرة أحتاج لدخول غزة للدراسة؟",
        )
        self.assertTrue(any("سياسة دخول/تأشيرة" in issue for issue in issues))

    def test_unresolved_visa_policy_accepts_honest_caveat(self):
        issues = answer_check.problems(
            "لا يمكن تأكيد نوع التأشيرة من البيانات الحالية؛ تحقق من الجهات الرسمية.",
            sources=[
                "التصنيف: غير محسوم رسمياً\n"
                "الإجابة: لا توجد معلومة موثقة عن نوع التأشيرة."
            ],
            excluded=[], asked_level=None,
            question="أي نوع تأشيرة أحتاج لدخول غزة للدراسة؟",
        )
        self.assertEqual(issues, [])

    def test_paid_application_fee_is_not_answered_from_waiver(self):
        issues = answer_check.problems(
            "لا يُسترد المبلغ لأن رسوم الطلب معفاة بالكامل.",
            sources=["إعفاء الطلبة الجدد من رسوم طلب الالتحاق"],
            excluded=[], asked_level=None,
            question="دفعت رسوم طلب الالتحاق ولم أسجل؛ هل أستردها؟",
        )
        self.assertTrue(any("حكم استرداد رسوم طلب الالتحاق" in x for x in issues))

    def test_paid_application_fee_honest_policy_gap_passes(self):
        issues = answer_check.problems(
            "سياسة الاسترداد غير موثقة في البيانات؛ راجع القبول والتسجيل.",
            sources=["إعفاء الطلبة الجدد من رسوم طلب الالتحاق"],
            excluded=[], asked_level=None,
            question="دفعت رسوم طلب الالتحاق ولم أسجل؛ هل أستردها؟",
            evidence_sufficient=False,
        )
        self.assertEqual(issues, [])


class TestReranker(unittest.TestCase):

    def test_disabled_flag_passthrough(self):
        with patch.object(config, "RERANK_ENABLED", False):
            out = rerank.rerank("سؤال", ["أ", "ب", "ج"], 2)
        self.assertEqual(out, ["أ", "ب"])

    def test_reorders_by_api_result(self):
        fake = {"results": [{"index": 2}, {"index": 0}]}
        with patch.object(config, "RERANK_ENABLED", True), \
             patch("app.rerank.requests.post") as post:
            post.return_value.json.return_value = fake
            post.return_value.raise_for_status.return_value = None
            out = rerank.rerank("سؤال", ["أ", "ب", "ج"], 2)
        self.assertEqual(out, ["ج", "أ"])
        self.assertEqual(post.call_args.kwargs["timeout"], config.RERANK_TIMEOUT_SECONDS)

    def test_api_failure_fails_open(self):
        with patch.object(config, "RERANK_ENABLED", True), \
             patch("app.rerank.requests.post", side_effect=OSError("down")):
            out = rerank.rerank("سؤال", ["أ", "ب", "ج"], 2)
        self.assertEqual(out, ["أ", "ب"])


if __name__ == "__main__":
    unittest.main()


class TestQuestionEchoAnswer(unittest.TestCase):
    """جواب-الصدى: النموذج يعيد السؤال بصيغة سؤال — يُرفض ويُعاد التوليد."""

    def test_echo_answer_detected(self):
        self.assertTrue(answer_check.question_echo_answer(
            "من وين أنزل كتب المواد والمساقات؟",
            "من أين تُنزل كتب المواد والمساقات؟",
        ))

    def test_real_answer_not_flagged(self):
        self.assertFalse(answer_check.question_echo_answer(
            "من وين أنزل كتب المواد والمساقات؟",
            "يوفر مدرسو المساقات نسخاً إلكترونية من الكتب داخل صفحة المساق "
            "على منصة المودل ليحمّلها الطلبة مباشرة.",
        ))

    def test_short_direct_answer_not_flagged(self):
        self.assertFalse(answer_check.question_echo_answer(
            "متى تأسست الجامعة الإسلامية؟",
            "تأسست الجامعة الإسلامية بغزة عام 1978.",
        ))

    def test_problems_rejects_echo(self):
        issues = answer_check.problems(
            "كيف أشارك في منتدى النقاش؟",
            sources=["دليل المودل"], excluded=[], asked_level=None,
            question="كيف أشارك في منتدى النقاش؟",
        )
        self.assertTrue(issues)
        self.assertIn("إعادة صياغة", issues[0])
