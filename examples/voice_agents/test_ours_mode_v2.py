"""V2 三态交互模式单测 — 2026-10-12(XIAOGE_MODE_V2=1,设备侧三态需求)。

覆盖(状态机见 app/interaction_host.py 模块注释):
- app.interaction_host:enter/exit_listening_v2、v2_wake_on_input、
  v2_user_speech_activity(语音活动按模式重臂)、
  v2_entrypoint_init / v2_session_start(零广播零定时器)、谓词、聆听指令文案。
- app.standby_host V2 同步:enter/exit_standby 成对同步 interaction_mode
  (不变式 standby ⟺ mode==sleeping)、从 listening 进待命补 listening off 帧、
  idle 到期"纯设备会话"例外(session=0 audio>0 不停表)、listen_timeout、
  V1 回归(mode 不动 / listen_ctrl 老路径照走)。
- webpanel.bridge:listening/standby/state 帧 V2 只派生 ctrl.state(按 mode 映射
  sleeping→open/SleepingHot(2026-10-13 按键死锁修复 B:gate 放开让音频照流,
  唤醒由服务端判定)、listening/dialogue→open/ActiveAgent;2026-10-13
  V2.1 起不再附 legacy listening/standby 帧——设备固件只认三态)、V1 输出
  字节不变、纯设备客户端(session_ws 空)广播转发链。
- webpanel.server:ctrl.hello 应答 V2 带 sleeping、欢迎语 V2 抑制 / V1 恰一句、
  唤醒帧窄处理(2026-10-13 按键死锁修复 A:authoritative+awake 的真实唤醒
  事件 marshal v2_wake_on_input,镜像/VAD/非 awake 帧零动作,merged 与
  /ws/audio 双通道;V1 不处理)。
- app.setup_taps:VAD 开口与 STT interim/final 语音活动锚点接线(现网问题#2,
  V1 开口重臂 idle 原路径不变)。
- web_ui_agent:not_addressed→聆听、聆听轮注入指令不过滤链(每轮都过 LLM)、
  休眠自愈唤醒、退出语门序、手发文字唤醒/退聆听、attach 休眠重置无欢迎语、
  transcription_node 语义唤醒(首个非空 chunk)。
- app.listening_host:listen_ctrl=None 下 V1 钩子天然失效(V2 不建控制器)。

本地无 livekit/aiohttp → 相关整类 skip(与 test_ours_exit_standby 同款 stub 舞);
服务器:cd examples/voice_agents && ../../.venv/bin/python -m pytest
test_ours_mode_v2.py -q。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE / "remote_bak_0910"):
    if (_p / "app" / "config_store.py").exists():
        sys.path.insert(0, str(_p))
        break

import app.config_store as config_store  # noqa: E402

# web_ui_agent import 前的 stub 舞(与 test_ours_exit_standby 一致)。
backends = types.ModuleType("app.backends")
backends.build_llm = lambda: None
backends.build_semantic_llm = lambda: None
backends.build_tts = lambda: None
backends.build_stt = lambda: None
backends.STT_BACKENDS = {}
backends.TTS_BACKENDS = {}
backends.backend_tabs_html = lambda *args, **kwargs: ""
backends.make_stt_backend = lambda *args, **kwargs: None
backends.make_tts_backend = lambda *args, **kwargs: None
sys.modules["app.backends"] = backends
providers_config = types.ModuleType("providers.config")
providers_config.funasr_hotwords = lambda: {}
sys.modules.setdefault("providers.config", providers_config)
silero = types.ModuleType("livekit.plugins.silero")
silero.VAD = types.SimpleNamespace(load=lambda *args, **kwargs: None)
sys.modules.setdefault("livekit.plugins.silero", silero)

try:
    import web_ui_agent  # noqa: E402
except Exception:  # pragma: no cover - 本地无 livekit
    web_ui_agent = None

try:
    import app.standby_host as standby_host  # noqa: E402
except Exception:  # pragma: no cover - 本地缺 aiohttp/webpanel 链
    standby_host = None

try:
    from app import interaction_host  # noqa: E402
except Exception:  # pragma: no cover - 本地缺 aiohttp/webpanel 链
    interaction_host = None

try:
    import app.listening_host as listening_host  # noqa: E402
except Exception:  # pragma: no cover - 本地无 livekit 链
    listening_host = None

try:
    import webpanel.bridge as bridge  # noqa: E402
except Exception:  # pragma: no cover - 本地缺 aiohttp
    bridge = None

try:
    import webpanel.server as wp_server  # noqa: E402
except Exception:  # pragma: no cover - 本地缺 aiohttp/webpanel 链
    wp_server = None

try:
    import app.setup_taps as setup_taps  # noqa: E402
except Exception:  # pragma: no cover - 本地缺 kws_interrupt 链
    setup_taps = None

_dep_web = web_ui_agent is not None and interaction_host is not None and standby_host is not None
_dep_ih = interaction_host is not None and standby_host is not None
pytestmark_web = pytest.mark.skipif(not _dep_web, reason="no livekit locally")
pytestmark_ih = pytest.mark.skipif(not _dep_ih, reason="interaction host deps missing locally")
pytestmark_bridge = pytest.mark.skipif(
    bridge is None or interaction_host is None, reason="bridge deps missing locally"
)
pytestmark_wps = pytest.mark.skipif(
    wp_server is None or interaction_host is None, reason="webpanel deps missing locally"
)
pytestmark_st = pytest.mark.skipif(
    setup_taps is None or interaction_host is None, reason="setup_taps deps missing locally"
)
pytestmark_lh = pytest.mark.skipif(listening_host is None, reason="listening host deps missing")


class _Ws:
    def __init__(self):
        self.sent = []

    async def send_str(self, s):
        self.sent.append(json.loads(s))


class _FakeLoop:
    """call_later 录调用(返回可 cancel 的假 handle),不真跑回调。"""

    def __init__(self):
        self.calls = []
        self.cancelled = []

    def call_later(self, delay, cb):
        self.calls.append((delay, cb))
        idx = len(self.calls)

        def _cancel():
            self.cancelled.append(idx)

        return types.SimpleNamespace(cancel=_cancel)

    def is_running(self):
        return True


def _listen_ctrl(rec, *, active=True):
    return types.SimpleNamespace(
        active=active,
        force_exit=lambda: rec["force_exits"].append(1),
    )


async def _chunks(*items):
    for it in items:
        yield it


class _TurnCtx:
    """add_message 录制桩(on_user_turn_completed 聆听分支的指令注入点)。"""

    def __init__(self):
        self.added = []

    def add_message(self, *, role, content):
        self.added.append((role, content))


def _msg(text):
    return types.SimpleNamespace(text_content=text, content=[text])


def _noop_filters(agent, counts):
    async def _f(original, spoke, finalize_user_message=True):
        counts["filters"] += 1
        return None

    agent._apply_turn_filters = _f


# ── app.interaction_host:三态状态机 ─────────────────────────────────────


@pytestmark_ih
class TestInteractionHost:
    @pytest.fixture
    def ih_env(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        """V2 host 环境:开关钉死开 + broadcast/埋点/standby 协作者全录制。"""
        from app.session_state import runtime

        rec = {"v2_frames": [], "v2_logs": [], "timer_ops": [], "standby_exits": []}
        monkeypatch.setattr(interaction_host, "_MODE_V2", True)
        monkeypatch.setattr(interaction_host, "broadcast",
                            lambda msg: rec["v2_frames"].append(msg))
        monkeypatch.setattr(interaction_host, "append_turn_log",
                            lambda line: rec["v2_logs"].append(line))
        monkeypatch.setattr(standby_host, "exit_standby",
                            lambda via: rec["standby_exits"].append(via))
        for op, fn in (("cancel_idle", "exit_cancel_idle_timer"),
                       ("arm_listen", "exit_arm_listen_timer"),
                       ("cancel_listen", "exit_cancel_listen_timer"),
                       ("arm_idle", "exit_arm_idle_timer"),
                       ("reset_idle", "exit_reset_idle_timer")):
            monkeypatch.setattr(
                standby_host, fn, (lambda o: lambda: rec["timer_ops"].append(o))(op))
        monkeypatch.setattr(runtime, "interaction_mode", "dialogue")
        monkeypatch.setattr(runtime, "standby", False)
        monkeypatch.setattr(runtime, "agent_output_in_flight", False)
        rec["runtime"] = runtime
        return rec

    def test_entrypoint_init_sleeps(self, ih_env):
        """进程起点(含池化预热)直接休眠:零广播、零定时器(agent_loop 无关)。"""
        rt = ih_env["runtime"]
        rt.interaction_mode = "dialogue"
        interaction_host.v2_entrypoint_init()
        assert rt.standby is True and rt.interaction_mode == "sleeping"
        assert rt.agent_output_in_flight is False
        assert ih_env["v2_frames"] == [] and ih_env["timer_ops"] == []
        assert any("V2_ENTRYPOINT_INIT mode=sleeping" in l for l in ih_env["v2_logs"])

    def test_entrypoint_init_noop_v1(self, ih_env, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(interaction_host, "_MODE_V2", False)
        rt = ih_env["runtime"]
        interaction_host.v2_entrypoint_init()
        assert rt.standby is False and rt.interaction_mode == "dialogue"
        assert ih_env["v2_logs"] == []

    def test_session_start_sleeps_no_broadcast(self, ih_env):
        """真会话 attach:重置休眠 + 撤两路定时器;零广播(hello 应答帧已带
        sleeping,避免 web 线程先于 marshal 读到上一会话遗留 mode 的竞窗)。"""
        rt = ih_env["runtime"]
        rt.interaction_mode = "dialogue"
        rt.standby = False
        interaction_host.v2_session_start()
        assert rt.standby is True and rt.interaction_mode == "sleeping"
        assert ih_env["v2_frames"] == []
        assert ih_env["timer_ops"] == ["cancel_idle", "cancel_listen"]
        assert any("V2_SESSION_START mode=sleeping" in l for l in ih_env["v2_logs"])

    def test_wake_from_sleeping(self, ih_env):
        rt = ih_env["runtime"]
        rt.interaction_mode = "sleeping"
        rt.standby = True
        interaction_host.v2_wake_on_input("voice_turn")
        assert ih_env["standby_exits"] == ["input_voice_turn"]
        assert any("V2_WAKE_ON_INPUT via voice_turn" in l for l in ih_env["v2_logs"])

    def test_wake_noop_in_dialogue(self, ih_env):
        rt = ih_env["runtime"]
        rt.interaction_mode = "dialogue"
        interaction_host.v2_wake_on_input("voice_turn")
        assert ih_env["standby_exits"] == [] and ih_env["v2_logs"] == []

    def test_wake_noop_v1(self, ih_env, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(interaction_host, "_MODE_V2", False)
        rt = ih_env["runtime"]
        rt.interaction_mode = "sleeping"
        interaction_host.v2_wake_on_input("voice_turn")
        assert ih_env["standby_exits"] == []

    def test_enter_listening(self, ih_env):
        rt = ih_env["runtime"]
        rt.interaction_mode = "dialogue"
        interaction_host.enter_listening_v2("not_addressed")
        assert rt.interaction_mode == "listening"
        assert ih_env["v2_frames"] == [{"type": "listening", "on": True}]
        assert ih_env["timer_ops"] == ["cancel_idle", "arm_listen"]
        assert any("V2_LISTEN_ENTER via not_addressed" in l for l in ih_env["v2_logs"])

    def test_enter_listening_idempotent(self, ih_env):
        rt = ih_env["runtime"]
        rt.interaction_mode = "listening"
        interaction_host.enter_listening_v2("not_addressed")
        assert ih_env["v2_frames"] == [] and ih_env["timer_ops"] == []

    def test_enter_listening_noop_v1(self, ih_env, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(interaction_host, "_MODE_V2", False)
        rt = ih_env["runtime"]
        interaction_host.enter_listening_v2("not_addressed")
        assert rt.interaction_mode == "dialogue" and ih_env["v2_frames"] == []

    def test_exit_listening(self, ih_env):
        rt = ih_env["runtime"]
        rt.interaction_mode = "listening"
        interaction_host.exit_listening_v2("semantic_wake")
        assert rt.interaction_mode == "dialogue"
        assert ih_env["v2_frames"] == [{"type": "listening", "on": False}]
        assert ih_env["timer_ops"] == ["cancel_listen", "arm_idle"]
        assert any("V2_LISTEN_EXIT via semantic_wake" in l for l in ih_env["v2_logs"])

    def test_exit_listening_noop_in_dialogue(self, ih_env):
        rt = ih_env["runtime"]
        rt.interaction_mode = "dialogue"
        interaction_host.exit_listening_v2("manual_text")
        assert ih_env["v2_frames"] == [] and ih_env["timer_ops"] == []

    def test_predicates(self, ih_env, monkeypatch: pytest.MonkeyPatch):
        rt = ih_env["runtime"]
        rt.interaction_mode = "sleeping"
        assert interaction_host.sleeping_v2_active() is True
        assert interaction_host.listening_v2_active() is False
        rt.interaction_mode = "listening"
        assert interaction_host.listening_v2_active() is True
        assert interaction_host.sleeping_v2_active() is False
        monkeypatch.setattr(interaction_host, "_MODE_V2", False)
        assert interaction_host.sleeping_v2_active() is False
        assert interaction_host.listening_v2_active() is False

    def test_listening_instruction_content(self):
        """聆听指令:果断判定标准 + not_addressed 出口(语义唤醒的另一半)。"""
        assert "倾向正常回答" in interaction_host.V2_LISTENING_INSTRUCTION
        assert "not_addressed" in interaction_host.V2_LISTENING_INSTRUCTION

    def test_speech_activity_dialogue_resets_idle(self, ih_env):
        """对话态语音活动:重臂 idle(2026-10-12 现网问题#2 的核心锚点)。"""
        ih_env["runtime"].interaction_mode = "dialogue"
        interaction_host.v2_user_speech_activity()
        assert ih_env["timer_ops"] == ["reset_idle"]

    def test_speech_activity_listening_arms_listen(self, ih_env):
        """聆听态语音活动:重臂聆听持续表(EXIT_LISTEN_S 语义)。"""
        ih_env["runtime"].interaction_mode = "listening"
        interaction_host.v2_user_speech_activity()
        assert ih_env["timer_ops"] == ["arm_listen"]

    def test_speech_activity_sleeping_noop(self, ih_env):
        """休眠态无"无输入"定时器:语音活动 no-op(唤醒由 v2_wake_on_input 管)。"""
        ih_env["runtime"].interaction_mode = "sleeping"
        interaction_host.v2_user_speech_activity()
        assert ih_env["timer_ops"] == []

    def test_speech_activity_noop_v1(self, ih_env, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(interaction_host, "_MODE_V2", False)
        ih_env["runtime"].interaction_mode = "dialogue"
        interaction_host.v2_user_speech_activity()
        assert ih_env["timer_ops"] == []


# ── app.standby_host:V2 同步不变式 ──────────────────────────────────────


@pytestmark_ih
class TestStandbyHostV2:
    @pytest.fixture
    def v2_std(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        """standby_host V2 环境:V2 开 + runtime/客户端计数全隔离。"""
        from app.session_state import runtime

        rec = {"frames": [], "logs": [], "aftermath": [], "force_exits": [],
               "session_clients": 1, "audio_clients": 1}
        monkeypatch.setattr(standby_host, "broadcast",
                            lambda msg: rec["frames"].append(msg))
        monkeypatch.setattr(standby_host, "append_turn_log",
                            lambda line: rec["logs"].append(line))
        monkeypatch.setattr(standby_host.listening_host, "listen_exit_aftermath",
                            lambda via, ask=True: rec["aftermath"].append((via, ask)))
        monkeypatch.setattr(standby_host, "EXIT_ENABLE", True)
        monkeypatch.setattr(standby_host, "session_client_count",
                            lambda: rec["session_clients"])
        monkeypatch.setattr(standby_host, "audio_client_count",
                            lambda: rec["audio_clients"])
        monkeypatch.setattr(interaction_host, "_MODE_V2", True)
        monkeypatch.setattr(runtime, "interaction_mode", "dialogue")
        monkeypatch.setattr(runtime, "standby", False)
        monkeypatch.setattr(runtime, "listen_ctrl", None)
        monkeypatch.setattr(runtime, "exit_idle_handle", None)
        monkeypatch.setattr(runtime, "exit_listen_handle", None)
        monkeypatch.setattr(runtime, "agent_loop", None)
        monkeypatch.setattr(runtime, "duplex_enabled", True)
        monkeypatch.setattr(runtime, "ws_audio_output", None)
        monkeypatch.setattr(runtime, "agent_output_in_flight", False)
        rec["runtime"] = runtime
        return rec

    def test_enter_syncs_sleeping(self, v2_std):
        rt = v2_std["runtime"]
        standby_host.enter_standby("idle")
        assert rt.standby is True and rt.interaction_mode == "sleeping"
        assert v2_std["frames"] == [{"type": "standby", "on": True, "reason": "idle"}]

    def test_enter_from_listening_adds_listening_off(self, v2_std):
        """从聆听进休眠:先置 mode 再广播——listening off 在 standby on 之前
        (桥接层按 mode 派生 ctrl.state,不能让衍生帧带旧态)。"""
        rt = v2_std["runtime"]
        rt.interaction_mode = "listening"
        standby_host.enter_standby("listen_timeout")
        assert rt.interaction_mode == "sleeping" and rt.standby is True
        assert [f["type"] for f in v2_std["frames"]] == ["listening", "standby"]
        assert v2_std["frames"][0] == {"type": "listening", "on": False}
        assert any("V2_LISTEN_EXIT via standby" in l for l in v2_std["logs"])

    def test_exit_syncs_dialogue(self, v2_std):
        rt = v2_std["runtime"]
        rt.standby = True
        rt.interaction_mode = "sleeping"
        standby_host.exit_standby("frontend_state")
        assert rt.standby is False and rt.interaction_mode == "dialogue"
        assert v2_std["frames"] == [{"type": "standby", "on": False,
                                     "reason": "frontend_state"}]

    def test_invariant_roundtrip(self, v2_std):
        """不变式 standby ⟺ mode==sleeping:enter/exit 两个来回每步成立。"""
        rt = v2_std["runtime"]
        for _ in range(2):
            standby_host.enter_standby("idle")
            assert rt.standby is True and rt.interaction_mode == "sleeping"
            standby_host.exit_standby("frontend_state")
            assert rt.standby is False and rt.interaction_mode == "dialogue"

    def test_idle_expired_pure_device_session_sleeps(self, v2_std):
        """V2 例外:设备只连 /ws/audio(session=0 audio>0)是真会话,idle 到期
        照常进休眠而非停表(2026-09-15 的停表修复只该拦池化待命)。"""
        rt = v2_std["runtime"]
        v2_std["session_clients"] = 0
        v2_std["audio_clients"] = 1
        rt.interaction_mode = "listening"  # 从聆听到期休眠:带 listening off
        loop = _FakeLoop()
        rt.agent_loop = loop
        standby_host._on_idle_expired()
        assert rt.standby is True and rt.interaction_mode == "sleeping"
        assert [f["type"] for f in v2_std["frames"]] == ["listening", "standby"]
        assert loop.calls == []  # 进休眠后不再顺延重臂

    def test_idle_expired_no_clients_still_stops(self, v2_std):
        """池化无会话(session=0 audio=0)仍彻底停表(原 2026-09-15 修复保持)。"""
        rt = v2_std["runtime"]
        v2_std["session_clients"] = 0
        v2_std["audio_clients"] = 0
        loop = _FakeLoop()
        rt.agent_loop = loop
        standby_host._on_idle_expired()
        assert rt.standby is False
        assert v2_std["frames"] == [] and loop.calls == []

    def test_listen_expired_sleeps_in_v2(self, v2_std):
        """聆听持续 EXIT_LISTEN_S 无语音 → 休眠(V2 无 ListeningController,
        走不了 V1 的 c.active 早退,先分流;listen_ctrl=None → 无 aftermath)。"""
        rt = v2_std["runtime"]
        rt.interaction_mode = "listening"
        standby_host._on_listen_expired()
        assert rt.standby is True and rt.interaction_mode == "sleeping"
        assert [f["type"] for f in v2_std["frames"]] == ["listening", "standby"]
        assert v2_std["aftermath"] == []
        assert any("STANDBY_ENTER via listen_timeout" in l for l in v2_std["logs"])

    def test_listen_expired_dialogue_noop_in_v2(self, v2_std):
        rt = v2_std["runtime"]
        rt.interaction_mode = "dialogue"
        standby_host._on_listen_expired()
        assert rt.standby is False and rt.interaction_mode == "dialogue"
        assert v2_std["frames"] == []

    def test_v1_enter_leaves_mode_untouched(self, v2_std, monkeypatch: pytest.MonkeyPatch):
        """V1 回归:开关关时 enter_standby 不碰 interaction_mode、不加帧。"""
        monkeypatch.setattr(interaction_host, "_MODE_V2", False)
        rt = v2_std["runtime"]
        rt.interaction_mode = "dialogue"
        standby_host.enter_standby("idle")
        assert rt.interaction_mode == "dialogue"
        assert [f["type"] for f in v2_std["frames"]] == ["standby"]

    def test_v1_listen_expired_uses_listen_ctrl_path(self, v2_std,
                                                     monkeypatch: pytest.MonkeyPatch):
        """V1 回归:聆听超时照走 ListeningController(force_exit + aftermath)。"""
        monkeypatch.setattr(interaction_host, "_MODE_V2", False)
        rt = v2_std["runtime"]
        rt.listen_ctrl = _listen_ctrl(v2_std)
        enters = []
        monkeypatch.setattr(standby_host, "enter_standby", lambda via: enters.append(via))
        standby_host._on_listen_expired()
        assert v2_std["force_exits"] == [1]
        assert v2_std["aftermath"] == [("standby", False)]
        assert enters == ["listen_timeout"]


# ── webpanel.bridge:V2 ctrl.state 派生与设备转发 ────────────────────────


@pytestmark_bridge
class TestBridgeV2:
    def _frames(self, monkeypatch: pytest.MonkeyPatch, mode: str, msg: dict, *,
                v2: bool = True) -> list:
        from app.session_state import runtime

        monkeypatch.setattr(interaction_host, "_MODE_V2", v2)
        monkeypatch.setattr(runtime, "interaction_mode", mode)
        return bridge._to_session_frames(msg)

    @pytest.mark.parametrize("mode,gate,resource", [
        # sleeping gate=open:2026-10-13 按键死锁修复 B(固件本地开门 C 落地
        # 后可回 kws_only);resource 仍 SleepingHot,mode 仍 sleeping。
        ("sleeping", "open", "SleepingHot"),
        ("listening", "open", "ActiveAgent"),
        ("dialogue", "open", "ActiveAgent"),
        ("bogus", "open", "ActiveAgent"),  # 未知值兜底回对话
    ])
    def test_state_frame_v2_derivation(self, monkeypatch: pytest.MonkeyPatch,
                                       mode, gate, resource):
        frames = self._frames(monkeypatch, mode, {"type": "state"})
        assert len(frames) == 1
        state = frames[0]
        assert state["type"] == "ctrl.state"
        assert state["interaction_mode"] == ("dialogue" if mode == "bogus" else mode)
        assert state["engine_gate"] == gate
        assert state["resource_state"] == resource
        assert state["link_state"] == "connected"

    def test_listening_frame_v2_state_only(self, monkeypatch: pytest.MonkeyPatch):
        """V2.1(2026-10-13):设备固件不再解析 legacy listening 帧——只发
        按当前 mode 派生的 ctrl.state,on/hint 不再上设备线。"""
        frames = self._frames(monkeypatch, "listening", {"type": "listening", "on": True})
        assert [f["type"] for f in frames] == ["ctrl.state"]
        assert frames[0]["interaction_mode"] == "listening"
        assert frames[0]["engine_gate"] == "open"
        assert "on" not in frames[0] and "hint" not in frames[0]

    def test_listening_off_while_sleeping(self, monkeypatch: pytest.MonkeyPatch):
        """enter_standby 从聆听进休眠的序列:mode 先置 sleeping 再广播——
        广播派生的 ctrl.state 必带目标态(V2.1 起唯一一帧)。"""
        frames = self._frames(monkeypatch, "sleeping", {"type": "listening", "on": False})
        assert [f["type"] for f in frames] == ["ctrl.state"]
        assert frames[0]["interaction_mode"] == "sleeping"
        assert frames[0]["engine_gate"] == "open"
        assert frames[0]["resource_state"] == "SleepingHot"

    def test_standby_frame_v2_state_only(self, monkeypatch: pytest.MonkeyPatch):
        """V2.1(2026-10-13):standby 帧不再上设备线,休眠全走 ctrl.state;
        reason 只留埋点日志(STANDBY_ENTER via=...)。"""
        frames = self._frames(monkeypatch, "sleeping",
                              {"type": "standby", "on": True, "reason": "idle"})
        assert [f["type"] for f in frames] == ["ctrl.state"]
        assert frames[0]["interaction_mode"] == "sleeping"
        assert "on" not in frames[0] and "reason" not in frames[0]

    def test_v1_output_unchanged(self, monkeypatch: pytest.MonkeyPatch):
        """V1 回归:listening/standby 帧原样透传(不派生 ctrl.state);
        state 帧字面量与 mode 无关。"""
        from app.session_state import runtime

        frames = self._frames(monkeypatch, "sleeping", {"type": "listening", "on": True},
                              v2=False)
        assert [f["type"] for f in frames] == ["listening"]
        assert frames[0]["on"] is True and frames[0]["hint"] == ""
        frames = self._frames(monkeypatch, "sleeping",
                              {"type": "standby", "on": True, "reason": "idle"}, v2=False)
        assert frames == [{
            "type": "standby",
            "trace_id": frames[0]["trace_id"],
            "session_id": frames[0]["session_id"],
            "on": True,
            "reason": "idle",
            "ts_ms": frames[0]["ts_ms"],
        }]
        frames = self._frames(monkeypatch, "sleeping", {"type": "state"}, v2=False)
        assert frames == [{
            "type": "ctrl.state",
            "trace_id": frames[0]["trace_id"],
            "session_id": frames[0]["session_id"],
            "link_state": "connected",
            "interaction_mode": "dialogue",
            "engine_gate": "open",
            "resource_state": "ActiveAgent",
            "ts_ms": frames[0]["ts_ms"],
        }]

    def test_v2_mode_on_follows_flag(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(interaction_host, "_MODE_V2", True)
        assert bridge._v2_mode_on() is True
        monkeypatch.setattr(interaction_host, "_MODE_V2", False)
        assert bridge._v2_mode_on() is False

    def test_broadcast_forwards_state_to_pure_device_clients(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """纯设备会话(session_ws 空、只有 /ws/audio):standby 广播把派生
        ctrl.state 落到设备连接——真实 web 循环上跑通整条转发链;V2.1 起
        设备线只有这一帧(legacy standby 帧不再发)。"""
        from app.session_state import runtime

        monkeypatch.setattr(interaction_host, "_MODE_V2", True)
        monkeypatch.setattr(runtime, "interaction_mode", "sleeping")
        monkeypatch.setattr(bridge.panel, "session_ws_clients", set())
        monkeypatch.setattr(bridge.panel, "ws_clients", set())
        ws = _Ws()
        monkeypatch.setattr(bridge.panel, "audio_ws_clients", {ws})

        async def scenario():
            bridge.panel.web_loop = asyncio.get_running_loop()
            try:
                bridge.broadcast({"type": "standby", "on": True, "reason": "idle"})
                await asyncio.sleep(0.05)  # 让 run_coroutine_threadsafe 的协程跑完
            finally:
                bridge.panel.web_loop = None

        asyncio.run(scenario())
        assert [f["type"] for f in ws.sent] == ["ctrl.state"]
        assert ws.sent[0]["interaction_mode"] == "sleeping"
        assert ws.sent[0]["engine_gate"] == "open"
        assert ws.sent[0]["resource_state"] == "SleepingHot"


# ── webpanel.server:hello 应答与欢迎语门 ────────────────────────────────


@pytestmark_wps
class TestServerV2:
    def _hello_env(self, monkeypatch: pytest.MonkeyPatch, *, v2: bool) -> None:
        monkeypatch.setattr(interaction_host, "_MODE_V2", v2)
        monkeypatch.setattr(wp_server, "append_turn_log", lambda line: None)

    def test_hello_state_v2_sleeping(self, monkeypatch: pytest.MonkeyPatch):
        """V2 会话起点应答:ctrl.ready + ctrl.state(sleeping, open,
        SleepingHot;gate 2026-10-13 起放开,按键死锁修复 B)——硬编码不读
        runtime(应答帧先于 attach marshal 到达)。"""
        self._hello_env(monkeypatch, v2=True)
        ws = _Ws()
        session_info = {"trace_id": "t1", "session_id": "s1",
                        "granted_caps": ["state"]}
        asyncio.run(wp_server._send_ctrl_ready_and_state(
            ws, {"trace_id": "t1", "session_id": "s1", "caps": ["state"]},
            session_info))
        assert [f["type"] for f in ws.sent] == ["ctrl.ready", "ctrl.state"]
        state = ws.sent[1]
        assert (state["interaction_mode"], state["engine_gate"],
                state["resource_state"]) == ("sleeping", "open", "SleepingHot")
        assert state["link_state"] == "connected"

    def test_hello_state_v1_dialogue(self, monkeypatch: pytest.MonkeyPatch):
        self._hello_env(monkeypatch, v2=False)
        ws = _Ws()
        asyncio.run(wp_server._send_ctrl_ready_and_state(
            ws, {"trace_id": "t1", "session_id": "s1", "caps": ["state"]},
            {"trace_id": "t1", "session_id": "s1", "granted_caps": ["state"]}))
        state = ws.sent[1]
        assert (state["interaction_mode"], state["engine_gate"],
                state["resource_state"]) == ("dialogue", "open", "ActiveAgent")

    def test_hello_caps_not_granted(self, monkeypatch: pytest.MonkeyPatch):
        """caps 未授权被滤掉:只回 ctrl.ready,不发 state(授权白名单照旧)。"""
        self._hello_env(monkeypatch, v2=True)
        ws = _Ws()
        asyncio.run(wp_server._send_ctrl_ready_and_state(
            ws, {"trace_id": "t1", "session_id": "s1", "caps": ["state"]},
            {"trace_id": "t1", "session_id": "s1", "granted_caps": []}))
        assert [f["type"] for f in ws.sent] == ["ctrl.ready"]
        assert ws.sent[0]["granted_caps"] == []

    def test_frontend_state_welcome_never_fires_both_modes(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """唤醒帧窄处理(2026-10-13)后也不触发欢迎语:V2 唤醒走
        v2_wake_on_input(无欢迎语设计),V1 对该帧零动作——旧版 V1 靠
        _xg_voice_welcome_sent 连接级去重只发一次,现在零次、连标记都不留。"""
        for v2 in (True, False):
            self._hello_env(monkeypatch, v2=v2)
            monkeypatch.setattr(wp_server.runtime, "standby", True)
            welcomes = []
            monkeypatch.setattr(wp_server, "_schedule_voice_welcome",
                                lambda tag="voice welcome": welcomes.append(tag))
            ws = _Ws()

            async def scenario():
                for _ in range(2):  # 连发两帧:旧版第二帧靠去重不再发
                    await wp_server._process_session_payload(
                        ws, {"type": "ctrl.frontend_state", "wake_state": "awake",
                             "trust_level": "authoritative", "wake_event": "button"},
                        {"trace_id": "t1"}, "s1")

            asyncio.run(scenario())
            assert welcomes == [], f"v2={v2}"
            assert not hasattr(ws, "_xg_voice_welcome_sent"), f"v2={v2}"

    def _wake_env(self, monkeypatch: pytest.MonkeyPatch, *, v2: bool) -> dict:
        """唤醒帧环境:休眠态 + 可跑假循环 + 录制 exit_standby/日志。

        marshalled 记 (回调, args) 元组——窄处理(2026-10-13 按键死锁修复 A)
        恢复后,真实唤醒帧 marshal interaction_host.v2_wake_on_input(
        "frontend_<event>") 回 agent 循环。interaction_host.append_turn_log
        一并 patch:测试会真调 v2_wake_on_input,防工件写进生产 turn log
        (exit_standby 从 standby_host 侧录制——server 不导入它)。"""
        self._hello_env(monkeypatch, v2=v2)
        logs, exits, marshalled = [], [], []
        monkeypatch.setattr(wp_server, "append_turn_log", lambda line: logs.append(line))
        monkeypatch.setattr(interaction_host, "append_turn_log",
                            lambda line: logs.append(line))
        from app import standby_host
        monkeypatch.setattr(standby_host, "exit_standby", lambda via: exits.append(via))

        def _marshal(cb, *args, **kwargs):
            marshalled.append((cb, args))

        monkeypatch.setattr(wp_server.runtime, "standby", True)
        monkeypatch.setattr(wp_server.runtime, "interaction_mode", "sleeping")
        monkeypatch.setattr(wp_server.runtime, "agent_loop",
                            types.SimpleNamespace(is_running=lambda: True,
                                                  call_soon_threadsafe=_marshal))
        return {"logs": logs, "exits": exits, "marshalled": marshalled}

    _WAKE_FRAME = {"type": "ctrl.frontend_state", "wake_state": "awake",
                   "trust_level": "authoritative", "wake_event": "button"}

    # 窄处理必须滤掉的噪声(2026-10-12 现网问题#1 的误唤醒源):
    _NOISE_FRAMES = [
        {"type": "ctrl.frontend_state", "wake_state": "awake",
         "trust_level": "observe", "wake_event": "button"},  # 周期/连接镜像
        {"type": "ctrl.frontend_state", "wake_state": "awake",
         "trust_level": "authoritative", "wake_event": "vad"},  # VAD 提示
        {"type": "ctrl.frontend_state", "wake_state": "asleep",
         "trust_level": "authoritative", "wake_event": "button"},  # 非 awake
    ]

    def test_frontend_state_wake_narrow_in_merged(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """窄处理(2026-10-13 按键死锁修复 A):authoritative+awake 真实唤醒
        事件(button/local_kws/gui)marshal v2_wake_on_input 回 agent 循环,
        回调端到端跑通 exit_standby;镜像/VAD/非 awake 帧零动作零日志。"""
        rec = self._wake_env(monkeypatch, v2=True)

        async def scenario():
            for event in ("button", "local_kws", "gui"):
                await wp_server._process_session_payload(
                    _Ws(), {**self._WAKE_FRAME, "wake_event": event},
                    {"trace_id": "t1"}, "s1")

        asyncio.run(scenario())
        assert [m[1] for m in rec["marshalled"]] == [
            ("frontend_button",), ("frontend_local_kws",), ("frontend_gui",)]
        assert all(m[0] is interaction_host.v2_wake_on_input
                   for m in rec["marshalled"])
        assert [l for l in rec["logs"] if "V2_FRONTEND_WAKE_ACCEPTED" in l] == [
            "V2_FRONTEND_WAKE_ACCEPTED wake_event=button src=merged",
            "V2_FRONTEND_WAKE_ACCEPTED wake_event=local_kws src=merged",
            "V2_FRONTEND_WAKE_ACCEPTED wake_event=gui src=merged",
        ]
        # 回调端到端:marshal 的确是 v2_wake_on_input,休眠态下走到 exit_standby
        cb, args = rec["marshalled"][0]
        cb(*args)
        assert rec["exits"] == ["input_frontend_button"]
        assert any("V2_WAKE_ON_INPUT via frontend_button" in l for l in rec["logs"])

        async def noise():
            for frame in self._NOISE_FRAMES:
                await wp_server._process_session_payload(
                    _Ws(), frame, {"trace_id": "t1"}, "s1")

        asyncio.run(noise())
        assert len(rec["marshalled"]) == 3 and len(rec["exits"]) == 1
        assert len([l for l in rec["logs"] if "V2_FRONTEND_WAKE_ACCEPTED" in l]) == 3

    def test_frontend_state_wake_inert_in_v1(self, monkeypatch: pytest.MonkeyPatch):
        """V1 不处理唤醒帧(2026-10-13 退役语义保留;V1 按键唤醒回滚走
        .bak_20261012_v2mode 文件集):零 marshal 零 exit,只剩 R522_WS_IN
        入口日志。"""
        rec = self._wake_env(monkeypatch, v2=False)

        async def scenario():
            await wp_server._process_session_payload(
                _Ws(), dict(self._WAKE_FRAME), {"trace_id": "t1"}, "s1")

        asyncio.run(scenario())
        assert rec["marshalled"] == [] and rec["exits"] == []
        assert not any("V2_FRONTEND_WAKE_ACCEPTED" in l for l in rec["logs"])
        assert any("R522_WS_IN type=ctrl.frontend_state" in l for l in rec["logs"])

    def test_frontend_state_wake_on_audio_path(self, monkeypatch: pytest.MonkeyPatch):
        """/ws/audio 同款窄处理:authoritative 帧照样唤醒(src=audio 标注通道);
        镜像/VAD/非 awake 帧仍落 AUDIO_WS_TEXT_DROP(不 4400 断音频生命线)。"""
        rec = self._wake_env(monkeypatch, v2=True)

        async def scenario():
            await wp_server._process_audio_text_frame(
                _Ws(), json.dumps(self._WAKE_FRAME), "s1")
            for frame in self._NOISE_FRAMES:
                await wp_server._process_audio_text_frame(
                    _Ws(), json.dumps(frame), "s1")

        asyncio.run(scenario())
        assert [m[1] for m in rec["marshalled"]] == [("frontend_button",)]
        assert any("V2_FRONTEND_WAKE_ACCEPTED wake_event=button src=audio" in l
                   for l in rec["logs"])
        drops = [l for l in rec["logs"] if "AUDIO_WS_TEXT_DROP" in l]
        assert len(drops) == 3 and all("type=ctrl.frontend_state" in l for l in drops)
        assert rec["exits"] == []


# ── web_ui_agent:轮次门序与语义唤醒 ────────────────────────────────────


def _turn_agent(cfg=None):
    """SimpleNamespace 绑定桩(与 test_ours_exit_standby 同款)。"""
    agent = types.SimpleNamespace(_agent_cfg=cfg or config_store.DEFAULT_CONFIG)
    agent.finalize_calls = []
    agent._wiring = types.SimpleNamespace(
        live=types.SimpleNamespace(finish_turn=lambda reason, utterance_id=None: "uid-t"))
    real_finalize = web_ui_agent.VoiceAgent._finalize_g3_user_message.__get__(agent)

    def finalize(original, enabled, utterance_id=None, reason="consumed_voice_turn"):
        agent.finalize_calls.append((original, enabled, reason))
        return real_finalize(original, enabled, utterance_id, reason)

    agent._finalize_g3_user_message = finalize

    async def noop_apply() -> None:
        return None

    agent._maybe_apply_agent_config = noop_apply
    agent._duplex_off = web_ui_agent.VoiceAgent._duplex_off.__get__(agent)
    agent._voice_welcome_said = False
    agent._voice_welcome_when_ready = (
        web_ui_agent.VoiceAgent._voice_welcome_when_ready.__get__(agent))
    agent.on_user_turn_completed = (
        web_ui_agent.VoiceAgent.on_user_turn_completed.__get__(agent))
    return agent


@pytestmark_web
class TestAgentV2:
    @pytest.fixture
    def web_env(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        logs = []
        monkeypatch.setattr(web_ui_agent, "_append_turn_log", lambda line: logs.append(line))
        return {"logs": logs}

    @pytest.fixture
    def v2_gates(self, web_env: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
        """on_user_turn_completed/handle_manual_text 的 V2 门环境:
        web_ui_agent 侧符号替换 + interaction_host/standby_host 协作者录制。"""
        from app.session_state import runtime

        rec = {
            "logs": web_env["logs"], "resets": 0, "enters": [], "broadcasts": [],
            "v2_frames": [], "v2_logs": [], "timer_ops": [], "standby_exits": [],
            "arms_listen": 0, "marks": 0, "welcomes": [], "generated": [],
        }
        monkeypatch.setattr(web_ui_agent, "EXIT_ENABLE", True)
        monkeypatch.setattr(web_ui_agent, "exit_reset_idle_timer",
                            lambda: rec.__setitem__("resets", rec["resets"] + 1))
        monkeypatch.setattr(web_ui_agent, "enter_standby",
                            lambda via: rec["enters"].append(via))
        monkeypatch.setattr(web_ui_agent, "broadcast",
                            lambda msg: rec["broadcasts"].append(msg))
        monkeypatch.setattr(web_ui_agent, "exit_arm_listen_timer",
                            lambda: rec.__setitem__("arms_listen", rec["arms_listen"] + 1))
        monkeypatch.setattr(web_ui_agent, "mark_agent_output_started",
                            lambda: rec.__setitem__("marks", rec["marks"] + 1))
        monkeypatch.setattr(web_ui_agent, "say_voice_welcome",
                            lambda voice=None: rec["welcomes"].append(voice))
        monkeypatch.setattr(web_ui_agent.runtime, "overlap_turn_state",
                            {"user_spoke_over_agent": False})
        # V2 开关 + interaction_host 广播/埋点 + standby_host 定时器/唤醒录制
        monkeypatch.setattr(interaction_host, "_MODE_V2", True)
        monkeypatch.setattr(interaction_host, "broadcast",
                            lambda msg: rec["v2_frames"].append(msg))
        monkeypatch.setattr(interaction_host, "append_turn_log",
                            lambda line: rec["v2_logs"].append(line))
        monkeypatch.setattr(standby_host, "exit_standby",
                            lambda via: rec["standby_exits"].append(via))
        for op, fn in (("cancel_idle", "exit_cancel_idle_timer"),
                       ("arm_listen", "exit_arm_listen_timer"),
                       ("cancel_listen", "exit_cancel_listen_timer"),
                       ("arm_idle", "exit_arm_idle_timer")):
            monkeypatch.setattr(
                standby_host, fn, (lambda o: lambda: rec["timer_ops"].append(o))(op))
        monkeypatch.setattr(runtime, "interaction_mode", "dialogue")
        monkeypatch.setattr(runtime, "standby", False)
        monkeypatch.setattr(runtime, "listen_ctrl", None)
        monkeypatch.setattr(runtime, "duplex_enabled", True)
        rec["runtime"] = runtime
        return rec

    def test_not_addressed_v2_enters_listening(self, v2_gates):
        """LLM 判非对话对象 → 聆听态(不进待命):撤 idle 臂聆听表。"""
        tool = web_ui_agent.VoiceAgent.not_addressed.__get__(_turn_agent())
        with pytest.raises(web_ui_agent.StopResponse):
            asyncio.run(tool(object()))
        rt = v2_gates["runtime"]
        assert rt.interaction_mode == "listening"
        assert v2_gates["v2_frames"] == [{"type": "listening", "on": True}]
        assert v2_gates["timer_ops"] == ["cancel_idle", "arm_listen"]
        assert v2_gates["enters"] == [] and v2_gates["standby_exits"] == []
        assert any("NOT_ADDRESSED_HIT" in l for l in v2_gates["logs"])
        assert any("V2_LISTEN_ENTER via not_addressed" in l for l in v2_gates["v2_logs"])

    def test_not_addressed_v2_interrupts_stray_speech(self, v2_gates):
        """防线(2026-10-13):V2 下工具同步触发 session.interrupt()——模型若在
        调用同轮带出文字(已入播报队列)要打断残留播报;context 无 session
        (裸 object(),旧测试口径)时不触发、不影响行为。"""
        tool = web_ui_agent.VoiceAgent.not_addressed.__get__(_turn_agent())
        with pytest.raises(web_ui_agent.StopResponse):
            asyncio.run(tool(object()))
        assert not any("NOT_ADDRESSED_INTERRUPT" in l for l in v2_gates["logs"])

        calls = []

        class _Session:
            def interrupt(self, **kwargs):
                calls.append(kwargs)
                return "future"  # 不 await,只记录

        ctx = types.SimpleNamespace(session=_Session())
        with pytest.raises(web_ui_agent.StopResponse):
            asyncio.run(tool(ctx))
        assert calls == [{}]
        assert any("NOT_ADDRESSED_INTERRUPT" in l for l in v2_gates["logs"])
        assert v2_gates["runtime"].interaction_mode == "listening"

    def test_not_addressed_v1_regression(self, v2_gates,
                                         monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(interaction_host, "_MODE_V2", False)
        tool = web_ui_agent.VoiceAgent.not_addressed.__get__(_turn_agent())
        with pytest.raises(web_ui_agent.StopResponse):
            asyncio.run(tool(object()))
        assert v2_gates["enters"] == ["not_addressed"]
        assert v2_gates["runtime"].interaction_mode == "dialogue"
        assert v2_gates["v2_frames"] == []

    def test_listening_turn_injects_instruction_skips_filters(self, v2_gates):
        """V2 聆听轮:每轮都过 LLM——不过过滤链/不打断,只注入聆听指令;
        正常返回(不 raise)让 _generate_reply 照跑 → 转写照发设备。"""
        rt = v2_gates["runtime"]
        rt.interaction_mode = "listening"
        agent = _turn_agent()
        counts = {"filters": 0, "continuation": 0, "auto_enter": 0, "listening_turn": 0}

        agent._handle_listening_turn = (
            lambda tc, nm, o: (counts.__setitem__("listening_turn",
                                                  counts["listening_turn"] + 1)
                               or (o, False)))
        agent._maybe_add_continuation_instruction = (
            lambda tc, o: (counts.__setitem__("continuation",
                                              counts["continuation"] + 1) or o))
        agent._maybe_auto_enter_listening = (
            lambda o, s: counts.__setitem__("auto_enter", counts["auto_enter"] + 1))
        _noop_filters(agent, counts)
        turn_ctx = _TurnCtx()

        asyncio.run(agent.on_user_turn_completed(turn_ctx, _msg("明天天气怎么样")))
        assert turn_ctx.added == [("system", interaction_host.V2_LISTENING_INSTRUCTION)]
        assert v2_gates["arms_listen"] == 1      # 聆听轮换锚聆听持续表
        assert v2_gates["resets"] == 0           # idle 不重臂(否则聆听中途误休眠)
        assert counts == {"filters": 0, "continuation": 0,
                          "auto_enter": 0, "listening_turn": 0}

    def test_voice_turn_wakes_sleeping(self, v2_gates):
        """休眠态照来的语音轮自愈唤醒(兜底端侧唤醒帧丢失/乱序)。"""
        rt = v2_gates["runtime"]
        rt.interaction_mode = "sleeping"
        rt.standby = True
        agent = _turn_agent()
        agent._handle_listening_turn = lambda tc, nm, o: (o, False)
        agent._maybe_add_continuation_instruction = lambda tc, o: o
        agent._maybe_auto_enter_listening = lambda o, s: None
        counts = {"filters": 0}
        _noop_filters(agent, counts)

        asyncio.run(agent.on_user_turn_completed(object(), _msg("小歌在吗")))
        assert v2_gates["standby_exits"] == ["input_voice_turn"]
        assert any("V2_WAKE_ON_INPUT via voice_turn" in l for l in v2_gates["v2_logs"])
        assert v2_gates["resets"] == 1   # 唤醒后正常轮:idle 重臂
        assert v2_gates["enters"] == []
        assert counts["filters"] == 1    # 正常轮照走过滤链

    def test_exit_phrase_wins_over_listening(self, v2_gates):
        """门序:退出语 > 聆听分支——聆听态说"拜拜"照常静默退出。"""
        rt = v2_gates["runtime"]
        rt.interaction_mode = "listening"
        agent = _turn_agent()
        with pytest.raises(web_ui_agent.StopResponse):
            asyncio.run(agent.on_user_turn_completed(object(), _msg("拜拜")))
        assert agent.finalize_calls == [("拜拜", True, "exit")]
        assert v2_gates["enters"] == ["semantic"]
        assert v2_gates["arms_listen"] == 0 and v2_gates["resets"] == 0

    def test_manual_text_wakes_sleeping(self, v2_gates):
        """手发文字 = 用户主动找小歌:休眠自愈唤醒 + 照常生成回复。"""
        rt = v2_gates["runtime"]
        rt.interaction_mode = "sleeping"
        rt.standby = True
        agent = _turn_agent()
        agent.session = types.SimpleNamespace(
            generate_reply=lambda **kw: v2_gates["generated"].append(kw))
        counts = {"filters": 0}
        _noop_filters(agent, counts)

        asyncio.run(web_ui_agent.VoiceAgent.handle_manual_text.__get__(agent)("讲个故事"))
        assert v2_gates["standby_exits"] == ["input_manual_text"]
        assert v2_gates["generated"] == [
            {"user_input": "讲个故事", "input_modality": "text"}]
        assert v2_gates["resets"] == 1 and v2_gates["marks"] == 1

    def test_manual_text_exits_listening(self, v2_gates):
        """面板文字必然是对小歌说的:聆听态即刻回对话(不等 LLM 判定)。"""
        rt = v2_gates["runtime"]
        rt.interaction_mode = "listening"
        agent = _turn_agent()
        agent.session = types.SimpleNamespace(
            generate_reply=lambda **kw: v2_gates["generated"].append(kw))
        counts = {"filters": 0}
        _noop_filters(agent, counts)

        asyncio.run(web_ui_agent.VoiceAgent.handle_manual_text.__get__(agent)("今天几号"))
        assert rt.interaction_mode == "dialogue"
        assert v2_gates["v2_frames"] == [{"type": "listening", "on": False}]
        assert v2_gates["timer_ops"] == ["cancel_listen", "arm_idle"]
        assert v2_gates["generated"] == [
            {"user_input": "今天几号", "input_modality": "text"}]

    def test_attach_resets_to_sleeping_without_welcome(self, v2_gates,
                                                       monkeypatch: pytest.MonkeyPatch):
        """V2 attach:休眠重置、撤两路定时器、不臂 idle、零欢迎语、零聆听帧;
        duplex_state 通知帧照发(面板开关对齐)。"""
        rt = v2_gates["runtime"]
        rt.interaction_mode = "dialogue"
        for attr in ("session_persona", "session_voice",
                     "session_voice_explicit", "session_persona_explicit"):
            monkeypatch.setattr(rt, attr, None)
        agent = _turn_agent()

        asyncio.run(web_ui_agent.VoiceAgent.handle_session_attach.__get__(agent)())
        assert rt.interaction_mode == "sleeping" and rt.standby is True
        assert v2_gates["timer_ops"] == ["cancel_idle", "cancel_listen"]
        assert v2_gates["v2_frames"] == []          # v2_session_start 零广播
        assert agent._voice_welcome_said is True    # 旧入口双保险压旗标
        assert v2_gates["resets"] == 0              # 休眠态无 idle 臂
        duplex_frames = [m for m in v2_gates["broadcasts"]
                         if m.get("type") == "ctrl.duplex_state"]
        assert len(duplex_frames) == 1
        assert any("SESSION_ATTACH_RESET" in l for l in v2_gates["logs"])
        # 随后的旧入口欢迎语也 no-op(旗标 + V2 早退双保险)
        asyncio.run(web_ui_agent.VoiceAgent.handle_voice_welcome.__get__(agent)())
        assert v2_gates["welcomes"] == []

    def test_voice_welcome_noop_in_v2(self, v2_gates):
        """V2 连接零欢迎语:handle_voice_welcome 整体 no-op(先于旗标置位)。"""
        agent = _turn_agent()
        asyncio.run(web_ui_agent.VoiceAgent.handle_voice_welcome.__get__(agent)())
        assert v2_gates["welcomes"] == []
        assert agent._voice_welcome_said is False

    def test_transcription_semantic_wake(self, v2_gates):
        """聆听态 LLM 产出文本 = 语义唤醒:首个非空 chunk 即切回对话(状态帧
        不滞后音频数秒),chunK 原样透传,assistant 气泡照广播。"""
        rt = v2_gates["runtime"]
        rt.interaction_mode = "listening"
        agent = types.SimpleNamespace(
            _awaiting_continue_confirmation=False, _last_assistant_text=None)
        agent._remember_assistant_turn = (
            web_ui_agent.VoiceAgent._remember_assistant_turn.__get__(agent))
        out = []

        async def scenario():
            agen = web_ui_agent.VoiceAgent.transcription_node.__get__(agent)
            async for chunk in agen(_chunks("在的", "", "呢"), None):
                out.append(chunk)

        asyncio.run(scenario())
        assert out == ["在的", "", "呢"]            # 透传,不吞不洗
        assert rt.interaction_mode == "dialogue"
        assert v2_gates["v2_frames"] == [{"type": "listening", "on": False}]
        assert v2_gates["timer_ops"] == ["cancel_listen", "arm_idle"]
        assert any("V2_LISTEN_EXIT via semantic_wake" in l for l in v2_gates["v2_logs"])
        assert any(m.get("role") == "assistant" and m.get("text") == "在的呢"
                   for m in v2_gates["broadcasts"])
        assert agent._last_assistant_text == "在的呢"

    def test_transcription_dialogue_noop(self, v2_gates):
        rt = v2_gates["runtime"]
        rt.interaction_mode = "dialogue"
        agent = types.SimpleNamespace(
            _awaiting_continue_confirmation=False, _last_assistant_text=None)
        agent._remember_assistant_turn = (
            web_ui_agent.VoiceAgent._remember_assistant_turn.__get__(agent))

        async def scenario():
            agen = web_ui_agent.VoiceAgent.transcription_node.__get__(agent)
            async for _ in agen(_chunks("你好呀"), None):
                pass

        asyncio.run(scenario())
        assert rt.interaction_mode == "dialogue"
        assert v2_gates["v2_frames"] == [] and v2_gates["timer_ops"] == []

    def test_transcription_v1_flag_off_no_exit(self, v2_gates,
                                               monkeypatch: pytest.MonkeyPatch):
        """V1 回归:开关关时 exit_listening_v2 幂等早退,mode 不动。"""
        monkeypatch.setattr(interaction_host, "_MODE_V2", False)
        rt = v2_gates["runtime"]
        rt.interaction_mode = "listening"
        agent = types.SimpleNamespace(
            _awaiting_continue_confirmation=False, _last_assistant_text=None)
        agent._remember_assistant_turn = (
            web_ui_agent.VoiceAgent._remember_assistant_turn.__get__(agent))

        async def scenario():
            agen = web_ui_agent.VoiceAgent.transcription_node.__get__(agent)
            async for _ in agen(_chunks("在的"), None):
                pass

        asyncio.run(scenario())
        assert rt.interaction_mode == "listening"
        assert v2_gates["v2_frames"] == []


# ── app.listening_host:V1 钩子在 listen_ctrl=None 下天然失效 ────────────


@pytestmark_lh
class TestListeningHostInert:
    def test_none_ctrl_disables_v1_hooks(self, monkeypatch: pytest.MonkeyPatch):
        """V2 不建 ListeningController(listen_ctrl=None):V1 聆听钩子全部
        no-op——不打断、无尾巴窗、进聆听 aftermath 无副作用。"""
        from app.session_state import runtime

        monkeypatch.setattr(runtime, "listen_ctrl", None)
        monkeypatch.setattr(runtime, "listen_exit_pending", False)
        monkeypatch.setattr(runtime, "listen_drain_until", 0.0)
        assert listening_host.listen_interrupt_blocked() is False
        assert listening_host.listen_tail_pending() is False
        listening_host.listen_enter_aftermath("kws", notice=True)  # 不抛即过


# ── app.setup_taps:语音活动锚点接线(2026-10-12 现网问题#2) ─────────────


@pytestmark_st
class TestSetupTapsV2:
    """VAD 开口与 STT interim/final 都按模式重臂定时器;V1 开口重臂 idle 原路径不变。"""

    @pytest.fixture
    def taps_env(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        from app.session_state import runtime

        rec = {"anchors": 0, "resets": 0}
        monkeypatch.setattr(interaction_host, "_MODE_V2", True)
        monkeypatch.setattr(interaction_host, "v2_user_speech_activity",
                            lambda: rec.__setitem__("anchors", rec["anchors"] + 1))
        monkeypatch.setattr(setup_taps, "exit_reset_idle_timer",
                            lambda: rec.__setitem__("resets", rec["resets"] + 1))
        monkeypatch.setattr(setup_taps, "listen_tail_pending", lambda: False)
        monkeypatch.setattr(setup_taps, "should_ignore_user_turn", lambda t: False)
        monkeypatch.setattr(setup_taps, "is_overlap_ack", lambda t: False)
        monkeypatch.setattr(setup_taps, "_log", lambda line: None)
        monkeypatch.setattr(runtime, "listen_ctrl", None)
        monkeypatch.setattr(runtime, "music_player", None)
        monkeypatch.setattr(runtime, "overlap_turn_state",
                            {"user_spoke_over_agent": False})
        return rec

    @staticmethod
    def _w():
        return types.SimpleNamespace(
            turn_trace={}, online_state={},
            tts_engine=types.SimpleNamespace(prewarm_connection=lambda: None),
            stt_engine=types.SimpleNamespace(),  # 无 prewarm_connection → hasattr 跳过
            session=types.SimpleNamespace(agent_state="listening"),
            live=None, live_from_main=False,
        )

    def test_user_state_speaking_v2_anchors(self, taps_env):
        """V2 开口:走 v2_user_speech_activity(按模式重臂),不再直臂 idle。"""
        event = types.SimpleNamespace(old_state="listening", new_state="speaking",
                                      created_at=1.0)

        async def scenario():
            setup_taps._handle_user_state(self._w(), event)

        asyncio.run(scenario())
        assert taps_env["anchors"] == 1 and taps_env["resets"] == 0

    def test_user_state_speaking_v1_resets_idle(
        self, taps_env, monkeypatch: pytest.MonkeyPatch
    ):
        """V1 回归:开口照旧 exit_reset_idle_timer,不进 V2 锚点。"""
        monkeypatch.setattr(interaction_host, "_MODE_V2", False)
        event = types.SimpleNamespace(old_state="listening", new_state="speaking",
                                      created_at=1.0)

        async def scenario():
            setup_taps._handle_user_state(self._w(), event)

        asyncio.run(scenario())
        assert taps_env["anchors"] == 0 and taps_env["resets"] == 1

    def test_stt_interim_anchors(self, taps_env):
        """interim 到达 = 还在说话(问题#2 核心):锚点触发。"""
        setup_taps._handle_stt_event(
            self._w(), types.SimpleNamespace(is_final=False, transcript="工资之后"))
        assert taps_env["anchors"] == 1

    def test_stt_final_anchors(self, taps_env):
        setup_taps._handle_stt_event(
            self._w(), types.SimpleNamespace(is_final=True, transcript="小歌你好"))
        assert taps_env["anchors"] == 1

    def test_stt_empty_transcript_no_anchor(self, taps_env):
        """空转写(非语音活动)不锚点——防 VAD 噪声频繁重臂表。"""
        setup_taps._handle_stt_event(
            self._w(), types.SimpleNamespace(is_final=False, transcript=""))
        assert taps_env["anchors"] == 0
