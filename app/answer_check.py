"""فاحص إجابة حتمي (المرحلة 3 من خطة التحسين) — regex لا LLM حاكم.

فحوص عالية الدقة بعد التوليد، كلفتها صفرية، وعند فشل أحدها يعاد التوليد مرة
واحدة بتعليمة تصحيحية:
  ١. نسب مئوية يتيمة: نسبة في الجواب لا وجود لأرقامها في المقاطع/السؤال/
     الذاكرة = هلوسة رقم (النمط القاتل: «الطب 80%» المنتحلة). النسب حصراً —
     لا كل الأرقام — لأن مجاميع الرسوم المحسوبة (13+18×15=283) مشروعة
     بقاعدة البرومت 14، بينما مفاتيح القبول لا تُشتق حسابياً أبداً.
  ٢. خرق الاستبعاد: السائل قال «مش منح» وظهرت المنح في الجواب.
  ٣. خرق المرحلة: سؤال بكالوريوس صريح وجواب يتمحور حول الماجستير/الدكتوراه.
  ٤. رابط/بريد/هاتف أو سنة مؤرخة لا يسندها أي مقطع.
  ٥. ادعاء أن الشات نفّذ حذفاً فعلياً، أو تحويل طلب قائمة كاملة إلى أمثلة.
"""

import re
from typing import Iterable, List

from app import query_rewrite
from app.text_norm import normalize_arabic

_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[%٪]")
_GRAD_TERMS = ("ماجستير", "الماجستير", "دكتوراه", "الدكتوراه", "اطروحه", "أطروحة")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>{}\[\]\"'`]+", re.IGNORECASE)
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[\w.%+-]+@[\w.-]+\.[A-Za-z\u0600-\u06ff]{2,}",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(
    r"(?:هاتف|جوال|واتس(?:اب)?|اتصل|للاتصال|"
    r"رقم\s+(?:الهاتف|الجوال|التواصل|الجامع[هة])|phone|mobile|tel)"
    r"[^0-9\u0660-\u0669\u06f0-\u06f9]{0,24}"
    r"((?:\+|00)?[\d\u0660-\u0669\u06f0-\u06f9]"
    r"[\d\u0660-\u0669\u06f0-\u06f9\s().\-]{5,}"
    r"[\d\u0660-\u0669\u06f0-\u06f9])",
    re.IGNORECASE,
)
_LABELLED_YEAR_RE = re.compile(
    r"(?:عام|سنة|سنه|للعام|العام|تأسست|تاسست|آخر\s+تحقق|اخر\s+تحقق|"
    r"تاريخ\s+التحقق|تحديث|محد[ثّ]ة?)"
    r"[^0-9\u0660-\u0669\u06f0-\u06f9]{0,18}"
    r"((?:19|20)[0-9]{2}(?:\s*[-/–]\s*(?:(?:19|20)[0-9]{2}|[0-9]{2}))?)",
    re.IGNORECASE,
)
_LABELLED_DATE_RE = re.compile(
    r"(?:آخر\s+تحقق|اخر\s+تحقق|تاريخ(?:\s+التحقق)?|تحديث)"
    r"[^0-9\u0660-\u0669\u06f0-\u06f9]{0,18}"
    r"((?:19|20)[0-9]{2}\s*[-/‐‑‒–—]\s*[0-9]{1,2}"
    r"\s*[-/‐‑‒–—]\s*[0-9]{1,2})",
    re.IGNORECASE,
)
_LINK_IDENTIFIER_RE = re.compile(
    r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", re.IGNORECASE
)
_DIGIT_TRANSLATION = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)
_TRUSTED_CONTACT_EVIDENCE = (
    "https://www.iugaza.edu.ps/ هاتف +970-8-2644400 regist@iugaza.edu.ps"
)
_DELETE_REQUEST_MARKS = (
    "احذف", "امسح", "حذف", "مسح", "انسى", "انس ", "لا تخزن",
)
_DELETE_TARGET_MARKS = (
    "بيانات", "سجل", "محادث", "ذاكره", "ذاكرت", "حساب", "رسائل", "معلوماتي",
)
_DELETE_CLAIMS = (
    "تم الحذف", "تم حذف", "تم مسح", "حذفت بيانات", "مسحت بيانات", "حذفت السجل",
    "مسحت السجل", "لن يتم تخزين", "لن احتفظ", "لا احتفظ ببيانات",
)
_INCOMPLETE_LIST_MARKS = (
    "على سبيل المثال", "ومن الامثله", "من الامثله", "من ابرز",
)
_WRONG_HIGHER_MARKS = (
    "اعلى من معدلك", "اكبر من معدلك", "يتجاوز معدلك", "لا يحققه معدلك",
)
_VAGUE_ADMISSION_GROUPS = (
    "جميع البرامج", "كل البرامج", "وغيرها", "وما شابه", "غير مفصله",
)
_DIRECT_RESOURCE_TERMS = ("وصف المساق", "وصف المساقات")
_UNCERTAINTY_MARKS = (
    "غير وارد", "غير مذكور", "لا تتوفر", "لا يوجد في المقاطع",
    "لا استطيع تاكيد", "لا يمكن تاكيد", "يحتاج تاكيدا",
)
_SOURCE_LINE_RE = re.compile(
    r"(?:اسم\s+المصدر|المصدر)\s*[:：]\s*\**([^\n*،,؛;]+)",
    re.IGNORECASE,
)


def unsupported_percentages(answer: str, sources: Iterable[str]) -> List[str]:
    """النسب المئوية في الجواب التي لا يظهر رقمها في أي مصدر مغذٍّ للبرومت."""
    blob = " ".join(sources)
    orphans = []
    for m in _PERCENT_RE.finditer(answer):
        digits = m.group(1).replace(",", ".")
        if digits not in blob and digits.split(".")[0] not in blob:
            orphans.append(m.group(0))
    return orphans


def violated_exclusions(answer: str, excluded: Iterable[str]) -> List[str]:
    """المواضيع التي استبعدها السائل صراحةً وظهرت في الجواب رغم ذلك."""
    norm_answer = normalize_arabic(answer)
    return [t for t in excluded if t and t in norm_answer]


def level_violation(answer: str, asked_level: str | None) -> bool:
    """سؤال بكالوريوس صريح أجيب بمصطلحات دراسات عليا (أكثر من ذكر عابر واحد
    للنفي — «لا يوجد ماجستير» المشروعة تمر)."""
    if asked_level != "bachelor":
        return False
    norm = normalize_arabic(answer)
    hits = sum(norm.count(normalize_arabic(t)) for t in _GRAD_TERMS)
    return hits >= 2


def _canonical_url(value: str) -> str:
    value = value.rstrip(".,،؛;:!?؟)]}»\"'")
    if value.lower().startswith("www."):
        value = "https://" + value
    return value.rstrip("/").lower()


def unsupported_urls(answer: str, sources: Iterable[str]) -> List[str]:
    """روابط الجواب التي لا يظهر رابط مكافئ لها في الدليل أو الحقائق الثابتة."""
    source_blob = " ".join(sources) + " " + _TRUSTED_CONTACT_EVIDENCE
    supported = {_canonical_url(m.group(0)) for m in _URL_RE.finditer(source_blob)}
    return [
        m.group(0).rstrip(".,،؛;:!?؟)]}»\"'")
        for m in _URL_RE.finditer(answer)
        if _canonical_url(m.group(0)) not in supported
    ]


def unsupported_emails(answer: str, sources: Iterable[str]) -> List[str]:
    source_blob = " ".join(sources) + " " + _TRUSTED_CONTACT_EVIDENCE
    supported = {m.group(0).lower() for m in _EMAIL_RE.finditer(source_blob)}
    return [
        m.group(0)
        for m in _EMAIL_RE.finditer(answer)
        if m.group(0).lower() not in supported
    ]


def _phone_digits(value: str) -> str:
    return re.sub(r"\D", "", value.translate(_DIGIT_TRANSLATION))


def unsupported_phones(answer: str, sources: Iterable[str]) -> List[str]:
    source_blob = " ".join(sources) + " " + _TRUSTED_CONTACT_EVIDENCE
    supported = [_phone_digits(m.group(1)) for m in _PHONE_RE.finditer(source_blob)]
    unsupported: List[str] = []
    for match in _PHONE_RE.finditer(answer):
        raw = match.group(1).strip()
        digits = _phone_digits(raw)
        if len(digits) < 7:
            continue
        if not any(
            digits == candidate
            or (len(candidate) >= 7 and digits.endswith(candidate))
            or (len(digits) >= 7 and candidate.endswith(digits))
            for candidate in supported
        ):
            unsupported.append(raw)
    return unsupported


def unsupported_labelled_years(answer: str, sources: Iterable[str]) -> List[str]:
    """سنوات يقدّمها الجواب كتاريخ/تحديث ولا تظهر في دليل الاسترجاع."""
    source_blob = " ".join(sources).translate(_DIGIT_TRANSLATION)
    source_years = set(re.findall(r"(?:19|20)[0-9]{2}", source_blob))
    answer_ascii = answer.translate(_DIGIT_TRANSLATION)
    unsupported: List[str] = []
    for match in _LABELLED_YEAR_RE.finditer(answer_ascii):
        for year in re.findall(r"(?:19|20)[0-9]{2}", match.group(1)):
            if year not in source_years and year not in unsupported:
                unsupported.append(year)
    return unsupported


def _canonical_date(value: str) -> str:
    value = value.translate(_DIGIT_TRANSLATION)
    for dash in "‐‑‒–—":
        value = value.replace(dash, "-")
    return re.sub(r"\s+", "", value).replace("/", "-")


def unsupported_labelled_dates(answer: str, sources: Iterable[str]) -> List[str]:
    """التاريخ الكامل المنسوب إلى تحقق/مصدر يجب أن يظهر كاملاً في الدليل.

    فحص السنة وحدها كان يمرر «2026-07-15» لأن دليلاً آخر يحمل سنة 2026
    رغم أن التاريخ الصحيح في سجل المصدر هو «2026-07-18».
    """
    source_blob = " ".join(sources).translate(_DIGIT_TRANSLATION)
    supported = {
        _canonical_date(match.group(0))
        for match in re.finditer(
            r"(?:19|20)[0-9]{2}\s*[-/‐‑‒–—]\s*[0-9]{1,2}"
            r"\s*[-/‐‑‒–—]\s*[0-9]{1,2}",
            source_blob,
        )
    }
    unsupported: List[str] = []
    answer_ascii = answer.translate(_DIGIT_TRANSLATION)
    for match in _LABELLED_DATE_RE.finditer(answer_ascii):
        raw = match.group(1)
        if _canonical_date(raw) not in supported and raw not in unsupported:
            unsupported.append(raw)
    return unsupported


def link_identifier_claims(question: str, answer: str) -> List[str]:
    """`link_id: admission_application` معرّف داخلي لا URL قابل للنقر."""
    if "رابط" not in normalize_arabic(question):
        return []
    found: List[str] = []
    for match in _LINK_IDENTIFIER_RE.finditer(answer):
        prefix = normalize_arabic(answer[max(0, match.start() - 80):match.start()])
        if "رابط" in prefix and match.group(0) not in found:
            found.append(match.group(0))
    return found


def vague_admission_groups(answer: str, sources: Iterable[str]) -> List[str]:
    """وجود أسماء البرامج في الجدول يمنع اختصارها بـ«جميع البرامج/وغيرها»."""
    source_list = list(sources)
    source_blob = "\n".join(source_list)
    if (
        "جدول مفاتيح القبول" not in source_blob
        or "البرامج:" not in source_blob
        # سؤال فرع بلا معدل قد يطلب خريطة عامة للكليات، لا قائمة كل برنامج.
        # التشدد التفصيلي هنا محجوز لحالة أهلية بمعدل معلوم.
        or "معدل الثانوية الذي ذكره المستخدم" not in source_blob
    ):
        return []
    expected = _expected_admission_faculties(source_list)
    found: List[str] = []
    generic_faculty_lines = 0
    for line in answer.splitlines():
        norm_line = normalize_arabic(line)
        # «هذه القائمة تشمل جميع البرامج التي تحقق الشرط» وصف للقائمة
        # المكتملة، لا اختصار لبرامج كلية. نرفض العبارة فقط في سطر كلية.
        if expected and not any(
            normalize_arabic(faculty) in norm_line for faculty in expected
        ):
            continue
        line_has_generic = False
        for mark in _VAGUE_ADMISSION_GROUPS:
            if normalize_arabic(mark) not in norm_line:
                continue
            line_has_generic = True
            if mark not in found:
                found.append(mark)
        if line_has_generic:
            generic_faculty_lines += 1
    # كلية واحدة مختصرة داخل جواب طويل نقص غير مثالي لكنه مقبول. نتدخل
    # عندما يصبح الاختصار نمطاً، أو حين لا يتضمن الجدول سوى كلية/كليتين.
    minimum = 1 if len(expected) <= 2 else 2
    return found if generic_faculty_lines >= minimum else []


def _expected_admission_faculties(sources: Iterable[str]) -> List[str]:
    expected: List[str] = []
    for source in sources:
        if not source.startswith("جدول مفاتيح القبول"):
            continue
        for line in source.splitlines():
            if "| البرامج:" not in line:
                continue
            faculty = line.split("|", 1)[0].strip()
            if faculty and faculty not in expected:
                expected.append(faculty)
    return expected


def missing_admission_faculties(answer: str, sources: Iterable[str]) -> List[str]:
    """كل كلية باقية بعد ترشيح الفرع/المعدل يجب أن تظهر في الجواب."""
    expected = _expected_admission_faculties(sources)
    norm_answer = normalize_arabic(answer)
    missing = [
        faculty for faculty in expected
        if normalize_arabic(faculty) not in norm_answer
    ]
    # الجواب المفيد لا يلزم أن يكون مثالياً: لا نعيد التوليد بسبب كلية واحدة.
    # نتدخل فقط عندما يفقد الجواب قرابة ثلث الكليات المؤهلة.
    severe_threshold = max(2, (len(expected) + 2) // 3)
    return missing if len(missing) >= severe_threshold else []


def mismatched_source_date(answer: str, sources: Iterable[str]) -> List[str]:
    """المصدر والتاريخ في الجواب يجب أن يجتمعا في سجل الدليل نفسه."""
    source_match = _SOURCE_LINE_RE.search(answer)
    date_match = _LABELLED_DATE_RE.search(answer.translate(_DIGIT_TRANSLATION))
    if not source_match or not date_match:
        return []
    label = source_match.group(1).strip(" .،؛:()[]{}\"'`")
    norm_label = normalize_arabic(label)
    if norm_label.startswith("ملف "):
        norm_label = norm_label[4:].strip()
    candidates = [
        source for source in sources
        if norm_label and norm_label in normalize_arabic(source)
    ]
    wanted = _canonical_date(date_match.group(1))
    if not candidates or not any(
        wanted in _canonical_date(source) for source in candidates
    ):
        return [f"{label} ↔ {date_match.group(1)}"]
    return []


def unsupported_direct_resource_claim(
    question: str, answer: str, sources: Iterable[str]
) -> bool:
    """لا نحول خدمة مجاورة إلى مسار مؤكد لمورد دقيق غير مذكور في الدليل."""
    norm_question = normalize_arabic(question)
    requested = [
        normalize_arabic(term)
        for term in _DIRECT_RESOURCE_TERMS
        if normalize_arabic(term) in norm_question
    ]
    if not requested:
        return False
    evidence = normalize_arabic("\n".join(sources))
    if any(term in evidence for term in requested):
        return False
    norm_answer = normalize_arabic(answer)
    if any(mark in norm_answer for mark in _UNCERTAINTY_MARKS):
        return False
    return True


def mislabelled_guide_as_direct_link(
    question: str, answer: str, sources: Iterable[str]
) -> List[str]:
    """لا نسمّي رابط دليل الخطوات بأنه رابط المورد المباشر نفسه."""
    norm_question = normalize_arabic(question)
    if (
        "رابط" not in norm_question
        or not any(mark in norm_question for mark in ("نفسه", "مباشر"))
        or any(mark in norm_question for mark in ("رابط الدليل", "رابط الخطوات"))
    ):
        return []
    source_list = list(sources)
    found: List[str] = []
    for match in _URL_RE.finditer(answer):
        raw_url = match.group(0)
        canonical = _canonical_url(raw_url)
        records = [
            source for source in source_list
            if raw_url.rstrip(".,،؛;:!?؟)]}»\"'") in source
        ]
        if not records:
            continue
        record_blob = normalize_arabic("\n".join(records))
        is_guide = (
            "/guide/" in canonical
            or "title: خطوات" in record_blob
            or "الموضوع: خطوات" in record_blob
            or "دليل الخطوات" in record_blob
        )
        # حقل link_id يثبت أن هدفاً داخلياً ذُكر بلا URL؛ الرابط الظاهر
        # والموسوم «خطوات/guide» يبقى مرجعاً للخطوات لا رابط الهدف نفسه.
        has_unresolved_target = any(
            "link_id:" in normalize_arabic(source) for source in source_list
        )
        if is_guide and has_unresolved_target and raw_url not in found:
            found.append(raw_url)
    return found


def contradicted_active_branch(answer: str, sources: Iterable[str]) -> List[str]:
    """يلتقط رجوع الجواب إلى فرع قديم رغم تثبيت الفرع الأحدث."""
    prefix = "الفرع الحالي الذي ذكره المستخدم:"
    expected = None
    for source in sources:
        if source.startswith(prefix):
            expected = source.split(":", 1)[1].strip()
            break
    if not expected:
        return []
    norm_expected = normalize_arabic(expected)
    branches = ("علمي", "أدبي", "شرعي", "تجاري", "صناعي")
    conflicts: List[str] = []
    for line in answer.splitlines():
        norm_line = normalize_arabic(line)
        for branch in branches:
            norm_branch = normalize_arabic(branch)
            if norm_branch == norm_expected:
                continue
            positions = [
                norm_line.find(phrase)
                for phrase in (
                    f"الفرع ال{norm_branch}",
                    f"الفرع {norm_branch}",
                    f"فرع ال{norm_branch}",
                    f"فرع {norm_branch}",
                )
                if norm_line.find(phrase) >= 0
            ]
            if not positions:
                continue
            start = min(positions)
            window = norm_line[max(0, start - 18):start + 35]
            if any(
                negation in window
                for negation in ("لا يقبل", "لا تقبل", "غير متاح", "لا يتاح")
            ):
                continue
            label = f"{expected} ↔ {branch}"
            if label not in conflicts:
                conflicts.append(label)
    return conflicts


def false_deletion_claim(question: str, answer: str) -> bool:
    """الشات يجيب فقط؛ لا يملك صلاحية تنفيذ حذف بيانات من داخل الرد."""
    norm_question = normalize_arabic(question)
    if not (
        any(mark in norm_question for mark in _DELETE_REQUEST_MARKS)
        and any(mark in norm_question for mark in _DELETE_TARGET_MARKS)
    ):
        return False
    norm_answer = normalize_arabic(answer)
    for claim in _DELETE_CLAIMS:
        normalized_claim = normalize_arabic(claim)
        start = norm_answer.find(normalized_claim)
        if start < 0:
            continue
        prefix = norm_answer[max(0, start - 35):start]
        if not any(
            negation in prefix
            for negation in ("لا استطيع", "لا يمكنني", "لم ", "لا اقدر", "لا املك")
        ):
            return True
    return False


def incomplete_list_style(question: str, answer: str) -> bool:
    """طلب «كل/جميع» لا يجوز أن يتحول إلى أمثلة منتقاة."""
    if not query_rewrite.wants_complete_list(question):
        return False
    norm = normalize_arabic(answer)
    # جواب مطوّل ومفيد قد يقول «من أبرز» ثم يغطي معظم العناصر؛ لا نعيد
    # توليده لمجرد أنه ليس مثالياً. هذا الحارس يبقى للقوائم القصيرة جداً.
    if len(norm) >= 250:
        return False
    for mark in _INCOMPLETE_LIST_MARKS:
        normalized_mark = normalize_arabic(mark)
        start = norm.find(normalized_mark)
        if start < 0:
            continue
        prefix = norm[max(0, start - 15):start]
        if "ليس" not in prefix and "ليست" not in prefix:
            return True
    return False


def inconsistent_admission_comparisons(question: str, answer: str) -> List[str]:
    """يلتقط تناقضاً حسابياً صريحاً مثل «70% أعلى من معدلك 85%»."""
    if not query_rewrite.has_admission_intent(question):
        return []
    question_ascii = question.translate(_DIGIT_TRANSLATION)
    rate_matches = list(_PERCENT_RE.finditer(question_ascii))
    if not rate_matches:
        return []
    try:
        student_rate = float(rate_matches[0].group(1).replace(",", "."))
    except ValueError:
        return []

    contradictions: List[str] = []
    for line in answer.translate(_DIGIT_TRANSLATION).splitlines():
        norm_line = normalize_arabic(line)
        if not any(
            normalize_arabic(mark) in norm_line for mark in _WRONG_HIGHER_MARKS
        ):
            continue
        for match in _PERCENT_RE.finditer(line):
            try:
                cutoff = float(match.group(1).replace(",", "."))
            except ValueError:
                continue
            if cutoff <= student_rate:
                label = f"{cutoff:g}% مقابل معدل {student_rate:g}%"
                if label not in contradictions:
                    contradictions.append(label)
    return contradictions


def branch_exclusivity_overclaim(
    question: str, answer: str, sources: Iterable[str]
) -> bool:
    """«تقبل العلمي» لا تعني أن البرنامج «علمي فقط»."""
    norm_question = normalize_arabic(question)
    if "علمي" not in norm_question:
        return False
    norm_answer = normalize_arabic(answer)
    exclusive_claims = (
        "جميع هذه التخصصات تتطلب فرعا علميا فقط",
        "جميع هذه البرامج تتطلب فرعا علميا",
        "كل التخصصات المذكوره للفرع العلمي فقط",
        "جميعها للفرع العلمي فقط",
        "جميعها علمي فقط",
    )
    if not any(claim in norm_answer for claim in exclusive_claims):
        return False
    # لا نرفض العبارة إن كان الدليل فعلاً كله علمي فقط. يكفي سطر واحد يحمل
    # العلمي مع فرع آخر لإثبات أن التعميم خاطئ.
    for source in sources:
        norm_source = normalize_arabic(source)
        for line in norm_source.splitlines():
            if "علمي" in line and any(
                branch in line for branch in ("ادبي", "شرعي", "تجاري", "صناعي")
            ):
                return True
    return False


def problems(answer: str, *, sources: Iterable[str], excluded: Iterable[str],
             asked_level: str | None, question: str = "") -> List[str]:
    """قائمة مشاكل الجواب بصياغة تعليمات تصحيحية جاهزة للبرومت — فارغة = سليم."""
    source_list = list(sources)
    # السؤال نفسه يسند رقماً كرره المستخدم، لكنه ليس وثيقة تسند رابط اتصال
    # أو تاريخاً جامعياً جديداً. أزل نسخة السؤال من فحوص الحقائق الدقيقة.
    evidence = [source for source in source_list if not question or source != question]
    found: List[str] = []
    percentage_sources = list(source_list)
    if question and question not in percentage_sources:
        percentage_sources.append(question)
    orphans = unsupported_percentages(answer, percentage_sources)
    if orphans:
        found.append(
            "وردت في جوابك نسب غير موجودة في المقاطع المعطاة: "
            + "، ".join(orphans)
            + " — احذفها أو استبدلها بما تسنده المقاطع حرفياً، ولا تخترع نسبة."
        )
    broken = violated_exclusions(answer, excluded)
    if broken:
        found.append(
            "ذكرتَ مواضيع استبعدها السائل صراحةً: " + "، ".join(broken)
            + " — أعد الجواب بدونها تماماً."
        )
    if level_violation(answer, asked_level):
        found.append(
            "السائل يسأل عن البكالوريوس وجوابك تمحور حول الدراسات العليا — "
            "أجب عن البكالوريوس."
        )
    if evidence:
        urls = unsupported_urls(answer, evidence)
        emails = unsupported_emails(answer, evidence)
        phones = unsupported_phones(answer, evidence)
        years = unsupported_labelled_years(answer, evidence)
        dates = unsupported_labelled_dates(answer, evidence)
        unsupported_exact = urls + emails + phones + years + dates
        if unsupported_exact:
            found.append(
                "أضفت رابطاً/بريداً/هاتفاً/سنة مؤرخة غير موجودة في المقاطع: "
                + "، ".join(unsupported_exact)
                + " — استخدم القيمة الواردة حرفياً أو احذفها وصرّح بعدم توفرها."
            )
    identifiers = link_identifier_claims(question, answer)
    if identifiers:
        found.append(
            "قدّمتَ معرّفاً داخلياً على أنه رابط: "
            + "، ".join(identifiers)
            + " — هذا ليس رابطاً قابلاً للفتح. الرابط يجب أن يبدأ بـ http:// "
            "أو https://؛ إن لم يوجد في المقاطع فصرّح بعدم توفره."
        )
    mislabelled_guides = mislabelled_guide_as_direct_link(
        question, answer, evidence
    )
    if mislabelled_guides:
        found.append(
            "سمّيتَ رابط دليل/خطوات بأنه رابط المورد المباشر نفسه: "
            + "، ".join(mislabelled_guides)
            + " — سمّه دليل الخطوات فقط؛ الرابط المباشر غير وارد في الدليل."
        )
    vague_groups = vague_admission_groups(answer, evidence)
    if vague_groups:
        found.append(
            "اختصرتَ قائمة قبول تحمل أسماء البرامج بعبارات مبهمة: "
            + "، ".join(vague_groups)
            + " — اذكر أسماء البرامج الموجودة بعد «البرامج:» لكل كلية، "
            "ولا تستخدم «جميع البرامج» أو «وغيرها» بديلاً عنها."
        )
    missing_faculties = missing_admission_faculties(answer, evidence)
    if missing_faculties:
        found.append(
            "أسقطتَ كليات مؤهلة ما زالت موجودة في جدول الفرع/المعدل المصفّى: "
            + "، ".join(missing_faculties)
            + " — أضف كل كلية مع برامجها، ولا تكتفِ بأول كلية."
        )
    source_date_errors = mismatched_source_date(answer, evidence)
    if source_date_errors:
        found.append(
            "ربطتَ مصدراً بتاريخ مأخوذ من سجل آخر أو غير موجود معه: "
            + "، ".join(source_date_errors)
            + " — استخدم المصدر وتاريخ التحقق من المقطع نفسه، أو قل إن "
            "التاريخ غير مذكور لهذا المصدر."
        )
    branch_conflicts = contradicted_active_branch(answer, source_list)
    if branch_conflicts:
        found.append(
            "أجبتَ لفرع قديم يخالف الفرع الأحدث المثبت في الحوار: "
            + "، ".join(branch_conflicts)
            + " — أعد القائمة للفرع الحالي وحده، ولا تستخدم قيود دور أقدم."
        )
    if unsupported_direct_resource_claim(question, answer, evidence):
        found.append(
            "وصفتَ مساراً مؤكداً للوصول إلى المورد المطلوب، لكن اسم المورد "
            "نفسه غير موجود في المقاطع — لا تستنتج وجوده من خدمة قريبة؛ "
            "صرّح بأن المسار الدقيق غير وارد ولا تخترع خطوات."
        )
    if false_deletion_claim(question, answer):
        found.append(
            "ادعيتَ تنفيذ حذف أو مسح بيانات، لكن الرد النصي لا ينفذ عمليات على "
            "الخادم — لا تقل «تم الحذف». اشرح للمستخدم طريقة الحذف المتاحة أو "
            "أنك لا تستطيع تنفيذه من داخل المحادثة."
        )
    if incomplete_list_style(question, answer):
        found.append(
            "السائل طلب قائمة كاملة لكنك قدمت أمثلة/أبرز العناصر فقط — استخرج "
            "جميع العناصر التي تسندها المقاطع. إن كان الدليل ناقصاً فاذكر حدوده "
            "صراحةً ولا تخترع عناصر."
        )
    comparison_errors = inconsistent_admission_comparisons(question, answer)
    if comparison_errors:
        found.append(
            "عكستَ مقارنة مفتاح القبول بمعدل الطالب: "
            + "، ".join(comparison_errors)
            + " — المفتاح الأصغر من معدل الطالب أو المساوي له يحققه الطالب؛ "
            "راجع كل سطر وأصلح الوصف والقائمة."
        )
    if branch_exclusivity_overclaim(question, answer, evidence):
        found.append(
            "عمّمت أن جميع البرامج «للفرع العلمي فقط»، بينما الدليل يتضمن "
            "برامج تقبل العلمي مع فروع أخرى — قل إنها تقبل الفرع العلمي، "
            "ولا تقل «فقط» إلا للبرنامج الذي ينص دليله على الحصرية."
        )
    return found
