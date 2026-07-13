"""Turn a conversational question into a self-contained retrieval query.

The contextualizer runs *before* RAG.  It may use previous user questions and
the authenticated student's trusted profile to resolve references such as
"my department", but previous assistant answers are deliberately excluded:
they are conversation text, not a factual source.
"""

from dataclasses import dataclass
import json
import re
from typing import Iterable

from app import config
from app.llm import chat_completion


PROFILE_FIELD_ALLOWLIST = frozenset(
    {"name", "major", "gpa", "rank", "academic_status", "data_source", "updated_at"}
)
_PRIVATE_QUERY_FIELDS = ("name", "gpa", "rank")
_GENERIC_SAFE_QUERY = "استفسار جامعي يحتاج إلى توضيح"
_LABELED_PRIVATE_NUMBER_RE = re.compile(
    r"((?:معدلي|المعدل(?:\s+التراكمي)?|ترتيبي(?:\s+على\s+الدفعة)?|"
    r"الترتيب(?:\s+على\s+الدفعة)?)\s*(?:هو|=|:)?\s*)"
    r"[0-9٠-٩]+(?:[.,٫][0-9٠-٩]+)?",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class ContextualizedQuery:
    retrieval_query: str
    topic: str = ""
    profile_fields: tuple[str, ...] = ()
    ambiguous: bool = False


_SYSTEM_PROMPT = """\
أنت طبقة تهيئة سؤال قبل البحث في ملفات الجامعة، ولست طبقة إجابة.
أعد JSON صالحاً فقط بهذه المفاتيح:
{"retrieval_query":"...","topic":"...","profile_fields":[],"ambiguous":false}

القواعد:
- اجعل retrieval_query سؤال بحث عربي مستقل وواضح يحافظ على مقصد السؤال كاملاً.
- استخدم أسئلة المستخدم السابقة فقط لفهم الإحالات والموضوع المستمر.
- لا تستخدم إجابات المساعد السابقة ولا تصدقها؛ هي غير مقدمة لك أصلاً.
- الملف الموثوق هو المصدر الوحيد لقيم الطالب. استخدم التخصص لحل «قسمي» ونحوها عند الحاجة.
- لا تضع الاسم أو المعدل أو الترتيب أو أي رقم خاص داخل retrieval_query.
- profile_fields يجب أن يحتوي فقط الحقول اللازمة للإجابة النهائية من:
  name, major, gpa, rank, academic_status, data_source, updated_at
- مثال المنح حسب المعدل: ابحث عن شروط المنح، وضع gpa في profile_fields دون وضع قيمته في retrieval_query.
- ambiguous=true فقط عندما لا يمكن تحديد المقصود من السؤال والسياق المتاح.
- لا تجب عن السؤال ولا تضف Markdown أو شرحاً خارج JSON.
"""


def _recent_user_questions(history: list) -> list[str]:
    turns = history[-config.HISTORY_TURNS_IN_PROMPT :]
    return [str(turn.get("user", "")).strip() for turn in turns if turn.get("user")]


def _approved_profile(profile: dict) -> dict:
    """Keep only approved profile fields; identity/student id is excluded."""
    return {
        key: value
        for key, value in profile.items()
        if key in PROFILE_FIELD_ALLOWLIST and value not in (None, "")
    }


def _extract_json(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("contextualizer did not return JSON")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("contextualizer JSON must be an object")
    return value


def _private_value_candidates(profile: dict) -> set[str]:
    candidates: set[str] = set()
    for field in _PRIVATE_QUERY_FIELDS:
        value = profile.get(field)
        if value in (None, ""):
            continue
        field_candidates = {str(value)}
        if field == "name":
            field_candidates.update(part for part in str(value).split() if len(part) >= 3)
        elif isinstance(value, (int, float)):
            field_candidates.add(f"{value:g}")
            field_candidates.update(
                candidate.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
                for candidate in tuple(field_candidates)
            )
        candidates.update(field_candidates)
    return candidates


def _clean_spacing(text: str) -> str:
    return re.sub(r"\s{2,}", " ", text).strip(" -،,؛")


def _safe_rewritten_query(query: str, original: str, profile: dict) -> str:
    """Remove private data added by the rewriter without damaging user text.

    Values already present in the user's question are not blindly deleted:
    rank=4 must not turn "رسوم 4 ساعات" into "رسوم ساعات". Explicit
    GPA/rank-labelled numbers are removed regardless, because retrieval needs
    the topic (for example scholarship rules), not the private value.
    """
    cleaned = _LABELED_PRIVATE_NUMBER_RE.sub(r"\1", query)
    original_folded = original.casefold()
    for candidate in sorted(_private_value_candidates(profile), key=len, reverse=True):
        if candidate and candidate.casefold() not in original_folded:
            cleaned = re.sub(re.escape(candidate), "", cleaned, flags=re.IGNORECASE)
    return _clean_spacing(cleaned) or _GENERIC_SAFE_QUERY


def _safe_fallback_query(original: str, profile: dict) -> str:
    """Sanitize the user's question when the rewrite service is unavailable."""
    cleaned = _clean_spacing(_LABELED_PRIVATE_NUMBER_RE.sub(r"\1", original))
    private_values = {value.casefold() for value in _private_value_candidates(profile)}
    if not cleaned or cleaned.casefold() in private_values:
        return _GENERIC_SAFE_QUERY
    return cleaned


def _safe_topic(value: object) -> str:
    cleaned = re.sub(r"[\r\n\t]+", " ", str(value or ""))
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -،,؛")
    return cleaned[:120]


def _validated_fields(values: Iterable[object]) -> tuple[str, ...]:
    seen = set()
    fields = []
    for value in values:
        field = str(value).strip()
        if field in PROFILE_FIELD_ALLOWLIST and field not in seen:
            seen.add(field)
            fields.append(field)
    return tuple(fields)


def contextualize(question: str, history: list, profile: dict | None = None) -> ContextualizedQuery:
    """Resolve a conversational question before retrieval.

    Any upstream/format failure degrades safely to the original question.  It
    never prevents the chat endpoint from answering and never guesses facts
    from old assistant messages.
    """
    original = question.strip()
    trusted_profile = _approved_profile(profile or {})
    # The rewrite model needs the major's value to resolve phrases such as
    # "my department". For all other fields, knowing availability is enough;
    # their private values are reserved for the final, field-filtered prompt.
    contextualizer_profile = {
        "major": trusted_profile.get("major"),
        "available_fields": list(trusted_profile),
    }
    payload = {
        "current_question": original,
        "previous_user_questions": _recent_user_questions(history),
        "trusted_student_profile": contextualizer_profile,
    }
    try:
        raw = chat_completion(
            _SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            max_tokens=180,
            temperature=0.0,
        )
        parsed = _extract_json(raw)
        raw_query = str(parsed.get("retrieval_query") or original).strip()[:500]
        query = _safe_rewritten_query(raw_query, original, trusted_profile)
        fields_value = parsed.get("profile_fields") or []
        if not isinstance(fields_value, (list, tuple, set)):
            fields_value = []
        return ContextualizedQuery(
            retrieval_query=query,
            topic=_safe_topic(parsed.get("topic")),
            profile_fields=_validated_fields(fields_value),
            ambiguous=(
                parsed.get("ambiguous") is True
                or str(parsed.get("ambiguous", "")).strip().lower() == "true"
            ),
        )
    except Exception:
        # Fail closed for private fields: preserve a safe retrieval topic, but
        # do not send the whole student profile just because rewriting failed.
        safe_original = _safe_fallback_query(original, trusted_profile)
        return ContextualizedQuery(
            retrieval_query=safe_original,
            profile_fields=(),
            ambiguous=bool(history) or safe_original == _GENERIC_SAFE_QUERY,
        )
