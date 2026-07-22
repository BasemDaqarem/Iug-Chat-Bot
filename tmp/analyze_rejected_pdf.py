import json
import re
import sys
import csv
from collections import Counter
from pathlib import Path

from pypdf import PdfReader


sys.stdout.reconfigure(encoding="utf-8")
pdf_path = Path(r"C:\Users\Mahmoud\Downloads\الأسئلة_غير_المقبولة_من_تحكيم_440.pdf")
text = "\n".join((page.extract_text() or "") for page in PdfReader(pdf_path).pages)
text = text.replace("\x1d", "")

entry_pattern = re.compile(
    r"(?P<qid>\d{3})Q\s+رقم\s*\|\s*\d+\s+(?P<title>.*?)\n"
    r"السؤال:\s*(?P<question>.*?)\nإجابة البوت:\s*(?P<answer>.*?)\n"
    r"سبب عدم القبول:?\s*(?P<reason>.*?)\nالصعوبة:\s*(?P<difficulty>\w+)\s+الدور\s*\|\s*(?P<turn>\d+)",
    re.DOTALL,
)

entries = []
for match in entry_pattern.finditer(text):
    item = {
        key: re.sub(r"\s+", " ", value).strip()
        for key, value in match.groupdict().items()
    }
    item["qid"] = "Q" + item["qid"]
    entries.append(item)

all_qids = ["Q" + value for value in re.findall(r"(\d{3})Q\s+رقم", text)]
parsed_qids = {entry["qid"] for entry in entries}
csv_path = Path(r"eval\retest_440_adaptive_rag_v2_2026-07-21\manual_review_final\manual_review_440.csv")
with csv_path.open(encoding="utf-8-sig", newline="") as handle:
    review_rows = {
        row["qid"]: row
        for row in csv.DictReader(handle)
    }
rejected_rows = [review_rows[qid] for qid in all_qids]
print(json.dumps({
    "all_qid_count": len(all_qids),
    "all_qids": all_qids,
    "parsed_count": len(entries),
    "missing_qids": [qid for qid in all_qids if qid not in parsed_qids],
    "difficulty_counts": Counter(row["difficulty"] for row in rejected_rows),
    "scenario_prefix_counts": Counter(row["scenario_id"][0] for row in rejected_rows),
    "turn_counts": Counter(row["turn"] for row in rejected_rows),
    "scenario_title_counts": Counter(row["scenario_title"] for row in rejected_rows),
    "compact_rows": [
        {
            "qid": row["qid"],
            "scenario_id": row["scenario_id"],
            "title": row["scenario_title"],
            "difficulty": row["difficulty"],
            "turn": row["turn"],
            "question": row["question"],
        }
        for row in rejected_rows
    ],
}, ensure_ascii=False, indent=2))
