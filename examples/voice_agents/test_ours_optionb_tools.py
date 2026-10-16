"""方案B(2026-09-16) 单测: 删规则层后, 设备控制工具化 + prompt 规则 + 帧协议。

锁定四件事:
1. `_build_cmd_frame` 帧结构与原 g3_intent.build_outputs 逐字段一致(commands.yaml
   是唯一事实来源), 设备侧零改动;
2. 3 条设备命令 + resume_music 变成 LLM function tool: 组帧走
   `_dispatch_g3_cmd_frames`(reason=llm_tool_*), 然后 raise StopResponse——与原
   确定性路径同为"一句口语确认"的零额外延迟口径;
3. 行为规则进 system prompt: `_FIXED_AGENT_INSTRUCTIONS` 的 设备控制 段
   (四档音量 up/down/mute/set + value 0-100/电池四型/gauge_id 门/能力问句/否定/
   过去未来/多操作/音乐工具分家), 人设名替换沿用 _build_agent_instructions;
4. 规则层符号(G3 注册表/语义路由/音量抱怨正则/改口正则/切歌正则…)在
   web_ui_agent 命名空间里不复存在, 防回潮。

运行: cd examples/voice_agents && ../../.venv/bin/python -m pytest
test_ours_optionb_tools.py -q (字母序在 agent_config/music 之后, 避开 fake 模块污染)。
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

backends = types.ModuleType("app.backends")
backends.build_llm = lambda: None
backends.build_tts = lambda: None
backends.build_stt = lambda: None
backends.STT_BACKENDS = {}
backends.TTS_BACKENDS = {}
backends.backend_tabs_html = lambda *args, **kwargs: ""
backends.make_stt_backend = lambda *args, **kwargs: None
backends.make_tts_backend = lambda *args, **kwargs: None
sys.modules.setdefault("app.backends", backends)
providers_config = types.ModuleType("providers.config")
providers_config.funasr_hotwords = lambda: {}
sys.modules.setdefault("providers.config", providers_config)
silero = types.ModuleType("livekit.plugins.silero")
silero.VAD = types.SimpleNamespace(load=lambda *args, **kwargs: None)
sys.modules.setdefault("livekit.plugins.silero", silero)

import web_ui_agent  # noqa: E402

from livekit.agents import StopResponse  # noqa: E402
from livekit.agents.llm import ChatContext  # noqa: E402


class _FakeSession:
    def __init__(self) -> None:
        self.says: list[str] = []
        self.say_options: list[dict[str, bool]] = []
        self.interrupts = 0
        self.replies: list[dict[str, str]] = []

    def say(
        self,
        text: str,
        *,
        add_to_chat_ctx: bool = True,
        allow_interruptions: bool = True,
    ) -> None:
        self.says.append(text)
        self.say_options.append(
            {"add_to_chat_ctx": add_to_chat_ctx, "allow_interruptions": allow_interruptions}
        )

    def interrupt(self, force: bool = False) -> None:
        self.interrupts += 1

    def generate_reply(self, *, user_input: str, input_modality: str) -> None:
        self.replies.append({"user_input": user_input, "input_modality": input_modality})


class _FakePlayer:
    """音乐播放器假件: 只需 is_playing(在播静默门) + resume_last_for_tool。"""

    def __init__(self, *, playing: bool = False, resume_name: str | None = "中秋月") -> None:
        self.is_playing = playing
        self._resume_name = resume_name
        self.resumes = 0

    async def resume_last_for_tool(self) -> str | None:
        self.resumes += 1
        return self._resume_name


def _new_agent() -> tuple[types.SimpleNamespace, _FakeSession]:
    session = _FakeSession()
    agent = types.SimpleNamespace(
        session=session,
        _wiring=types.SimpleNamespace(live=None),
    )
    agent._music_control_intent = web_ui_agent.VoiceAgent._music_control_intent.__get__(agent)
    agent._apply_turn_filters = web_ui_agent.VoiceAgent._apply_turn_filters.__get__(agent)
    agent._finalize_g3_user_message = web_ui_agent.VoiceAgent._finalize_g3_user_message.__get__(agent)
    agent._reset_live_transcript = lambda *args, **kwargs: None
    agent._chat_ctx = ChatContext.empty()
    agent._remember_executed = web_ui_agent.VoiceAgent._remember_executed.__get__(agent)
    return agent, session


def _tool_self() -> tuple[types.SimpleNamespace, types.SimpleNamespace]:
    """工具直调假实例: 记录 _dispatch_g3_cmd_frames / _remember_executed 调用。

    类级取 FunctionTool 不绑实例, self 仍在签名首位, 传假实例占位。
    """
    calls = types.SimpleNamespace(dispatched=[], remembered=[])
    fake_self = types.SimpleNamespace(
        _dispatch_g3_cmd_frames=lambda frames, **kwargs: calls.dispatched.append((frames, kwargs)),
        _remember_executed=lambda desc: calls.remembered.append(desc),
    )
    # Exercise the real gate-disabled compatibility branch, not a no-op safety mock.
    fake_self._gate_enabled = lambda: False
    for name in ("_require_gate_turn", "_authorize_music", "_music_guard_kwargs"):
        method = getattr(web_ui_agent.VoiceAgent, name, None)
        if method is not None:
            setattr(fake_self, name, method.__get__(fake_self))
    return fake_self, calls


# ── 1. 帧协议: 与原 g3_intent.build_outputs 同构 ──────────────────────────────


@pytest.mark.parametrize(
    ("action", "capability_id", "params"),
    [
        ("adjust_volume", "audio.volume", {"command": "up"}),
        ("take_photo", "camera.photo", {"mode": "on"}),
        ("check_battery", "power.battery", {"type": "cap"}),
    ],
)
def test_cmd_frame_shape_matches_protocol(
    monkeypatch: pytest.MonkeyPatch, action: str, capability_id: str, params: dict
) -> None:
    monkeypatch.delenv("XIAOGE_SESSION_ID", raising=False)
    frame = web_ui_agent._build_cmd_frame(action, capability_id, params)

    assert set(frame) == {
        "type", "trace_id", "session_id", "utterance_id", "cmd_id",
        "capability_id", "action", "params", "risk_level",
        "ack_timeout_ms", "result_timeout_ms", "issued_at_ms",
    }
    assert frame["type"] == "data.cmd"
    assert frame["action"] == action
    assert frame["capability_id"] == capability_id
    assert frame["params"] == params
    assert frame["risk_level"] == "low"  # 三条命令全 low, 无高风险确认路径
    assert frame["ack_timeout_ms"] == 3000
    assert frame["result_timeout_ms"] == 3000
    assert frame["session_id"] == "sess-g3-local"  # 无 env 时的兜底会话号
    assert re.fullmatch(r"trace-g3-\d{13}", frame["trace_id"])
    assert re.fullmatch(r"utt-g3-\d{13}", frame["utterance_id"])
    assert re.fullmatch(r"cmd-g3-\d{6}", frame["cmd_id"])
    assert frame["issued_at_ms"] == int(frame["trace_id"].rsplit("-", 1)[1])


def test_cmd_frame_session_id_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XIAOGE_SESSION_ID", "sess-e2e-42")
    frame = web_ui_agent._build_cmd_frame("adjust_volume", "audio.volume", {"command": "down"})
    assert frame["session_id"] == "sess-e2e-42"


def test_cmd_frame_ids_differ_across_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    # 帧标识是毫秒粒度: 同毫秒内两次组帧完全相同(与原 g3_intent 口径一致),
    # 跨毫秒则 trace/utt/cmd/issued_at 全部不同。
    times = iter([1000.0, 1001.0])
    monkeypatch.setattr(web_ui_agent.time, "time", lambda: next(times))

    a = web_ui_agent._build_cmd_frame("take_photo", "camera.photo", {"mode": "on"})
    b = web_ui_agent._build_cmd_frame("take_photo", "camera.photo", {"mode": "on"})

    assert a["issued_at_ms"] == 1_000_000
    assert b["issued_at_ms"] == 1_001_000
    assert a["trace_id"] != b["trace_id"]
    assert a["utterance_id"] != b["utterance_id"]
    assert a["cmd_id"] != b["cmd_id"]


# ── 2. 设备控制工具: 组帧 + StopResponse(零额外延迟口径) ───────────────────────


def test_adjust_volume_tool_dispatches_frame_and_stops() -> None:
    async def run() -> None:
        fake_self, calls = _tool_self()

        with pytest.raises(StopResponse):
            await web_ui_agent.VoiceAgent.adjust_volume(fake_self, None, "up")

        assert len(calls.dispatched) == 1
        frames, kwargs = calls.dispatched[0]
        assert kwargs["reason"] == "llm_tool_adjust_volume"
        assert len(frames) == 1
        frame = frames[0]
        assert frame["type"] == "data.cmd"
        assert frame["action"] == "adjust_volume"
        assert frame["capability_id"] == "audio.volume"
        assert frame["params"] == {"command": "up"}

    asyncio.run(run())


@pytest.mark.parametrize("command", ["up", "down", "mute"])
def test_adjust_volume_tool_accepts_direction_commands(command: str) -> None:
    async def run() -> None:
        fake_self, calls = _tool_self()

        with pytest.raises(StopResponse):
            await web_ui_agent.VoiceAgent.adjust_volume(fake_self, None, command)

        frames, _ = calls.dispatched[0]
        assert frames[0]["params"] == {"command": command}  # 非 set 不带 value

    asyncio.run(run())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (60, 60), (30, 30), (0, 0), (100, 100), (50, 50),  # 常规档位
        (101, 50), (-1, 50),                               # 越界回落协议默认 50
        ("60", 50), (None, 50),                            # LLM 传脏类型回落 50
    ],
)
def test_adjust_volume_tool_set_carries_value(value: object, expected: int) -> None:
    """volset(2026-09-20 自 llm-bak 移植): set 帧恒带 value(0-100),
    缺失/越界/脏类型回落 50(设备侧缺值同款兜底)。"""
    async def run() -> None:
        fake_self, calls = _tool_self()

        with pytest.raises(StopResponse):
            await web_ui_agent.VoiceAgent.adjust_volume(fake_self, None, "set", value)

        frames, _ = calls.dispatched[0]
        assert frames[0]["params"] == {"command": "set", "value": expected}

    asyncio.run(run())


def test_adjust_volume_tool_rejects_invalid_command() -> None:
    async def run() -> None:
        fake_self, calls = _tool_self()

        # LLM 传错枚举 → 纠正性文本(不下帧不 StopResponse), 让它重调。
        result = await web_ui_agent.VoiceAgent.adjust_volume(fake_self, None, "loud")

        assert "参数不合法" in result
        assert "up/down/mute/set" in result
        assert calls.dispatched == []

    asyncio.run(run())


def test_take_photo_tool_dispatches_frame_and_stops() -> None:
    async def run() -> None:
        fake_self, calls = _tool_self()

        with pytest.raises(StopResponse):
            await web_ui_agent.VoiceAgent.take_photo(fake_self, None)

        assert len(calls.dispatched) == 1
        frames, kwargs = calls.dispatched[0]
        assert kwargs["reason"] == "llm_tool_take_photo"
        frame = frames[0]
        assert frame["action"] == "take_photo"
        assert frame["capability_id"] == "camera.photo"
        assert frame["params"] == {"mode": "on"}

    asyncio.run(run())


@pytest.mark.parametrize(
    ("battery_type", "gauge_id", "expected_params"),
    [
        ("cap", None, {"type": "cap"}),
        ("vol", None, {"type": "vol"}),
        ("cur", None, {"type": "cur"}),
        # chg 已随充电状态退出交互协议(2026-09-20), 非法入参见 rejects_invalid_type。
        ("cap", 1, {"type": "cap", "gauge_id": 1}),
        ("cur", 99, {"type": "cur"}),                 # 越界编号当没传
    ],
)
def test_check_battery_tool_params_variants(
    battery_type: str, gauge_id: int | None, expected_params: dict
) -> None:
    async def run() -> None:
        fake_self, calls = _tool_self()

        with pytest.raises(StopResponse):
            await web_ui_agent.VoiceAgent.check_battery(fake_self, None, battery_type, gauge_id)

        assert len(calls.dispatched) == 1
        frames, kwargs = calls.dispatched[0]
        assert kwargs["reason"] == "llm_tool_check_battery"
        frame = frames[0]
        assert frame["action"] == "check_battery"
        assert frame["capability_id"] == "power.battery"
        assert frame["params"] == expected_params

    asyncio.run(run())


def test_check_battery_tool_rejects_invalid_type() -> None:
    async def run() -> None:
        fake_self, calls = _tool_self()

        result = await web_ui_agent.VoiceAgent.check_battery(fake_self, None, "power")

        assert "参数不合法" in result
        assert "cap/vol/cur" in result
        assert "chg" not in result  # 充电状态已退出协议, 不在合法枚举里
        assert calls.dispatched == []

        # 旧提示词语境下的模型可能仍带 chg: 一并拒绝, 零帧下发。
        result = await web_ui_agent.VoiceAgent.check_battery(fake_self, None, "chg")
        assert "参数不合法" in result
        assert calls.dispatched == []

    asyncio.run(run())


def test_resume_music_tool_states(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        # ① 音乐功能未启用: 如实说, 不炸。
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", None)
        fake_self, calls = _tool_self()
        result = await web_ui_agent.VoiceAgent.resume_music(fake_self, None)
        assert result == "音乐功能没启用。"
        assert calls.remembered == []

        # ② 没播过: 让 LLM 如实告诉用户, 不写执行标记(没执行)。
        player = _FakePlayer(resume_name=None)
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        result = await web_ui_agent.VoiceAgent.resume_music(fake_self, None)
        assert "刚才没有播放过音乐" in result
        assert calls.remembered == []

        # ③ 恢复成功: 写命令记忆标记("刚才放了什么歌"可答), 指引 LLM 口语确认。
        player = _FakePlayer(resume_name="中秋月")
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        result = await web_ui_agent.VoiceAgent.resume_music(fake_self, None)
        assert "已恢复播放《中秋月》" in result
        assert calls.remembered == ["恢复播放云端音乐(《中秋月》)"]

    asyncio.run(run())


# ── 3. 行为规则进 prompt ───────────────────────────────────────────────────


def test_prompt_contains_device_control_rules() -> None:
    fixed = web_ui_agent._FIXED_AGENT_INSTRUCTIONS

    # 三条设备命令 + 音乐四工具的分家约定。
    assert "# 设备控制（必须遵守）" in fixed
    for tool_name in (
        "adjust_volume", "take_photo", "check_battery",
        "play_music", "stop_music", "switch_music", "resume_music",
    ):
        assert tool_name in fixed
    assert "不要用设备工具控制音乐" in fixed
    assert "音量例外" in fixed

    # 单一操作直接执行；谈论、否定、回顾、假设及多操作需要谨慎区分。
    assert "禁止或否定" in web_ui_agent._FIXED_AGENT_INSTRUCTIONS
    assert "说的是禁止或否定" in web_ui_agent._FIXED_AGENT_INSTRUCTIONS
    assert "先问要做哪一个" in web_ui_agent._FIXED_AGENT_INSTRUCTIONS

    # 音量四档(volset, 2026-09-20 自 llm-bak 移植): max/min 并入 set(100/0)。
    assert "up/down/mute/set 四档" in fixed
    assert "set value=100" in fixed  # "调到最大"→set 100 映射在位

    # 电池三型映射 + gauge_id 门(只有"2号电池"才传) + 数值不编 + chg 退出协议。
    assert "问电量/还有多少电→cap" in fixed
    assert "充电状态已退出交互协议" in fixed
    assert "→chg" not in fixed
    assert "2号电池" in fixed
    assert "你不要自己编数" in fixed

    # 能力问句/否定/过去未来引用 → 不调工具正常聊。
    assert "直接说会，不调工具" in fixed
    assert "禁止或否定" in fixed
    assert "引用过去或假设未来" in fixed

    # 一句话多个操作先问。
    assert "先问要做哪一个" in fixed


def test_agent_instructions_persona_name_replaced_in_fixed_section() -> None:
    default_cfg = web_ui_agent.AgentConfig(persona_name="小歌")
    default_instructions = web_ui_agent._build_agent_instructions(default_cfg)
    assert "# 你是谁" in default_instructions
    assert "关于小歌自身的事实性问题" in default_instructions
    assert "小歌都会啥" in default_instructions

    renamed_cfg = web_ui_agent.AgentConfig(persona_name="小美")
    renamed_instructions = web_ui_agent._build_agent_instructions(renamed_cfg)
    # 固定规则段里的"小歌"随名字整体替换(知识约定/示例自洽用新名字)。
    assert "你叫小美" in renamed_instructions
    assert "关于小美自身的事实性问题" in renamed_instructions
    assert "小美都会啥" in renamed_instructions
    assert "关于小歌自身的事实性问题" not in renamed_instructions


def test_voice_agent_constructor_accepts_optional_project_key() -> None:
    params = list(inspect.signature(web_ui_agent.VoiceAgent.__init__).parameters)
    assert params == ["self", "api_key"]


# ── 4. 规则层符号不复存在(防回潮) ──────────────────────────────────────────


def test_rule_layer_symbols_removed_from_module() -> None:
    removed = (
        # G3 确定性注册表 / 语义路由 LLM 兜底
        "g3_intent", "SemanticRouter", "SemanticDecision", "SemanticRoute",
        # 创作意图特殊禁工具分支
        "_is_creative_request", "_CREATIVE_REQUEST_VERB_RE", "_CREATIVE_REQUEST_TOPIC_RE",
        "_CREATIVE_FOLLOWUP_RE", "_CREATIVE_REPLY_INSTRUCTIONS",
        "SemanticRouterConfig", "KnowledgeHit",
        "_semantic_router", "_maybe_handle_g3_protocol_turn",
        "_build_g3_state", "_g3_validation_and_frames", "_remember_g3_pending_high_risk",
        "_semantic_frames", "_play_cloud_music", "_execute_route",
        # 音乐 play/resume/switch/改口 正则前置层
        "_MUSIC_NEXT_RE", "_MUSIC_PREV_RE", "_MUSIC_SWITCH_BARE_RE", "_MUSIC_MINDCHANGE",
        # 音量抱怨/祈使正则层
        "_VOLUME_COMPLAINT_RE", "_VOLUME_IMPERATIVE_RE", "_natural_volume_reply",
    )
    for name in removed:
        assert not hasattr(web_ui_agent, name), f"规则层符号回潮: {name}"


# ── 5. 在播静默门放行设备话术(音量/拍照/电量是音乐在播时也该响应的) ──────────


@pytest.mark.parametrize(
    "text",
    [
        "音量大一点", "帮我拍张照", "问一下电量",
        # 2026-09-17 四门放行(playgate 移植): 停止否定/音量口语/问句/点名
        "我不想再听这歌了", "这歌听够了", "太吵了", "小点声",
        "声音太大了", "这是什么歌", "小歌还在吗", "算了",
        # 2026-09-20 volgen(自 llm-bak 移植): 裸步进音量——没有"音量/声音"关键词,
        # 原先被在播静默门吞掉;两条锚定分支(动词式/裸副词式)都要过门落 LLM。
        "调大一点", "调小一点嘛", "帮我调小一点", "再大一点", "大一点", "小一点呀",
    ],
)
def test_in_play_gate_passes_device_commands(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    async def run() -> None:
        player = _FakePlayer(playing=True)
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda *args, **kwargs: None)

        # 云端音乐在播时说设备命令: 过静默门落 LLM 调工具, 不被当闲话吞掉。
        await agent._apply_turn_filters(text, False)
        await asyncio.sleep(0)

        assert session.says == []
        assert session.interrupts == 0

    asyncio.run(run())
