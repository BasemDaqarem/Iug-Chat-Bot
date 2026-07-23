# -*- coding: utf-8 -*-
"""كبح معدل التضمين: الميزانية بالدقيقة + إعادة المحاولة عند 429.

الخلفية الحية: ملف academic_programs (520 مقطعاً هرمياً ≈ مليون توكن) فشل
نشره حتمياً على حدّ Jina المجاني (100 ألف توكن/دقيقة) لأن build_index كان
يرسل الدفعات متتالية بلا كبح ولا إعادة محاولة.
"""

import unittest
from unittest.mock import patch

import numpy as np

from app import config, embeddings
from app.errors import UpstreamServiceError


def _fake_vectors(texts):
    return np.ones((len(texts), 4), dtype=np.float32)


class TestEmbedThrottle(unittest.TestCase):

    def test_budget_pauses_between_windows(self):
        chunks = ["م" * 1000] * 6            # كل دفعة (حجم 2) ≈ 1600 توكن مقدّر
        sleeps = []
        with patch.object(config, "EMBED_BATCH_SIZE", 2), \
             patch.object(config, "EMBED_TPM_BUDGET", 2000), \
             patch("app.embeddings.embed_texts", side_effect=_fake_vectors), \
             patch("app.embeddings.time.sleep", side_effect=sleeps.append):
            index = embeddings.build_index(chunks)
        self.assertEqual(index.shape, (6, 4))
        # الدفعة الثانية والثالثة تتجاوزان الميزانية → توقفان للنافذة التالية
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(0 < s <= 62 for s in sleeps))

    def test_zero_budget_disables_pacing(self):
        chunks = ["م" * 1000] * 6
        sleeps = []
        with patch.object(config, "EMBED_BATCH_SIZE", 2), \
             patch.object(config, "EMBED_TPM_BUDGET", 0), \
             patch("app.embeddings.embed_texts", side_effect=_fake_vectors), \
             patch("app.embeddings.time.sleep", side_effect=sleeps.append):
            embeddings.build_index(chunks)
        self.assertEqual(sleeps, [])

    def test_429_retries_batch_then_succeeds(self):
        calls = {"n": 0}

        def flaky(texts):
            calls["n"] += 1
            if calls["n"] == 1:
                raise UpstreamServiceError(
                    "rate limited", details={"provider": "jina", "status": 429}
                )
            return _fake_vectors(texts)

        sleeps = []
        with patch.object(config, "EMBED_BATCH_SIZE", 8), \
             patch("app.embeddings.embed_texts", side_effect=flaky), \
             patch("app.embeddings.time.sleep", side_effect=sleeps.append):
            index = embeddings.build_index(["نص"] * 3)
        self.assertEqual(index.shape[0], 3)
        self.assertIn(65, sleeps)            # انتظر نافذة كاملة قبل الإعادة
        self.assertEqual(calls["n"], 2)

    def test_non_429_error_propagates_immediately(self):
        def boom(texts):
            raise UpstreamServiceError(
                "server down", details={"provider": "jina", "status": 500}
            )

        with patch.object(config, "EMBED_BATCH_SIZE", 8), \
             patch("app.embeddings.embed_texts", side_effect=boom), \
             patch("app.embeddings.time.sleep") as slept:
            with self.assertRaises(UpstreamServiceError):
                embeddings.build_index(["نص"] * 3)
        slept.assert_not_called()

    def test_persistent_429_raises_after_three_attempts(self):
        calls = {"n": 0}

        def always_limited(texts):
            calls["n"] += 1
            raise UpstreamServiceError(
                "rate limited", details={"provider": "jina", "status": 429}
            )

        with patch.object(config, "EMBED_BATCH_SIZE", 8), \
             patch("app.embeddings.embed_texts", side_effect=always_limited), \
             patch("app.embeddings.time.sleep"):
            with self.assertRaises(UpstreamServiceError):
                embeddings.build_index(["نص"] * 3)
        self.assertEqual(calls["n"], 3)


if __name__ == "__main__":
    unittest.main()
