# -*- coding: utf-8 -*-
"""Run IUG-RAG-240 through the local application pipeline.

This runner intentionally reads only ``أسئلة_IUG_RAG_240_فقط.md`` for the
generation phase.  The separate adjudication key is never passed to the bot.
Every completed case is appended to JSONL so an interrupted run can resume.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Do this before importing application modules, whose logger uses stdout.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")
logging.getLogger().setLevel(logging.WARNING)

from app import file_catalog, retrieval  # noqa: E402
from app.chatbot import IUGChatbot  # noqa: E402
from app.rbac import Principal, Role  # noqa: E402
from app.sessions import SessionStore  # noqa: E402


CASE_RE = re.compile(r"^### (?P<qid>[EMH]\d{3}) — (?P<title>.+)$", re.MULTILINE)
FIELD_RE = re.compile(r"^- \*\*(?P<name>[^*]+):\*\* (?P<value>.+)$", re.MULTILINE)


def parse_cases(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    matches = list(CASE_RE.finditer(text))
    cases: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        block = text[match.end(): matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        fields = {item.group("name"): item.group("value") for item in FIELD_RE.finditer(block)}
        question = fields.get("السؤال")
        if question:
            cases.append({
                "qid": match.group("qid"),
                "title": match.group("title"),
                "difficulty": fields.get("الصعوبة", ""),
                "role": fields.get("الدور", "زائر"),
                "context_mode": fields.get("وضع السياق", ""),
                "session_setup": fields.get("تهيئة الجلسة", ""),
                "question": question,
            })
    expected = [*(f"E{i:03d}" for i in range(1, 97)), *(f"M{i:03d}" for i in range(1, 97)), *(f"H{i:03d}" for i in range(1, 49))]
    if [case["qid"] for case in cases] != expected:
        raise ValueError(f"Expected E001..E096, M001..M096, H001..H048; found {len(cases)} cases")
    return cases


def load_context_fixtures(path: Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Context fixtures must be a JSON object keyed by QID")
    fixtures: dict[str, dict[str, Any]] = {}
    for qid, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError(f"{qid}: fixture must be an object")
        turns = value.get("setup_turns", [])
        if not isinstance(turns, list):
            raise ValueError(f"{qid}: setup_turns must be a list")
        for index, turn in enumerate(turns, 1):
            if not isinstance(turn, dict) or not str(turn.get("user") or "").strip():
                raise ValueError(f"{qid}: setup turn {index} is missing user text")
            mode = turn.get("assistant_mode")
            if mode not in {"generate", "literal"}:
                raise ValueError(f"{qid}: setup turn {index} has invalid assistant_mode")
            if mode == "literal" and not str(turn.get("assistant") or "").strip():
                raise ValueError(f"{qid}: literal setup turn {index} has no assistant text")
        fixtures[str(qid)] = dict(value)
    return fixtures


def attach_and_validate_fixtures(
    cases: list[dict[str, Any]],
    fixtures: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    known = {case["qid"] for case in cases}
    unknown = sorted(set(fixtures) - known)
    if unknown:
        raise ValueError("Fixtures reference unknown QIDs: " + ", ".join(unknown))
    for case in cases:
        fixture = fixtures.get(case["qid"])
        setup_description = str(case.get("session_setup") or "")
        context_required = not setup_description.startswith("جلسة جديدة")
        if context_required and fixture is None:
            raise ValueError(
                f"{case['qid']}: context-dependent case has no structured setup_turns"
            )
        if fixture is not None:
            turns = fixture.get("setup_turns") or []
            if (
                context_required
                and not turns
                and not fixture.get("intentional_empty_context")
            ):
                raise ValueError(
                    f"{case['qid']}: required context is empty without intentional_empty_context"
                )
            case["context_fixture"] = fixture
        else:
            case["context_fixture"] = {
                "setup_turns": [], "intentional_empty_context": True
            }
    return cases


def load_completed(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    latest: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        latest[row["qid"]] = row
    return {
        qid: row for qid, row in latest.items()
        if not row.get("error") and row.get("answer", "").strip()
    }


def principal_for(role: str, qid: str) -> Principal:
    if role == "طالب":
        # The benchmark's privacy cases identify the caller as student 12345.
        return Principal("12345", Role.STUDENT)
    return Principal.guest(f"guest:iug-rag-240:{qid}:{uuid4().hex}")


def run_turn(
    bot: IUGChatbot,
    principal: Principal,
    allowed: set[str],
    question: str,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    return bot.chat_as_principal(
        question,
        principal,
        allowed_collections=allowed,
        client_history=history[-5:] or None,
    )


def execute_case(
    bot: IUGChatbot, case: dict[str, Any], allowed: set[str]
) -> dict[str, Any]:
    principal = principal_for(case["role"], case["qid"])
    fixture = dict(case.get("context_fixture") or {})
    history: list[dict[str, Any]] = []
    setup_records: list[dict[str, str]] = []
    sent_question = str(fixture.get("final_question") or case["question"])
    token = retrieval.begin_trace()
    started = time.perf_counter()
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    try:
        for setup_turn in fixture.get("setup_turns", []):
            setup_question = str(setup_turn["user"])
            mode = str(setup_turn["assistant_mode"])
            if mode == "generate":
                setup_answer = run_turn(
                    bot, principal, allowed, setup_question, history
                ).get("answer", "")
                if not str(setup_answer).strip():
                    raise ValueError(
                        "Generated setup turn returned an empty answer"
                    )
                origin = "generated_setup"
            else:
                setup_answer = str(setup_turn["assistant"])
                origin = "injected_test_condition"
            history.append({
                "user": setup_question,
                "assistant": setup_answer,
                "at": time.time(),
                "origin": origin,
                "status": setup_turn.get("status", "grounded"),
            })
            setup_records.append({
                "question": setup_question,
                "answer": setup_answer,
                "assistant_mode": mode,
            })
        result = run_turn(bot, principal, allowed, sent_question, history)
        if not str(result.get("answer", "")).strip():
            raise ValueError("Empty answer")
    except Exception as exc:  # Record and continue: a benchmark must expose failures.
        error = {"type": type(exc).__name__, "message": str(exc)}
        details = getattr(exc, "details", None)
        if isinstance(details, dict) and details:
            error["details"] = details
    finally:
        trace = retrieval.end_trace(token)

    result = result or {}
    return {
        **case,
        "sent_question": sent_question,
        "history_snapshot": [
            {
                "user": turn["user"],
                "assistant": turn["assistant"],
                "origin": turn.get("origin"),
                "status": turn.get("status"),
            }
            for turn in history
        ],
        "setup_turns": setup_records,
        "answer": result.get("answer", ""),
        "source": result.get("source", ""),
        "trace_id": result.get("trace_id"),
        "top_chunks": result.get("top_chunks", []),
        "retrieval_metadata": result.get("retrieval_metadata", {}),
        "retrieval_trace": trace,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=Path, default=Path(__file__).with_name("أسئلة_IUG_RAG_240_فقط.md"))
    parser.add_argument("--fixtures", type=Path, default=Path(__file__).with_name("context_fixtures.json"))
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).with_name("run_local_2026-07-22"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--start-qid")
    parser.add_argument(
        "--rerun-qid",
        action="append",
        default=[],
        help="Run only these QIDs and append fresh attempts, even if completed.",
    )
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args()

    cases = attach_and_validate_fixtures(
        parse_cases(args.questions), load_context_fixtures(args.fixtures)
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    records_path = args.output_dir / "responses.jsonl"
    checkpoint_path = args.output_dir / "checkpoint.json"
    done = load_completed(records_path)
    rerun_qids = set(args.rerun_qid)
    known_qids = {case["qid"] for case in cases}
    unknown_reruns = sorted(rerun_qids - known_qids)
    if unknown_reruns:
        raise ValueError("Unknown --rerun-qid values: " + ", ".join(unknown_reruns))

    print("تهيئة البوت المحلي والفهارس…", flush=True)
    bot = IUGChatbot(sessions=SessionStore())
    bot.initialize()
    available = {item["collection"] for item in bot.get_uploaded_files_list()}
    allowed = file_catalog.allowed_collections(Principal.guest(), available)
    readiness = bot.readiness()
    (args.output_dir / "run_metadata.json").write_text(json.dumps({
        "started_at": datetime.now(timezone.utc).isoformat(),
        "execution": "local_python_pipeline",
        "judging": "manual_by_codex_only",
        "questions_file": str(args.questions),
        "context_fixtures_file": str(args.fixtures),
        "readiness": readiness,
        "public_collection_count": len(allowed),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"جاهز: {len(allowed)} مجموعة عامة؛ المكتمل سابقاً {len(done)}/240.", flush=True)

    running = args.start_qid is None
    executed = 0
    with records_path.open("a", encoding="utf-8", buffering=1) as output:
        for number, case in enumerate(cases, 1):
            if rerun_qids and case["qid"] not in rerun_qids:
                continue
            if not running:
                running = case["qid"] == args.start_qid
                if not running:
                    continue
            if case["qid"] in done and case["qid"] not in rerun_qids:
                continue
            if args.limit is not None and executed >= args.limit:
                break
            row = execute_case(bot, case, allowed)
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            executed += 1
            ok = "ERROR" if row["error"] else "OK"
            print(f"{number:03d}/240 {case['qid']} {ok} {row['latency_ms']/1000:.2f}s", flush=True)
            completed = len(load_completed(records_path))
            checkpoint_path.write_text(json.dumps({
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "completed_successfully": completed,
                "total": 240,
                "last_qid": case["qid"],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            if args.delay:
                time.sleep(args.delay)
    print(f"اكتمل هذا المرور: {len(load_completed(records_path))}/240 إجابة ناجحة.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
