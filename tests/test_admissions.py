import unittest

import numpy as np

from app.admissions import (
    AdmissionCatalog,
    AdmissionFact,
    extract_admission_facts,
)


class TestAdmissionExtraction(unittest.TestCase):

    def test_keeps_percentages_attached_to_their_programs(self):
        docs = [
            {"faculty_name": "تكنولوجيا المعلومات"},
            {
                "category": "شروط القبول والتنسيق لمرحلة البكالوريوس",
                "admission_criteria": [
                    {
                        "departments": ["تكنولوجيا المعلومات"],
                        "min_high_school_percentage": 65,
                        "allowed_high_school_branches": ["علمي فقط"],
                    },
                    {
                        "departments": ["تكنولوجيا الوسائط المتعددة وتطوير الويب"],
                        "min_high_school_percentage_scientific": 65,
                        "min_high_school_percentage_literary_sharia": 70,
                    },
                ],
            },
        ]

        facts = extract_admission_facts("نشرة كلية تكنولوجيا المعلومات", docs)
        it = [fact for fact in facts if fact.program == "تكنولوجيا المعلومات"]
        multimedia = [
            fact for fact in facts
            if fact.program == "تكنولوجيا الوسائط المتعددة وتطوير الويب"
        ]

        self.assertEqual({fact.min_percentage for fact in it}, {65})
        self.assertEqual({fact.branches for fact in it}, {("علمي",)})
        self.assertEqual({fact.min_percentage for fact in multimedia}, {65, 70})
        self.assertNotIn("العلوم الصحية", {fact.faculty for fact in facts})


class TestAdmissionResolution(unittest.TestCase):

    @staticmethod
    def fact(branches=("علمي",), source="المصدر الرسمي"):
        return AdmissionFact(
            faculty="تكنولوجيا المعلومات",
            program="تكنولوجيا المعلومات",
            degree="بكالوريوس",
            branches=branches,
            min_percentage=65,
            source=source,
            path="doc[0].admission_criteria",
        )

    def catalog(self, facts):
        catalog = AdmissionCatalog()
        catalog._facts = list(facts)
        catalog._search_facts = list(facts)
        catalog._index = np.array([[1.0, 0.0] for _ in facts], dtype=np.float32)
        return catalog

    def test_returns_one_atomic_program_without_mixing_other_percentage(self):
        it = self.fact()
        multimedia = AdmissionFact(
            faculty="تكنولوجيا المعلومات",
            program="تكنولوجيا الوسائط المتعددة وتطوير الويب",
            degree="بكالوريوس",
            branches=("أدبي", "شرعي"),
            min_percentage=70,
            source="نشرة الكلية",
            path="doc[1].admission_criteria",
        )
        catalog = self.catalog([it, multimedia])

        result = catalog.resolve(
            "ما معدل قبول برنامج تكنولوجيا المعلومات؟",
            embed=lambda _q: np.array([[1.0], [0.0]], dtype=np.float32),
        )

        self.assertIsNotNone(result)
        self.assertIn("65%", result.answer)
        self.assertNotIn("70%", result.answer)
        self.assertNotIn("العلوم الصحية", result.answer)

    def test_reports_conflicting_branch_sets(self):
        catalog = self.catalog([
            self.fact(("علمي",), "ملف القبول"),
            self.fact(("علمي", "صناعي"), "نشرة الكلية"),
        ])

        result = catalog.resolve(
            "ما معدل قبول برنامج تكنولوجيا المعلومات؟",
            embed=lambda _q: np.array([[1.0], [0.0]], dtype=np.float32),
        )

        self.assertIsNotNone(result)
        self.assertIn("متعارضة", result.answer)
        self.assertIn("المصدر الرسمي الأحدث", result.answer)

    def test_reports_conflicting_rates_for_the_same_branch(self):
        first = self.fact(("علمي",), "ملف القبول")
        second = AdmissionFact(
            faculty=first.faculty,
            program=first.program,
            degree=first.degree,
            branches=first.branches,
            min_percentage=70,
            source="نشرة الكلية",
            path="doc[1]",
        )
        catalog = self.catalog([first, second])

        result = catalog.resolve(
            "ما معدل قبول برنامج تكنولوجيا المعلومات؟",
            embed=lambda _q: np.array([[1.0], [0.0]], dtype=np.float32),
        )

        self.assertIsNotNone(result)
        self.assertIn("متعارضة في النسبة", result.answer)

    def test_explicit_degree_filters_other_study_levels(self):
        bachelor = self.fact()
        master = AdmissionFact(
            faculty=bachelor.faculty,
            program=bachelor.program,
            degree="ماجستير",
            branches=("علمي",),
            min_percentage=75,
            source="الدراسات العليا",
            path="doc[2]",
        )
        catalog = self.catalog([bachelor, master])

        result = catalog.resolve(
            "ما معدل قبول بكالوريوس تكنولوجيا المعلومات؟",
            embed=lambda _q: np.array([[1.0], [0.0]], dtype=np.float32),
        )

        self.assertIsNotNone(result)
        self.assertIn("65%", result.answer)
        self.assertNotIn("75%", result.answer)

    def test_personal_gpa_without_a_catalog_entity_falls_through(self):
        catalog = self.catalog([self.fact()])
        result = catalog.resolve(
            "كم معدلي؟",
            embed=lambda _q: np.array([[1.0], [0.0]], dtype=np.float32),
        )
        self.assertIsNone(result)

    def test_non_admission_numeric_topic_falls_through(self):
        catalog = self.catalog([self.fact()])
        result = catalog.resolve(
            "كم سعر ساعة تكنولوجيا المعلومات؟",
            embed=lambda _q: (_ for _ in ()).throw(AssertionError("embedding should not run")),
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
