"""
Compact Okapi-BM25 lexical scorer (no external dependency).

Complements the dense embedding index: BM25 rewards exact term overlap, so
queries that hinge on a specific number / code / name ("رسوم 80 دينار",
"CS202") retrieve the right chunk even when embedding similarity is lukewarm.
Corpora here are small (hundreds of chunks), so the straightforward scan is
more than fast enough and stays readable.
"""

import math
from typing import List

import numpy as np

from app import config
from app.text_norm import tokenize


class BM25:

    def __init__(self, docs: List[str], k1: float = None, b: float = None):
        self.k1 = config.BM25_K1 if k1 is None else k1
        self.b = config.BM25_B if b is None else b

        self._tokens = [tokenize(d) for d in docs]
        self.n = len(self._tokens)
        self._doc_len = np.array([len(t) for t in self._tokens], dtype=np.float32)
        self._avgdl = float(self._doc_len.mean()) if self.n else 0.0

        # term frequency per doc + document frequency per term
        self._tf: List[dict] = []
        df: dict = {}
        for toks in self._tokens:
            counts: dict = {}
            for w in toks:
                counts[w] = counts.get(w, 0) + 1
            self._tf.append(counts)
            for w in counts:
                df[w] = df.get(w, 0) + 1

        self._idf = {
            w: math.log(1 + (self.n - f + 0.5) / (f + 0.5))
            for w, f in df.items()
        }

    def scores(self, query: str) -> np.ndarray:
        """BM25 score of `query` against every document (same order as `docs`)."""
        out = np.zeros(self.n, dtype=np.float32)
        if self.n == 0 or self._avgdl == 0:
            return out
        for w in set(tokenize(query)):
            idf = self._idf.get(w)
            if idf is None:
                continue
            for i, tf in enumerate(self._tf):
                f = tf.get(w, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self._doc_len[i] / self._avgdl)
                out[i] += idf * (f * (self.k1 + 1)) / denom
        return out
