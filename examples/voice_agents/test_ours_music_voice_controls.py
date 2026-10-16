from __future__ import annotations

import asyncio
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

import app.setup_taps as setup_taps  # noqa: E402
import web_ui_agent  # noqa: E402
from app.music_player import _switch_target  # noqa: E402

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


class _FakeMusicPlayer:
    def __init__(
        self,
        *,
        playing: bool = False,
        last_name: str = "song_one",
        switch_name: str = "中秋月",
    ) -> None:
        self.is_playing = playing
        self.last_name = last_name
        self.switch_name = switch_name
        self.plays: list[str | None] = []
        self.switches: list[str] = []
        self.resumes = 0
        self.stops = 0

    async def play_for_tool(self, music_id: str | None) -> str | None:
        self.plays.append(music_id)
        self.is_playing = True
        return self.last_name

    async def switch_for_tool(self, direction: str) -> str | None:
        self.switches.append(direction)
        self.is_playing = True
        return self.switch_name

    async def resume_last_for_tool(self) -> str | None:
        self.resumes += 1
        self.is_playing = True
        return self.last_name or None

    async def stop_for_tool(self) -> bool:
        self.stops += 1
        was = self.is_playing
        self.is_playing = False
        return was


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
    agent._finish_consumed_voice_turn = (
        web_ui_agent.VoiceAgent._finish_consumed_voice_turn.__get__(agent)
    )
    agent.handle_manual_text = web_ui_agent.VoiceAgent.handle_manual_text.__get__(agent)
    # 面板热配(2026-09-10)在 handle_manual_text 起点新增的钩子调用:音乐测试
    # 只关心命令路由,真钩子行为由 test_ours_agent_config.py 全覆盖,这里 no-op。
    async def _noop_maybe_apply_agent_config() -> None:
        return None

    agent._maybe_apply_agent_config = _noop_maybe_apply_agent_config
    # 退出机制/全双工门(2026-09-15)在 handle_manual_text 链上新增的实例调用:
    # 音乐测试只关心命令路由,门行为由 test_ours_exit_standby.py 全覆盖,这里固定开。
    agent._duplex_off = lambda: False
    agent._chat_ctx = ChatContext.empty()
    agent._remember_executed = web_ui_agent.VoiceAgent._remember_executed.__get__(agent)
    # 命令派发方法(方案B 2026-09-16): 设备控制工具(LLM 直调)组帧的唯一出口。
    agent._dispatch_g3_cmd_frames = (
        web_ui_agent.VoiceAgent._dispatch_g3_cmd_frames.__get__(agent)
    )
    return agent, session


# ── 方案B(2026-09-16): 前置音乐正则层只剩"停止"一路, 其余全落 LLM 工具 ────────
# play/resume/switch/改口 语义判别交给聊天 LLM(function tools), 过滤层对这类
# 话术必须"零消费": 不抛 StopResponse、不碰播放器、不播罐头, 原话进 generate_reply。


@pytest.mark.parametrize("command", ["唱首歌吧", "唱一首歌吧", "放首歌吧"])
def test_generic_play_with_particle_falls_through_to_chat(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda *args, **kwargs: None)

        await agent.handle_manual_text(command)
        await asyncio.sleep(0)

        assert player.plays == []  # 过滤层不播: 播放由 LLM 调 play_music 工具完成
        assert session.says == []  # 罐头"好的，播放《…》。"已随正则层删除
        assert session.replies == [{"user_input": command, "input_modality": "text"}]

    asyncio.run(run())


def test_voice_music_turn_falls_through_to_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(last_name="声动未来-聚力同行")
        agent, _ = _new_agent()
        events: list[dict] = []
        resets: list[str] = []
        agent._reset_live_transcript = resets.append
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda message: events.append(message) or True)

        # 语音轮同口径: 过滤层不消费播放轮(不抛 StopResponse), 用户气泡/重置都
        # 不发生在过滤层——原话进 LLM, 由 play_music 工具播。
        await agent._apply_turn_filters(
            "，播放音乐", False, finalize_user_message=True
        )
        await asyncio.sleep(0)

        assert events == []  # 无 music_play 路径的用户气泡广播
        assert resets == []  # 无 music_play 重置——这轮没被吃掉

    asyncio.run(run())


def test_manual_music_turn_falls_through_without_filter_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        events: list[dict] = []
        resets: list[str] = []
        agent._reset_live_transcript = resets.append
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda message: events.append(message) or True)

        await agent.handle_manual_text("播放音乐")
        await asyncio.sleep(0)

        # 用户气泡由 generate_reply 之后的会话层链路展示, 过滤层零广播(天然无重复)。
        user_messages = [event for event in events if event.get("role") == "user"]
        assert user_messages == []
        assert resets == []
        assert session.replies == [{"user_input": "播放音乐", "input_modality": "text"}]

    asyncio.run(run())


def test_bare_continue_does_not_resume_music(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=False, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)

        # 裸"继续"多指"接着说": 不再劫持成音乐续播, 落回 LLM 聊天。
        await agent._apply_turn_filters("继续", False)
        await asyncio.sleep(0)

        assert player.resumes == 0
        assert session.says == []
        assert session.interrupts == 0

    asyncio.run(run())


def test_manual_bare_continue_falls_through_to_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=False, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda *args, **kwargs: None)

        await agent.handle_manual_text("继续")
        await asyncio.sleep(0)

        assert player.resumes == 0
        assert session.says == []
        # 落 LLM 聊天接着说, 不被劫持成"继续播放《…》"。
        assert session.replies == [{"user_input": "继续", "input_modality": "text"}]

    asyncio.run(run())


# ── 停止: 方案B 保留的唯一前置音乐直行路径(在播时秒停, 零 LLM 往返) ──────────


@pytest.mark.parametrize("command", ["停止播放", "别唱了", "不要唱了", "停"])
def test_explicit_stop_music_says_ack(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)

        with pytest.raises(StopResponse):
            await agent._apply_turn_filters(command, False)
        await asyncio.sleep(0)

        assert player.stops == 1
        assert session.says == ["好的，音乐停了。"]
        assert session.say_options == [{"add_to_chat_ctx": False, "allow_interruptions": False}]
        assert session.interrupts == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    ("command", "playing", "reason"),
    [("暂停播放", True, "music_stop")],
)
def test_voice_music_stop_close_live_turn(
    monkeypatch: pytest.MonkeyPatch, command: str, playing: bool, reason: str
) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=playing, last_name="声动未来-聚力同行")
        agent, _ = _new_agent()
        resets: list[str] = []
        agent._reset_live_transcript = resets.append
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda message: True)

        with pytest.raises(StopResponse):
            await agent._apply_turn_filters(command, False, finalize_user_message=True)
        await asyncio.sleep(0)

        assert resets == [reason]

    asyncio.run(run())


def test_bare_stop_does_not_match_longer_word(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)

        # "停车"不是停止音乐: StopResponse 来自在播静默门(讲非音乐话被吞), 音乐
        # 停止分支与停止词都不命中, 播放器不动、零播报。
        with pytest.raises(StopResponse):
            await agent._apply_turn_filters("停车", False)

        assert player.stops == 0
        assert session.says == []

    asyncio.run(run())


def test_manual_chat_while_music_playing_uses_standard_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda *args, **kwargs: None)
        async def _noop(*args, **kwargs) -> None:
            return None

        agent._maybe_apply_agent_config = _noop
        agent._duplex_off = lambda: False
        agent._apply_turn_filters = _noop

        await agent.handle_manual_text("讲个故事")

        assert player.stops == 0
        assert session.says == []
        assert session.replies == [{"user_input": "讲个故事", "input_modality": "text"}]

    asyncio.run(run())


# ---- 2026-09-17 在播静默门四门放行(playgate 移植): 停止否定/问句/点名/音量口语 ----


@pytest.mark.parametrize(
    "text",
    [
        "我不想再听这歌了",  # 停止否定: 裸停正则 miss, 过门落 LLM 由 stop_music 兜底
        "这歌听够了",
        "听腻了",
        "算了",
        "太吵了",
        "小点声",
        "声音太大了",
        "安静点",
        "这是什么歌",  # 问句门
        "小歌还在吗",  # 点名门
    ],
)
def test_in_play_gate_passes_stop_negatives_and_questions(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    async def run() -> None:
        # 云端在播时这些话不能被静默吞掉: 过门落 LLM(停止类由 stop_music 兜底);
        # 也不是停止分支直行(播放器不动、零播报)。
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda *args, **kwargs: None)

        await agent._apply_turn_filters(text, False)
        await asyncio.sleep(0)

        assert player.stops == 0
        assert session.says == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "text",
    [
        "讲个故事",  # 闲聊照旧吞
        "今天天气不错",
        "停车",  # 长词反例照旧吞
        "哈哈真好听",  # 泛词不进门: 放歌中夸好听是跟唱, 不是控制
    ],
)
def test_in_play_gate_still_swallows_plain_chat(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)

        with pytest.raises(StopResponse):
            await agent._apply_turn_filters(text, False)

        assert player.stops == 0
        assert session.says == []

    asyncio.run(run())


def test_music_playing_pass_gate_doors() -> None:
    gate = web_ui_agent._music_playing_pass_gate

    # 问句门
    assert gate(None, "这是什么歌")
    assert gate(None, "换一首好吗")
    # 点名门: 小歌 恒放行; 人设名经 _agent_cfg
    assert gate(None, "小歌")
    cfg = types.SimpleNamespace(persona_name="小甜")
    assert gate(types.SimpleNamespace(_agent_cfg=cfg), "小甜你唱得真好听")
    assert not gate(types.SimpleNamespace(), "小甜")  # 无人设名时不靠名字放行
    # 音乐词门
    assert gate(None, "太难听了")
    assert gate(None, "太吵了")
    # 设备词门(词表)
    assert gate(None, "音量大一点")
    # 全 miss
    assert not gate(None, "讲个故事")
    assert not gate(None, "停车")


def test_manual_stop_music_handled_by_filters_not_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "broadcast", lambda *args, **kwargs: None)

        # 方案B: 音乐停止仍前置直行(在播时秒停, 不等 LLM 工具往返), 不进 generate_reply。
        await agent.handle_manual_text("停止播放")
        await asyncio.sleep(0)

        assert player.stops == 1
        assert session.says == ["好的，音乐停了。"]
        assert session.say_options == [{"add_to_chat_ctx": False, "allow_interruptions": False}]
        assert session.replies == []

    asyncio.run(run())


def test_final_bare_stop_defers_to_music_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    player = _FakeMusicPlayer(playing=True)
    session = _FakeSession()
    wiring = types.SimpleNamespace(
        session=session,
        live_from_main=False,
        live=None,
    )
    event = types.SimpleNamespace(is_final=True, transcript="停")
    monkeypatch.setattr(setup_taps.runtime, "music_player", player)
    monkeypatch.setattr(setup_taps.runtime, "listen_ctrl", None)
    monkeypatch.setattr(setup_taps, "_log", lambda *args, **kwargs: None)

    setup_taps._handle_stt_event(wiring, event)

    assert session.interrupts == 0


def test_final_bare_stop_interrupts_without_music(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    wiring = types.SimpleNamespace(
        session=session,
        live_from_main=False,
        live=None,
    )
    event = types.SimpleNamespace(is_final=True, transcript="停")
    monkeypatch.setattr(setup_taps.runtime, "music_player", None)
    monkeypatch.setattr(setup_taps.runtime, "listen_ctrl", None)
    monkeypatch.setattr(setup_taps, "listen_interrupt_blocked", lambda: False)
    monkeypatch.setattr(setup_taps, "_log", lambda *args, **kwargs: None)

    setup_taps._handle_stt_event(wiring, event)

    assert session.interrupts == 1


# ── _music_control_intent 方案B 契约: 只剩 stop 一路, 其余全部让给 LLM ─────────


@pytest.mark.parametrize(
    ("text", "music_ctx", "expected"),
    [
        # 停止: 带音乐词/裸"停"
        ("停止播放", False, "stop"),
        ("别唱了", False, "stop"),
        ("不要唱了", False, "stop"),
        # 裸停止口语: 只在音乐上下文(云端在播)收口(2026-09-20 起"停"家族并入)
        ("停", True, "stop"),
        ("停下吧", True, "stop"),
        ("停", False, None),
        ("别放了", True, "stop"),
        ("暂停", True, "stop"),
        ("别放了", False, None),
        # 2026-09-17 主语前缀(llm-bak playgate 移植): "我不想听了"带主语此前 ^ 锚定够不着
        ("我不想听了", True, "stop"),
        ("我们不想听了", True, "stop"),
        ("咱们不想唱了", True, "stop"),
        ("我不想听", True, "stop"),
        ("我不想听了", False, None),
        # play/resume/switch/改口: 语义判别交给 LLM 工具
        ("唱首歌吧", False, None),
        ("播放声动未来吧", False, None),
        ("放本地的音乐", False, None),
        ("算了，放云端的", False, None),
        ("算了，还是云端的吧", False, None),
        ("下一首", False, None),
        ("切歌", False, None),
        ("继续播放", False, None),
        ("唱吧", False, None),
        # 反例: 长词不误伤
        ("停车", True, None),
        ("别放故事", True, None),
        ("暂停视频", True, None),
    ],
)
def test_music_control_intent_stop_only(text: str, music_ctx: bool, expected: str | None) -> None:
    assert web_ui_agent._music_control_intent(text, music_ctx=music_ctx) == expected


# ---- 2026-09-03 命令记忆标记: StopResponse 命令写入 chat_ctx 轻量标记 ----


def _marker_texts(agent) -> list[str]:
    return [str(m.content[0]) for m in agent._chat_ctx.items if m.content]


def test_music_stop_writes_chat_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)

        with pytest.raises(StopResponse):
            await agent._apply_turn_filters("停止播放", False)
        await asyncio.sleep(0)

        assert _marker_texts(agent) == ["[系统] 已执行: 停止云端音乐"]

    asyncio.run(run())


def test_repeated_identical_markers_merge_with_counter() -> None:
    agent, session = _new_agent()
    agent._remember_executed("前进(100厘米)")
    agent._remember_executed("前进(100厘米)")
    agent._remember_executed("前进(100厘米)")

    assert _marker_texts(agent) == ["[系统] 已执行: 前进(100厘米) ×3"]


def test_marker_merge_breaks_after_other_message() -> None:
    agent, session = _new_agent()
    agent._remember_executed("前进(100厘米)")
    agent._chat_ctx.add_message(role="assistant", content="好的，我往前走。")
    agent._remember_executed("前进(100厘米)")

    assert _marker_texts(agent) == [
        "[系统] 已执行: 前进(100厘米)",
        "好的，我往前走。",
        "[系统] 已执行: 前进(100厘米)",
    ]


def test_describe_executed_frames_formats_move_turn_and_generic() -> None:
    frames = [
        {"type": "data.cmd", "action": "navigation.move",
         "params": {"direction": "forward", "distance_cm": 100}},
        {"type": "data.reply", "text": "x"},
        {"type": "data.cmd", "action": "motion.turn",
         "params": {"direction": "left", "angle_deg": 90}},
        {"type": "data.cmd", "action": "gesture.perform",
         "params": {"gesture": "laugh"}},
    ]

    assert web_ui_agent._describe_executed_frames(frames) == (
        "前进(100厘米)+左转(90度)+gesture.perform(gesture=laugh)"
    )


# ---- 2026-09-04 命令口语化确认: 动作进话里, 替代"好的，正在执行" ----


def test_natural_command_ack_names_single_move(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_ui_agent.random, "choice", lambda seq: seq[0])
    frames = [{"type": "data.cmd", "action": "navigation.move",
               "params": {"direction": "forward", "distance_cm": 100}}]

    assert web_ui_agent._natural_command_ack(frames) == "好嘞，我这就往前走"


def test_natural_command_ack_chains_multiple_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_ui_agent.random, "choice", lambda seq: seq[0])
    frames = [
        {"type": "data.cmd", "action": "navigation.move",
         "params": {"direction": "backward", "distance_cm": 50}},
        {"type": "data.reply", "text": "x"},
        {"type": "data.cmd", "action": "motion.turn",
         "params": {"direction": "left", "angle_deg": 90}},
    ]

    assert web_ui_agent._natural_command_ack(frames) == "好嘞，我先往后退，然后向左转"


def test_natural_command_ack_falls_back_for_unknown_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_ui_agent.random, "choice", lambda seq: seq[0])
    frames = [{"type": "data.cmd", "action": "led.set", "params": {"color": "red"}}]

    assert web_ui_agent._natural_command_ack(frames) == "好嘞，这就来"


# ---- LLM 音乐工具: play 直行(方案B 下播放/切歌/续播的唯一入口) ----


def test_play_music_tool_plays_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(last_name="声动未来-聚力同行")
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)

        # LLM 调 play_music 即直接播云端; 工具结果给 LLM 再组织口语确认。
        # (类级取 FunctionTool 不绑实例, self 仍在签名首位, 传 None 占位。)
        result = await web_ui_agent.VoiceAgent.play_music(None, None, "")

        assert "已准备播放《声动未来-聚力同行》" in result
        assert player.plays == [None]

    asyncio.run(run())


def test_switch_music_tool_remembers_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, _ = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)

        # 切歌工具成功后写命令记忆标记("刚才放的什么歌"可答)。
        result = await web_ui_agent.VoiceAgent.switch_music(agent, None, "next")

        assert "已切换到《中秋月》" in result
        assert player.switches == ["next"]
        assert _marker_texts(agent) == ["[系统] 已执行: 切换云端音乐(《中秋月》)"]

    asyncio.run(run())


# ---- 2026-09-16 切歌目标纯函数: 曲库列表序环绕 ----


def test_switch_target_wraparound() -> None:
    names = ["一念千年_国风版", "中秋月", "声动未来-聚力同行"]
    assert _switch_target(names, names[2], "next") == names[0]  # 末首下一首 → 首首
    assert _switch_target(names, names[0], "prev") == names[2]  # 首首上一首 → 末首
    assert _switch_target(names, names[0], "next") == names[1]
    assert _switch_target(names, names[1], "prev") == names[0]


def test_switch_target_missing_anchor_returns_none() -> None:
    # 无锚点(没播过) / 锚点已不在曲库 / 空库 → None, 由调用方随机起播/拒绝。
    assert _switch_target([], "任何歌", "next") is None
    assert _switch_target(["a", "b"], "", "next") is None
    assert _switch_target(["a", "b"], "已删的歌", "next") is None


# ---- 2026-09-14 停止路由: 裸停止口语(有音乐上下文才收口), 2026-09-15 起上下文=云端在播 ----


@pytest.mark.parametrize(
    "command",
    [
        "别放了",
        "不要放了",
        "先别放了",
        "不想听了",
        "暂停",
        "暂停吧",
        "停一下",
        "先暂停一下",
        "别播了",  # 裸播动词: 非对象词剔除不剔动词自身的"播", 仍须过停止门
        "我不想听了",  # 2026-09-17 主语前缀(playgate 移植)
        "我们不想听了",
        "咱们别放了",
    ],
)
def test_bare_stop_phrase_with_music_ctx_stops_cloud(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    async def run() -> None:
        # 裸停止口语无音乐词; 云端在播(music_ctx)按停止路由停云端并播报确认,
        # 不再走本地 pause 帧(通道已下线)。
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        broadcasts: list[dict] = []
        monkeypatch.setattr(web_ui_agent, "broadcast", _capture_broadcast(broadcasts))

        with pytest.raises(StopResponse):
            await agent._apply_turn_filters(command, False)
        await asyncio.sleep(0)

        assert player.stops == 1
        assert session.says == ["好的，音乐停了。"]
        assert session.say_options == [{"add_to_chat_ctx": False, "allow_interruptions": False}]
        assert broadcasts == []

    asyncio.run(run())


def test_bare_stop_phrase_without_music_ctx_stays_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        # 无音乐上下文(不在播): "别放了"保持聊天语义, 不下帧不停播放器。
        player = _FakeMusicPlayer(playing=False, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        broadcasts: list[dict] = []
        monkeypatch.setattr(web_ui_agent, "broadcast", _capture_broadcast(broadcasts))

        await agent._apply_turn_filters("别放了", False)
        await asyncio.sleep(0)

        assert broadcasts == []
        assert session.says == []
        assert player.stops == 0 and player.plays == []


def test_bare_stop_without_music_ctx_silently_interrupts_not_music_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        # 2026-09-20 自 llm-bak 移植: 裸"停"无音乐在播时不进音乐停止路由(否则会
        # 播"音乐现在没在播"), 而是落停止词分支静默打断小歌正在说的话。
        player = _FakeMusicPlayer(playing=False, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent, "listen_interrupt_blocked", lambda: False)

        with pytest.raises(StopResponse):
            await agent._apply_turn_filters("停", False)
        await asyncio.sleep(0)

        assert player.stops == 0
        assert session.says == []
        assert session.interrupts == 1

    asyncio.run(run())


@pytest.mark.parametrize("command", ["唱吧", "放吧", "听吧"])
def test_bare_play_phrase_falls_through_to_chat(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    async def run() -> None:
        # "唱吧"/"放吧"是播放不是停止: 裸停止正则必须带 别/不要/不想 前缀, 别误收口。
        # 方案B: 播放语义也不再被正则层消费, 一律落 LLM(play_music 工具)。
        player = _FakeMusicPlayer(playing=False, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        broadcasts: list[dict] = []
        monkeypatch.setattr(web_ui_agent, "broadcast", _capture_broadcast(broadcasts))

        await agent.handle_manual_text(command)
        await asyncio.sleep(0)

        assert player.plays == []
        assert session.says == []
        assert session.replies == [{"user_input": command, "input_modality": "text"}]
        assert broadcasts == []

    asyncio.run(run())


@pytest.mark.parametrize("command", ["别放故事", "暂停视频", "别放广播"])
def test_ctx_bare_stop_non_target_not_stopped(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    async def run() -> None:
        # 非音乐对象(故事/视频/广播)即使带停止词也不触发音乐停止路由。
        player = _FakeMusicPlayer(playing=False, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        broadcasts: list[dict] = []
        monkeypatch.setattr(web_ui_agent, "broadcast", _capture_broadcast(broadcasts))

        await agent._apply_turn_filters(command, False)
        await asyncio.sleep(0)

        assert broadcasts == []
        assert player.stops == 0

    asyncio.run(run())


def test_switch_phrase_without_player_falls_to_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        # music_player 未启用(None): 音乐分支全不进, 落 LLM 聊天, 不炸不播报。
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", None)
        broadcasts: list[dict] = []
        monkeypatch.setattr(web_ui_agent, "broadcast", _capture_broadcast(broadcasts))

        await agent._apply_turn_filters("下一首", False)
        await asyncio.sleep(0)

        assert broadcasts == []
        assert session.says == []

    asyncio.run(run())


# ---- 2026-09-15 本地通道下线总回归: 任何音乐话术都不得再出现 control_music 帧 ----


def _capture_broadcast(broadcasts: list[dict]):
    def fake_broadcast(message: dict) -> bool:
        broadcasts.append(message)
        return True

    return fake_broadcast


def test_no_control_music_frames_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        player = _FakeMusicPlayer(playing=True, last_name="声动未来-聚力同行")
        agent, session = _new_agent()
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        broadcasts: list[dict] = []
        monkeypatch.setattr(web_ui_agent, "broadcast", _capture_broadcast(broadcasts))

        # 方案B: 大部分话术落 LLM(play/resume/switch 工具), 停止词走过滤直行;
        # 无论哪路, 都不得出现 control_music / media.music 帧(本地通道已下线)。
        for text in (
            "放首歌吧",
            "放本地的音乐",
            "放云端的音乐",
            "唱首歌你",
            "算了，放云端的",
            "下一曲",
            "继续播放",
            "停止播放",
            "唱吧",
        ):
            try:
                await agent._apply_turn_filters(text, False)
            except StopResponse:
                pass
            await asyncio.sleep(0)

        for message in broadcasts:
            for frame in message.get("frames", []):
                assert frame.get("action") != "control_music"
                assert frame.get("capability_id") != "media.music"

    asyncio.run(run())
