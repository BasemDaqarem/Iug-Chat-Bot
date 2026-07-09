import unittest

import numpy as np

from app.retrieval import hybrid_rank, rrf_order


class TestRRFOrder(unittest.TestCase):

    def test_agreeing_rankers_keep_top(self):
        dense = np.array([0.9, 0.1, 0.5])
        lexical = np.array([5.0, 0.0, 1.0])
        self.assertEqual(rrf_order(dense, lexical)[0], 0)

    def test_lexical_rescues_a_dense_miss(self):
        # doc 2 has a mediocre dense score but a strong exact lexical match;
        # fusion should lift it above doc 1, which nothing supports.
        dense = np.array([0.9, 0.4, 0.35])
        lexical = np.array([0.0, 0.0, 9.0])
        order = rrf_order(dense, lexical)
        self.assertLess(order.index(2), order.index(1))

    def test_zero_lexical_never_contributes(self):
        # with all-zero lexical scores, order is pure dense
        dense = np.array([0.2, 0.8, 0.5])
        lexical = np.zeros(3)
        self.assertEqual(rrf_order(dense, lexical), [1, 2, 0])


class TestHybridRank(unittest.TestCase):

    def setUp(self):
        self.chunks = ["a", "b", "c"]

    def test_keeps_confident_by_dense_threshold(self):
        dense = np.array([0.9, 0.05, 0.8])
        lexical = np.zeros(3)
        out = hybrid_rank(self.chunks, dense, lexical, top_k=3, threshold=0.5)
        self.assertEqual(set(out), {"a", "c"})  # b below threshold, no lexical

    def test_keeps_lexical_match_below_threshold(self):
        dense = np.array([0.9, 0.05, 0.1])
        lexical = np.array([0.0, 3.0, 0.0])  # b rescued by lexical
        out = hybrid_rank(self.chunks, dense, lexical, top_k=3, threshold=0.5)
        self.assertIn("b", out)

    def test_fallback_to_best_when_nothing_confident(self):
        dense = np.array([0.1, 0.2, 0.15])
        lexical = np.zeros(3)
        out = hybrid_rank(self.chunks, dense, lexical, top_k=3, threshold=0.9)
        self.assertEqual(out, ["b"])  # single best fused candidate

    def test_empty(self):
        self.assertEqual(hybrid_rank([], np.array([]), np.array([]), 3, 0.5), [])


if __name__ == "__main__":
    unittest.main()
