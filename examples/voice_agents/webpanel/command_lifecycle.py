"""Track R5.2.2 command acknowledgements and results by ``cmd_id``."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Literal

_TERMINAL_RESULT_STATUS = frozenset({"succeeded", "failed", "canceled", "timeout"})
_FAILURE_RESULT_STATUS = frozenset({"failed", "canceled", "timeout"})

Lifecycle = Literal["accepted", "unknown", "duplicate", "late"]
CommandOutcome = Literal["success", "failure"]


@dataclass(frozen=True)
class LifecycleUpdate:
    lifecycle: Lifecycle
    outcome: CommandOutcome | None = None


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
    outcome: CommandOutcome | None = None


class CommandLifecycleTracker:
    """Apply no-replay lifecycle rules and atomically select one command outcome."""

    def __init__(self) -> None:
        self._records: dict[str, CommandRecord] = {}
        self._audit_events: list[dict[str, object]] = []
        self._lock = threading.Lock()

    @property
    def audit_events(self) -> list[dict[str, object]]:
        with self._lock:
            return [dict(event) for event in self._audit_events]

    def issue(self, frame: dict[str, object], *, now_ms: int | None = None) -> bool:
        """Register a command at dispatch time and return whether it is newly issued."""
        if frame.get("type") != "data.cmd":
            return False
        cmd_id = str(frame.get("cmd_id") or "")
        if not cmd_id:
            return False
        registered_at_ms = _now_ms() if now_ms is None else now_ms
        ack_timeout_ms = int(frame.get("ack_timeout_ms") or 0)
        result_timeout_ms = int(frame.get("result_timeout_ms") or 0)
        record = CommandRecord(
            cmd_id=cmd_id,
            trace_id=str(frame.get("trace_id") or ""),
            session_id=str(frame.get("session_id") or ""),
            utterance_id=str(frame.get("utterance_id") or ""),
            ack_deadline_ms=registered_at_ms + ack_timeout_ms,
            result_deadline_ms=registered_at_ms + result_timeout_ms,
        )
        with self._lock:
            if cmd_id in self._records:
                self._audit("duplicate_issue", cmd_id, registered_at_ms)
                return False
            frame["issued_at_ms"] = registered_at_ms
            self._records[cmd_id] = record
            self._audit("issued", cmd_id, registered_at_ms)
            return True

    def accept(self, frame: dict[str, object], *, now_ms: int | None = None) -> Lifecycle:
        """Return the protocol classification for backward-compatible callers."""
        return self.accept_update(frame, now_ms=now_ms).lifecycle

    def accept_update(
        self, frame: dict[str, object], *, now_ms: int | None = None
    ) -> LifecycleUpdate:
        """Atomically classify an endpoint frame and return any new user outcome."""
        observed_at_ms = _now_ms() if now_ms is None else now_ms
        cmd_id = str(frame.get("cmd_id") or "")
        with self._lock:
            record = self._records.get(cmd_id)
            if record is None or not self._same_command(record, frame):
                self._audit("unknown_cmd_id", cmd_id, observed_at_ms)
                return LifecycleUpdate("unknown")
            return self._accept_known(record, frame, observed_at_ms)

    def _accept_known(
        self, record: CommandRecord, frame: dict[str, object], observed_at_ms: int
    ) -> LifecycleUpdate:
        if record.timeout_phase is not None:
            self._audit(
                "late", record.cmd_id, observed_at_ms, status=str(frame.get("status") or "")
            )
            return LifecycleUpdate("late")
        if record.outcome is not None:
            return self._classify_settled(record, frame, observed_at_ms)
        if record.ack_status is None and observed_at_ms >= record.ack_deadline_ms:
            return self._expire_on_accept(record, frame, observed_at_ms, "delivery_timeout")
        if observed_at_ms > record.result_deadline_ms:
            return self._expire_on_accept(record, frame, observed_at_ms, "execution_timeout")
        if frame.get("type") == "data.cmd_ack":
            return self._accept_ack(record, frame, observed_at_ms)
        return self._accept_result(record, frame, observed_at_ms)

    def _classify_settled(
        self, record: CommandRecord, frame: dict[str, object], observed_at_ms: int
    ) -> LifecycleUpdate:
        status = str(frame.get("status") or "")
        known_status = (
            record.ack_status if frame.get("type") == "data.cmd_ack" else record.result_status
        )
        event: Lifecycle = "duplicate" if status == known_status else "late"
        self._audit(event, record.cmd_id, observed_at_ms, status=status)
        return LifecycleUpdate(event)

    def _expire_on_accept(
        self,
        record: CommandRecord,
        frame: dict[str, object],
        observed_at_ms: int,
        phase: str,
    ) -> LifecycleUpdate:
        record.timeout_phase = phase
        record.outcome = "failure"
        self._audit(phase, record.cmd_id, observed_at_ms)
        self._audit("late", record.cmd_id, observed_at_ms, status=str(frame.get("status") or ""))
        return LifecycleUpdate("late", "failure")

    def _accept_ack(
        self, record: CommandRecord, frame: dict[str, object], observed_at_ms: int
    ) -> LifecycleUpdate:
        status = str(frame.get("status") or "")
        if record.result_status in _TERMINAL_RESULT_STATUS:
            self._audit("late", record.cmd_id, observed_at_ms, status=status)
            return LifecycleUpdate("late")
        if record.ack_status is not None:
            self._audit("duplicate", record.cmd_id, observed_at_ms, status=status)
            return LifecycleUpdate("duplicate")
        record.ack_status = status
        self._audit("ack", record.cmd_id, observed_at_ms, status=status)
        if status in {"rejected", "duplicate"}:
            record.outcome = "failure"
            return LifecycleUpdate("accepted", "failure")
        return LifecycleUpdate("accepted")

    def _accept_result(
        self, record: CommandRecord, frame: dict[str, object], observed_at_ms: int
    ) -> LifecycleUpdate:
        status = str(frame.get("status") or "")
        if record.ack_status in {"rejected", "duplicate"}:
            self._audit("late", record.cmd_id, observed_at_ms, status=status)
            return LifecycleUpdate("late")
        if record.result_status in _TERMINAL_RESULT_STATUS or record.result_status == status:
            self._audit("duplicate", record.cmd_id, observed_at_ms, status=status)
            return LifecycleUpdate("duplicate")
        record.result_status = status
        self._audit("result", record.cmd_id, observed_at_ms, status=status)
        if status == "succeeded":
            record.outcome = "success"
            return LifecycleUpdate("accepted", "success")
        if status in _FAILURE_RESULT_STATUS:
            record.outcome = "failure"
            return LifecycleUpdate("accepted", "failure")
        return LifecycleUpdate("accepted")

    def expire(self, *, now_ms: int | None = None) -> list[dict[str, object]]:
        observed_at_ms = _now_ms() if now_ms is None else now_ms
        expired: list[dict[str, object]] = []
        with self._lock:
            for record in self._records.values():
                if record.outcome is not None or record.timeout_phase is not None:
                    continue
                if record.ack_status is None and observed_at_ms >= record.ack_deadline_ms:
                    record.timeout_phase = "delivery_timeout"
                elif record.ack_status is not None and observed_at_ms > record.result_deadline_ms:
                    record.timeout_phase = "execution_timeout"
                else:
                    continue
                record.outcome = "failure"
                event = self._audit(record.timeout_phase, record.cmd_id, observed_at_ms)
                event["outcome"] = "failure"
                expired.append(dict(event))
        return expired

    @staticmethod
    def _same_command(record: CommandRecord, frame: dict[str, object]) -> bool:
        return (
            record.trace_id == frame.get("trace_id")
            and record.session_id == frame.get("session_id")
            and record.utterance_id == frame.get("utterance_id")
        )

    def _audit(self, event: str, cmd_id: str, at_ms: int, *, status: str = "") -> dict[str, object]:
        item: dict[str, object] = {"event": event, "cmd_id": cmd_id, "at_ms": at_ms}
        if status:
            item["status"] = status
        self._audit_events.append(item)
        return item


def _now_ms() -> int:
    return int(time.time() * 1000)
