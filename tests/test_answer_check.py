# -*- coding: utf-8 -*-
"""فاحص الإجابة الحتمي (م3) + الـ Reranker (م2 — بمحاكاة النداء)."""
import unittest
from unittest.mock import patch

from app import answer_check, config, rerank


class TestAnswerCheck(unittest.TestCase):

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
