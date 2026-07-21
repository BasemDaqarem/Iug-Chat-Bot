# -*- coding: utf-8 -*-
"""Evidence-grounded, resumable evaluation of the 440 rerun.

The evaluator may use only:
  * JSON knowledge files in data/
  * records in iug_admission_pages_json.zip
  * the exact chunks sent to the chatbot for each answer
  * explicit chatbot product/security rules supplied in the system prompt

It does not use the previous answer as a correctness reference. Historical
findings are retained in the output but are not shown to the evaluator.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.lexical import BM25  # noqa: E402


DEFAULT_ZIP = Path(r"C:\Users\Mahmoud\Downloads\iug_admission_pages_json.zip")
DEFAULT_LIVE_CORPUS = (
    ROOT
    / "eval"
    / "retest_440_detailed_2026-07-18"
    / "live_public_bot_corpus.json"
)
DEFAULT_MODEL = "google/gemini-2.5-flash"

VERDICTS = ["correct", "partial", "incorrect", "unverifiable"]
GROUNDING = ["fully_grounded", "mostly_grounded", "weakly_grounded", "unsupported"]
RETRIEVAL = [
    "sufficient",
    "incomplete",
    "wrong_focus",
    "data_absent",
    "data_conflicting",
    "not_needed",
]
RERANKER = ["yes", "no", "maybe"]
ERRORS = [
    "hallucination",
    "wrong_number_or_date",
    "incomplete_list",
    "wrong_entity",
    "wrong_academic_stage",
    "context_leak",
    "wrong_referral_or_link",
    "unsafe_advice",
    "unnecessary_refusal",
    "failed_to_acknowledge_data_gap",
    "contradiction",
    "bad_format_or_truncation",
    "off_topic_or_did_not_answer",
    "overclaim",
    "other",
]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "answered_question": {"type": "boolean"},
        "grounding": {"type": "string", "enum": GROUNDING},
        "retrieval_status": {"type": "string", "enum": RETRIEVAL},
        "error_categories": {
            "type": "array",
            "items": {"type": "string", "enum": ERRORS},
        },
        "reason_ar": {"type": "string"},
        "key_issues": {"type": "array", "items": {"type": "string"}},
        "supported_points": {"type": "array", "items": {"type": "string"}},
        "correct_evidence_sources": {"type": "array", "items": {"type": "string"}},
        "better_answer_outline": {"type": "string"},
        "could_reranker_help": {"type": "string", "enum": RERANKER},
        "reranker_reason": {"type": "string"},
        "needs_manual_review": {"type": "boolean"},
    },
    "required": [
        "verdict",
        "score",
        "confidence",
        "answered_question",
        "grounding",
        "retrieval_status",
        "error_categories",
        "reason_ar",
        "key_issues",
        "supported_points",
        "correct_evidence_sources",
        "better_answer_outline",
        "could_reranker_help",
        "reranker_reason",
        "needs_manual_review",
    ],
}

SYSTEM = """أنت مراجع جودة دقيق لإجابات مساعد الجامعة الإسلامية بغزة.
مصدر الحقيقة الوحيد هو «أدلة البوت النهائية» و«مراجع corpus الإضافية» في
الرسالة. ممنوع استخدام المعرفة الخارجية أو افتراض سياسات غير موجودة.

قواعد المنتج الملزمة:
- المقصود هو الجامعة الإسلامية بغزة (IUG)، أما «جامعة غزة» فجامعة مختلفة.
- الزائر يرى الملفات العامة فقط ولا يجوز عرض بيانات شخصية لطالب أو موظف.
- إذا كانت معلومة مؤسسية غير موجودة أو متعارضة، يجب التصريح بعدم توفرها
  أو الحاجة لتأكيد رسمي بدل التخمين.
- في الاحتيال أو رفع وثائق لموقع مجهول، يجب التحذير وعدم تشجيع الدفع أو الرفع.
- الأسئلة العامة خارج نطاق الجامعة تُجاب بأدب وفق قرار «البوت صديق الطالب»،
  لكن لا يجوز اختراع معلومة آنية أو رقم محدد بلا دليل.
- جدول Markdown ممنوع في الواجهة؛ القائمة النصية مقبولة.

معيار الأحكام:
- correct (90-100): أجاب المطلوب مباشرة، وجميع الادعاءات الجوهرية مسندة،
  والقوائم التي قُدمت ككاملة كاملة فعلًا.
- partial (60-89): الجوهر صحيح لكن ناقص أو فيه ادعاء ثانوي غير مسند/صياغة
  مربكة لا تقلب النتيجة.
- incorrect (0-59): خطأ جوهري، هلوسة، رقم/تاريخ/رابط خاطئ، قائمة ناقصة
  قُدمت ككاملة، تسريب سياق، نصيحة غير آمنة، أو رفض رغم وجود المعلومة.
- unverifiable: الأدلة المقدمة لا تسمح بالحكم ولا يوجد تناقض صريح.

تقييم الـReranker:
- yes فقط إذا كانت الإجابة ضعيفة لأن المقاطع النهائية لم تحوِ الدليل الصحيح
  بينما مراجع corpus الإضافية تحتويه، وكانت المشكلة تبدو مشكلة ترتيب/اختيار.
- no إذا كان الدليل الصحيح موجودًا أصلًا في المقاطع النهائية (مشكلة توليد)،
  أو لا توجد بيانات أصلًا، أو الإجابة صحيحة.
- maybe عند عدم اليقين.

أعد JSON فقط وفق المخطط، واكتب السبب والنقاط بالعربية."""


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def flatten(value: Any, prefix: str = "") -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return f"{prefix}{value}"
    if isinstance(value, list):
        return "\n".join(flatten(item, prefix) for item in value)
    if isinstance(value, dict):
        return "\n".join(
            flatten(item, f"{prefix}{key}: ") for key, item in value.items()
        )
    return f"{prefix}{value}"


def corpus_documents(
    data_dir: Path,
    zip_path: Path,
    live_corpus_path: Path | None = None,
) -> list[dict[str, str]]:
    docs: list[dict[str, str]] = []
    for path in sorted(data_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        items = data if isinstance(data, list) else [data]
        for index, item in enumerate(items, 1):
            docs.append(
                {
                    "source": f"data/{path.name}#{index}",
                    "text": flatten(item),
                }
            )
    with zipfile.ZipFile(zip_path) as archive:
        for name in sorted(archive.namelist()):
            data = json.loads(archive.read(name).decode("utf-8-sig"))
            items = data if isinstance(data, list) else [data]
            for index, item in enumerate(items, 1):
                docs.append(
                    {
                        "source": f"zip/{name}#{index}",
                        "text": flatten(item),
                    }
                )
    if live_corpus_path is not None and live_corpus_path.exists():
        live = json.loads(live_corpus_path.read_text(encoding="utf-8"))
        for collection in live.get("collections", []):
            name = collection["collection"]
            for index, chunk in enumerate(collection.get("chunks", []), 1):
                docs.append(
                    {
                        "source": f"live/{name}#chunk-{index}",
                        "text": chunk,
                    }
                )
    return docs


class CorpusSearch:
    def __init__(self, docs: list[dict[str, str]]) -> None:
        self.docs = docs
        self.bm25 = BM25([doc["text"] for doc in docs])

    def search(self, query: str, top_k: int = 9) -> list[dict[str, Any]]:
        scores = self.bm25.scores(query)
        order = np.argsort(scores)[::-1]
        matches = []
        for idx in order:
            score = float(scores[int(idx)])
            if score <= 0 and matches:
                break
            doc = self.docs[int(idx)]
            matches.append(
                {
                    "source": doc["source"],
                    "bm25_score": round(score, 6),
                    "text": doc["text"],
                }
            )
            if len(matches) >= top_k:
                break
        return matches


def compact_history(history: list[dict[str, str]]) -> str:
    if not history:
        return "(السؤال أول السيناريو)"
    return "\n\n".join(
        f"المستخدم: {turn['question'][:500]}\n"
        f"المساعد: {turn['answer'][:1100]}"
        for turn in history[-4:]
    )


def compact_chunks(chunks: list[str], max_chunks: int = 8, chars: int = 1600) -> str:
    if not chunks:
        return "(مسار فوري بلا مقاطع)"
    return "\n\n".join(
        f"[مقطع نهائي {index}]\n{chunk[:chars]}"
        for index, chunk in enumerate(chunks[:max_chunks], 1)
    )


def compact_references(matches: list[dict[str, Any]], chars: int = 1700) -> str:
    if not matches:
        return "(لا توجد مطابقة لفظية في corpus)"
    return "\n\n".join(
        f"[مرجع {index}: {match['source']} | BM25={match['bm25_score']}]\n"
        f"{match['text'][:chars]}"
        for index, match in enumerate(matches, 1)
    )


def prompt_for(
    record: dict[str, Any],
    history: list[dict[str, str]],
    references: list[dict[str, Any]],
) -> str:
    return f"""المعرف: {record['qid']}
السيناريو: {record['scenario_id']} — {record['scenario_title']}
الصعوبة: {record['difficulty']} — الدور: {record['turn']}

سياق المحادثة الجديدة:
{compact_history(history)}

السؤال:
{record['question']}

الإجابة الجديدة:
{record['after_answer']}

المصدر الذي أبلغه مسار البوت: {record.get('after_source')}
ملفات المقاطع النهائية:
{json.dumps(record.get('top_chunk_sources') or [], ensure_ascii=False)}

أدلة البوت النهائية التي دخلت البرومبت فعلًا:
{compact_chunks(record.get('top_chunks') or [])}

مراجع corpus إضافية مسترجعة من data/ وZIP:
{compact_references(references)}
"""


def parse_json_content(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]
    return json.loads(text)


def call_evaluator(model: str, prompt: str, retries: int = 4) -> tuple[dict, dict]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 1400,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "iug_grounded_evaluation",
                "strict": True,
                "schema": SCHEMA,
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {config.CHAT_API_KEY}",
        "Content-Type": "application/json",
    }
    last_error = None
    for attempt in range(1, retries + 1):
        t0 = time.perf_counter()
        try:
            response = requests.post(
                config.CHAT_API_URL,
                headers=headers,
                json=payload,
                timeout=120,
            )
            latency_ms = round((time.perf_counter() - t0) * 1000)
            if response.status_code == 429 and attempt < retries:
                time.sleep(float(response.headers.get("Retry-After") or 2**attempt))
                continue
            response.raise_for_status()
            body = response.json()
            evaluation = parse_json_content(
                body["choices"][0]["message"]["content"]
            )
            return evaluation, {
                "requested_model": model,
                "response_model": body.get("model"),
                "latency_ms": latency_ms,
                "attempts": attempt,
                "finish_reason": body["choices"][0].get("finish_reason"),
                "usage": body.get("usage") or {},
            }
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"Evaluation failed after {retries} attempts: {last_error}")


def completed(path: Path) -> dict[str, dict[str, Any]]:
    return {row["qid"]: row for row in load_jsonl(path)} if path.exists() else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--results",
        type=Path,
        default=ROOT / "eval" / "retest_440_detailed_2026-07-18" / "after_results.jsonl",
    )
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP)
    parser.add_argument("--live-corpus", type=Path, default=DEFAULT_LIVE_CORPUS)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "eval" / "retest_440_detailed_2026-07-18"
        / "grounded_evaluation_440.jsonl",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--expected-count", type=int, default=440)
    parser.add_argument("--select-from-evaluation", type=Path)
    parser.add_argument(
        "--select-verdicts",
        default="partial,incorrect,unverifiable",
        help="Comma-separated verdicts used with --select-from-evaluation.",
    )
    args = parser.parse_args()

    records = load_jsonl(args.results)
    if args.select_from_evaluation is not None:
        verdicts = {
            value.strip()
            for value in args.select_verdicts.split(",")
            if value.strip()
        }
        selected = {
            row["qid"]
            for row in load_jsonl(args.select_from_evaluation)
            if not row.get("error")
            and (row.get("evaluation") or {}).get("verdict") in verdicts
        }
        records_with_history: list[dict[str, Any]] = []
        scenario_for_history = None
        accumulated_history: list[dict[str, str]] = []
        for record in records:
            if record["scenario_id"] != scenario_for_history:
                scenario_for_history = record["scenario_id"]
                accumulated_history = []
            if record["qid"] in selected:
                copied = dict(record)
                copied["client_history_snapshot"] = list(accumulated_history[-5:])
                records_with_history.append(copied)
            accumulated_history.append(
                {
                    "question": record["question"],
                    "answer": record["after_answer"],
                }
            )
        records = records_with_history
    if len(records) != args.expected_count:
        raise RuntimeError(
            f"Expected {args.expected_count} result records; found {len(records)}"
        )
    docs = corpus_documents(args.data_dir, args.zip, args.live_corpus)
    search = CorpusSearch(docs)
    done = completed(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"Corpus ready: {len(docs)} reference records; "
        f"{len(done)}/{args.expected_count} evaluations already completed.",
        flush=True,
    )

    scenario = None
    history: list[dict[str, str]] = []
    executed = 0
    with args.output.open("a", encoding="utf-8", buffering=1) as output:
        for index, record in enumerate(records, 1):
            if record["scenario_id"] != scenario:
                scenario = record["scenario_id"]
                history = []
            record_history = record.get("client_history_snapshot")
            prompt_history = (
                [
                    {
                        "question": turn.get("question") or turn.get("user") or "",
                        "answer": turn.get("answer") or turn.get("assistant") or "",
                    }
                    for turn in record_history
                ]
                if record_history is not None
                else history
            )
            if record["qid"] in done:
                history.append(
                    {"question": record["question"], "answer": record["after_answer"]}
                )
                continue
            if args.limit is not None and executed >= args.limit:
                break
            query = record["question"] + "\n" + "\n".join(
                turn["question"] for turn in prompt_history[-2:]
            )
            references = search.search(query)
            started = datetime.now(timezone.utc).isoformat()
            evaluation = meta = error = None
            try:
                evaluation, meta = call_evaluator(
                    args.model, prompt_for(record, prompt_history, references)
                )
            except Exception as exc:
                error = {"type": type(exc).__name__, "message": str(exc)}
            row = {
                "qid": record["qid"],
                "scenario_id": record["scenario_id"],
                "evaluation": evaluation,
                "reference_matches": [
                    {
                        "source": match["source"],
                        "bm25_score": match["bm25_score"],
                    }
                    for match in references
                ],
                "historical_findings": record.get("before_findings") or [],
                "evaluator_meta": meta,
                "error": error,
                "started_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            executed += 1
            verdict = (evaluation or {}).get("verdict", "ERROR")
            score = (evaluation or {}).get("score")
            reranker = (evaluation or {}).get("could_reranker_help")
            print(
                f"{index:03d}/{args.expected_count} {record['qid']} {verdict} "
                f"score={score} reranker={reranker} "
                f"{(meta or {}).get('latency_ms', 0) / 1000:.2f}s",
                flush=True,
            )
            history.append(
                {"question": record["question"], "answer": record["after_answer"]}
            )


if __name__ == "__main__":
    main()
