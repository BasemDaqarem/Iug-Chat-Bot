# -*- coding: utf-8 -*-
"""
اختبارات مسار iug_kb_v2: التحقق، فلترة الحالة، نصا الاسترجاع والدليل،
الـmetadata المسطحة، توافق كتالوج القبول، فك بوابة اسم الملف عن المستخلص،
وقراءة مرحلة المقطع من metadata السجل بدل اسم الملف.
"""

import copy
import json
import os
import unittest
from unittest.mock import patch

import numpy as np

from app import chunking, config, embeddings, kb_v2
from app.admissions import extract_admission_facts
from app.chatbot import IUGChatbot
from app.sessions import SessionStore
from app.uploaded_files import UploadedFilesStore
from tests.test_equivalence import FIXTURE_DATA, fake_embed

REAL_KB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    ))),
    "IUG_knowledge_clean_iug_kb_v2", "canonical",
)


def _record(**overrides) -> dict:
    """سجل iug_kb_v2 صالح افتراضياً — تُخصَّص حقوله بالوسائط."""
    base = {
        "schema_version": "iug_kb_v2",
        "record_id": "academic_program_test01_v1",
        "canonical_id": "academic_program_test01",
        "record_type": "academic_program",
        "domain": "academic_programs",
        "subdomain": "bachelor",
        "title": "هندسة الحاسوب — الهندسة — بكالوريوس",
        "language": "ar",
        "institution": "الجامعة الإسلامية بغزة",
        "scope": {"degree_level": "bachelor", "faculty": "الهندسة",
                  "program": "هندسة الحاسوب", "campus": "main"},
        "data": {
            "program_name": "هندسة الحاسوب",
            "faculty_name": "الهندسة",
            "degree_level": "bachelor",
            "credit_hour_fee": 30,
            "currency": "JOD",
            "admission_criteria": {
                "min_high_school_percentage": 80,
                "allowed_high_school_branches": ["علمي"],
            },
        },
        "conditions": [],
        "answer_text": "برنامج هندسة الحاسوب ضمن كلية الهندسة. رسوم الساعة 30 ديناراً.",
        "notes": [],
        "retrieval": {
            "example_queries": ["كم معدل قبول هندسة الحاسوب؟"],
            "aliases": ["حاسوب"],
            "keywords": ["هندسة الحاسوب", "معدل القبول"],
            "ambiguous_queries": ["سؤال غامض لا يدخل نص البحث"],
            "clarification_question": "أي مرحلة تقصد؟",
            "contextual_text": "معلومة تخص برنامج هندسة الحاسوب في كلية الهندسة.",
        },
        "contact": {},
        "sources": [{"source_type": "official_url",
                     "source_title": "دليل القبول الرسمي",
                     "source_url": None, "authority": "official"}],
        "validity": {"status": "active", "verified_at": None,
                     "effective_from": None, "effective_to": None},
        "governance": {"version": 1, "is_canonical": True,
                       "access_scope": "public", "source_priority": 90,
                       "verification_status": "verified",
                       "source_files": [], "content_hash": "x"},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


def _variant(suffix: str, **overrides) -> dict:
    rec = _record(**overrides)
    rec["record_id"] = f"rec_{suffix}_v1"
    rec["canonical_id"] = f"rec_{suffix}"
    return rec


class FakeCollection:
    """بديل Mongo في الذاكرة يكفي مسار upload_json/load_one."""

    def __init__(self):
        self.docs: list[dict] = []

    def find(self, _query=None):
        return [copy.deepcopy(d) for d in self.docs]

    def drop(self):
        self.docs = []

    def insert_many(self, docs):
        for i, doc in enumerate(docs):
            stored = copy.deepcopy(doc)
            stored.setdefault("_id", f"id{i}")
            self.docs.append(stored)


class KbV2StoreBase(unittest.TestCase):
    """قاعدة مشتركة: متجر ملفات على Mongo مزيف + embeddings حتمية."""

    def setUp(self):
        embeddings.reset_query_cache()
        self.store = UploadedFilesStore()
        self.fake_col = FakeCollection()
        self.patches = [
            patch("app.uploaded_files.get_uploaded_collection",
                  return_value=self.fake_col),
            patch("app.uploaded_files.drop_uploaded_collection"),
            patch("app.uploaded_files.index_store.build_or_load",
                  side_effect=lambda name, texts, build: build(texts)),
            patch("app.uploaded_files.index_store.delete"),
            patch("app.embeddings.embed_texts", side_effect=fake_embed),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)


# ═══ 1-4: القبول والرفض والفلترة ═══════════════════════════════════════════

class TestValidationAndFiltering(KbV2StoreBase):

    def test_valid_v2_file_is_accepted_and_indexed(self):
        result = self.store.upload_json("academic_programs", [_record()])
        self.assertEqual(result["inserted"], 1)
        chunks = self.store.chunks_of("academic_programs")
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].startswith("[ملف: academic_programs]"))

    def test_missing_required_field_rejects_file_with_clear_message(self):
        bad = _record()
        del bad["answer_text"]
        with self.assertRaises(ValueError) as ctx:
            self.store.upload_json("academic_programs", [_record(), bad])
        message = str(ctx.exception)
        self.assertIn("السجل 2", message)
        self.assertIn("answer_text", message)
        # الرفض قبل أي لمس للتخزين — لا جيل منشوراً
        self.assertFalse(self.store.has("academic_programs"))

    def test_active_indexed_draft_and_expired_not(self):
        docs = [
            _variant("active"),
            _variant("draft", validity={"status": "draft", "verified_at": None,
                                        "effective_from": None, "effective_to": None}),
            _variant("expired", validity={"status": "expired", "verified_at": None,
                                          "effective_from": None, "effective_to": None}),
            _variant("out_of_window", validity={
                "status": "active", "verified_at": None,
                "effective_from": None, "effective_to": "2000-01-01"}),
        ]
        self.store.upload_json("academic_programs", docs)
        chunks = self.store.chunks_of("academic_programs")
        self.assertEqual(len(chunks), 1)
        self.assertIn("rec_active", " ".join(
            m.get("record_id", "") for m in self.store._v2_meta["academic_programs"]
        ))
        # السجلات كلها بقيت مخزنة في Mongo (جزء من الملف) رغم عدم فهرستها
        self.assertEqual(len(self.fake_col.docs), 4)

    def test_verified_at_null_is_accepted(self):
        rec = _record(validity={"status": "active", "verified_at": None,
                                "effective_from": None, "effective_to": None})
        result = self.store.upload_json("academic_programs", [rec])
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(len(self.store.chunks_of("academic_programs")), 1)

    def test_all_draft_file_is_rejected_not_half_indexed(self):
        rec = _variant("d1", validity={"status": "draft", "verified_at": None,
                                       "effective_from": None, "effective_to": None})
        with self.assertRaises(RuntimeError):
            self.store.upload_json("contacts", [rec])
        self.assertFalse(self.store.has("contacts"))


# ═══ 5-7: نصا الاسترجاع والدليل والـmetadata ═══════════════════════════════

class TestProjectionTexts(unittest.TestCase):

    def test_retrieval_text_has_search_bait_but_no_ambiguity_fields(self):
        text = kb_v2.build_retrieval_text(_record(), "academic_programs")
        self.assertIn("كم معدل قبول هندسة الحاسوب؟", text)   # example_queries
        self.assertIn("حاسوب", text)                          # aliases
        self.assertIn("معلومة تخص برنامج هندسة الحاسوب", text)  # contextual
        self.assertNotIn("سؤال غامض لا يدخل نص البحث", text)  # ambiguous_queries
        self.assertNotIn("أي مرحلة تقصد؟", text)              # clarification

    def test_evidence_text_has_answer_scope_and_source(self):
        text = kb_v2.build_evidence_text(_record(), "academic_programs")
        self.assertTrue(text.startswith("[ملف: academic_programs]"))
        self.assertIn("برنامج هندسة الحاسوب ضمن كلية الهندسة", text)  # الإجابة
        self.assertIn("النطاق:", text)
        self.assertIn("الهندسة", text)
        self.assertIn("المصدر: دليل القبول الرسمي", text)
        # الدليل نظيف من حشو البحث
        self.assertNotIn("كم معدل قبول هندسة الحاسوب؟", text)

    def test_metadata_is_flat_scalars_only(self):
        meta = kb_v2.flat_metadata(_record())
        self.assertEqual(meta["canonical_id"], "academic_program_test01")
        self.assertEqual(meta["degree_level"], "bachelor")
        self.assertEqual(meta["faculty"], "الهندسة")
        self.assertEqual(meta["status"], "active")
        self.assertEqual(meta["version"], 1)
        for value in meta.values():
            self.assertNotIsInstance(value, (dict, list))

    def test_degree_level_mapping_master_doctorate(self):
        master = _record(scope={"degree_level": "master"},
                         data={"degree_level": "master"})
        doctorate = _record(scope={"degree_level": "doctorate"},
                            data={"degree_level": "doctorate"})
        self.assertEqual(kb_v2.flat_metadata(master)["degree_level"], "masters")
        self.assertEqual(kb_v2.flat_metadata(doctorate)["degree_level"], "phd")

    def test_chunk_id_format(self):
        projection = kb_v2.build_projection([_record()], "academic_programs")
        self.assertEqual(projection[0].id, "academic_program_test01#v1#chunk0")


# ═══ 8-9: المساران القديم والجديد، والبحث بنص البحث والدليل للإجابة ═══════

class TestSearchUsesRetrievalTextAndReturnsEvidence(KbV2StoreBase):

    def test_legacy_files_still_use_legacy_path(self):
        legacy_docs = [{"course": "رياضيات", "grade": 95}]
        self.store.upload_json("ملف_علامات", legacy_docs)
        chunks = self.store.chunks_of("ملف_علامات")
        self.assertEqual(len(chunks), 1)
        # المسار القديم لا يمر بإسقاط v2 ولا يملك metadata له
        self.assertNotIn("ملف_علامات", self.store._v2_meta)
        self.assertIsNone(self.store.degree_level_of(chunks[0]))

    def test_search_ranks_by_retrieval_text_and_returns_evidence_text(self):
        # طُعم البحث (كلمة مميزة في example_queries فقط) غائب عن الدليل —
        # العثور على السجل بها يثبت أن الترتيب جرى على retrieval_text،
        # والنص العائد هو evidence_text النظيف.
        bait = "استعلام_فريد_للاصطياد"
        rec = _record(retrieval={
            "example_queries": [f"سؤال فيه {bait} لا يشبه الدليل"],
            "aliases": [], "keywords": [],
            "ambiguous_queries": [], "clarification_question": None,
            "contextual_text": "سياق عادي.",
        })
        self.store.upload_json("academic_programs", [rec])
        with patch("app.embeddings.embed_texts", side_effect=fake_embed):
            results = self.store.search_all(bait, top_k=3, threshold=-10.0)
        self.assertTrue(results)
        self.assertNotIn(bait, results[0])            # الدليل بلا الحشو
        self.assertIn("برنامج هندسة الحاسوب", results[0])  # الإجابة النظيفة

    def test_dedupe_by_canonical_id_keeps_top_chunk_only(self):
        rec = _record()
        self.store.upload_json("ملف_اول", [rec])
        second = FakeCollection()
        with patch("app.uploaded_files.get_uploaded_collection",
                   return_value=second):
            self.store.upload_json("ملف_ثان", [copy.deepcopy(rec)])
        with patch("app.embeddings.embed_texts", side_effect=fake_embed):
            results = self.store.search_all(
                "هندسة الحاسوب", top_k=6, threshold=-10.0
            )
        canonical_hits = [r for r in results if "هندسة الحاسوب" in r]
        self.assertEqual(len(canonical_hits), 1)   # سجل واحد رغم ملفين


# ═══ 10: الحذف والتحديث لا يتركان فهارس قديمة ═════════════════════════════

class TestDeleteAndUpdateLeaveNoOrphans(KbV2StoreBase):

    def test_delete_purges_all_index_versions_and_v2_state(self):
        self.store.upload_json("academic_programs", [_record()])
        chunk = self.store.chunks_of("academic_programs")[0]
        self.assertEqual(self.store.degree_level_of(chunk), "bachelor")
        with patch("app.uploaded_files.index_store.delete") as purge:
            self.store.delete("academic_programs")
        purged = [c.args[0] for c in purge.call_args_list]
        self.assertIn("uploaded::kb2::academic_programs", purged)
        self.assertFalse(self.store.has("academic_programs"))
        self.assertIsNone(self.store.degree_level_of(chunk))
        self.assertIsNone(self.store.canonical_id_of(chunk))

    def test_reupload_replaces_projection_atomically(self):
        self.store.upload_json("academic_programs", [_record()])
        old_chunk = self.store.chunks_of("academic_programs")[0]
        newer = _record(answer_text="نسخة محدثة من الإجابة الرسمية.")
        self.store.upload_json("academic_programs", [newer])
        chunks = self.store.chunks_of("academic_programs")
        self.assertEqual(len(chunks), 1)
        self.assertIn("نسخة محدثة", chunks[0])
        self.assertIsNone(self.store.degree_level_of(old_chunk))


# ═══ 11: البيانات الحقيقية + كتالوج القبول وخريطة doc_index ═══════════════

@unittest.skipUnless(os.path.isdir(REAL_KB_DIR), "حزمة البيانات الحقيقية غير موجودة")
class TestRealDataAdmissionCatalog(KbV2StoreBase):

    def _load_real(self, name):
        with open(os.path.join(REAL_KB_DIR, name), encoding="utf-8") as f:
            return json.load(f)

    def test_real_academic_programs_yield_82_facts_from_10_faculties(self):
        docs = self._load_real("academic_programs.json")
        facts = extract_admission_facts("academic_programs", docs)
        self.assertEqual(len(facts), 82)
        self.assertEqual(len({f.faculty for f in facts}), 10)
        self.assertTrue(all(f.degree == "بكالوريوس" for f in facts))

    def test_doc_index_map_survives_draft_exclusion(self):
        # وسط الملف سجل draft: المؤشرات يجب أن تقفز فوقه وتبقى مشيرة
        # لمواضع Mongo الفعلية (شرط سلامة vector_for_fact).
        docs = [
            _variant("a"),
            _variant("d", validity={"status": "draft", "verified_at": None,
                                    "effective_from": None, "effective_to": None}),
            _variant("b"),
        ]
        projection = kb_v2.build_projection(docs, "ملف")
        self.assertEqual([p.doc_index for p in projection], [0, 2])

    def test_vector_for_fact_alignment_on_mixed_statuses(self):
        draft = _variant("d", validity={"status": "draft", "verified_at": None,
                                        "effective_from": None,
                                        "effective_to": None})
        docs = [_variant("a"), draft, _variant("b")]
        self.store.upload_json("academic_programs", docs)
        doc_indexes = self.store._chunk_doc_indexes["academic_programs"]
        index = self.store._indexes["academic_programs"]
        self.assertEqual(doc_indexes, [0, 2])
        self.assertEqual(len(index), 2)  # صف متجه لكل سجل مفهرس فقط


# ═══ 12-13: تعديلا chatbot (أ) و(ب) ═══════════════════════════════════════

def _graduate(suffix, program, faculty, level, level_ar):
    return _variant(
        suffix,
        title=f"{program} — {level_ar}",
        subdomain=level,
        scope={"degree_level": level, "faculty": faculty,
               "program": program, "campus": "main"},
        data={"program_name": program, "faculty_name": faculty,
              "degree_level": level, "credit_hour_fee": 65,
              "currency": "JOD", "admission_criteria": {}},
        answer_text=f"برنامج {program} {level_ar} رسوم الساعة 65 ديناراً.",
        retrieval={
            "example_queries": [f"ما برامج {level_ar} المتاحة؟"],
            "aliases": [f"{level_ar} {program}"],
            "keywords": [level_ar, "برامج", "تخصصات"],
            "ambiguous_queries": [], "clarification_question": None,
            "contextual_text": f"برنامج {level_ar} في كلية {faculty}.",
        },
    )


class ChatV2Base(unittest.TestCase):
    """بوت كامل ملفاته المرفوعة بفورمات v2 وأسماء إنجليزية فقط.

    العيّنة أكبر من حُرّاس «لا تفرّغ السياق» (3 بكالوريوس + 3 ماجستير)
    حتى يُختبر الفلتر نفسه لا حارس الإفراغ."""

    V2_DOCS = [
        _variant("cs", title="هندسة الحاسوب — الهندسة — بكالوريوس"),
        _variant(
            "business",
            title="إدارة الأعمال — الاقتصاد — بكالوريوس",
            scope={"degree_level": "bachelor", "faculty": "الاقتصاد",
                   "program": "إدارة الأعمال", "campus": "main"},
            data={"program_name": "إدارة الأعمال", "faculty_name": "الاقتصاد",
                  "degree_level": "bachelor", "credit_hour_fee": 18,
                  "currency": "JOD", "admission_criteria": {
                      "min_high_school_percentage": 65,
                      "allowed_high_school_branches": ["علمي", "أدبي"]}},
            answer_text="برنامج إدارة الأعمال بكالوريوس رسوم الساعة 18 ديناراً.",
        ),
        _variant(
            "nursing",
            title="التمريض — التمريض — بكالوريوس",
            scope={"degree_level": "bachelor", "faculty": "التمريض",
                   "program": "التمريض", "campus": "main"},
            data={"program_name": "التمريض", "faculty_name": "التمريض",
                  "degree_level": "bachelor", "credit_hour_fee": 22,
                  "currency": "JOD", "admission_criteria": {
                      "min_high_school_percentage": 70,
                      "allowed_high_school_branches": ["علمي"]}},
            answer_text="برنامج التمريض بكالوريوس رسوم الساعة 22 ديناراً.",
        ),
        _graduate("master_crisis", "إدارة الأزمات", "الاقتصاد",
                  "master", "الماجستير"),
        _graduate("master_math", "الرياضيات التطبيقية", "العلوم",
                  "master", "الماجستير"),
        _graduate("master_edu", "المناهج وطرق التدريس", "التربية",
                  "master", "الماجستير"),
        _graduate("phd_math", "الرياضيات", "العلوم",
                  "doctorate", "الدكتوراه"),
    ]

    def setUp(self):
        embeddings.reset_query_cache()
        self.llm_calls = []
        self.bot = IUGChatbot(sessions=SessionStore())
        data = copy.deepcopy(FIXTURE_DATA)
        self.bot._kb._data = data
        self.bot._kb._chunks, self.bot._kb._chunk_meta = chunking.build_chunks(data)
        with patch("app.embeddings.embed_texts", side_effect=fake_embed):
            self.bot._kb._index = embeddings.build_index(self.bot._kb._chunks)

        store = self.bot._uploaded
        fake_col = FakeCollection()
        with patch("app.uploaded_files.get_uploaded_collection",
                   return_value=fake_col), \
             patch("app.uploaded_files.index_store.build_or_load",
                   side_effect=lambda name, texts, build: build(texts)), \
             patch("app.embeddings.embed_texts", side_effect=fake_embed):
            # اسم إنجليزي بلا «قبول/معدلات» — عمداً
            store.upload_json("academic_programs", copy.deepcopy(self.V2_DOCS))

    def _chat(self, question, session="s1", search_result=None):
        eligibility = "تقبلني" in question or "معدلي" in question

        def fake_llm(headers, payload):
            self.llm_calls.append(payload)
            if eligibility:
                # جواب يرضي الفاحص الحتمي: يسرد كليات المستخلص الثلاث بنسبها
                # وبصيغة «الشرط المبدئي» لا ضمان القبول النهائي.
                return ("معدلك يحقق الشرط المبدئي للتقديم إلى: "
                        "كلية الهندسة (هندسة الحاسوب 80%)، "
                        "كلية الاقتصاد (إدارة الأعمال 65%)، "
                        "كلية التمريض (التمريض 70%)؛ "
                        "والقبول النهائي يتبع إجراءات الجامعة.")
            return "هذه هي البرامج المتاحة كما وردت في الأدلة أعلاه."

        patches = [
            patch.object(config, "CHAT_API_KEY", "test-key"),
            patch("app.embeddings.embed_texts", side_effect=fake_embed),
            patch("app.chatbot.file_catalog.recency_map", return_value={}),
            patch("app.llm._post_with_retry", side_effect=fake_llm),
        ]
        if search_result is not None:
            patches.append(patch.object(
                self.bot._uploaded, "search_all",
                return_value=list(search_result),
            ))
        with patches[0], patches[1], patches[2], patches[3]:
            if search_result is not None:
                with patches[4]:
                    return self.bot.chat_with_all_files(question, session)
            return self.bot.chat_with_all_files(question, session)

    def _system(self):
        return self.llm_calls[-1]["messages"][0]["content"]


class TestDigestWithoutArabicFileNames(ChatV2Base):

    def test_admission_question_injects_digest_despite_english_names(self):
        """(أ) المستخلص الرقمي يُحقن ولو لم يطابق أي اسم ملف عربي —
        كان مشروطاً بملف اسمه يحوي «قبول/معدلات» فيموت مع أسماء v2."""
        res = self._chat("ما هي التخصصات التي يمكن أن تقبلني إذا كان معدلي 85؟")
        joined = "\n".join(res["top_chunks"])
        self.assertIn("مفاتيح القبول", joined)      # كتلة المستخلص وصلت
        self.assertIn("الهندسة", joined)
        self.assertIn("80", joined)
        # ولا جدول كامل (لا ملف مسمى قبول) — تعليمات المستخلص وحدها
        self.assertNotIn("جدول مفاتيح القبول متوفر أعلاه كاملاً", self._system())

    def test_degree_level_read_from_metadata_not_file_name(self):
        """(ب) مرحلة المقطع من metadata سجل v2 لا من اسم الملف."""
        store = self.bot._uploaded
        chunks = store.chunks_of("academic_programs")
        levels = [store.degree_level_of(chunk) for chunk in chunks]
        self.assertEqual(levels.count("bachelor"), 3)
        self.assertEqual(levels.count("masters"), 3)
        self.assertEqual(levels.count("phd"), 1)
        self.assertNotIn(None, levels)

    def test_programs_question_drops_graduate_chunks_from_mixed_file(self):
        """سؤال «الخيارات الأكاديمية» على ملف v2 مختلط المراحل يسقط مقاطع
        الماجستير/الدكتوراه ويبقي البكالوريوس (فلتر المرحلة من metadata)."""
        store = self.bot._uploaded
        mixed = store.chunks_of("academic_programs")  # الثلاثة بمراحلهم
        res = self._chat(
            "اعطيني الخيارات الأكاديمية المتاحة للتخصصات",
            search_result=mixed,
        )
        joined = "\n".join(res["top_chunks"])
        self.assertIn("هندسة الحاسوب", joined)
        self.assertNotIn("دكتوراه الرياضيات", joined)
        self.assertNotIn("إدارة الأزمات", joined)

    def test_explicit_masters_question_keeps_masters_chunks(self):
        store = self.bot._uploaded
        mixed = store.chunks_of("academic_programs")
        res = self._chat("شو برامج الماجستير المتاحة؟", search_result=mixed)
        joined = "\n".join(res["top_chunks"])
        self.assertIn("إدارة الأزمات", joined)
        self.assertNotIn("هندسة الحاسوب", joined)   # بكالوريوس أُسقط


if __name__ == "__main__":
    unittest.main()
