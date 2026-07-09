import unittest

from app.lexical import BM25


class TestBM25(unittest.TestCase):

    def setUp(self):
        self.docs = [
            "رسوم كلية الهندسة 30 دينار للساعة",
            "رسوم كلية العلوم 20 دينار للساعة",
            "مواعيد التسجيل تبدأ يوم الأحد",
        ]
        self.bm25 = BM25(self.docs)

    def test_exact_term_ranks_its_doc_first(self):
        scores = self.bm25.scores("رسوم الهندسة")
        self.assertEqual(int(scores.argmax()), 0)

    def test_orthographic_variant_still_matches(self):
        # query uses a different alef/ta-marbuta form than the corpus
        scores = self.bm25.scores("كليه الهندسه")
        self.assertGreater(scores[0], 0)

    def test_unknown_term_scores_zero_everywhere(self):
        scores = self.bm25.scores("زيمبابوي")
        self.assertEqual(scores.sum(), 0)

    def test_empty_corpus(self):
        self.assertEqual(BM25([]).scores("أي شيء").tolist(), [])


if __name__ == "__main__":
    unittest.main()
