"""
Arabic-aware text normalization + tokenization, shared by lexical (BM25)
retrieval. Normalization folds the orthographic variants Arabic writers use
interchangeably (hamza forms, alef maqsura, ta marbuta, tatweel, diacritics)
so "الإدارة" and "الادارة" match the same lexical token.
"""

import re
from typing import List

# Arabic diacritics (tashkeel) + superscript alef + Quranic marks.
_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"

# Fold interchangeable letter forms to a single canonical form.
_FOLD = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ئ": "ي",
    "ؤ": "و",
    "ة": "ه",
})

# A token is a run of Arabic letters or Latin letters/digits (keeps "CS202",
# "80", "2023" intact — exactly the exact-match terms lexical search rescues).
_TOKEN = re.compile(r"[0-9A-Za-zء-ي]+")


def normalize_arabic(text: str) -> str:
    text = _DIACRITICS.sub("", text)
    text = text.replace(_TATWEEL, "")
    return text.translate(_FOLD)


def tokenize(text: str) -> List[str]:
    return _TOKEN.findall(normalize_arabic(text.lower()))
