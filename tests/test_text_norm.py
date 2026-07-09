import unittest

from app.text_norm import normalize_arabic, tokenize


class TestNormalize(unittest.TestCase):

    def test_folds_alef_variants(self):
        self.assertEqual(normalize_arabic("أإآا"), "اااا")

    def test_folds_ya_ta_marbuta_and_removes_tatweel(self):
        self.assertEqual(normalize_arabic("الادارةى"), "الادارهي")
        self.assertEqual(normalize_arabic("كلـــية"), "كليه")

    def test_strips_diacritics(self):
        self.assertEqual(normalize_arabic("مُحَمَّد"), "محمد")


class TestTokenize(unittest.TestCase):

    def test_keeps_codes_and_numbers(self):
        self.assertEqual(tokenize("رسوم CS202 هي 80 دينار"), ["رسوم", "cs202", "هي", "80", "دينار"])

    def test_variants_tokenize_the_same(self):
        self.assertEqual(tokenize("الإدارة"), tokenize("الاداره"))

    def test_empty(self):
        self.assertEqual(tokenize("   "), [])


if __name__ == "__main__":
    unittest.main()
