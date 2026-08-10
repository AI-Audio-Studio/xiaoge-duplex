"""Track R5.2.2 command acknowledgements and results by ``cmd_id``."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

_TERMINAL_RESULT_STATUS = frozenset({"succeeded", "failed", "canceled", "timeout"})


@dataclass
class CommandRecord:
    cmd_id: str
    trace_id: str
    session_id: str
    utterance_id: str
    ack_deadline_ms: int
    result_deadline_ms: int
    ack_status: str | None = None
    result_status: str | None = None
    timeout_phase: str | None = None


class CommandLifecycleTracker:
    """Apply no-replay lifecycle rules without driving conversation state."""

    def __init__(self) -> None:
        self._records: dict[str, CommandRecord] = {}
        self._audit_events: list[dict[str, object]] = []
        self._lock = threading.Lock()

    @property
    def audit_events(self) -> list[dict[str, object]]:
        with self._lock:
            return [dict(event) for event in self._audit_events]

    def issue(self, frame: dict[str, object]) -> None:
        if frame.get("type") != "data.cmd":
            return
        cmd_id = str(frame.get("cmd_id") or "")
        if not cmd_id:
            return
        issued_at_ms = int(frame.get("issued_at_ms") or _now_ms())
        ack_timeout_ms = int(frame.get("ack_timeout_ms") or 0)
        result_timeout_ms = int(frame.get("result_timeout_ms") or 0)
        record = CommandRecord(
            cmd_id=cmd_id,
            trace_id=str(frame.get("trace_id") or ""),
            session_id=str(frame.get("session_id") or ""),
            utterance_id=str(frame.get("utterance_id") or ""),
            ack_deadline_ms=issued_at_ms + ack_timeout_ms,
            result_deadline_ms=issued_at_ms + result_timeout_ms,
        )
        with self._lock:
            if cmd_id in self._records:
                self._audit("duplicate_issue", cmd_id, issued_at_ms)
                return
            self._records[cmd_id] = record
            self._audit("issued", cmd_id, issued_at_ms)

    def accept(self, frame: dict[str, object], *, now_ms: int | None = None) -> str:
        """Return accepted, unknown, duplicate, or late for an ack/result frame."""
        observed_at_ms = _now_ms() if now_ms is None else now_ms
        cmd_id = str(frame.get("cmd_id") or "")
        with self._lock:
            record = self._records.get(cmd_id)
            if record is None or not self._same_command(record, frame):
                self._audit("unknown_cmd_id", cmd_id, observed_at_ms)
                return "unknown"
            if record.timeout_phase is not None:
                self._audit("late", cmd_id, observed_at_ms, status=str(frame.get("status") or ""))
                return "late"
            if frame.get("type") == "data.cmd_ack":
                return self._accept_ack(record, frame, observed_at_ms)
            return self._accept_result(record, frame, observed_at_ms)

    def _accept_ack(
        self, record: CommandRecord, frame: dict[str, object], observed_at_ms: int
    ) -> str:
        status = str(frame.get("status") or "")
        if record.result_status in _TERMINAL_RESULT_STATUS:
            self._audit("late", record.cmd_id, observed_at_ms, status=status)
            return "late"
        if record.ack_status is not None:
            self._audit("duplicate", record.cmd_id, observed_at_ms, status=status)
            return "duplicate"
        record.ack_status = status
        self._audit("ack", record.cmd_id, observed_at_ms, status=status)
        return "accepted"

    def _accept_result(
        self, record: CommandRecord, frame: dict[str, object], observed_at_ms: int
    ) -> str:
        status = str(frame.get("status") or "")
        if record.ack_status in {"rejected", "duplicate"}:
            self._audit("late", record.cmd_id, observed_at_ms, status=status)
            return "late"
        if record.result_status in _TERMINAL_RESULT_STATUS or record.result_status == status:
            self._audit("duplicate", record.cmd_id, observed_at_ms, status=status)
            return "duplicate"
        record.result_status = status
        self._audit("result", record.cmd_id, observed_at_ms, status=status)
        return "accepted"

    def expire(self, *, now_ms: int | None = None) -> list[dict[str, object]]:
        observed_at_ms = _now_ms() if now_ms is None else now_ms
        expired: list[dict[str, object]] = []
        with self._lock:
            for record in self._records.values():
                if record.timeout_phase is not None or record.result_status in _TERMINAL_RESULT_STATUS:
                    continue
                if record.ack_status in {"rejected", "duplicate"}:
                    continue
                if record.ack_status is None and observed_at_ms >= record.ack_deadline_ms:
                    record.timeout_phase = "delivery_timeout"
                elif record.ack_status is not None and observed_at_ms >= record.result_deadline_ms:
                    record.timeout_phase = "execution_timeout"
                else:
                    continue
                event = self._audit(record.timeout_phase, record.cmd_id, observed_at_ms)
                expired.append(dict(event))
        return expired

    @staticmethod
    def _same_command(record: CommandRecord, frame: dict[str, object]) -> bool:
        return (
            record.trace_id == frame.get("trace_id")
            and record.session_id == frame.get("session_id")
            and record.utterance_id == frame.get("utterance_id")
        )

    def _audit(
        self, event: str, cmd_id: str, at_ms: int, *, status: str = ""
    ) -> dict[str, object]:
        item: dict[str, object] = {"event": event, "cmd_id": cmd_id, "at_ms": at_ms}
        if status:
            item["status"] = status
        self._audit_events.append(item)
        return item


def _now_ms() -> int:
    return int(time.time() * 1000)
