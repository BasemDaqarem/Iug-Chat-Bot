"""
Data-isolation tests for the access-filtered retrieval layer.

The guarantee under test: another student's sensitive record is never a
retrieval candidate for a session that does not own it — it is masked out
BEFORE ranking, so it can neither be returned nor leaked into the LLM context.
"""

import copy
import unittest
from unittest.mock import patch

from app import chunking, config, embeddings
from app.chatbot import IUGChatbot
from tests.test_equivalence import FIXTURE_DATA, fake_embed

# Identifiers of student 67890 (سالم) — must never surface for student 12345.
OTHER_STUDENT_MARKERS = ["سالم يوسف", "67890", "70.1"]


class TestRetrievalIsolation(unittest.TestCase):

    def setUp(self):
        embeddings.reset_query_cache()  # isolate the module-level query cache
        self.bot = IUGChatbot()
        data = copy.deepcopy(FIXTURE_DATA)
        self.bot._kb._data = data
        self.bot._kb._chunks, self.bot._kb._chunk_meta = chunking.build_chunks(data)
        with patch("app.embeddings.embed_texts", side_effect=fake_embed):
            self.bot._kb._index = embeddings.build_index(self.bot._kb._chunks)

    def _search(self, question, session_id):
        with patch("app.embeddings.embed_texts", side_effect=fake_embed):
            # ask for everything so nothing is trimmed by top_k / threshold
            return self.bot._kb.search_for(question, session_id, top_k=99, threshold=-1.0)

    def test_owner_can_retrieve_own_record(self):
        results = self._search("محمد", "12345")
        self.assertTrue(any("محمد أحمد خالد" in c for c in results))

    def test_other_students_record_never_retrieved(self):
        # Even a query that names the other student returns nothing of theirs.
        for question in ("سالم", "معدل 70.1", "بيانات الطالب سالم يوسف حسن"):
            results = self._search(question, "12345")
            blob = "\n".join(results)
            for marker in OTHER_STUDENT_MARKERS:
                self.assertNotIn(marker, blob, f"leaked {marker!r} for query {question!r}")

    def test_guest_sees_no_sensitive_record(self):
        results = self._search("طالب", "guest")
        blob = "\n".join(results)
        self.assertNotIn("محمد أحمد خالد", blob)
        for marker in OTHER_STUDENT_MARKERS:
            self.assertNotIn(marker, blob)

    def test_chat_context_excludes_other_student(self):
        calls = []

        def fake_groq(headers, payload):
            calls.append(payload)
            return "ok"

        with patch.object(config, "CHAT_API_KEY", "k"), \
             patch("app.embeddings.embed_texts", side_effect=fake_embed), \
             patch("app.llm._post_with_retry", side_effect=fake_groq):
            self.bot.chat("ما هي التخصصات المتاحة؟", "12345")

        system = calls[-1]["messages"][0]["content"]
        for marker in OTHER_STUDENT_MARKERS:
            self.assertNotIn(marker, system)


if __name__ == "__main__":
    unittest.main()
