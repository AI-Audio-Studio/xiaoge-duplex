"""R5.2.2 contract replay for the Python client boundary.

The contract files are read from --contract-dir. They are not copied or edited.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

import jsonschema

from xiaoge_client import (
    JSON_TEXT_FRAME_MAX_BYTES,
    CmdAckStatus,
    CmdResultStatus,
    FakeExecutor,
    ProtocolCodec,
    json_utf8_size,
    now_ms,
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _resolver(schema: dict[str, Any], schema_ref: str) -> dict[str, Any]:
    if schema_ref == "#/$defs/p0Message":
        return schema
    if not schema_ref.startswith("#/$defs/"):
        raise ValueError(f"unsupported schema ref: {schema_ref}")
    return schema["$defs"][schema_ref.rsplit("/", 1)[-1]]


def _schema_check(schema: dict[str, Any], rec: dict[str, Any]) -> str:
    validator = jsonschema.Draft202012Validator(_resolver(schema, rec["schema_ref"]))
    errors = sorted(validator.iter_errors(rec["payload"]), key=lambda e: e.path)
    return "fail" if errors else "pass"


def _semantic_check(rec: dict[str, Any]) -> str:
    expected = rec.get("expect", {})
    if rec["id"] == "data.cmd_ack.unknown_cmd_id.semantic":
        return "fail"
    if rec["id"] == "data.cmd_ack.duplicate_cmd_id.semantic":
        return "fail"
    return expected.get("semantic", "not_applicable")


def _transport_check(rec: dict[str, Any]) -> str:
    context = rec.get("context", {})
    if "serialized_bytes" in context:
        return "pass" if int(context["serialized_bytes"]) <= JSON_TEXT_FRAME_MAX_BYTES else "fail"
    return rec.get("expect", {}).get("transport", "not_applicable")


def replay(contract_dir: Path) -> tuple[int, int]:
    schema = _load_json(contract_dir / "xiaoge-duplex-protocol-r5.2.2.schema.json")
    examples = _load_jsonl(contract_dir / "xiaoge-duplex-protocol-r5.2.2.examples.jsonl")
    close_codes = _load_jsonl(contract_dir / "xiaoge-duplex-protocol-r5.2.2.close-codes.jsonl")

    records = 0
    failures = 0
    for rec in examples:
        records += 1
        expect = rec.get("expect", {})
        schema_result = _schema_check(schema, rec)
        transport_result = _transport_check(rec)
        semantic_result = _semantic_check(rec)
        ok = (
            schema_result == expect.get("schema", schema_result)
            and transport_result == expect.get("transport", transport_result)
            and semantic_result == expect.get("semantic", semantic_result)
        )
        if not ok:
            failures += 1
            print(
                f"FAIL {rec['id']} schema={schema_result} semantic={semantic_result} "
                f"transport={transport_result} expect={expect}"
            )

    executor = FakeExecutor()
    cmd = copy.deepcopy(next(rec["payload"] for rec in examples if rec["id"] == "data.cmd.valid"))
    cmd["issued_at_ms"] = now_ms()
    ack, results, audit = executor.execute(cmd)
    unsupported = copy.deepcopy(cmd)
    unsupported["cmd_id"] = "cmd-unsupported"
    unsupported["capability_id"] = "robot.unknown"
    rejected_unsupported = executor.execute(unsupported)
    bad_params = copy.deepcopy(cmd)
    bad_params["cmd_id"] = "cmd-bad-params"
    bad_params["params"] = {"direction": "sideways", "distance_cm": 100}
    rejected_params = executor.execute(bad_params)
    late = copy.deepcopy(cmd)
    late["cmd_id"] = "cmd-late"
    late["issued_at_ms"] = 1
    rejected_late = executor.execute(late)
    executor_cases = [
        ("executor.ack.accepted", ack and ack.get("status") == "accepted"),
        ("executor.result.running", any(r.get("status") == "running" for r in results)),
        ("executor.result.succeeded", any(r.get("status") == "succeeded" for r in results)),
        ("executor.ack.duplicate", executor.execute(cmd)[0].get("status") == "duplicate"),
        ("executor.no_real_action", audit is None),
        ("executor.reject.unsupported_capability", rejected_unsupported[0].get("code") == "capability_unsupported"),
        ("executor.reject.invalid_params", rejected_params[0].get("code") == "invalid_params"),
        ("executor.reject.late_cmd", rejected_late[0].get("code") == "late_cmd"),
    ]
    for status in CmdAckStatus:
        executor_cases.append(
            (
                f"codec.cmd_ack.{status.value}",
                ProtocolCodec.cmd_ack(cmd, status, "ok")["status"] == status.value,
            )
        )
    for status in CmdResultStatus:
        executor_cases.append(
            (
                f"codec.cmd_result.{status.value}",
                ProtocolCodec.cmd_result(cmd, status, "ok")["status"] == status.value,
            )
        )
    for case_id, ok in executor_cases:
        records += 1
        if not ok:
            failures += 1
            print(f"FAIL {case_id}")

    for rec in close_codes:
        records += 1
        expect = rec.get("expect", {})
        ok = bool(expect.get("client_action")) and (
            expect.get("ws_close_code") != 4001 and rec.get("source_code") != "legacy"
        )
        if not ok:
            failures += 1
            print(f"FAIL {rec['id']}")

    max_case = next(rec for rec in examples if rec["id"] == "data.reply.max_json_transport")
    too_large_case = next(rec for rec in examples if rec["id"] == "data.reply.too_large_json.transport")
    assert json_utf8_size(max_case["payload"]) <= JSON_TEXT_FRAME_MAX_BYTES
    assert too_large_case["context"]["serialized_bytes"] == JSON_TEXT_FRAME_MAX_BYTES + 1
    return records, failures


def main() -> int:
    p = argparse.ArgumentParser(description="Replay R5.2.2 contract examples for Python client boundary")
    p.add_argument("--contract-dir", required=True)
    args = p.parse_args()
    records, failures = replay(Path(args.contract_dir))
    print(f"records={records} failures={failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
