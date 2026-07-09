import unittest

import numpy as np

from app.retrieval import rank_chunks


def _q(vec):
    v = np.array(vec, dtype=np.float32)
    return (v / np.linalg.norm(v)).reshape(-1, 1)


class TestRankChunks(unittest.TestCase):

    def setUp(self):
        rows = np.array([[1, 0], [0, 1], [0.7, 0.7]], dtype=np.float32)
        self.index = rows / np.linalg.norm(rows, axis=1, keepdims=True)
        self.chunks = ["c_x", "c_y", "c_diag"]

    def test_orders_by_similarity(self):
        results = rank_chunks(_q([1, 0]), self.chunks, self.index, top_k=3, threshold=0.0)
        self.assertEqual(results[0], "c_x")
        self.assertEqual(results[1], "c_diag")

    def test_threshold_filters(self):
        results = rank_chunks(_q([1, 0]), self.chunks, self.index, top_k=3, threshold=0.5)
        self.assertEqual(results, ["c_x", "c_diag"])  # c_y score 0 < 0.5

    def test_top_k_limits(self):
        results = rank_chunks(_q([1, 0]), self.chunks, self.index, top_k=1, threshold=0.0)
        self.assertEqual(results, ["c_x"])

    def test_fallback_to_best_when_all_below_threshold(self):
        results = rank_chunks(_q([1, 0]), self.chunks, self.index, top_k=3, threshold=0.999)
        self.assertEqual(results, ["c_x"])

    def test_empty_inputs(self):
        self.assertEqual(rank_chunks(_q([1, 0]), [], None, 3, 0.0), [])
        self.assertEqual(rank_chunks(_q([1, 0]), [], np.array([], dtype=np.float32), 3, 0.0), [])
        self.assertEqual(rank_chunks(_q([1, 0]), ["a"], np.array([], dtype=np.float32), 3, 0.0), [])


if __name__ == "__main__":
    unittest.main()
