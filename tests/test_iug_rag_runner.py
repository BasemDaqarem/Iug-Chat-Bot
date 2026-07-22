import json
from pathlib import Path

import pytest

from eval.iug_rag_240.run_iug_rag_240 import (
    attach_and_validate_fixtures,
    load_context_fixtures,
    load_completed,
    parse_cases,
)


ROOT = Path(__file__).resolve().parents[1] / "eval" / "iug_rag_240"


def test_all_240_cases_have_valid_structured_context_fixtures():
    cases = attach_and_validate_fixtures(
        parse_cases(ROOT / "أسئلة_IUG_RAG_240_فقط.md"),
        load_context_fixtures(ROOT / "context_fixtures.json"),
    )
    assert len(cases) == 240
    assert all("context_fixture" in case for case in cases)
    for case in cases:
        description = case["session_setup"]
        fixture = case["context_fixture"]
        if not description.startswith("جلسة جديدة"):
            assert fixture["setup_turns"] or fixture.get(
                "intentional_empty_context"
            )


def test_runner_rejects_missing_followup_setup():
    case = {
        "qid": "M999",
        "session_setup": "السؤال السابق عن برنامج محدد",
    }
    with pytest.raises(ValueError, match="structured setup_turns"):
        attach_and_validate_fixtures([case], {})


def test_literal_and_generated_assistant_turns_are_explicitly_distinguished():
    fixtures = load_context_fixtures(ROOT / "context_fixtures.json")
    modes = {
        turn["assistant_mode"]
        for fixture in fixtures.values()
        for turn in fixture.get("setup_turns", [])
    }
    assert modes == {"generate", "literal"}
    assert fixtures["H019"]["setup_turns"][0]["status"] == "insufficient_evidence"


def test_resume_uses_latest_attempt_not_any_historical_success(tmp_path):
    path = tmp_path / "responses.jsonl"
    rows = [
        {"qid": "E001", "answer": "قديم ناجح", "error": None},
        {"qid": "E001", "answer": "", "error": {"type": "Failure"}},
        {"qid": "E002", "answer": "أحدث ناجح", "error": None},
    ]
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )
    completed = load_completed(path)
    assert "E001" not in completed
    assert completed["E002"]["answer"] == "أحدث ناجح"
