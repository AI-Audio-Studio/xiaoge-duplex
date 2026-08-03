from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_AGENT_DIR = Path(__file__).resolve().parents[1] / "examples" / "voice_agents"
sys.path.insert(0, str(_AGENT_DIR))

from common import qa_log  # noqa: E402


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_format_qa_record_has_only_requested_fields() -> None:
    record = json.loads(qa_log.format_qa_record("你好\n吗", '回答"是"', timestamp=0.125))
    assert list(record) == ["timestamp", "asr", "llm"]
    assert record["timestamp"].endswith("00.125")
    assert record["asr"] == "你好\n吗"
    assert record["llm"] == '回答"是"'


def test_process_marker_follows_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XIAOGE_SESSION_ID", "worker-a1")
    record = json.loads(qa_log.format_qa_record("问题", "回答", timestamp=0.125))
    assert list(record) == ["timestamp", "process", "asr", "llm"]
    assert record["process"] == "worker-a1"


def test_append_qa_log_writes_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_file = tmp_path / "qa.jsonl"
    monkeypatch.setattr(qa_log, "QA_LOG", log_file)
    qa_log.append_qa_log("问题", "回答")
    daily_file = qa_log.daily_log_path()
    assert daily_file.name.startswith("qa_") and daily_file.suffix == ".jsonl"
    assert _wait_for(lambda: daily_file.exists() and "回答" in daily_file.read_text("utf-8"))
    assert json.loads(daily_file.read_text("utf-8"))["asr"] == "问题"


def test_final_messages_are_paired_before_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[tuple[str, str]] = []
    monkeypatch.setattr(qa_log, "append_qa_log", lambda asr, llm: written.append((asr, llm)))
    pairer = qa_log.QAPairLog()

    pairer.add_assistant("开场白")
    pairer.add_user("用户问题")
    pairer.add_assistant("助手回答")

    assert written == [("用户问题", "助手回答")]
