import unittest

from app.chunking import (
    build_chunks,
    build_uploaded_chunks,
    doc_to_chunk_texts,
    extract_display_name,
    extract_owner_id,
    flatten_json_to_text,
    is_sensitive_doc,
)


class TestFlatten(unittest.TestCase):

    def test_scalar_dict(self):
        self.assertEqual(flatten_json_to_text({"a": 1, "b": "x"}), ["a: 1", "b: x"])

    def test_nested_dict(self):
        self.assertEqual(flatten_json_to_text({"a": {"b": {"c": 5}}}), ["a.b.c: 5"])

    def test_scalar_list_uses_parent_key(self):
        # Legacy quirk kept on purpose: scalar list items repeat the parent
        # key instead of key[i].
        self.assertEqual(flatten_json_to_text({"a": [1, 2]}), ["a: 1", "a: 2"])

    def test_list_of_dicts_uses_indexed_key(self):
        self.assertEqual(
            flatten_json_to_text({"a": [{"b": 1}, {"b": 2}]}),
            ["a[0].b: 1", "a[1].b: 2"],
        )

    def test_bare_scalar(self):
        self.assertEqual(flatten_json_to_text(7, prefix="x"), ["x: 7"])


class TestIntrospection(unittest.TestCase):

    def test_sensitive_detection(self):
        self.assertTrue(is_sensitive_doc({"privacy": {"allowed_users": ["1"]}}))
        self.assertFalse(is_sensitive_doc({"privacy": {"allowed_users": []}}))
        self.assertFalse(is_sensitive_doc({"privacy": "yes"}))
        self.assertFalse(is_sensitive_doc({}))

    def test_owner_id_priority(self):
        self.assertEqual(extract_owner_id({"student_id": 123, "user_id": 9}), "123")
        self.assertEqual(extract_owner_id({"privacy": {"allowed_users": ["55"]}}), "55")
        self.assertIsNone(extract_owner_id({"x": 1}))
        self.assertEqual(extract_owner_id({"student_id": "", "user_id": 9}), "9")

    def test_display_name_priority(self):
        self.assertEqual(extract_display_name({"student_name": "أحمد", "name": "x"}), "أحمد")
        self.assertEqual(extract_display_name({"title": "عنوان"}), "عنوان")
        self.assertIsNone(extract_display_name({}))


class TestDocToChunks(unittest.TestCase):

    def test_scalars_only(self):
        texts = doc_to_chunk_texts("col", {"a": 1}, False, None)
        self.assertEqual(texts, ["[col]\na: 1"])

    def test_sensitive_header(self):
        texts = doc_to_chunk_texts("col", {"a": 1}, True, "77")
        self.assertEqual(texts, ["[SENSITIVE|collection=col|owner=77]\na: 1"])

    def test_nested_list_splits_per_item(self):
        doc = {"name": "prog", "items": [{"x": 1}, {"x": 2}]}
        texts = doc_to_chunk_texts("col", doc, False, None)
        self.assertEqual(len(texts), 3)  # overview + 2 items
        self.assertEqual(texts[0], "[col]\nname: prog")
        self.assertEqual(texts[1], "[col] :: items\nname: prog\nx: 1")
        self.assertEqual(texts[2], "[col] :: items\nname: prog\nx: 2")

    def test_only_nested_list_has_no_overview(self):
        doc = {"items": [{"x": 1}]}
        texts = doc_to_chunk_texts("col", doc, False, None)
        self.assertEqual(texts, ["[col] :: items\nx: 1"])

    def test_empty_doc_yields_bare_header(self):
        self.assertEqual(doc_to_chunk_texts("col", {}, False, None), ["[col]"])


class TestBuildChunks(unittest.TestCase):

    DATA = {
        "programs": [
            {"_id": "p1", "seeded_at": "t", "name": "هندسة", "fees": 120},
        ],
        "rankings": [
            {"_id": "s1", "student_id": "123", "student_name": "محمد أحمد",
             "gpa": 90, "privacy": {"allowed_users": ["123"]}},
        ],
        "empty": [],
    }

    def test_meta_parallel_and_cleaned(self):
        chunks, meta = build_chunks(self.DATA)
        self.assertEqual(len(chunks), len(meta))
        self.assertEqual(len(chunks), 2)

        prog_meta = meta[0]
        self.assertEqual(prog_meta["collection"], "programs")
        self.assertEqual(prog_meta["doc_id"], "p1")
        self.assertFalse(prog_meta["sensitive"])
        self.assertNotIn("_id", prog_meta["raw"])
        self.assertNotIn("seeded_at", prog_meta["raw"])

        rank_meta = meta[1]
        self.assertTrue(rank_meta["sensitive"])
        self.assertEqual(rank_meta["owner_id"], "123")
        self.assertEqual(rank_meta["display_name"], "محمد أحمد")
        self.assertTrue(chunks[1].startswith("[SENSITIVE|collection=rankings|owner=123]"))

    def test_originals_not_mutated(self):
        build_chunks(self.DATA)
        self.assertIn("_id", self.DATA["programs"][0])
        self.assertIn("seeded_at", self.DATA["programs"][0])


class TestUploadedChunks(unittest.TestCase):

    def test_header_and_cleanup(self):
        docs = [{"_id": "x", "__file_meta__": {"n": 1}, "a": 5}, {}]
        chunks = build_uploaded_chunks(docs, "ملف_تجريبي")
        self.assertEqual(chunks, ["[ملف: ملف_تجريبي]\na: 5"])  # empty doc skipped


if __name__ == "__main__":
    unittest.main()
