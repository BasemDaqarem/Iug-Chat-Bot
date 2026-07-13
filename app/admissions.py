"""Structured admission facts extracted from uploaded JSON documents.

The general RAG index intentionally flattens documents for broad semantic
search. Admission percentages are different: a number is only meaningful
when it stays attached to its faculty, programme, degree and school branch.
This module keeps those fields atomic and answers matching questions without
combining unrelated chunks.
"""

from dataclasses import dataclass
import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.embeddings import embed_query
from app.text_norm import normalize_arabic, tokenize


_DEGREE_BACHELOR = "بكالوريوس"
_GENERIC_ENTITY_TOKENS = {
    "كليه", "قسم", "برنامج", "تخصص", "مرحله", "درجه", "بكالوريوس", "عام",
}
_ADMISSION_INTENT_TOKENS = {
    "قبول", "تنسيق", "التحاق", "التحق", "ادخل", "دخول", "معدل", "نسبه", "ثانويه",
}
_NON_ADMISSION_TOPICS = {"سعر", "رسوم", "تكلفه", "ساعه"}


@dataclass(frozen=True)
class AdmissionFact:
    faculty: str
    program: str
    degree: str
    branches: Tuple[str, ...]
    min_percentage: float
    source: str
    path: str
    effective_year: str = ""
    doc_index: int = -1

    def descriptor(self) -> str:
        branches = "، ".join(self.branches) if self.branches else "غير محدد"
        return (
            f"شروط القبول والحد الأدنى لمعدل الثانوية | كلية {self.faculty} | "
            f"برنامج {self.program} | درجة {self.degree} | الفروع {branches} | "
            f"النسبة {self.min_percentage:g} بالمئة"
        )

    def context_line(self) -> str:
        branches = "، ".join(self.branches) if self.branches else "غير محدد"
        return (
            f"المصدر: {self.source} | الكلية: {self.faculty} | البرنامج: {self.program} | "
            f"الدرجة: {self.degree} | الفروع: {branches} | "
            f"الحد الأدنى: {self.min_percentage:g}%"
        )


@dataclass(frozen=True)
class AdmissionResolution:
    answer: str
    facts: Tuple[AdmissionFact, ...]

    @property
    def top_chunks(self) -> List[str]:
        return [fact.context_line() for fact in self.facts]


def _scalar(value) -> Optional[str]:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _strings(value) -> List[str]:
    if isinstance(value, list):
        return [text for item in value if (text := _scalar(item))]
    text = _scalar(value)
    return [text] if text else []


def _number(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 100 else None


def _branch_name(value: str) -> str:
    normalized = normalize_arabic(str(value)).strip().lower()
    normalized = normalized.replace(" فقط", "").strip()
    aliases = {
        "scientific": "علمي",
        "industrial": "صناعي",
        "literary": "أدبي",
        "sharia": "شرعي",
        "علمي": "علمي",
        "صناعي": "صناعي",
        "ادبي": "أدبي",
        "شرعي": "شرعي",
    }
    return aliases.get(normalized, str(value).replace("فقط", "").strip())


def _branches(value) -> Tuple[str, ...]:
    result = []
    for item in _strings(value):
        branch = _branch_name(item)
        if branch and branch not in result:
            result.append(branch)
    return tuple(result)


def _faculty_from_text(text: str) -> str:
    match = re.search(r"كلية\s+(.+?)(?:\s*[-–—]\s*|$)", str(text))
    return match.group(1).strip() if match else ""


def _default_faculty(collection: str, docs: Sequence[dict]) -> str:
    explicit = {
        str(doc.get("faculty_name")).strip()
        for doc in docs
        if isinstance(doc, dict) and _scalar(doc.get("faculty_name"))
    }
    if len(explicit) == 1:
        return next(iter(explicit))
    return _faculty_from_text(collection)


def _degree_from(node: dict, inherited: str) -> str:
    explicit = _scalar(node.get("degree")) or _scalar(node.get("academic_degree"))
    if explicit:
        return explicit
    category = str(node.get("category", ""))
    if "بكالوريوس" in category:
        return _DEGREE_BACHELOR
    return inherited


def _year_from(node: dict, inherited: str) -> str:
    for key in ("effective_year", "academic_year", "year"):
        if value := _scalar(node.get(key)):
            return value
    return inherited


def _percentage_fields(node: dict) -> List[Tuple[float, Tuple[str, ...], str]]:
    results: List[Tuple[float, Tuple[str, ...], str]] = []
    allowed = _branches(node.get("allowed_high_school_branches"))

    generic = _number(node.get("min_high_school_percentage"))
    if generic is not None:
        results.append((generic, allowed, "min_high_school_percentage"))

    direct = _number(node.get("min_percentage"))
    branch = _scalar(node.get("branch"))
    if direct is not None and branch:
        results.append((direct, (_branch_name(branch),), "min_percentage"))

    suffixes = {
        "min_high_school_percentage_scientific": ("علمي",),
        "min_high_school_percentage_industrial": ("صناعي",),
        "min_high_school_percentage_literary": ("أدبي",),
        "min_high_school_percentage_sharia": ("شرعي",),
        "min_high_school_percentage_literary_sharia": ("أدبي", "شرعي"),
    }
    for key, field_branches in suffixes.items():
        value = _number(node.get(key))
        if value is not None:
            results.append((value, field_branches, key))
    return results


def extract_admission_facts(
    collection: str,
    docs: Sequence[dict],
) -> List[AdmissionFact]:
    """Extract atomic admission facts from the supported generic JSON shapes."""
    default_faculty = _default_faculty(collection, docs)
    facts: List[AdmissionFact] = []

    def walk(node, context: dict, path: str) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, dict(context), f"{path}[{index}]")
            return
        if not isinstance(node, dict):
            return

        local = dict(context)
        local["faculty"] = (
            _scalar(node.get("faculty_name"))
            or _scalar(node.get("faculty"))
            or _faculty_from_text(str(node.get("category", "")))
            or local.get("faculty", "")
        )
        local["degree"] = _degree_from(node, local.get("degree", ""))
        local["year"] = _year_from(node, local.get("year", ""))

        programs = []
        for key in ("program_name", "program", "department_name"):
            programs.extend(_strings(node.get(key)))
        programs.extend(_strings(node.get("departments")))
        if programs:
            local["programs"] = tuple(dict.fromkeys(programs))

        percentages = _percentage_fields(node)
        if percentages and local.get("faculty"):
            target_programs = local.get("programs") or ("عام",)
            degree = local.get("degree") or _DEGREE_BACHELOR
            for percentage, branches, field_name in percentages:
                for program in target_programs:
                    facts.append(AdmissionFact(
                        faculty=str(local["faculty"]),
                        program=str(program),
                        degree=str(degree),
                        branches=tuple(branches),
                        min_percentage=percentage,
                        source=collection,
                        path=f"{path}.{field_name}" if path else field_name,
                        effective_year=str(local.get("year", "")),
                        doc_index=int(local.get("doc_index", -1)),
                    ))

        for key, value in node.items():
            if isinstance(value, (dict, list)):
                child_path = f"{path}.{key}" if path else key
                walk(value, dict(local), child_path)

    for index, doc in enumerate(docs):
        if isinstance(doc, dict):
            clean = {key: value for key, value in doc.items() if key not in ("_id", "__file_meta__")}
            walk(
                clean,
                {
                    "faculty": default_faculty,
                    "degree": "",
                    "year": "",
                    "doc_index": index,
                },
                f"doc[{index}]",
            )

    return list(dict.fromkeys(facts))


def _entity_tokens(value: str) -> set:
    return {token for token in tokenize(value) if token not in _GENERIC_ENTITY_TOKENS}


def _format_resolution(facts: Sequence[AdmissionFact]) -> str:
    by_program: Dict[Tuple[str, str, str], List[AdmissionFact]] = {}
    for fact in facts:
        key = (fact.program, fact.degree, fact.effective_year)
        by_program.setdefault(key, []).append(fact)

    grouped_variants: Dict[Tuple[str, str, Tuple[Tuple[float, Tuple[str, ...]], ...]], dict] = {}
    for (program, degree, effective_year), program_facts in by_program.items():
        variants = tuple(sorted({
            (fact.min_percentage, fact.branches)
            for fact in program_facts
        }))
        group = grouped_variants.setdefault(
            (degree, effective_year, variants),
            {"programs": [], "facts": [], "variants": variants},
        )
        group["programs"].append(program)
        group["facts"].extend(program_facts)

    parts = []
    for (degree, effective_year, _signature), group in grouped_variants.items():
        variant_keys = group["variants"]
        programs = group["programs"]
        program_facts = group["facts"]
        rates = {key[0] for key in variant_keys}
        branch_sets = {key[1] for key in variant_keys}
        rates_by_branch: Dict[str, set] = {}
        for rate, branches in variant_keys:
            for branch in branches or ("غير محدد",):
                rates_by_branch.setdefault(branch, set()).add(rate)
        rate_conflict = any(len(branch_rates) > 1 for branch_rates in rates_by_branch.values())
        branch_conflict = len(rates) == 1 and len(branch_sets) > 1

        if programs == ["عام"]:
            label = f"كلية {program_facts[0].faculty}"
        elif len(programs) == 1:
            label = f"برنامج {programs[0]}"
        else:
            label = "البرامج " + "، ".join(programs)
        details = degree
        if effective_year:
            details += f"، سنة {effective_year}"
        if details:
            label += f" ({details})"

        variant_parts = []
        for rate, branches in variant_keys:
            branch_text = " و".join(branches) if branches else "فروع غير محددة"
            variant_parts.append(f"{rate:g}% لـ{branch_text}")
        part = f"{label}: " + "، ".join(variant_parts)
        if rate_conflict:
            part += " — المصادر متعارضة في النسبة للفرع نفسه"
        elif branch_conflict:
            part += " — المصادر متعارضة في الفروع المسموح بها"
        parts.append(part)

    sources = sorted({fact.source for fact in facts})
    answer = ". ".join(parts)
    if any("متعارضة" in part for part in parts):
        answer += ". يلزم اعتماد المصدر الرسمي الأحدث من الجامعة"
    if sources:
        answer += ". المصادر: " + "، ".join(sources[:3])
    return answer + "."


class AdmissionCatalog:
    """In-memory, self-refreshing structured view of uploaded admission data."""

    def __init__(self):
        self._by_collection: Dict[str, List[AdmissionFact]] = {}
        self._facts: List[AdmissionFact] = []
        self._search_facts: List[AdmissionFact] = []
        self._index: Optional[np.ndarray] = None

    @property
    def facts(self) -> Tuple[AdmissionFact, ...]:
        return tuple(self._facts)

    def replace_collection(self, collection: str, docs: Sequence[dict], rebuild: bool = True) -> None:
        self._by_collection[collection] = extract_admission_facts(collection, docs)
        if rebuild:
            self.rebuild()

    def remove_collection(self, collection: str, rebuild: bool = True) -> None:
        self._by_collection.pop(collection, None)
        if rebuild:
            self.rebuild()

    def rebuild(
        self,
        vector_for_fact: Optional[Callable[[AdmissionFact], Optional[np.ndarray]]] = None,
    ) -> None:
        self._facts = list(dict.fromkeys(
            fact
            for collection_facts in self._by_collection.values()
            for fact in collection_facts
        ))
        self._search_facts = []
        self._index = None
        if not self._facts or vector_for_fact is None:
            return
        vectors = []
        for fact in self._facts:
            vector = vector_for_fact(fact)
            if vector is None:
                continue
            array = np.asarray(vector, dtype=np.float32).reshape(-1)
            if array.size == 0:
                continue
            self._search_facts.append(fact)
            vectors.append(array)
        if vectors:
            self._index = np.vstack(vectors)

    def resolve(
        self,
        question: str,
        threshold: float = 0.38,
        embed: Callable[[str], np.ndarray] = embed_query,
    ) -> Optional[AdmissionResolution]:
        if (
            not self._facts
            or self._index is None
            or len(self._index) != len(self._search_facts)
        ):
            return None

        question_tokens = set(tokenize(question))
        if question_tokens & _NON_ADMISSION_TOPICS:
            return None
        if not question_tokens & _ADMISSION_INTENT_TOKENS:
            return None

        q_vec = embed(question)
        scores = (self._index @ q_vec).flatten()
        if not len(scores):
            return None
        best_index = int(np.argmax(scores))
        if float(scores[best_index]) < threshold:
            return None

        normalized_question = normalize_arabic(question).lower()
        best_fact = self._search_facts[best_index]

        faculty_matches: Dict[str, int] = {}
        program_matches: Dict[Tuple[str, str], int] = {}
        for fact in self._facts:
            faculty_norm = normalize_arabic(fact.faculty).lower()
            program_norm = normalize_arabic(fact.program).lower()
            faculty_overlap = len(question_tokens & _entity_tokens(fact.faculty))
            program_overlap = len(question_tokens & _entity_tokens(fact.program))
            if faculty_norm and faculty_norm in normalized_question:
                faculty_overlap += 10
            if program_norm and program_norm != normalize_arabic("عام") and program_norm in normalized_question:
                program_overlap += 10
            faculty_matches[fact.faculty] = max(
                faculty_overlap,
                faculty_matches.get(fact.faculty, 0),
            )
            program_key = (fact.faculty, fact.program)
            program_matches[program_key] = max(
                program_overlap,
                program_matches.get(program_key, 0),
            )

        best_faculty, best_faculty_score = max(
            faculty_matches.items(),
            key=lambda item: item[1],
            default=(best_fact.faculty, 0),
        )
        (program_faculty, best_program), best_program_score = max(
            program_matches.items(),
            key=lambda item: item[1],
            default=((best_fact.faculty, best_fact.program), 0),
        )

        if best_faculty_score == 0 and best_program_score == 0:
            return None

        asks_for_faculty = "كليه" in question_tokens and best_faculty_score > 0
        if asks_for_faculty:
            selected = [fact for fact in self._facts if fact.faculty == best_faculty]
        elif best_program_score > 0:
            selected = [
                fact for fact in self._facts
                if fact.program == best_program and fact.faculty == program_faculty
            ]
        elif best_faculty_score > 0:
            selected = [
                fact for fact in self._facts
                if fact.faculty == best_faculty
            ]

        if not selected:
            return None

        requested_degree = ""
        degree_markers = (
            ("بكالوريوس", "بكالوريوس"),
            ("ماجستير", "ماجستير"),
            ("دكتوراه", "دكتوراه"),
            ("دبلوم", "دبلوم"),
            ("دراسات عليا", "ماجستير"),
        )
        for marker, degree in degree_markers:
            if normalize_arabic(marker) in normalized_question:
                requested_degree = normalize_arabic(degree)
                break

        requested_branches = {
            branch
            for marker, branch in (
                ("علمي", "علمي"),
                ("صناعي", "صناعي"),
                ("ادبي", "أدبي"),
                ("شرعي", "شرعي"),
            )
            if marker in question_tokens
        }
        requested_years = set(re.findall(r"20\d{2}", question))

        filtered = selected
        if requested_degree:
            filtered = [
                fact for fact in filtered
                if requested_degree in normalize_arabic(fact.degree).lower()
            ]
        if requested_branches:
            filtered = [
                fact for fact in filtered
                if requested_branches & set(fact.branches)
            ]
        if requested_years:
            filtered = [
                fact for fact in filtered
                if any(year in fact.effective_year for year in requested_years)
            ]
        if (requested_degree or requested_branches or requested_years) and not filtered:
            return AdmissionResolution(
                "لا توجد في ملفات القبول المنظمة معلومة مطابقة للمرحلة أو الفرع أو السنة المحددة.",
                tuple(),
            )

        selected = filtered
        selected = list(dict.fromkeys(selected))
        return AdmissionResolution(_format_resolution(selected), tuple(selected))
