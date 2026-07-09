"""
Corpus-equivalence tests: prove the refactored app/ package still builds the
EXACT same chunks, metadata, embeddings index, dense ranking, and history
formatting as the original monolith (tests/legacy_chatbot_core.py is a
byte-for-byte copy of the pre-refactor chatbot_core.py).

This guarantees the persistence cache and the hybrid retrieval layer are built
on an unchanged, faithful corpus. The chat *behavior* has intentionally moved
beyond the monolith (access-filtered hybrid retrieval) and is covered by
test_chat.py / test_security.py, not here.
"""

import copy
import unittest
from unittest.mock import patch

import numpy as np

import tests.legacy_chatbot_core as legacy
from app import chunking, embeddings
from app.chatbot import IUGChatbot as NewBot
from app.retrieval import rank_chunks
from app.sessions import SessionStore

FIXTURE_DATA = {
    "programs": [
        {"_id": "p1", "seeded_at": "2026-01-01", "name": "هندسة الحاسوب", "fees": 120,
         "courses": [{"code": "CS101", "title": "برمجة 1"}, {"code": "CS202", "title": "خوارزميات"}],
         "tags": ["nmr", "acc"]},
        {"_id": "p2", "info": {"dean": "د. أحمد", "office": {"room": 5, "floor": 2}}},
    ],
    "students_rankings": [
        {"_id": "s1", "student_id": "12345", "student_name": "محمد أحمد خالد",
         "gpa": 88.5, "rank": 3, "privacy": {"allowed_users": ["12345"]}},
        {"_id": "s2", "student_id": "67890", "student_name": "سالم يوسف حسن",
         "gpa": 70.1, "rank": 40, "privacy": {"allowed_users": ["67890"]}},
    ],
    "announcements": [
        {"_id": "a1", "title": "بدء التسجيل", "body": "يبدأ التسجيل يوم الأحد"},
    ],
    "empty_col": [],
}

UPLOADED_DOCS = [
    {"_id": "u1", "course": "رياضيات", "grade": 95},
    {"_id": "u2", "course": "فيزياء", "grade": 80},
]


def fake_embed(texts):
    """Deterministic pseudo-embedding — same text always gives same vector."""
    out = []
    for t in texts:
        v = np.zeros(8, dtype=np.float32)
        for i, ch in enumerate(t):
            v[i % 8] += (ord(ch) % 97) / 97.0
        out.append(v)
    return np.array(out, dtype=np.float32)


class TestCorpusEquivalence(unittest.TestCase):
    """One legacy bot and one new bot over identical fixture data, with
    embeddings faked identically for both."""

    def setUp(self):
        self.lbot = legacy.IUGChatbot()
        ldata = copy.deepcopy(FIXTURE_DATA)
        self.lbot._data = ldata
        self.lbot._chunks, self.lbot._chunk_meta = self.lbot._build_chunks(ldata)
        with patch.object(legacy.IUGChatbot, "_jina_embed", staticmethod(fake_embed)):
            self.lbot._index = legacy.IUGChatbot._build_index(self.lbot._chunks)

        self.nbot = NewBot()
        ndata = copy.deepcopy(FIXTURE_DATA)
        self.nbot._kb._data = ndata
        self.nbot._kb._chunks, self.nbot._kb._chunk_meta = chunking.build_chunks(ndata)
        with patch("app.embeddings.embed_texts", side_effect=fake_embed):
            self.nbot._kb._index = embeddings.build_index(self.nbot._kb._chunks)

    def test_chunks_and_meta_identical(self):
        self.assertEqual(self.lbot._chunks, self.nbot._kb._chunks)
        self.assertEqual(self.lbot._chunk_meta, self.nbot._kb._chunk_meta)

    def test_index_identical(self):
        np.testing.assert_array_equal(self.lbot._index, self.nbot._kb._index)

    def test_uploaded_chunks_identical(self):
        ldocs = copy.deepcopy(UPLOADED_DOCS)
        ndocs = copy.deepcopy(UPLOADED_DOCS)
        self.assertEqual(
            legacy.IUGChatbot._build_uploaded_chunks(ldocs, "ملف_علامات"),
            chunking.build_uploaded_chunks(ndocs, "ملف_علامات"),
        )

    def test_dense_ranking_identical(self):
        q = fake_embed(["سؤال عن الرسوم"])
        q = (q / np.linalg.norm(q)).T
        legacy_ranked = legacy.IUGChatbot._rank_chunks(
            q, self.lbot._chunks, self.lbot._index, 5, 0.1)
        new_ranked = rank_chunks(q, self.nbot._kb._chunks, self.nbot._kb._index, 5, 0.1)
        self.assertEqual(legacy_ranked, new_ranked)

    def test_history_formatting_identical(self):
        for history in ([], [{"user": f"q{i}", "assistant": f"a{i}"} for i in range(9)]):
            self.assertEqual(
                legacy.IUGChatbot.fmt_history(history),
                SessionStore.format_for_prompt(history),
            )


if __name__ == "__main__":
    unittest.main()
