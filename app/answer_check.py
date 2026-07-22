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
from app.text_norm import normalize_arabic, tokenize

_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[%٪]")
_MONEY_RE = re.compile(
    r"([0-9٠-٩۰-۹]+(?:[.,][0-9٠-٩۰-۹]+)?)\s*"
    r"(دينار(?:اً|ا)?|شيكل(?:اً|ا)?|دولار(?:اً|ا)?)"
)
_STRUCTURED_AMOUNT_RE = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_\[\].]*\.)?"
    r"(?:amount|fee|credit_hour_fee|hour_fee|tuition)\s*:\s*"
    r"([0-9٠-٩۰-۹]+(?:[.,][0-9٠-٩۰-۹]+)?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_STRUCTURED_KEY_RE = re.compile(
    r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_\[\].]*)\s*:",
    re.IGNORECASE,
)
_STRUCTURED_ENTITY_LEAVES = (
    "program_name", "program", "specialization", "major", "department",
    "faculty", "faculty_name", "college", "degree_or_request",
    "service_type", "title", "topic", "category", "name",
)
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
_INTERNAL_METADATA_KEY_RE = re.compile(
    r"\b(?:link_id|action|embedding_text|document_type|intents?|keywords?)\s*:",
    re.IGNORECASE,
)
_QUOTED_PHRASE_RE = re.compile(r'[«“"]([^»”"\n]{2,100})[»”"]')
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


def _canonical_amount(value: str) -> str:
    translated = value.translate(_DIGIT_TRANSLATION).replace(",", ".")
    try:
        return f"{float(translated):g}"
    except ValueError:
        return translated


def _canonical_currency(value: str) -> str:
    norm = normalize_arabic(value)
    for currency in ("دينار", "شيكل", "دولار"):
        if currency in norm:
            return currency
    return norm


def unsupported_money_amounts(
    answer: str,
    sources: Iterable[str],
    *,
    question: str = "",
    entity_terms: Iterable[str] = (),
) -> List[str]:
    """Currency amounts must occur in evidence for the requested entity.

    Arithmetic totals are allowed when the answer visibly shows a formula;
    the validator targets quoted fees/prices, not transparent calculations.
    """
    norm_question = normalize_arabic(question)
    if not any(mark in norm_question for mark in ("رسم", "رسوم", "سعر", "تكلف", "مبلغ")):
        return []
    normalized_entities = []
    for term in entity_terms:
        normalized = normalize_arabic(str(term))
        if not normalized:
            continue
        variants = [normalized]
        without_conjunction = normalized[1:] if normalized.startswith("و") else normalized
        if without_conjunction != normalized:
            variants.append(without_conjunction)
        if without_conjunction.startswith("ال") and len(without_conjunction) > 4:
            variants.append(without_conjunction[2:])
        for variant in variants:
            if len(variant) >= 3 and variant not in normalized_entities:
                normalized_entities.append(variant)
    source_list = list(sources)
    unsupported: List[str] = []
    for match in _MONEY_RE.finditer(answer):
        line_start = answer.rfind("\n", 0, match.start()) + 1
        line_end = answer.find("\n", match.end())
        if line_end < 0:
            line_end = len(answer)
        line = answer[line_start:line_end]
        if any(operator in line for operator in ("=", "+", "×", "*")):
            continue
        wanted_amount = _canonical_amount(match.group(1))
        wanted_currency = _canonical_currency(match.group(2))
        supported = False
        for source in source_list:
            source_matches = list(_MONEY_RE.finditer(source))
            candidates = [
                item for item in source_matches
                if _canonical_amount(item.group(1)) == wanted_amount
                and _canonical_currency(item.group(2)) == wanted_currency
            ]
            structured_amounts = [
                item for item in _STRUCTURED_AMOUNT_RE.finditer(source)
                if _canonical_amount(item.group(1)) == wanted_amount
            ]
            # Uploaded fee records use ``amount`` as a numeric JOD field.
            # Accept that exact structured value only for a single-record,
            # entity-bound source; never use a number from a multi-record blob.
            if (
                not candidates
                and wanted_currency == "دينار"
                and len(structured_amounts) == 1
            ):
                norm_source = normalize_arabic(source)
                all_structured = list(_STRUCTURED_AMOUNT_RE.finditer(source))
                amount_line_start = source.rfind("\n", 0, structured_amounts[0].start()) + 1
                amount_line_end = source.find("\n", structured_amounts[0].end())
                if amount_line_end < 0:
                    amount_line_end = len(source)
                amount_line = source[amount_line_start:amount_line_end]
                key_match = _STRUCTURED_KEY_RE.match(amount_line)
                key = key_match.group("key") if key_match else ""
                prefix = key.rsplit(".", 1)[0] if "." in key else ""
                indexed_entity_match = False
                if prefix and normalized_entities:
                    sibling_re = re.compile(
                        rf"^\s*{re.escape(prefix)}\."
                        rf"(?:{'|'.join(_STRUCTURED_ENTITY_LEAVES)})\s*:\s*(.+)$",
                        re.IGNORECASE | re.MULTILINE,
                    )
                    sibling_text = "\n".join(
                        sibling.group(1) for sibling in sibling_re.finditer(source)
                    )
                    norm_sibling = normalize_arabic(sibling_text)
                    indexed_entity_match = any(
                        entity in norm_sibling for entity in normalized_entities
                    )
                if (
                    indexed_entity_match
                    or (
                        len(all_structured) == 1
                        and (
                            not normalized_entities
                            or any(
                                entity in norm_source
                                for entity in normalized_entities
                            )
                        )
                    )
                ):
                    supported = True
                    break
            if not candidates:
                continue
            norm_source = normalize_arabic(source)
            if not normalized_entities:
                supported = True
                break
            if len(source_matches) == 1 and any(
                entity in norm_source for entity in normalized_entities
            ):
                supported = True
                break
            for candidate in candidates:
                left = max(
                    source.rfind(separator, 0, candidate.start())
                    for separator in ("\n", "،", ",", "؛", ";", ".")
                )
                right_positions = [
                    source.find(separator, candidate.end())
                    for separator in ("\n", "،", ",", "؛", ";", ".")
                ]
                right_positions = [position for position in right_positions if position >= 0]
                right = min(right_positions) if right_positions else len(source)
                segment = normalize_arabic(source[left + 1:right])
                entity_scopes_whole_record = any(
                    norm_source.find(entity) < source_matches[0].start()
                    for entity in normalized_entities
                    if norm_source.find(entity) >= 0
                )
                if entity_scopes_whole_record or any(
                    entity in segment for entity in normalized_entities
                ):
                    supported = True
                    break
            if supported:
                break
        if not supported:
            label = match.group(0)
            if label not in unsupported:
                unsupported.append(label)
    return unsupported


_COUNT_TARGETS = {
    "faculties": ("كليه", "كليات"),
    "programs": ("برنامج", "برامج", "تخصص", "تخصصات"),
}
_COUNT_WORDS = {
    "تسع": 9, "تسعه": 9, "عشر": 10, "عشره": 10,
    "احد عشر": 11, "احدى عشره": 11, "احدي عشره": 11,
    "اثنا عشر": 12, "اثنتا عشره": 12,
}


def _nearby_counts(text: str, target_terms: tuple[str, ...]) -> set[int]:
    norm = normalize_arabic(text).translate(_DIGIT_TRANSLATION)
    term_pattern = "|".join(re.escape(term) for term in target_terms)
    found = {
        int(match.group(1))
        for match in re.finditer(rf"(?<!\d)(\d{{1,3}})\s*(?:{term_pattern})", norm)
    }
    for word, value in _COUNT_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\s+(?:{term_pattern})", norm):
            found.add(value)
    return found


def contradicted_requested_count(
    question: str, answer: str, sources: Iterable[str]
) -> List[str]:
    norm_question = normalize_arabic(question)
    if not any(mark in norm_question for mark in ("كم عدد", "ما عدد", "عددهم")):
        return []
    for target_terms in _COUNT_TARGETS.values():
        if not any(term in norm_question for term in target_terms):
            continue
        expected = set()
        for source in sources:
            expected.update(_nearby_counts(source, target_terms))
        claimed = _nearby_counts(answer, target_terms)
        if len(expected) == 1 and claimed and not claimed <= expected:
            return [
                f"العدد المذكور {sorted(claimed)} لا يطابق العدد المسند {sorted(expected)}"
            ]
    return []


_ABSENCE_MARKS = (
    "لا يوجد", "لا توجد", "غير موجود", "غير موجوده", "غير متوفر",
    "غير متوفره", "لا تتوفر", "لم اجد",
)


def false_absence_claim(
    answer: str,
    sources: Iterable[str],
    *,
    evidence_sufficient: bool | None,
    retrieval_degraded: bool = False,
) -> bool:
    if not evidence_sufficient and not retrieval_degraded:
        return False
    norm_answer = normalize_arabic(answer)
    if not any(mark in norm_answer for mark in _ABSENCE_MARKS):
        return False
    norm_sources = normalize_arabic("\n".join(sources))
    # An explicit negative statement in the evidence may itself be the fact.
    return not any(mark in norm_sources for mark in _ABSENCE_MARKS)


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


def exposed_internal_metadata(answer: str) -> List[str]:
    """Structured retrieval keys are never user-facing steps or links."""
    return list(dict.fromkeys(
        match.group(0).rstrip(":").strip()
        for match in _INTERNAL_METADATA_KEY_RE.finditer(answer)
    ))


def unsupported_quoted_procedure_labels(
    question: str, answer: str, sources: Iterable[str]
) -> List[str]:
    """Reject invented interface labels/statuses in procedural answers."""
    norm_question = normalize_arabic(question)
    if not any(mark in norm_question for mark in (
        "كيف", "خطوه", "خطوات", "طلب", "بوابه", "تسجيل", "دفع",
        "استخرج", "اتأكد", "اتاكد",
    )):
        return []
    evidence = normalize_arabic("\n".join(sources))
    question_tokens = set(tokenize(question))
    status_marks = {
        "مرسل", "تم الارسال", "قيد المعالجه", "قيد المراجعه", "مقبول",
        "مرفوض", "مكتمل", "غير مكتمل",
    }
    unsupported: List[str] = []
    for match in _QUOTED_PHRASE_RE.finditer(answer):
        phrase = match.group(1).strip()
        normalized = normalize_arabic(phrase)
        if len(normalized) < 3 or normalized in evidence:
            continue
        phrase_tokens = set(tokenize(phrase))
        prefix = normalize_arabic(answer[max(0, match.start() - 70):match.start()])
        explicit_ui_label = any(mark in prefix for mark in (
            "شاشه", "صفحه", "زر", "اختر", "حاله", "قائمه",
        ))
        if explicit_ui_label:
            if phrase not in unsupported:
                unsupported.append(phrase)
            continue
        # Quoting/reordering words already supplied by the user is phrasing,
        # not an invented interface label.  A generic one-word caption is also
        # harmless unless it asserts a concrete workflow status.
        if (
            phrase_tokens
            and phrase_tokens.intersection(question_tokens)
            and normalized not in status_marks
        ):
            continue
        if len(phrase_tokens) == 1 and normalized not in status_marks:
            continue
        if phrase not in unsupported:
            unsupported.append(phrase)
    return unsupported


def unsupported_workflow_status_claims(
    question: str, answer: str, sources: Iterable[str]
) -> List[str]:
    """Do not invent a success/status signal for an uncertain web workflow."""
    norm_question = normalize_arabic(question)
    if not any(mark in norm_question for mark in (
        "انحفظ", "حفظ الطلب", "انرسل", "ارسل الطلب", "تاكيد واضح",
        "تأكيد واضح", "حاله الطلب", "حالة الطلب",
    )):
        return []
    evidence = normalize_arabic("\n".join(sources))
    norm_answer = normalize_arabic(answer)
    markers = (
        "رساله تاكيد", "اشعار علي الشاشه",
        "مقبول", "مرفوض", "قيد المراجعه", "قيد المعالجه",
    )
    unsupported: List[str] = []
    for marker in markers:
        start = norm_answer.find(marker)
        if start < 0 or marker in evidence:
            continue
        prefix = norm_answer[max(0, start - 70):start]
        if any(negation in prefix for negation in (
            "لا توجد", "لا يوجد", "لا يمكن تاكيد", "لا يمكن التاكد",
            "لا يمكن معرفه", "لا يمكن الجزم", "لا يمكن التحقق",
            "لا نستطيع", "لم يظهر", "غير موثق", "غير واضحه",
            "لا تعرض الادله", "لا تفترض",
        )):
            continue
        unsupported.append(marker)
    return unsupported


def unresolved_external_entry_claim(
    question: str, answer: str, sources: Iterable[str]
) -> bool:
    """Do not turn an explicitly unresolved visa/entry record into policy.

    These questions are outside the university's authority and are especially
    unsafe to answer from model memory.  A retrieved record marked unresolved
    must result in a caveat and official verification, not an invented visa
    class or issuing authority.
    """
    norm_question = normalize_arabic(question)
    if not any(mark in norm_question for mark in (
        "تاشيره", "تصريح الدخول", "دخول غزه",
    )):
        return False
    evidence = normalize_arabic("\n".join(sources))
    if not any(mark in evidence for mark in (
        "غير محسوم رسميا", "يحتاج تاكيد الجامعه", "غير موثق",
    )):
        return False
    norm_answer = normalize_arabic(answer)
    uncertainty = any(mark in norm_answer for mark in (
        "لا تتوفر", "غير متاح", "غير موثق", "لا يمكن تاكيد",
        "لا يمكن الجزم", "لا تحسم", "يحتاج تاكيد", "تحقق",
    ))
    return not uncertainty


def unsupported_application_fee_refund_claim(
    question: str, answer: str, sources: Iterable[str]
) -> bool:
    """A current waiver does not decide a refund for money already paid."""
    norm_question = normalize_arabic(question)
    if "دفعت" not in norm_question or "استرد" not in norm_question:
        return False
    evidence = normalize_arabic("\n".join(sources))
    direct_policy = any(mark in evidence for mark in (
        "رسوم طلب الالتحاق غير مسترده",
        "رسوم طلب الالتحاق مسترده",
        "يحق استرداد رسوم طلب الالتحاق",
        "لا يحق استرداد رسوم طلب الالتحاق",
    ))
    if direct_policy:
        return False
    norm_answer = normalize_arabic(answer)
    uncertainty = any(mark in norm_answer for mark in (
        "غير موثق", "لا يمكن تاكيد", "لا يمكن الجزم", "لا تتوفر",
        "لا توجد سياسه", "راجع", "تواصل",
    ))
    return not uncertainty


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
    explicit_direct_request = bool(
        "رابط" in norm_question
        and any(mark in norm_question for mark in ("نفسه", "مباشر"))
        and not any(mark in norm_question for mark in ("رابط الدليل", "رابط الخطوات"))
    )
    source_list = list(sources)
    found: List[str] = []
    for match in _URL_RE.finditer(answer):
        raw_url = match.group(0)
        canonical = _canonical_url(raw_url)
        line_start = answer.rfind("\n", 0, match.start()) + 1
        line_end = answer.find("\n", match.end())
        if line_end < 0:
            line_end = len(answer)
        answer_line = normalize_arabic(answer[line_start:line_end])
        procedural_portal_label = any(
            mark in answer_line
            for mark in ("بوابه", "بوابة", "رابط طلب الالتحاق", "ادخل الى")
        )
        if not explicit_direct_request and not procedural_portal_label:
            continue
        records = [
            source for source in source_list
            if raw_url.rstrip(".,،؛;:!?؟)]}»\"'") in source
        ]
        if not records:
            continue
        nearby_records = []
        for source in records:
            position = source.find(raw_url.rstrip(".,،؛;:!?؟)]}»\"'"))
            if position >= 0:
                nearby_records.append(source[max(0, position - 500):position + 500])
        nearby_blob = normalize_arabic("\n".join(nearby_records))
        is_guide = (
            "/guide/" in canonical
            or "title: خطوات" in nearby_blob
            or "الموضوع: خطوات" in nearby_blob
            or "دليل الخطوات" in nearby_blob
        )
        # حقل link_id يثبت أن هدفاً داخلياً ذُكر بلا URL؛ الرابط الظاهر
        # والموسوم «خطوات/guide» يبقى مرجعاً للخطوات لا رابط الهدف نفسه.
        has_unresolved_target = any(
            "link_id:" in normalize_arabic(source) for source in source_list
        )
        if (
            is_guide
            and (procedural_portal_label or has_unresolved_target)
            and raw_url not in found
        ):
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
    norm_answer_full = normalize_arabic(answer).strip()
    # In a rejected eligibility decision, mentioning the program's required
    # branch explains *why* the user's current branch is ineligible; it is not
    # stale-context leakage or replacement of the user's branch.
    eligibility_rejection = (
        "لا يسمح", "لا يمكنك", "لا يمكن", "لا يتيح", "لا تحقق",
        "غير مسموح", "غير متاح", "غير موهل", "غير موجود", "غير وارد",
        "لا يقبل", "لا تقبل", "لا تذكر", "لا تشمل", "لا يرد",
    )
    if norm_answer_full.startswith(("لا", "ليس")) or any(
        mark in norm_answer_full for mark in eligibility_rejection
    ):
        return []
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


def ignored_admission_branch_restriction(
    question: str,
    answer: str,
    sources: Iterable[str],
    entity_terms: Iterable[str],
) -> List[str]:
    """Reject a positive eligibility answer when the named program excludes
    the user's branch.

    Rate and branch are conjunctive admission conditions.  A high rate must
    never turn an explicitly scientific-only program into an eligible option
    for a literary-branch student.
    """
    if not query_rewrite.has_admission_intent(question):
        return []
    norm_question = normalize_arabic(question)
    branches = ("علمي", "ادبي", "شرعي", "تجاري", "صناعي")
    requested = next(
        (branch for branch in branches if branch in norm_question), None
    )
    terms = []
    for value in entity_terms:
        term = normalize_arabic(str(value))
        if term.startswith("ال") and len(term) > 4:
            term = term[2:]
        if len(term) >= 3 and term not in terms:
            terms.append(term)
    if requested is None or not terms:
        return []

    def lexical_core(value: str) -> str:
        values = []
        for token in re.findall(r"[\w\u0600-\u06ff]+", normalize_arabic(value)):
            if token.startswith("و") and len(token) > 4:
                token = token[1:]
            if token.startswith("ال") and len(token) > 4:
                token = token[2:]
            values.append(token)
        return " ".join(values)

    entity_phrase = " ".join(terms)

    allowed_sets = []
    for source in sources:
        for raw_line in source.splitlines():
            line = normalize_arabic(raw_line)
            # Require the entity as one adjacent phrase.  A wide faculty row
            # can contain ``تعليم العلوم`` and ``الحاسوب وأساليب تدريسه`` as
            # two different programs; their separated tokens must not be
            # mistaken for the single program ``علم الحاسوب``.
            if entity_phrase not in lexical_core(line) or "الفروع" not in line:
                continue
            branch_part = line.split("الفروع", 1)[1].split("|", 1)[0]
            allowed = {branch for branch in branches if branch in branch_part}
            if allowed:
                allowed_sets.append(allowed)
    if not allowed_sets or any(requested in allowed for allowed in allowed_sets):
        return []

    norm_answer = normalize_arabic(answer)
    rejection_marks = (
        "لا يسمح", "لا يمكنك", "لا يمكن", "لا يتيح", "لا تحقق",
        "غير مسموح", "غير متاح", "غير موهل", "غير موجود", "غير وارد",
        "لا يقبل", "لا تقبل", "لا تذكر", "لا تشمل", "لا يرد",
    )
    if norm_answer.strip().startswith(("لا", "ليس")) or any(
        mark in norm_answer for mark in rejection_marks
    ):
        return []
    allowed_label = "، ".join(sorted(set().union(*allowed_sets)))
    return [f"الفرع {requested} غير موجود ضمن الفروع المسموحة ({allowed_label})"]


def missing_cutoff_branch_scope(
    question: str,
    answer: str,
    sources: Iterable[str],
    entity_terms: Iterable[str],
) -> List[str]:
    """A program cutoff is incomplete when its branch scope is known but omitted."""
    norm_question = normalize_arabic(question)
    if not any(mark in norm_question for mark in (
        "الحد الادني", "اقل معدل", "مفتاح", "معدل القبول",
    )):
        return []
    # If the user already supplied a branch (typically in an eligibility
    # comparison), the answer need not repeat it as long as the branch/rate
    # decision is correct.  This check is for a bare cutoff question whose
    # answer would otherwise hide an important scope restriction.
    if any(branch in set(tokenize(question)) for branch in (
        "علمي", "ادبي", "شرعي", "تجاري", "صناعي",
    )):
        return []
    terms = []
    for value in entity_terms:
        term = normalize_arabic(str(value))
        if term.startswith("ال") and len(term) > 4:
            term = term[2:]
        if len(term) >= 3 and term not in terms:
            terms.append(term)
    if not terms:
        return []

    def lexical_core(value: str) -> str:
        values = []
        for token in re.findall(r"[\w\u0600-\u06ff]+", normalize_arabic(value)):
            if token.startswith("و") and len(token) > 4:
                token = token[1:]
            if token.startswith("ال") and len(token) > 4:
                token = token[2:]
            values.append(token)
        return " ".join(values)

    entity_phrase = " ".join(terms)
    branches = ("علمي", "ادبي", "شرعي", "تجاري", "صناعي")
    expected: set[str] = set()
    for source in sources:
        for raw_line in source.splitlines():
            line = normalize_arabic(raw_line)
            if entity_phrase not in lexical_core(line) or "الفروع" not in line:
                continue
            branch_part = line.split("الفروع", 1)[1].split("|", 1)[0]
            expected.update(branch for branch in branches if branch in branch_part)
    if not expected:
        return []
    norm_answer = normalize_arabic(answer)
    return [] if any(branch in norm_answer for branch in expected) else sorted(expected)


def inconsistent_eligibility_polarity(question: str, answer: str) -> bool:
    """An eligibility answer must not open with yes and conclude ineligible."""
    if not query_rewrite.has_admission_intent(question):
        return False
    norm = normalize_arabic(answer).strip()
    rejection_marks = (
        "لا يحقق", "لا تحقق", "لا يفي", "غير مول", "غير مسموح",
        "لا يسمح", "لا يمكنك", "لا يمكن قبول",
    )
    return norm.startswith("نعم") and any(mark in norm for mark in rejection_marks)


def guaranteed_final_admission(question: str, answer: str) -> List[str]:
    """Meeting an indexed cutoff is preliminary eligibility, not admission."""
    if not query_rewrite.has_admission_intent(question):
        return []
    norm = normalize_arabic(answer)
    guarantee_marks = (
        "يقبلك في", "سيتم قبولك", "ستقبل في", "قبولك مضمون",
        "مضمون القبول", "مقبول نهاييا", "تضمن قبولك", "تقبلك",
    )
    found = []
    for marker in guarantee_marks:
        start = norm.find(marker)
        if start < 0:
            continue
        prefix = norm[max(0, start - 12):start]
        if not any(neg in prefix for neg in ("لا", "ليس", "غير")):
            found.append(marker)
    return found


def missing_competitive_admission_caveat(
    question: str, answer: str, sources: Iterable[str]
) -> bool:
    """A dated competitive cutoff must never be presented as a fixed fact."""
    if not query_rewrite.has_admission_intent(question):
        return False
    norm_question = normalize_arabic(question)
    if "طب" not in norm_question:
        return False
    source_blob = normalize_arabic("\n".join(sources))
    if not any(mark in source_blob for mark in ("تنافسي", "يتغير", "متغير")):
        return False
    if not re.search(r"2025\s*[/\-]\s*2026", source_blob):
        return False
    norm_answer = normalize_arabic(answer)
    has_date = bool(re.search(r"2025\s*[/\-]\s*2026", norm_answer))
    has_caveat = any(
        mark in norm_answer
        for mark in ("تنافسي", "يتغير", "متغير", "عدد المتقدمين")
    )
    return not (has_date and has_caveat)


def wrong_external_certificate_order(
    question: str, answer: str, sources: Iterable[str]
) -> bool:
    """Foreign-certificate data entry must precede university-number lookup."""
    norm_question = normalize_arabic(question)
    if not any(mark in norm_question for mark in ("خارج غزه", "خارج فلسطين", "شهاده من الخارج")):
        return False
    norm_sources = normalize_arabic("\n".join(sources))
    if "شهاده" not in norm_sources or "الرقم الجامعي" not in norm_sources:
        return False
    norm_answer = normalize_arabic(answer)
    certificate_positions = [
        pos for mark in ("ارسال الشهاده", "ارسل الشهاده", "ارسلها اولا")
        if (pos := norm_answer.find(mark)) >= 0
    ]
    number_positions = [
        pos for mark in ("الحصول علي الرقم الجامعي", "استخراج الرقم الجامعي")
        if (pos := norm_answer.find(mark)) >= 0
    ]
    return bool(
        certificate_positions
        and number_positions
        and min(number_positions) < min(certificate_positions)
    )


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
             asked_level: str | None, question: str = "",
             entity_terms: Iterable[str] = (),
             evidence_sufficient: bool | None = None,
             retrieval_degraded: bool = False) -> List[str]:
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
    unsupported_money = unsupported_money_amounts(
        answer,
        evidence,
        question=question,
        entity_terms=entity_terms,
    )
    if unsupported_money:
        found.append(
            "ذكرتَ مبلغاً لا يظهر في دليل الكيان المطلوب: "
            + "، ".join(unsupported_money)
            + " — استخدم قيمة مرتبطة بالبرنامج/المرحلة نفسها، لا رقماً من سجل مجاور."
        )
    count_errors = contradicted_requested_count(question, answer, evidence)
    if count_errors:
        found.append(
            "عدد العناصر في جوابك لا يطابق العدد المسند في الأدلة: "
            + "؛ ".join(count_errors)
            + " — راجع العدد والقائمة معاً."
        )
    if false_absence_claim(
        answer,
        evidence,
        evidence_sufficient=evidence_sufficient,
        retrieval_degraded=retrieval_degraded,
    ):
        if retrieval_degraded and not evidence_sufficient:
            found.append(
                "ادعيتَ أن المعلومة غير موجودة أثناء حالة استرجاع متدهورة — "
                "قل إنك لا تستطيع تأكيدها حالياً ولا تستخدم نفياً قطعياً."
            )
        else:
            found.append(
                "ادعيتَ أن المعلومة غير موجودة رغم أن عقد الأدلة عدّها مسندة — "
                "أجب من الدليل المتاح ولا تستخدم نفياً عاماً."
            )
    if unresolved_external_entry_claim(question, answer, evidence):
        found.append(
            "حوّلتَ سياسة دخول/تأشيرة مصنفة في الدليل كغير محسومة إلى حكم "
            "قطعي — قل إن نوع التأشيرة أو جهة التصريح لا يمكن تأكيدهما من "
            "البيانات الحالية، ولا تستخدم المعرفة العامة لتخمينهما."
        )
    if unsupported_application_fee_refund_claim(question, answer, evidence):
        found.append(
            "استنتجتَ حكم استرداد رسوم طلب الالتحاق من إعفاء أو معلومة "
            "مجاورة، مع أن المستخدم دفع فعلاً ولا توجد سياسة استرداد مباشرة "
            "في الدليل — صرّح بأن الاسترداد غير موثق ووجّه للقبول والتسجيل."
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
    internal_keys = exposed_internal_metadata(answer)
    if internal_keys:
        found.append(
            "عرضتَ مفاتيح ميتاداتا داخلية كأنها جزء من جواب المستخدم: "
            + "، ".join(internal_keys)
            + " — احذف المفاتيح والمعرّفات الداخلية وصغ المعلومة العربية المسندة فقط."
        )
    unsupported_labels = unsupported_quoted_procedure_labels(
        question, answer, evidence
    )
    if unsupported_labels:
        found.append(
            "اخترعتَ تسمية أو حالة واجهة غير موجودة حرفياً في الدليل: "
            + "، ".join(unsupported_labels)
            + " — احذفها ولا تستنتج حالات شاشة من إجراء عام."
        )
    unsupported_statuses = unsupported_workflow_status_claims(
        question, answer, evidence
    )
    if unsupported_statuses:
        found.append(
            "ادعيتَ حالة واجهة غير موثقة لإثبات حفظ أو إرسال الطلب: "
            + "، ".join(unsupported_statuses)
            + " — لا تفترض رسالة نجاح أو حالة طلب؛ وجّه للتحقق من الطلب "
            "في البوابة أو من القبول والتسجيل."
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
    norm_question = normalize_arabic(question)
    admission_option_list = (
        query_rewrite.has_admission_intent(question)
        and any(
            marker in norm_question
            for marker in ("خيارات", "تخصصات", "كليات", "برامج", "القائمه")
        )
    )
    vague_groups = (
        vague_admission_groups(answer, evidence)
        if admission_option_list else []
    )
    if vague_groups:
        found.append(
            "اختصرتَ قائمة قبول تحمل أسماء البرامج بعبارات مبهمة: "
            + "، ".join(vague_groups)
            + " — اذكر أسماء البرامج الموجودة بعد «البرامج:» لكل كلية، "
            "ولا تستخدم «جميع البرامج» أو «وغيرها» بديلاً عنها."
        )
    missing_faculties = (
        missing_admission_faculties(answer, evidence)
        if admission_option_list else []
    )
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
    ignored_branch = ignored_admission_branch_restriction(
        question, answer, evidence, entity_terms
    )
    if ignored_branch:
        found.append(
            "تجاهلتَ قيد فرع الثانوية عند تقرير الأهلية: "
            + "، ".join(ignored_branch)
            + " — شروط المعدل والفرع تُطبّق معاً؛ ارفض الأهلية حتى لو كان "
            "المعدل أعلى من المفتاح."
        )
    missing_scope = missing_cutoff_branch_scope(
        question, answer, evidence, entity_terms
    )
    if missing_scope:
        found.append(
            "ذكرتَ مفتاح القبول وأسقطتَ نطاق فرع الثانوية المسند للبرنامج: "
            + "، ".join(missing_scope)
            + " — اذكر المعدل والفروع المسموح بها معاً."
        )
    if inconsistent_eligibility_polarity(question, answer):
        found.append(
            "بدأتَ جواب الأهلية بـ«نعم» ثم قررت أن الشرط غير متحقق — "
            "ابدأ بـ«لا» وقدّم قراراً واحداً واضحاً ومتسقاً."
        )
    guarantees = guaranteed_final_admission(question, answer)
    if guarantees:
        found.append(
            "حوّلتَ تحقق الشرط المبدئي إلى ضمان قبول نهائي: "
            + "، ".join(guarantees)
            + " — قل فقط إن المستخدم يحقق الشرط المبدئي؛ القبول النهائي "
            "ليس مضموناً ويتبع إجراءات الجامعة والمنافسة."
        )
    if missing_competitive_admission_caveat(question, answer, evidence):
        found.append(
            "عرضتَ مفتاح الطب التنافسي المؤرخ كأنه رقم ثابت — اذكر أن 91% "
            "هو مرجع عام 2025/2026 فقط، وأن المفتاح تنافسي ومتغير ولا يضمن "
            "القبول النهائي."
        )
    if wrong_external_certificate_order(question, answer, evidence):
        found.append(
            "عكستَ ترتيب حالة الشهادة الصادرة من الخارج: إذا لم تكن بياناتها "
            "مسجلة، تُرسل الشهادة أولاً إلى القبول والتسجيل لإدخال البيانات، "
            "ثم يُستخرج الرقم الجامعي ويُستكمل الطلب الإلكتروني. لا تساوِ بين "
            "وجود الطالب خارج غزة وكون الشهادة صادرة من الخارج."
        )
    if branch_exclusivity_overclaim(question, answer, evidence):
        found.append(
            "عمّمت أن جميع البرامج «للفرع العلمي فقط»، بينما الدليل يتضمن "
            "برامج تقبل العلمي مع فروع أخرى — قل إنها تقبل الفرع العلمي، "
            "ولا تقل «فقط» إلا للبرنامج الذي ينص دليله على الحصرية."
        )
    return found
