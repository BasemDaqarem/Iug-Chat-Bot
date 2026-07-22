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
    # «ما برامج كلية العلوم؟» كانت تسقط على سجلات الماجستير لأن عناصر
    # التخصصات مصوغة بكلمة «تخصصات» — المرادف يجسر الفجوة المعجمية (ثبت Q257)
    "تخصصات": {"برامج", "البرامج", "برنامج", "البرنامج"},
    # المتابعة «أنا أقصد عمداءهم» كانت تبحث بكلمة صرفية لا تطابق «عميد»
    # الموجودة في السجلات؛ إضافة الاسم المؤسسي تحسن dense وBM25 معاً.
    # نضمّن المفرد أيضاً لأن سجلات الأشخاص تقول «عميد كلية ...»، بينما
    # السؤال الدارج يأتي غالباً بالجمع «عمداءهم». وجود الصيغتين في استعلام
    # الاسترجاع أنقذ الدليل من مرتبة متأخرة من دون مسار إجابة مخصص.
    "عميد كلية عمداء الكليات": {
        "عمداء", "عمداءهم", "العمداء", "عميد", "العميد",
    },
}

# نية القبول قد تأتي بلا فعل «يقبلني»: «التخصصات المتاحة لمعدلي 85» —
# اجتماع (معدل + تخصص/كلية) يكفي دليلاً أن المقصود مفاتيح القبول. فحص احتوائي
# على النص المطبَّع كي تصمد أمام حروف الجر الملتصقة (لمعدلي، بالتخصصات...).
_GRADE_MARKS = ("معدل",)
_MAJOR_MARKS = ("تخصص", "كليه", "كليات", "برنامج", "برامج", "قبول", "خيار")
_BRANCH_MARKS = ("علمي", "ادبي", "شرعي", "صناعي", "تجاري", "فرعي")
# أسماء الكليات كموضوع أكاديمي ذاتي: «وكم للطب؟» بعد نقاش المفاتيح موضوعها
# كلية وإن خلت من كلمة تخصص/كلية (ثبت Q093 — الصيغة الطويلة لا ترث بالقصر)
_FACULTY_MARKS = ("طب", "هندس", "تمريض", "قباله", "علوم", "اداب", "تربيه",
                  "شريعه", "اقتصاد", "تكنولوجيا", "اصول الدين")
_ELIGIBILITY_MARKS = (
    "احقق", "يحقق", "يقبلني", "تقبلني", "يمكنني", "يمكنك",
    "التقديم", "دخول", "قبولي", "الخيارات الممكنه", "الخيارات المتاحه",
)


def has_admission_intent(query: str) -> bool:
    """«أي التخصصات تقبلني بمعدلي؟» — مقارنة معدل الثانوية بمفاتيح القبول.
    تحتاج تغطية استرجاع أعرض من المعتاد (كل جدول المفاتيح لا مقطعاً منه)."""
    norm = normalize_arabic(query)
    tokens = set(tokenize(query))
    # «معدل استمرار منحة تخصص الكيمياء» uses معدل + تخصص but asks about
    # scholarship retention, not high-school admission.  Without an explicit
    # admission verb/term, do not inject the full admissions table.
    if (
        any(mark in norm for mark in ("منحه", "منح", "اعفاء"))
        and "استمرار" in norm
        and not any(mark in norm for mark in ("قبول", "يقبل", "تقبل", "ثانويه"))
    ):
        return False
    if tokens & _CANONICAL_TERMS["معدلات القبول"]:
        return True
    grade_admission = (
        any(g in norm for g in _GRADE_MARKS)
        and any(
            m in norm
            for m in _MAJOR_MARKS + _FACULTY_MARKS + _ELIGIBILITY_MARKS
        )
    )
    branch_admission = (
        any(branch in norm for branch in _BRANCH_MARKS)
        and any(mark in norm for mark in _MAJOR_MARKS + ("متاح", "يقبل"))
    )
    return grade_admission or branch_admission


def inherits_admission_intent(raw_question: str, base: str, expanded: str) -> bool:
    """هل تستحق نية القبول أن تُورَّث من سياق المحادثة؟ السؤال بنيته الذاتية
    أولاً، وإلا فالوراثة لحالتين مثبتتين حياً: (١) متابعة قصيرة/إحالة
    («وكم للطب؟» بعد نقاش المفاتيح)، (٢) سؤال يحمل موضوعاً أكاديمياً
    (تخصصات/كلية/قبول) وسياقه يحمل المعدل («أنا بسأل عن تخصصات» بعد
    «معدلي 85»). أما «وشو الرسوم؟» بلا موضوع أكاديمي ذاتي فلا تُجرّ له
    جداول المفاتيح عبثاً."""
    if has_admission_intent(base):
        return True
    if expanded == base or not has_admission_intent(expanded):
        return False
    if is_pure_reference(raw_question) or len(tokenize(raw_question)) <= _SHORT_QUESTION_TOKENS:
        return True
    norm_base = normalize_arabic(base)
    return any(m in norm_base for m in _MAJOR_MARKS + _FACULTY_MARKS)


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


# ── وعي المرحلة الأكاديمية والاستبعاد ────────────────────────────────────────
# جذر ~10 أخطاء في تقييم الـ90: 65 سجل رسوم دراسات عليا غنية بكلمات
# «برامج/تخصصات» كانت تبتلع استرجاع أسئلة البكالوريوس («ما برامج كلية
# العلوم؟» أجاب بماجستير، ونفى وجود الدكتوراه لسؤال الدرجات).
_PHD_MARKS = ("دكتوراه", "دكتوراة")
_MASTERS_MARKS = ("ماجستير", "ماستر", "دراسات عليا")
_BACHELOR_MARKS = ("بكالوريوس", "توجيهي", "ثانويه")


def detect_degree_level(text: str) -> str | None:
    """المرحلة الأكاديمية المقصودة صراحةً: bachelor/masters/phd — أو None
    (لم تُذكر، أو ذُكرت أكثر من مرحلة كسؤال «الدرجات التي تمنحونها؟» فلا
    يصح ترشيح أي مرحلة عندها)."""
    norm = normalize_arabic(text)
    found = set()
    if any(m in norm for m in _PHD_MARKS):
        found.add("phd")
    if any(m in norm for m in _MASTERS_MARKS):
        found.add("masters")
    if any(m in norm for m in _BACHELOR_MARKS):
        found.add("bachelor")
    return found.pop() if len(found) == 1 else None


_BRANCH_ALIASES = (
    ("علمي", "علمي"),
    ("ادبي", "أدبي"),
    ("شرعي", "شرعي"),
    ("صناعي", "صناعي"),
    ("تجاري", "تجاري"),
)
_RATE_NEAR_GRADE_RE = re.compile(
    r"(?:معدل(?:ي|ك|ه|ها|هم)?|نسبت(?:ي|ك|ه|ها)?)"
    r"[^0-9\u0660-\u0669\u06f0-\u06f9]{0,18}"
    r"([0-9\u0660-\u0669\u06f0-\u06f9]{2,3}(?:[.,][0-9]+)?)"
)
_ARABIC_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)


def latest_academic_constraints(question: str, history: list) -> dict:
    """استخرج أحدث فرع/معدل/مرحلة من الحوار، بلا توليد جواب.

    في الحوار اللولبي قد يذكر المستخدم «علمي» ثم يصحح في الدور التالي إلى
    «أدبي» ثم يقول «رتبهم». تمرير السجل كله وحده يترك النموذج يختار القيد
    الأقدم. نقرأ من الحالي إلى الأقدم ونثبت أول قيمة لكل حقل؛ وبذلك الأحدث
    يلغي الأقدم، مع بقاء الاسترجاع والجواب قائمين على البيانات لا resolver.
    """
    result = {"branch": None, "rate": None, "degree": None}
    texts = [question]
    for turn in reversed(history[-5:]):
        if not is_fresh(turn):
            continue
        previous = str(turn.get("user") or "").strip()
        if previous:
            texts.append(previous)

    for text in texts:
        norm = normalize_arabic(text)
        if result["branch"] is None:
            # Exact normalized tokens only: «مواد علمية» describes subjects,
            # not the student's high-school branch.  Substring matching used
            # to treat «علمية» as «علمي» and silently impose a false branch.
            norm_tokens = set(tokenize(text))
            for marker, label in _BRANCH_ALIASES:
                if marker in norm_tokens:
                    result["branch"] = label
                    break
        if result["rate"] is None:
            match = _RATE_NEAR_GRADE_RE.search(norm)
            if match:
                try:
                    value = float(
                        match.group(1).translate(_ARABIC_DIGITS).replace(",", ".")
                    )
                except ValueError:
                    value = -1
                if 0 <= value <= 100:
                    result["rate"] = value
        if result["degree"] is None:
            if "بكالوريوس فقط" in norm:
                result["degree"] = "bachelor"
            elif "ماجستير فقط" in norm or "دراسات عليا فقط" in norm:
                result["degree"] = "masters"
            elif "دكتوراه فقط" in norm or "دكتوراة فقط" in norm:
                result["degree"] = "phd"
            else:
                result["degree"] = detect_degree_level(text)
        if all(value is not None for value in result.values()):
            break
    return result


_PROGRAMS_INTENT = ("تخصص", "برامج", "برنامج", "خيارات اكاديمي", "خيار اكاديمي",
                    "اقسام", "قسم اكاديمي")


def wants_academic_programs(question: str) -> bool:
    """سؤال عن التخصصات/البرامج/الخيارات الأكاديمية — افتراضه البكالوريوس
    ما لم تُذكر مرحلة أعلى صراحةً."""
    norm = normalize_arabic(question)
    return any(m in norm for m in _PROGRAMS_INTENT)


# ── شكل الإجابة وتكلفة الاسترجاع ────────────────────────────────────────────
# هذه ليست Resolvers لإجابات بعينها؛ هي إشارات عامة لشكل السؤال. القوائم
# الشاملة تحتاج سياقاً أوسع، بينما الـreranker pointwise قد يرفع أفضل عنصر
# ويُسقط بقية العناصر (ثبت في إعادة اختبار Q018/Q020).
_COMPLETE_LIST_PHRASES = (
    "ما هي التخصصات المتاحه",
    "ما التخصصات المتاحه",
    "جميع التخصصات",
    "كل التخصصات",
    "القائمه الكامله",
    "قائمه كامله",
    "اعمل لي قائمتين",
    "رتبهم",
    "رتبها",
    "اذكرهم",
    "اذكرها",
    "عددهم",
    "عدديهم",
    "اسماء الكليات",
    "اسماء البرامج",
)
_COMPLETE_LIST_TOKENS = {"جميع", "كافه", "كافة", "كامل", "كامله", "كاملة"}


def wants_complete_list(question: str) -> bool:
    """هل طلب السائل تغطية/قائمة كاملة لا مجرد مثال؟"""
    norm = normalize_arabic(question)
    tokens = set(tokenize(question))
    normalized_tokens = {normalize_arabic(token) for token in _COMPLETE_LIST_TOKENS}
    return bool(tokens & normalized_tokens) or any(
        normalize_arabic(phrase) in norm for phrase in _COMPLETE_LIST_PHRASES
    )


def add_coverage_terms(query: str) -> str:
    """مرساة بحث عامة لطلبات القوائم، من دون اسم ملف أو إجابة مبرمجة."""
    norm = normalize_arabic(query)
    if "القائمه الكامله" in norm:
        return query
    if wants_academic_programs(query) or "كليه" in norm or "كليات" in norm:
        return f"{query} (القائمة الكاملة للتخصصات مرتبة حسب الكلية)"
    return f"{query} (القائمة الكاملة لجميع العناصر)"


def is_multi_part_question(question: str) -> bool:
    """طلب مركّب يحتاج الإجابة عن أجزاء مستقلة بعناوين منفصلة."""
    norm = normalize_arabic(question)
    if "وافصل" in norm or "كل جزء" in norm or "قائمتين" in norm:
        return True
    # الفاصلة المنقوطة غالباً تفصل نيتين، بخلاف الفاصلة العربية التي تكثر
    # داخل السؤال البسيط والقوائم الاسمية.
    if "؛" in question and len(tokenize(question)) >= 8:
        return True
    interrogatives = (
        "ما ", "ماذا", "كيف", "هل ", "مين", "متى", "وين", "اين",
        "أين", "كم ", "شو ",
    )
    return sum(norm.count(normalize_arabic(mark)) for mark in interrogatives) >= 2


_RERANK_EXACT_MARKS = (
    "رابط", "صفحه", "بوابه", "بريد", "ايميل", "رقم", "مصدر", "فقرة",
)
_RERANK_REPAIR_MARKS = (
    "اقصد", "قصدي", "مش ", "ليس ", "بدل", "نفسه", "نفسها",
)


def should_use_reranker(
    raw_question: str,
    search_question: str,
    *,
    admission_intent: bool = False,
) -> bool:
    """بوابة رخيصة للـreranker.

    لا يُستخدم للقوائم الشاملة أو جداول القبول لأنها تحتاج تغطية لا اختيار
    أفضل مقطع واحد. يُستخدم للمتابعات/التصحيحات والبحث عن مورد دقيق فقط.
    """
    if admission_intent or wants_complete_list(raw_question) \
            or is_multi_part_question(raw_question):
        return False
    norm = normalize_arabic(raw_question)
    context_expanded = normalize_arabic(search_question) != norm
    exact_lookup = any(mark in norm for mark in _RERANK_EXACT_MARKS)
    repair = any(mark in norm for mark in _RERANK_REPAIR_MARKS)
    return context_expanded or exact_lookup or repair


_DIRECT_EVIDENCE_MARKS = (
    "رابط", "صفحه", "بوابه", "بريد", "ايميل", "رقم هاتف", "مصدر",
    "مرجع", "وصف المساق", "وصف المساقات", "وين الاقي", "اين اجد",
)


def requires_direct_evidence(question: str) -> bool:
    """طلب مورد/مكان/حقل دقيق لا يجوز إكماله من معلومة مجاورة.

    هذه إشارة لصياغة البرومت فقط وليست resolver: لا تُنتج جواباً ولا تعرف
    أسماء ملفات أو روابط. فائدتها منع تحويل «رابط دليل الخطوات» إلى «رابط
    نموذج الطلب»، أو استنتاج وجود وصف المساق من مجرد وجود كتب على Moodle.
    """
    norm = normalize_arabic(question)
    return any(normalize_arabic(mark) in norm for mark in _DIRECT_EVIDENCE_MARKS)


_OVERLAP_STOPWORDS = {
    "ما", "ماذا", "هل", "من", "في", "على", "عن", "الى", "إلى", "شو",
    "كيف", "كم", "هذا", "هذه", "انا", "أنا", "بدي", "ممكن", "لو", "ولا",
    "مش", "ليس", "نفس", "بس", "فقط",
}


def candidates_support_query(query: str, chunks: list[str]) -> bool:
    """حارس recall قبل دفع كلفة الـreranker.

    الـreranker لا يستطيع استحضار وثيقة غير موجودة في مرشحي المرحلة الأولى.
    نطلب تطابق جذرين دلاليين على الأقل في أحد المرشحين؛ وإلا نبقي RRF ونوفر
    النداء البطيء. نزيل «الـ» ولواحق الجمع الشائعة وحروف المد ثم نقارن
    بهيكل خفيف؛ فهذا يجمع صيغاً مثل «عمداء/عميد» و«كليات/كلية» دون نموذج
    صرفي إضافي.
    """
    query_tokens = {
        token for token in tokenize(query)
        if len(token) >= 4 and token not in _OVERLAP_STOPWORDS
    }
    if not query_tokens or not chunks:
        return False
    def _stem(token: str) -> str:
        bare = token[2:] if token.startswith("ال") and len(token) > 5 else token
        for suffix in ("يات", "اتهم", "ات", "ون", "ين", "هم", "ها", "ه"):
            if bare.endswith(suffix) and len(bare) - len(suffix) >= 3:
                bare = bare[:-len(suffix)]
                break
        skeleton = "".join(char for char in bare if char not in "اوي")
        return (skeleton or bare)[:3]

    query_stems = {_stem(token) for token in query_tokens}
    for chunk in chunks:
        chunk_stems = {
            _stem(token) for token in tokenize(chunk)
            if len(token) >= 4 and token not in _OVERLAP_STOPWORDS
        }
        if len(query_stems & chunk_stems) >= 2:
            return True
    return False


def prefer_exact_role_chunks(query: str, chunks: list[str]) -> tuple[list[str], bool]:
    """قدّم سجلات الدور المؤسسي المطابق حرفياً عندما تكون وفيرة.

    دليل الأشخاص يحوي «عميد كلية»، «نائب عميد»، و«عميد شؤون/مركز» في الملف
    نفسه. إذا كان الاستعلام يطلب عمداء الكليات ووجدنا عدة سجلات حرفية
    `degree_or_request: عميد كلية ...` نستخدمها وحدها؛ فهذا ترشيح حقلي عام
    لا يولّد أسماء ولا يجيب السؤال، ويمنع الضجيج الإداري من إزاحة الكليات.
    """
    norm = normalize_arabic(query)
    if "عميد كليه" not in norm and not (
        "عمداء" in norm and ("كليه" in norm or "كليات" in norm)
    ):
        return list(chunks), False
    exact = []
    for chunk in chunks:
        normalized_chunk = normalize_arabic(chunk)
        if "degree_or_request: عميد كليه" not in normalized_chunk:
            continue
        if "degree_or_request: نايب عميد" in normalized_chunk:
            continue
        exact.append(chunk)
    if len(exact) < 3:
        return list(chunks), False
    return exact, True


def file_degree_level(file_name: str) -> str | None:
    """مرحلة ملف المعرفة من اسمه (ترويسة [ملف: X] في كل مقطع) — None للعام."""
    norm = normalize_arabic(file_name)
    if any(m in norm for m in _PHD_MARKS):
        return "phd"
    if any(m in norm for m in _MASTERS_MARKS):
        return "masters"
    if "بكالوريوس" in norm:
        return "bachelor"
    return None


# «مش منح»، «بدون رسوم»، «لا تحكيلي عن تأجيل» — السائل استبعد موضوعاً صراحةً
# لكن البحث الدلالي كان يلتقطه لأنه الأبرز لفظياً (ثبت في Q097 وQ340).
# Topic exclusions are deliberately lexicon-bound.  Generic negation such as
# «بلا مصدر»، «بدون ما تضمن»، or «مش رقم محفوظ» describes HOW the user wants
# the answer; it must not silently ban the following word from retrieval or
# from the final answer.  Only explicit discourse exclusions of known topics
# are removed from the retrieval query.
_EXCLUSION_TOPICS = (
    "الصفحة الرئيسية",
    "الهندسة",
    "المنح",
    "ماجستير",
    "دكتوراه",
    "تأجيل",
    "رسوم",
    "منح",
)
_EXCLUSION_TOPIC_PATTERN = "|".join(
    re.escape(normalize_arabic(topic))
    for topic in sorted(_EXCLUSION_TOPICS, key=len, reverse=True)
)
_EXCLUSION_RE = re.compile(
    rf"(?:\bمش\s+عن\b|\bمو\s+عن\b|\bليس\s+عن\b|"
    rf"\bمش\b|\bمو\b|\bما\s+بدي\b|لا\s+تحكيلي\s+عن|"
    rf"لا\s+تعطيني|خلينا\s+من)\s+({_EXCLUSION_TOPIC_PATTERN})"
    rf"(?=$|[\s،,؛;.!؟:()])"
)
# مواضيع معروفة → آثارها في أسماء الملفات (بعضها بالإنجليزية)
_EXCLUSION_TOPIC_FILES = {
    "منح": ("منح", "scholarship"),
    "المنح": ("منح", "scholarship"),
    "ماجستير": ("ماجستير",),
    "دكتوراه": ("دكتوراه",),
    "تأجيل": ("تأجيل",),
    "الهندسه": ("هندسه",),
    "الهندسة": ("هندسه",),
}


def extract_exclusions(question: str) -> list[str]:
    """Return only explicit, known topic exclusions.

    This intentionally does not interpret «بدون/بلا + word» as a topic ban;
    those forms commonly express an answer constraint rather than a subject
    switch (for example «لا تذكر مبلغاً بلا مصدر»).
    """
    norm = normalize_arabic(question)
    tokens = [m.group(1).strip("؛،,.!؟:()\"'») ") for m in _EXCLUSION_RE.finditer(norm)]
    return list(dict.fromkeys(t for t in tokens if t))


def exclusion_file_markers(excluded: list[str]) -> set[str]:
    """آثار المواضيع المستبعدة في أسماء الملفات (للترشيح الرخيص)."""
    markers: set[str] = set()
    for token in excluded:
        markers.update(_EXCLUSION_TOPIC_FILES.get(token, ()))
        if len(token) >= 4:  # الاسم نفسه قد يطابق اسم ملف مباشرة
            markers.add(token)
    return markers


def positive_query(question: str) -> str:
    """Return the positive retrieval intent with explicit exclusions removed.

    The original user message is never changed for the LLM or history.  This
    helper is retrieval-only: phrases such as «خلينا من الهندسة» otherwise
    contain a strong lexical/dense signal that can dominate the actual request
    («شو منح المتفوقين؟»).  Exclusions are retained separately in QueryPlan and
    applied as filters/instructions, while the search query keeps only what the
    user positively asked for.
    """
    if not question:
        return question
    normalized = normalize_arabic(question)
    # Do not treat repair phrases («مش قصدي ...») as a bare excluded topic; the
    # conversation frame handles them as corrections using the latest user turn.
    cleaned = _EXCLUSION_RE.sub(" ", normalized)
    cleaned = re.sub(r"^[\s،,؛;:.-]+|[\s،,؛;:.-]+$", "", cleaned)
    cleaned = re.sub(r"[\s،,؛;:.-]{2,}", " ", cleaned).strip()
    return cleaned or normalized


# ── 2. anaphora / follow-ups → previous turn ─────────────────────────────────
# Standalone tokens that signal the question leans on the previous turn.
_ANAPHORA_TOKENS = {
    "هذا", "هذه", "ذلك", "تلك", "هاي", "هيك",          # demonstratives
    "نفس", "المذكور", "السابق", "سابقا",                # same/aforementioned
    "اقصد", "قصدي", "قصدت", "يعني",                     # "I mean …" repairs
    "كمان", "ايضا", "برضه", "برضو",                     # "also …" continuations
    "اذكرهم", "اذكرها", "اذكرهما", "عددهم", "عدديهم",   # «اذكرهم/عدّدهم» — enumerate-them
    "رتبهم", "رتبها", "سمهم", "سميهم", "وضحهم", "اشرحهم",
    "الثانيه", "والثانيه", "الثالثه", "والثالثه",
    "واحد", "واحده", "فيهم", "منهم",
    "القايمه",                                            # «تتغير القائمة؟»
    "الرقم", "رقمه", "رقمها", "مكانه", "مكانها",
    "شروطه", "شروطها", "رسومه", "رسومها", "رابطه", "رابطها",
    "الطلبات", "هؤلاء",
}
_COMMON_REFERENCE_TOKENS = {
    "الخطه", "الموعد", "الرابط", "القائمه", "المبلغ", "الجهه",
    "الفرع", "والفرع", "الحد", "والحد", "الادنى",
    # «شو مفتاحه؟» إحالة حين يكون السؤال قصيراً فقط.  وجود الكلمة داخل
    # سؤال مكتمل («سعر هندسة الحاسوب وما المفتاح؟») لا يجعله تابعاً.
    "المفتاح", "مفتاحه", "مفتاحها",
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
    tokens = tokenize(question)
    token_set = set(tokens)
    if token_set & _ANAPHORA_TOKENS:
        return True
    # Common nouns are referential only in short/elliptical questions.  A
    # self-contained query such as «أريد الخطة الدراسية لهندسة الحاسوب» must
    # not inherit an unrelated previous topic merely because it says الخطة.
    return len(tokens) <= 4 and bool(token_set & _COMMON_REFERENCE_TOKENS)


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


# واو العطف الاستفهامية في أول السؤال («وكم للطب؟»، «وشو شروطها؟») إعلان
# استمرارٍ صريح على الموضوع السابق ولو طال السؤال بعدها — بدونها كانت
# «وكم للطب؟ بدي مصدر حديث مش رقم محفوظ قديم» (9 كلمات) لا تُسلسل فيضيع
# موضوع «معدل القبول» من الدور السابق (ثبت Q093).
_CONTINUATION_STARTS = {
    "وكم", "وما", "وشو", "وايش", "وماذا", "وهل", "ومين", "ومن", "ومتى", "ووين",
    "وأين", "واين", "وكيف", "وليش", "ولماذا", "وانا", "وأنا",
}
_CONTINUATION_PREFIXES = ("ولل", "وبالنسبه")
_ELLIPTICAL_COST_MARKS = ("هيكلف", "يكلفني", "تكلفني", "بكلف", "بتكلف")

# تصحيحات طويلة تحمل موضوعاً جزئياً لكنها تعتمد على الدور السابق لتحديد
# القيمة الناقصة («أنا بسأل عن تخصصات، مش منح» يحتاج المعدل والفرع السابقين).
# لا نجعل كل جملة فيها «مش» متابعة كي لا نلوث الأسئلة المستقلة المنفية.
_CORRECTION_STARTS = (
    "انا بسال عن", "انا بحكي عن", "سوالي عن", "سؤالي عن",
    "مش قصدي", "مو قصدي", "ليس قصدي", "لا اقصد", "بل اقصد",
    "تصحيح", "قصدي",
)


def is_correction(question: str) -> bool:
    """Whether the current turn explicitly repairs the previous user turn."""
    norm = normalize_arabic(question)
    return any(norm.startswith(normalize_arabic(mark)) for mark in _CORRECTION_STARTS)


def is_assistant_response_reference(question: str) -> bool:
    """True only when the user explicitly points at the assistant's last text."""
    norm = normalize_arabic(question)
    answer_marks = (
        "اجابتك", "جوابك", "ردك", "النقطه السابقه", "النقطة السابقة",
        "ما قصدك", "ماذا تقصد", "اشرح كلامك", "وضح كلامك",
    )
    return any(normalize_arabic(mark) in norm for mark in answer_marks)


def is_source_metadata_followup(question: str) -> bool:
    """هل يطلب الدور الحالي بيانات مصدر الإجابة السابقة لا موضوعاً جديداً؟"""
    norm = normalize_arabic(question)
    return (
        any(mark in norm for mark in ("مصدر", "مرجع"))
        and any(
            mark in norm
            for mark in ("تاريخ", "اسمه", "اسم المصدر", "اذكر")
        )
    )


def needs_history_context(question: str) -> bool:
    tokens = tokenize(question)
    if not tokens:
        return False
    norm = normalize_arabic(question)
    if tokens[0] in _CONTINUATION_STARTS or any(
        tokens[0].startswith(prefix) for prefix in _CONTINUATION_PREFIXES
    ):
        return True
    if tokens[0] == "طيب" and len(tokens) <= 7:
        return True
    if has_reference_tokens(question):
        return True
    if is_correction(question):
        return True
    if is_assistant_response_reference(question):
        return True
    # «اذكر اسم المصدر وتاريخه» سؤال metadata تابع بطبيعته: المصدر لأي
    # معلومة لا يُعرف إلا من الدور السابق. اقتران المصدر بالتاريخ/الاسم
    # يمنع جرّ سؤال مستقل مثل «ما مصدر الطاقة؟» إلى سياق قديم.
    if is_source_metadata_followup(question):
        return True
    if len(tokens) <= _SHORT_QUESTION_TOKENS and any(
        mark in norm for mark in _ELLIPTICAL_COST_MARKS
    ):
        return True
    # Shortness alone is not proof of a follow-up.  «من رئيس الجامعة؟» is a
    # complete independent question even though it is only three tokens.
    return False


def with_history_context(question: str, history: list) -> str:
    """Prefix the previous user turn onto the retrieval query when the current
    question can't stand alone. BM25+RRF then ranks the shared topic («تأجيل»)
    above lexical noise, and dense retrieval sees the full intent.

    Chain hop: when previous turns are themselves follow-ups («التخصصات؟» ←
    «قصدي بكالوريوس» ← «وأنا علمي» ← «رتبهم»), climb until the first
    self-contained topic, at most four turns. This handles looping dialogue
    without a question-specific resolver.

    Freshness gate: chaining happens ONLY onto a turn from the CURRENT sitting
    (is_fresh) — the history is persistent across days, and without this gate
    today's «أذكرهم» inherited yesterday's department-heads topic."""
    if not history or not needs_history_context(question):
        return question
    if not is_fresh(history[-1]):
        return question  # آخر دور من جلسة قديمة — لا وراثة موضوع
    parts = []
    index = len(history) - 1
    while index >= 0 and len(parts) < 4:
        turn = history[index]
        if not is_fresh(turn):
            break
        previous = str(turn.get("user") or "").strip()
        if not previous:
            break
        parts.insert(0, previous)
        if not needs_history_context(previous):
            break
        index -= 1
    if not parts:
        return question
    return " — ".join(parts + [question])
