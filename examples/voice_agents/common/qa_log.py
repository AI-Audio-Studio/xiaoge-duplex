"""Minimal per-turn ASR/LLM conversation log with non-blocking disk I/O."""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import queue
import threading
import time
from pathlib import Path

QA_LOG = Path(
    os.getenv("XIAOGE_DEPLOY_QA_LOG") or os.getenv("QA_LOG", "qwen_voice_qa.log")
).resolve()

_queue: queue.Queue[tuple[Path, str] | None] = queue.Queue(maxsize=1000)
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def _writer_loop() -> None:
    while True:
        item = _queue.get()
        if item is None:
            return
        batches: dict[Path, list[str]] = {item[0]: [item[1]]}
        stop = False
        with contextlib.suppress(queue.Empty):
            while True:
                next_item = _queue.get_nowait()
                if next_item is None:
                    stop = True
                    break
                batches.setdefault(next_item[0], []).append(next_item[1])
        for path, lines in batches.items():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as output:
                    output.writelines(lines)
            except Exception:
                pass
        if stop:
            return


def _flush_at_exit() -> None:
    with contextlib.suppress(Exception):
        _queue.put(None, timeout=0.3)
    if _thread is not None:
        with contextlib.suppress(Exception):
            _thread.join(timeout=1.5)


def _ensure_writer() -> None:
    global _thread
    if _thread is not None:
        return
    with _thread_lock:
        if _thread is None:
            _thread = threading.Thread(target=_writer_loop, name="qa-log", daemon=True)
            _thread.start()
            atexit.register(_flush_at_exit)


def daily_log_path(timestamp: float | None = None) -> Path:
    """Resolve the configured base name to one file per local calendar day."""
    at = time.time() if timestamp is None else timestamp
    day = time.strftime("%Y%m%d", time.localtime(at))
    return QA_LOG.with_name(f"{QA_LOG.stem}_{day}{QA_LOG.suffix}")


def format_qa_record(asr: str, llm: str, *, timestamp: float | None = None) -> str:
    """Return one JSONL record containing only timestamp, final ASR and LLM text."""
    at = time.time() if timestamp is None else timestamp
    payload = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at))
        + f".{int((at % 1) * 1000):03d}"
    }
    process = os.getenv("XIAOGE_SESSION_ID", "").strip()
    if process:
        payload["process"] = process
    payload.update({"asr": asr, "llm": llm})
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"


def append_qa_log(asr: str, llm: str) -> None:
    """Queue one completed question/answer pair without blocking the agent loop."""
    if not asr.strip() or not llm.strip():
        return
    timestamp = time.time()
    _ensure_writer()
    with contextlib.suppress(queue.Full):
        _queue.put_nowait(
            (daily_log_path(timestamp), format_qa_record(asr, llm, timestamp=timestamp))
        )


class QAPairLog:
    """Pair final user messages with subsequent final assistant messages."""

    def __init__(self) -> None:
        self._pending_asr: list[str] = []

    def add_user(self, text: str) -> None:
        if text:
            self._pending_asr.append(text)

    def add_assistant(self, text: str) -> None:
        if self._pending_asr and text:
            append_qa_log(self._pending_asr.pop(0), text)
