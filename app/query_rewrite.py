"""
Retrieval-query rewriting — pure string operations, zero extra LLM calls.

The retrieval layer only ever sees the literal question text, which fails in
two recurring ways:

  1. Self-references: «رئيس قسمي» carries no department name, so the search
     can't rank the student's own department above any other.
  2. Anaphora / follow-ups: «كم رسوم هذا الطلب» carries none of the words of
     the previous turn («تأجيل الفصل»), so the search fetches the wrong topic.

Both are fixed by REWRITING THE RETRIEVAL QUERY ONLY — the question shown to
the LLM (and stored in history) stays exactly what the student typed.
"""

import re
from typing import Optional

from app.sessions import is_fresh
from app.text_norm import normalize_arabic, tokenize

# ── 1. self-references → student profile ─────────────────────────────────────
# First-person possessives whose referent lives in the student's profile.
# Matched on NORMALIZED tokens (see text_norm: ة→ه, أ/إ→ا ...).
#
# The BARE definite forms «القسم»/«الكليه» count too: a logged-in student
# saying «رئيس القسم» means HIS department (proven live — Basem's «كيف ممكن
# اتواصل مع رئيس القسم؟» found nothing). A named faculty tokenizes WITHOUT
# the article on the marker («قسم هندسة الحاسوب» → قسم, هندسه, …), so these
# exact tokens never fire when another faculty is spelled out.
_SELF_REF_TOKENS = {
    "قسمي", "تخصصي", "كليتي", "برنامجي", "دراستي", "مساقاتي", "موادي",
    "قسمنا", "كليتنا",
    "القسم", "الكليه",
}


def expand_self_references(question: str, major: Optional[str]) -> str:
    """Append the student's major when the question points at «my …» so the
    hybrid search can rank the right department's chunks. Appending (not
    substituting) keeps the original wording for dense retrieval."""
    if not major:
        return question
    tokens = set(tokenize(question))
    if tokens & _SELF_REF_TOKENS and not set(tokenize(major)) <= tokens:
        return f"{question} — {major}"
    return question


# Topics that are implicitly about the student's OWN faculty even without a
# possessive marker: a CS student asking «كيف انجز التدريب الميداني» means his
# faculty's training, not any faculty's. Substrings of NORMALIZED text.
_IMPLICIT_PERSONAL_TOPICS = ("تدريب", "مشروع التخرج", "الخطه الدراسيه")

# … unless the question explicitly names a faculty/department of its own
# choosing («التدريب الميداني في كلية الطب») — then we must not override it.
_EXPLICIT_FACULTY_MARKERS = ("كليه", "قسم", "تخصص")
_SELF_FACULTY_FORMS = ("كليتي", "قسمي", "تخصصي")


def personalize_implicit_topics(question: str, major: Optional[str]) -> str:
    """Append the student's major when the topic is inherently faculty-bound
    but the question names no faculty, so retrieval prefers HIS faculty's
    chunks (when they exist) over another faculty's."""
    if not major:
        return question
    norm = normalize_arabic(question)
    if not any(topic in norm for topic in _IMPLICIT_PERSONAL_TOPICS):
        return question
    names_a_faculty = any(m in norm for m in _EXPLICIT_FACULTY_MARKERS)
    says_my_faculty = any(s in norm for s in _SELF_FACULTY_FORMS)
    if names_a_faculty and not says_my_faculty:
        return question
    if set(tokenize(major)) <= set(tokenize(question)):
        return question  # major already present (e.g. via expand_self_references)
    return f"{question} — {major}"


def personalize_query(question: str, major: Optional[str]) -> str:
    """Single entry point: possessive self-references + implicit topics."""
    return personalize_implicit_topics(
        expand_self_references(question, major), major
    )


# ── canonical academic terms ─────────────────────────────────────────────────
# Students ask with colloquial VERBS («كيف اجل الفصل؟») while the corpus
# stores formal NOUNS («رسوم طلب تأجيل الدراسة»). BM25 has no Arabic stemming,
# so the verb form never matches lexically and dense retrieval drifts to the
# nearest topic («ثوابت الفصل الدراسي»). Appending the canonical noun anchors
# both retrievers. Verb sets are written in NORMALIZED form (أ→ا, ة→ه).
_CANONICAL_TERMS = {
    "تأجيل الدراسة": {"اجل", "اجلت", "باجل", "ناجل", "ياجل", "تاجل", "اجيل"},
    "تسجيل المساقات": {"اسجل", "بسجل", "نسجل", "يسجل", "سجلت"},
    "انسحاب": {"انسحب", "اسحب", "بنسحب", "ينسحب", "انسحبت"},
    # «أي تخصص يقبلني؟» — سؤال طالب الثانوية عن مفاتيح القبول، لا عن المنح
    "معدلات القبول": {"يقبلني", "تقبلني", "بقبلني", "بتقبلني", "انقبل", "بنقبل", "قبولي"},
}

# نية القبول قد تأتي بلا فعل «يقبلني»: «التخصصات المتاحة لمعدلي 85» —
# اجتماع (معدل + تخصص/كلية) يكفي دليلاً أن المقصود مفاتيح القبول. فحص احتوائي
# على النص المطبَّع كي تصمد أمام حروف الجر الملتصقة (لمعدلي، بالتخصصات...).
_GRADE_MARKS = ("معدل",)
_MAJOR_MARKS = ("تخصص", "كليه", "كليات", "برنامج", "برامج")


def has_admission_intent(query: str) -> bool:
    """«أي التخصصات تقبلني بمعدلي؟» — مقارنة معدل الثانوية بمفاتيح القبول.
    تحتاج تغطية استرجاع أعرض من المعتاد (كل جدول المفاتيح لا مقطعاً منه)."""
    norm = normalize_arabic(query)
    tokens = set(tokenize(query))
    if tokens & _CANONICAL_TERMS["معدلات القبول"]:
        return True
    return any(g in norm for g in _GRADE_MARKS) and any(m in norm for m in _MAJOR_MARKS)


def add_canonical_terms(query: str) -> str:
    tokens = set(tokenize(query))
    additions = [
        noun for noun, verbs in _CANONICAL_TERMS.items()
        if tokens & verbs and not set(tokenize(noun)) <= tokens
    ]
    norm = normalize_arabic(query)
    if has_admission_intent(query) \
            and "معدلات القبول" not in additions and "معدلات القبول" not in norm:
        additions.append("معدلات القبول")
    if not additions:
        return query
    return f"{query} ({' ، '.join(additions)})"


# ── 2. anaphora / follow-ups → previous turn ─────────────────────────────────
# Standalone tokens that signal the question leans on the previous turn.
_ANAPHORA_TOKENS = {
    "هذا", "هذه", "ذلك", "تلك", "هاي", "هيك",          # demonstratives
    "نفس", "المذكور", "السابق", "سابقا",                # same/aforementioned
    "اقصد", "قصدي", "قصدت", "يعني",                     # "I mean …" repairs
    "كمان", "ايضا", "برضه", "برضو",                     # "also …" continuations
    "اذكرهم", "اذكرها", "اذكرهما", "عددهم", "عدديهم",   # «اذكرهم/عدّدهم» — enumerate-them
    "سمهم", "سميهم", "وضحهم", "اشرحهم",                 # (المفعول ضمير عائد على السابق)
}
# A follow-up is usually VERY short («كم هيكلفني؟», «وللماجستير؟»); at 4+
# tokens questions usually carry their own topic and prepending the previous
# turn only pollutes retrieval (proven live: «كيف بدي اجل الفصل» + a nursing
# turn returned nothing useful).
_SHORT_QUESTION_TOKENS = 3


def has_reference_tokens(question: str) -> bool:
    """هل في السؤال إشارة عائدة («اذكرهم»، «هذا»...)؟ إجابة سؤال كهذا تعتمد
    على سياق محادثة صاحبه، فلا يجوز تخزينها في كاش مشترك يقدّمها لغيره
    (ثبت عملياً: أول زائر يسأل «اذكرهم» كان جوابه العشوائي يُكاش للجميع)."""
    return bool(set(tokenize(question)) & _ANAPHORA_TOKENS)


def is_pure_reference(question: str) -> bool:
    """سؤال إحالة خالص («اذكرهم»، «وشو تخصصه؟»): قصير وكل موضوعه في الدور
    السابق — البحث بنصه الخام ضجيج محض، والاستعلام الموسّع بالسياق هو
    الصواب وحده (عكس الأسئلة القصيرة ذات الموضوع الذاتي مثل «مين رئيس
    الجامعة؟» التي يجب أن يتقدم بحثها الخام)."""
    tokens = tokenize(question)
    return (
        0 < len(tokens) <= _SHORT_QUESTION_TOKENS
        and bool(set(tokens) & _ANAPHORA_TOKENS)
    )


def needs_history_context(question: str) -> bool:
    tokens = tokenize(question)
    if not tokens:
        return False
    if set(tokens) & _ANAPHORA_TOKENS:
        return True
    return len(tokens) <= _SHORT_QUESTION_TOKENS


def with_history_context(question: str, history: list) -> str:
    """Prefix the previous user turn onto the retrieval query when the current
    question can't stand alone. BM25+RRF then ranks the shared topic («تأجيل»)
    above lexical noise, and dense retrieval sees the full intent.

    Chain hop: when the previous turn is ITSELF a vague follow-up («كم
    هيكلفني؟» then «وشو الشروط؟»), it carries no topic words — climb one more
    step to the turn before it so the anchoring topic survives the chain.

    Freshness gate: chaining happens ONLY onto a turn from the CURRENT sitting
    (is_fresh) — the history is persistent across days, and without this gate
    today's «أذكرهم» inherited yesterday's department-heads topic."""
    if not history or not needs_history_context(question):
        return question
    if not is_fresh(history[-1]):
        return question  # آخر دور من جلسة قديمة — لا وراثة موضوع
    last_user = str(history[-1].get("user") or "").strip()
    if not last_user:
        return question
    parts = [last_user]
    if needs_history_context(last_user) and len(history) >= 2 and is_fresh(history[-2]):
        anchor = str(history[-2].get("user") or "").strip()
        if anchor:
            parts.insert(0, anchor)
    return " — ".join(parts + [question])
