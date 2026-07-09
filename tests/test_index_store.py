import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from app import config, index_store


class TestIndexStore(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = patch.object(config, "INDEX_CACHE_DIR", self._tmp.name)
        self._patch.start()
        self.chunks = ["مقطع أول", "مقطع ثاني", "CS202 خوارزميات"]
        self.index = np.arange(6, dtype=np.float32).reshape(3, 2)

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_save_then_load_roundtrips(self):
        index_store.save("kb", self.chunks, self.index, "model-x")
        loaded = index_store.load("kb", self.chunks, "model-x")
        np.testing.assert_array_equal(loaded, self.index)

    def test_changed_chunks_invalidate_cache(self):
        index_store.save("kb", self.chunks, self.index, "model-x")
        self.assertIsNone(index_store.load("kb", self.chunks + ["جديد"], "model-x"))
        self.assertIsNone(index_store.load("kb", ["مختلف", "تماما", "هنا"], "model-x"))

    def test_changed_model_invalidates_cache(self):
        index_store.save("kb", self.chunks, self.index, "model-x")
        self.assertIsNone(index_store.load("kb", self.chunks, "model-y"))

    def test_missing_cache_is_a_miss(self):
        self.assertIsNone(index_store.load("never-saved", self.chunks, "model-x"))

    def test_build_or_load_builds_then_caches(self):
        calls = []

        def build_fn(chunks):
            calls.append(chunks)
            return self.index

        with patch.object(config, "EMBED_MODEL", "model-x"):
            first = index_store.build_or_load("kb", self.chunks, build_fn)
            second = index_store.build_or_load("kb", self.chunks, build_fn)

        np.testing.assert_array_equal(first, self.index)
        np.testing.assert_array_equal(second, self.index)
        self.assertEqual(len(calls), 1)  # second call served from cache

    def test_names_do_not_collide(self):
        other = self.index + 100
        index_store.save("kb", self.chunks, self.index, "m")
        index_store.save("uploaded::file", self.chunks, other, "m")
        np.testing.assert_array_equal(index_store.load("kb", self.chunks, "m"), self.index)
        np.testing.assert_array_equal(index_store.load("uploaded::file", self.chunks, "m"), other)


class FakeIndexCol:
    def __init__(self):
        self.docs = {}

    def find_one(self, query):
        return self.docs.get(query["_id"])

    def update_one(self, query, update, upsert=False):
        self.docs[query["_id"]] = {"_id": query["_id"], **update["$set"]}


class TestMongoBackend(unittest.TestCase):

    def setUp(self):
        self.chunks = ["مقطع أول", "مقطع ثاني", "CS202"]
        self.index = np.arange(6, dtype=np.float32).reshape(3, 2)
        self.col = FakeIndexCol()
        for target, val in (("_index_col", self.col),):
            p = patch.object(index_store, target, return_value=val)
            p.start(); self.addCleanup(p.stop)
        for attr, val in (("INDEX_BACKEND", "mongo"), ("EMBED_MODEL", "m")):
            p = patch.object(config, attr, val)
            p.start(); self.addCleanup(p.stop)

    def test_save_load_roundtrip_via_mongo(self):
        index_store.save("kb", self.chunks, self.index, "m")
        self.assertIn("kb", self.col.docs)                 # stored in the collection
        np.testing.assert_array_equal(index_store.load("kb", self.chunks, "m"), self.index)

    def test_changed_chunks_invalidate(self):
        index_store.save("kb", self.chunks, self.index, "m")
        self.assertIsNone(index_store.load("kb", self.chunks + ["زائد"], "m"))

    def test_build_or_load_builds_once_then_serves_from_mongo(self):
        calls = []

        def build(chunks):
            calls.append(chunks)
            return self.index

        first = index_store.build_or_load("kb", self.chunks, build)
        second = index_store.build_or_load("kb", self.chunks, build)
        np.testing.assert_array_equal(first, self.index)
        np.testing.assert_array_equal(second, self.index)
        self.assertEqual(len(calls), 1)                    # 2nd served from Mongo


if __name__ == "__main__":
    unittest.main()
