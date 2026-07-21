"""
Caching tests — the unit behavior of TTLCache AND (most importantly) the
privacy gate: a PUBLIC answer is cached and reused, but a response built from
a student's private record is NEVER cached and is always regenerated.
"""

import copy
import unittest
from unittest.mock import patch

from app import chunking, config, embeddings
from app.cache import TTLCache
from app.chatbot import IUGChatbot
from app.sessions import SessionStore
from tests.test_equivalence import FIXTURE_DATA, UPLOADED_DOCS, fake_embed


# ═════════════════════════════════════════════════════════════════════════
#  Unit: TTLCache
# ═════════════════════════════════════════════════════════════════════════


class TestTTLCache(unittest.TestCase):

    def test_set_get_hit_and_miss(self):
        c = TTLCache("t", maxsize=10, ttl=100)
        self.assertIsNone(c.get("a"))            # miss
        c.set("a", 1)
        self.assertEqual(c.get("a"), 1)          # hit
        s = c.stats()
        self.assertEqual((s["hits"], s["misses"]), (1, 1))
        self.assertEqual(s["hit_rate"], 0.5)

    def test_lru_eviction(self):
        c = TTLCache("t", maxsize=2, ttl=100)
        c.set("a", 1); c.set("b", 2)
        c.get("a")                # 'a' now most-recently-used
        c.set("c", 3)             # evicts least-recently-used → 'b'
        self.assertEqual(c.get("a"), 1)
        self.assertIsNone(c.get("b"))
        self.assertEqual(c.get("c"), 3)
        self.assertEqual(c.stats()["evictions"], 1)

    def test_ttl_expiry(self):
        c = TTLCache("t", maxsize=10, ttl=100)
        with patch("app.cache.time.monotonic") as clock:
            clock.return_value = 1_000.0
            c.set("a", 1)
            self.assertEqual(c.get("a"), 1)      # still fresh (t=1000)
            clock.return_value = 1_200.0         # 200s later, ttl=100 → expired
            self.assertIsNone(c.get("a"))
        self.assertEqual(c.stats()["expirations"], 1)

    def test_clear(self):
        c = TTLCache("t", maxsize=10, ttl=100)
        c.set("a", 1)
        c.clear()
        self.assertIsNone(c.get("a"))
        self.assertEqual(c.stats()["size"], 0)


# ═════════════════════════════════════════════════════════════════════════
#  Integration: privacy-gated answer cache
# ═════════════════════════════════════════════════════════════════════════


class CacheBotBase(unittest.TestCase):

    def setUp(self):
        embeddings.reset_query_cache()
        self.llm_calls = 0
        self.bot = IUGChatbot(sessions=SessionStore())

        data = copy.deepcopy(FIXTURE_DATA)
        self.bot._kb._data = data
        self.bot._kb._chunks, self.bot._kb._chunk_meta = chunking.build_chunks(data)
        with patch("app.embeddings.embed_texts", side_effect=fake_embed):
            self.bot._kb._index = embeddings.build_index(self.bot._kb._chunks)

        nchunks = chunking.build_uploaded_chunks(copy.deepcopy(UPLOADED_DOCS), "ملف_علامات")
        self.bot._uploaded._chunks["ملف_علامات"] = nchunks
        with patch("app.embeddings.embed_texts", side_effect=fake_embed):
            self.bot._uploaded._indexes["ملف_علامات"] = embeddings.build_index(nchunks)

    def _chat(self, method, *args):
        def fake_groq(headers, payload):
            self.llm_calls += 1
            return "إجابة عامة"

        with patch.object(config, "CHAT_API_KEY", "k"), \
             patch("app.embeddings.embed_texts", side_effect=fake_embed), \
             patch("app.llm._post_with_retry", side_effect=fake_groq):
            return getattr(self.bot, method)(*args)


class TestPublicAnswerCache(CacheBotBase):

    def test_public_question_always_reaches_llm(self):
        first = self._chat("chat", "ما هي رسوم هندسة الحاسوب؟", "guest_A")
        second = self._chat("chat", "ما هي رسوم هندسة الحاسوب؟", "guest_B")
        self.assertEqual(self.llm_calls, 2)
        self.assertTrue(first["retrieval_metadata"]["answer_cache_bypassed"])
        self.assertTrue(second["retrieval_metadata"]["answer_cache_bypassed"])
        self.assertEqual(self.bot.cache_stats()["public_answers"]["size"], 0)

    def test_all_files_question_always_reaches_llm(self):
        self._chat("chat_with_all_files", "كم علامة الرياضيات؟", "gA")
        self._chat("chat_with_all_files", "كم علامة الرياضيات؟", "gB")
        self.assertEqual(self.llm_calls, 2)

    def test_content_change_does_not_enable_answer_cache(self):
        self._chat("chat_with_all_files", "كم علامة الرياضيات؟", "gA")
        with patch("app.embeddings.embed_texts", side_effect=fake_embed):
            self.bot._uploaded._chunks["ملف_جديد"] = chunking.build_uploaded_chunks(
                [{"course": "كيمياء", "grade": 70}], "ملف_جديد")
            self.bot._answer_cache.clear()
        self._chat("chat_with_all_files", "كم علامة الرياضيات؟", "gC")
        self.assertEqual(self.llm_calls, 2)
        self.assertEqual(self.bot._answer_cache.stats()["size"], 0)


class TestPrivateNeverCached(CacheBotBase):

    def test_student_record_answer_is_never_cached(self):
        # Student 12345 owns a sensitive record → private turn → must hit the
        # LLM every time and must NOT read/write the shared cache.
        self._chat("chat", "متى يبدأ التسجيل؟", "12345")
        self._chat("chat", "متى يبدأ التسجيل؟", "12345")
        self.assertEqual(self.llm_calls, 2)                       # never cached
        self.assertEqual(self.bot._answer_cache.stats()["size"], 0)

    def test_owner_does_not_receive_a_guests_cached_answer(self):
        # A guest caches a public answer for a question...
        self._chat("chat", "متى يبدأ التسجيل؟", "guest")
        self.assertEqual(self.llm_calls, 1)
        # ...the owning student asking the SAME question must still be served
        # in real time (their turn includes their private context).
        self._chat("chat", "متى يبدأ التسجيل؟", "12345")
        self.assertEqual(self.llm_calls, 2)

    def test_followup_turn_is_not_cached(self):
        # First turn (public) is cached; a second turn in the SAME session has
        # history, so it is regenerated, not served from cache.
        self._chat("chat", "ما هي رسوم هندسة الحاسوب؟", "s1")
        self._chat("chat", "ما هي رسوم هندسة الحاسوب؟", "s1")  # now has history
        self.assertEqual(self.llm_calls, 2)


class TestQueryEmbeddingCache(CacheBotBase):

    def test_repeated_question_reuses_embedding(self):
        embeddings.reset_query_cache()
        calls = []

        def counting_embed(texts):
            calls.append(texts)
            return fake_embed(texts)

        with patch("app.embeddings.embed_texts", side_effect=counting_embed):
            embeddings.embed_query("سؤال متكرر")
            embeddings.embed_query("سؤال متكرر")

        self.assertEqual(len(calls), 1)  # second call served from cache


if __name__ == "__main__":
    unittest.main()
