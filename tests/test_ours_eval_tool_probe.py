"""Pure offline tests: fake transports only, never instantiate production agents."""

import asyncio
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from eval_10097.tool_probe import (
    TOOL_NAMES,
    BudgetExceeded,
    ProbeBaseline,
    QwenConfig,
    RecordingStub,
    RequestBudget,
    ToolProbe,
    extract_tool_schemas,
)


@pytest.fixture
def schemas():
    return extract_tool_schemas()


def baseline(config, schemas, instructions="frozen prompt"):
    return ProbeBaseline(
        config.model,
        config.base_url,
        hashlib.sha256(instructions.encode()).hexdigest(),
        hashlib.sha256(
            json.dumps(schemas, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        verified=True,
    )


def config():
    return QwenConfig(base_url="https://example.invalid/v1", model="offline-test-model")


def completion(calls=(), content=""):
    return {
        "choices": [
            {
                "message": {
                    "content": content,
                    "tool_calls": [
                        {"function": {"name": name, "arguments": json.dumps(arguments)}}
                        for name, arguments in calls
                    ],
                }
            }
        ]
    }


def test_inventory_signature_docs_and_no_production_import(schemas):
    assert {item["function"]["name"] for item in schemas} == TOOL_NAMES
    lookup = {item["function"]["name"]: item["function"] for item in schemas}
    volume = lookup["adjust_volume"]["parameters"]
    assert volume["required"] == ["command"]
    assert volume["properties"]["command"]["enum"] == ["up", "down", "mute", "set"]
    assert volume["properties"]["value"]["default"] == 50
    assert lookup["check_battery"]["parameters"]["required"] == ["battery_type"]
    assert "Args:" not in lookup["play_music"]["description"]
    assert "music_id" in lookup["play_music"]["parameters"]["properties"]
    assert "web_ui_agent" not in sys.modules
    assert "app.session_state" not in sys.modules


def test_ast_source_never_executes(tmp_path):
    source = Path(__file__).resolve().parents[1] / "examples/voice_agents/web_ui_agent.py"
    isolated = tmp_path / "source.py"
    isolated.write_text(
        "raise RuntimeError('must never execute')\n"
        + source.read_text(encoding="utf-8").replace("from __future__ import annotations", ""),
        encoding="utf-8",
    )
    assert len(extract_tool_schemas(isolated)) == 9
    isolated.write_text("class VoiceAgent:\n    pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="inventory"):
        extract_tool_schemas(isolated)


@pytest.mark.parametrize("limit,used", [(121, 0), (-1, 0), (2, 3), (True, 0), (120, -1)])
def test_budget_rejects_invalid_counters(limit, used):
    with pytest.raises(ValueError):
        RequestBudget(limit, used)


def test_atomic_hard_budget():
    budget = RequestBudget()

    def reserve(_):
        try:
            budget.consume()
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(160))) == 120
    assert budget.used == 120
    assert budget.remaining == 0
    with pytest.raises(ValueError):
        budget.consume(0)


@pytest.mark.parametrize(
    "name,args",
    [
        ("play_music", {}),
        ("stop_music", {}),
        ("switch_music", {}),
        ("adjust_volume", {"command": "set", "value": 30}),
        ("take_photo", {}),
        ("check_battery", {"battery_type": "cap"}),
        ("resume_music", {}),
        ("query_knowledge", {"query": "硬件规格"}),
        ("query_weather", {"city": "北京"}),
    ],
)
def test_all_exits_are_record_only(schemas, name, args):
    stub = RecordingStub(schemas)
    recorded = stub.record(name, json.dumps(args))
    assert recorded["status"] == "recorded"
    assert recorded["reason"] == "stub_only_no_execution"
    assert recorded["arguments"] == args
    assert len(stub.calls) == 1


@pytest.mark.parametrize(
    "name,args,reason",
    [
        ("dispatch_device", "{}", "unknown_tool"),
        ("take_photo", "not json", "invalid_json"),
        ("adjust_volume", '{"command":"wrong"}', "invalid_arguments"),
        ("adjust_volume", '{"command":"set","value":true}', "invalid_arguments"),
        ("adjust_volume", '{"command":"set","value":"30"}', "invalid_arguments"),
        ("check_battery", "{}", "invalid_arguments"),
        ("take_photo", '{"execute":true}', "invalid_arguments"),
        ("take_photo", "[]", "invalid_arguments"),
    ],
)
def test_unknown_invalid_calls_rejected_and_retained(schemas, name, args, reason):
    stub = RecordingStub(schemas)
    result = stub.record(name, args)
    assert result["status"] == "rejected"
    assert result["reason"] == reason
    assert stub.calls == [result]


async def test_natural_language_shared_budget_and_request(schemas):
    requests = []

    async def fake(request):
        requests.append(request)
        return completion([("adjust_volume", {"command": "set", "value": 30})])

    cfg = config()
    budget = RequestBudget(limit=2)
    budget.consume()  # Shared upstream text/audio reservation.
    probe = ToolProbe(fake, schemas, budget, cfg, baseline(cfg, schemas))
    result = await probe.run("frozen prompt", "音量调到30", [{"role": "user", "content": "你好"}])
    assert result["status"] == "ok"
    assert result["request_number"] == 2
    assert result["calls"][0]["arguments"] == {"command": "set", "value": 30}
    assert requests[0]["messages"][-1]["content"] == "音量调到30"
    assert requests[0]["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert requests[0]["tool_choice"] == "auto"
    assert result["elapsed_ms"] >= 0
    blocked = await probe.run("frozen prompt", "拍照")
    assert blocked["reason"] == "budget_exhausted"
    assert len(requests) == 1


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_failures_consume_budget_no_retry_no_secret(schemas, failure):
    calls = 0

    async def fake(_):
        nonlocal calls
        calls += 1
        if failure == "timeout":
            await asyncio.sleep(1)
        raise RuntimeError("SECRET must not leak")

    cfg = QwenConfig(base_url="https://example.invalid", model="fake", timeout_s=0.001)
    budget = RequestBudget(1)
    result = await ToolProbe(fake, schemas, budget, cfg, baseline(cfg, schemas)).run(
        "frozen prompt", "拍照"
    )
    assert result["status"] == failure
    assert "SECRET" not in json.dumps(result)
    assert budget.used == calls == 1


async def test_missing_or_mismatched_baseline_never_calls_transport(schemas):
    async def forbidden(_):
        pytest.fail("transport must not be called")

    cfg = config()
    budget = RequestBudget()
    result = await ToolProbe(forbidden, schemas, budget, cfg).run("frozen prompt", "拍照")
    assert result["status"] == "blocked"
    probe = ToolProbe(forbidden, schemas, budget, cfg, baseline(cfg, schemas))
    assert (await probe.run("changed prompt", "拍照"))["status"] == "blocked"
    assert budget.used == 0


def test_config_tls_and_explicit_model_env():
    with pytest.raises(ValueError, match="baseline"):
        QwenConfig.from_env({})
    cfg = QwenConfig.from_env({"QWEN_BASE_URL": "https://example.invalid", "QWEN_MODEL": "fake"})
    assert cfg.verify_ssl is True
    assert QwenConfig().verify_ssl is True
    assert (
        QwenConfig.from_env(
            {
                "QWEN_BASE_URL": "https://example.invalid",
                "QWEN_MODEL": "fake",
                "QWEN_VERIFY_SSL": "false",
            }
        ).verify_ssl
        is False
    )
