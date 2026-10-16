"""退出机制/待命态单测 — 2026-09-15(与 duplex 开关同批)。

覆盖:
- web_ui_agent._is_exit_phrase 规则表:整句锚定(歌名/正文/问句不误伤)、
  env 追加词口径(整句比对,不带语气容差)。
- app.standby_host:enter/exit 幂等、聆听在态连带退出(不问整理)、
  idle 守卫链(池化无会话=彻底停表 / duplex 关/音频在途/纯文字面板顺延/
  正常触发)、聆听持续定时器 fire、EXIT_ENABLE 软关、会话起点
  standby_cancel_all(脏 standby 清除补播 off 帧)。
- 真实会话 attach 重置(2026-09-15 幽灵待命修复):handle_session_attach
  per-session 重置全序列;webpanel.server ctrl.hello →
  _schedule_session_attach_reset marshal 链(handler 注册/回落/循环未就绪)。
- on_user_turn_completed / handle_manual_text 门序:语义退出 > duplex
  (2026-09-16 起待命不设门:纯通知态,期间轮次照常处理照常回复)。
- _handle_stt_event 气泡抑制:仅聆听尾巴期(待命期照常弹,同 2026-09-16)。

本地无 livekit → web_ui_agent/setup_taps 相关整类 skip(与
test_ours_agent_config 同款 stub 舞);standby_host 依赖 webpanel.bridge(aiohttp)
本地缺则同样 skip。服务器:cd examples/voice_agents && ../../.venv/bin/python -m pytest。
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

# web_ui_agent import 前的 stub 舞(与 test_ours_agent_config 一致)。
backends = types.ModuleType("app.backends")
backends.build_llm = lambda: None
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
    import app.setup_taps as setup_taps  # noqa: E402
except Exception:  # pragma: no cover - 本地无 livekit
    setup_taps = None

try:
    import webpanel.server as wp_server  # noqa: E402
except Exception:  # pragma: no cover - 本地缺 aiohttp/webpanel 链
    wp_server = None

pytestmark_web = pytest.mark.skipif(web_ui_agent is None, reason="no livekit locally")
pytestmark_std = pytest.mark.skipif(standby_host is None, reason="standby deps missing locally")
pytestmark_taps = pytest.mark.skipif(setup_taps is None, reason="no livekit locally")
pytestmark_wps = pytest.mark.skipif(wp_server is None, reason="webpanel deps missing locally")


# ── _is_exit_phrase 规则表 ─────────────────────────────────────────────


@pytestmark_web
class TestExitPhrase:
    @pytest.mark.parametrize("text", [
        "拜拜", "拜拜。", "再见！", "回见", "拜",
        "退下", "你退下吧", "你先退下", "先退了啊",
        "下去吧", "回去啦", "走吧", "去休息吧", "去歇着吧", "待着吧", "下班吧", "退了哦",
        "没你的事了", "没你什么事了", "不用陪我了", "不用陪了",
        "先这样吧", "就到这里吧", "今天先到这", "去忙你的吧", "去忙吧",
    ])
    def test_positive(self, text):
        assert web_ui_agent._is_exit_phrase(text) is True

    @pytest.mark.parametrize("text", [
        "播放拜拜这首歌",   # 歌名正文不误伤(整句锚定)
        "再见女朋友",
        "退下是什么意思",   # 问句/解释请求
        "小歌退下了吗",     # 吗 不在语气词表
        "拜拜了您嘞",
        "先这样吧好不好",
        "别退下",           # 否定前缀 = 挽留,不该退
        "下首歌",
        "走路去学校",
        "去上班",           # 词表只有 下班 没有 上班
        "那就退了",         # 闭集保守:双前缀不匹配,落 LLM(env 可补)
    ])
    def test_negative(self, text):
        assert web_ui_agent._is_exit_phrase(text) is False

    def test_empty_and_none(self):
        assert web_ui_agent._is_exit_phrase("") is False
        assert web_ui_agent._is_exit_phrase(None) is False

    def test_env_phrases_whole_sentence_only(self, monkeypatch: pytest.MonkeyPatch):
        """env 追加词 = 归一化后整句比对(不带语气容差):"撤了"命中,"撤了吧"落 LLM。"""
        monkeypatch.setattr(web_ui_agent, "_EXIT_PHRASES_ENV", frozenset({"撤了"}))
        assert web_ui_agent._is_exit_phrase("撤了") is True
        assert web_ui_agent._is_exit_phrase("撤了吧。") is False


# ── app.standby_host 纯逻辑(runtime 单例字段逐一隔离,测完自动还原) ────


class _FakeLoop:
    """call_later 录调用(返回可 cancel 的假 handle),不真跑回调。"""

    def __init__(self):
        self.calls = []  # (delay, cb)
        self.cancelled = []  # handle 序号

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


@pytest.fixture
def std_env(monkeypatch: pytest.MonkeyPatch) -> dict:
    """standby_host 测试环境:broadcast/埋点/聆听收尾录制 + runtime 全字段隔离。"""
    from app.session_state import runtime
    rec = {"frames": [], "logs": [], "aftermath": [], "force_exits": []}
    monkeypatch.setattr(standby_host, "broadcast", lambda msg: rec["frames"].append(msg))
    monkeypatch.setattr(standby_host, "append_turn_log",
                        lambda line: rec["logs"].append(line))
    monkeypatch.setattr(standby_host.listening_host, "listen_exit_aftermath",
                        lambda via, ask=True: rec["aftermath"].append((via, ask)))
    monkeypatch.setattr(standby_host, "EXIT_ENABLE", True)  # 钉死:不随宿主 env 漂
    # 默认假装有一个会话 WS 客户端(真会话在测);池化无会话场景单独覆写为 0。
    monkeypatch.setattr(standby_host, "session_client_count", lambda: 1)
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


@pytestmark_std
class TestStandbyHost:
    def test_enter_broadcasts_on_frame(self, std_env):
        standby_host.enter_standby("semantic")
        assert std_env["runtime"].standby is True
        assert std_env["frames"] == [{"type": "standby", "on": True,
                                      "reason": "semantic"}]
        assert any("STANDBY_ENTER via semantic" in l for l in std_env["logs"])

    def test_enter_idempotent(self, std_env):
        std_env["runtime"].standby = True
        standby_host.enter_standby("idle")
        assert std_env["frames"] == [] and std_env["logs"] == []

    def test_enter_force_exits_listening_without_ask(self, std_env):
        std_env["runtime"].listen_ctrl = _listen_ctrl(std_env)
        standby_host.enter_standby("semantic")
        assert std_env["force_exits"] == [1]
        assert std_env["aftermath"] == [("standby", False)]  # 不问整理
        assert std_env["frames"] and std_env["frames"][0]["on"] is True

    def test_enter_cancels_both_timers(self, std_env):
        rt = std_env["runtime"]
        loop = _FakeLoop()
        rt.agent_loop = loop
        standby_host.exit_arm_idle_timer()
        standby_host.exit_arm_listen_timer()
        standby_host.enter_standby("idle")
        assert loop.cancelled == [1, 2]
        assert rt.exit_idle_handle is None and rt.exit_listen_handle is None

    def test_exit_idempotent(self, std_env):
        standby_host.exit_standby("frontend_state")  # 本就不在待命
        assert std_env["frames"] == [] and std_env["logs"] == []

    def test_exit_broadcasts_off_and_rearms_idle(self, std_env):
        rt = std_env["runtime"]
        loop = _FakeLoop()
        rt.agent_loop = loop
        rt.standby = True
        standby_host.exit_standby("frontend_state")
        assert rt.standby is False
        assert std_env["frames"] == [{"type": "standby", "on": False,
                                      "reason": "frontend_state"}]
        assert any("STANDBY_EXIT via frontend_state" in l for l in std_env["logs"])
        assert loop.calls and loop.calls[0][0] == standby_host.EXIT_IDLE_S

    def test_arm_idle_cancel_then_rearm(self, std_env):
        rt = std_env["runtime"]
        loop = _FakeLoop()
        rt.agent_loop = loop
        standby_host.exit_arm_idle_timer()
        assert len(loop.calls) == 1
        assert loop.calls[0][0] == standby_host.EXIT_IDLE_S
        assert rt.exit_idle_handle is not None
        standby_host.exit_arm_idle_timer(5.0)
        assert loop.cancelled == [1]  # 重臂先撤旧
        assert loop.calls[-1][0] == 5.0

    def test_arm_idle_noop_when_disabled(self, std_env, monkeypatch: pytest.MonkeyPatch):
        loop = _FakeLoop()
        std_env["runtime"].agent_loop = loop
        monkeypatch.setattr(standby_host, "EXIT_ENABLE", False)  # env 软关
        standby_host.exit_arm_idle_timer()
        assert loop.calls == []

    def test_reset_rearms_to_full(self, std_env):
        rt = std_env["runtime"]
        loop = _FakeLoop()
        rt.agent_loop = loop
        standby_host.exit_arm_idle_timer(3.0)  # 先短臂(顺延态)
        standby_host.exit_reset_idle_timer()
        assert loop.calls[-1][0] == standby_host.EXIT_IDLE_S  # 锚点重臂回满额

    def test_cancel_all_resets_session_start(self, std_env):
        rt = std_env["runtime"]
        loop = _FakeLoop()
        rt.agent_loop = loop
        rt.standby = True
        standby_host.exit_arm_idle_timer()
        standby_host.exit_arm_listen_timer()
        standby_host.standby_cancel_all()
        assert rt.standby is False
        assert any("STANDBY_RESET session_start" in l for l in std_env["logs"])
        # 脏 standby 清除补播 off 帧(端侧状态收敛;2026-09-15 幽灵竞窗修复)
        assert std_env["frames"] == [{"type": "standby", "on": False,
                                      "reason": "session_start"}]
        assert loop.cancelled == [1, 2]

    def test_cancel_all_clean_is_silent(self, std_env):
        """干净态重置零广播零埋点(幂等,不给新会话塞杂音)。"""
        loop = _FakeLoop()
        std_env["runtime"].agent_loop = loop
        standby_host.exit_arm_idle_timer()
        standby_host.standby_cancel_all()
        assert std_env["frames"] == [] and std_env["logs"] == []
        assert loop.cancelled == [1]


@pytestmark_std
class TestIdleGuardChain:
    """_on_idle_expired 守卫链:duplex 关/音频在途/无客户端 → 顺延重臂;正常 → standby。"""

    def _armed(self, std_env):
        loop = _FakeLoop()
        std_env["runtime"].agent_loop = loop
        return loop

    def test_expired_during_standby_still_runs_guards(self, std_env,
                                                      monkeypatch: pytest.MonkeyPatch):
        """2026-09-16:待命不再是早退门——到期照常走守卫链(真 enter_standby
        幂等早退、不重播 on 帧);若在此早退,待命期轮次重臂的定时器会死在这里。"""
        enters = []
        monkeypatch.setattr(standby_host, "enter_standby", lambda via: enters.append(via))
        monkeypatch.setattr(standby_host, "audio_client_count", lambda: 2)
        self._armed(std_env)
        std_env["runtime"].standby = True
        standby_host._on_idle_expired()
        assert enters == ["idle"]

    def test_duplex_off_defers(self, std_env, monkeypatch: pytest.MonkeyPatch):
        enters = []
        monkeypatch.setattr(standby_host, "enter_standby", lambda via: enters.append(via))
        loop = self._armed(std_env)
        std_env["runtime"].duplex_enabled = False
        standby_host._on_idle_expired()
        assert enters == []
        assert loop.calls == [(standby_host._DEFER_S, standby_host._on_idle_expired)]

    def test_audio_in_flight_defers(self, std_env, monkeypatch: pytest.MonkeyPatch):
        enters = []
        monkeypatch.setattr(standby_host, "enter_standby", lambda via: enters.append(via))
        loop = self._armed(std_env)
        std_env["runtime"].ws_audio_output = types.SimpleNamespace(_pushed_duration=1.2)
        standby_host._on_idle_expired()
        assert enters == []
        assert loop.calls and loop.calls[0][0] == standby_host._DEFER_S

    def test_agent_output_in_flight_defers(self, std_env, monkeypatch: pytest.MonkeyPatch):
        enters = []
        monkeypatch.setattr(standby_host, "enter_standby", lambda via: enters.append(via))
        loop = self._armed(std_env)
        std_env["runtime"].agent_output_in_flight = True
        standby_host._on_idle_expired()
        assert enters == []
        assert loop.calls and loop.calls[0][0] == standby_host._DEFER_S

    def test_agent_output_finished_allows_standby(self, std_env, monkeypatch: pytest.MonkeyPatch):
        enters = []
        monkeypatch.setattr(standby_host, "enter_standby", lambda via: enters.append(via))
        monkeypatch.setattr(standby_host, "audio_client_count", lambda: 2)
        self._armed(std_env)
        std_env["runtime"].agent_output_in_flight = False
        standby_host._on_idle_expired()
        assert enters == ["idle"]

    def test_no_audio_clients_defers(self, std_env, monkeypatch: pytest.MonkeyPatch):
        enters = []
        monkeypatch.setattr(standby_host, "enter_standby", lambda via: enters.append(via))
        loop = self._armed(std_env)
        monkeypatch.setattr(standby_host, "audio_client_count", lambda: 0)
        standby_host._on_idle_expired()
        assert enters == []
        assert loop.calls and loop.calls[0][0] == standby_host._DEFER_S

    def test_no_session_clients_stops_timer(self, std_env, monkeypatch: pytest.MonkeyPatch):
        """池化无会话时停表，attach 后重新计时。"""
        enters = []
        monkeypatch.setattr(standby_host, "enter_standby", lambda via: enters.append(via))
        loop = self._armed(std_env)
        monkeypatch.setattr(standby_host, "session_client_count", lambda: 0)
        monkeypatch.setattr(standby_host, "audio_client_count", lambda: 0)
        standby_host._on_idle_expired()
        assert enters == []
        assert loop.calls == []

    def test_marking_output_finished_clears_guard(self, std_env):
        std_env["runtime"].agent_output_in_flight = True
        standby_host.mark_agent_output_finished()
        assert std_env["runtime"].agent_output_in_flight is False

    def test_normal_triggers_standby_idle(self, std_env, monkeypatch: pytest.MonkeyPatch):
        enters = []
        monkeypatch.setattr(standby_host, "enter_standby", lambda via: enters.append(via))
        monkeypatch.setattr(standby_host, "audio_client_count", lambda: 2)
        self._armed(std_env)
        standby_host._on_idle_expired()
        assert enters == ["idle"]



@pytestmark_std
class TestListenTimer:
    def test_arm_listen(self, std_env):
        rt = std_env["runtime"]
        loop = _FakeLoop()
        rt.agent_loop = loop
        standby_host.exit_arm_listen_timer()
        assert len(loop.calls) == 1
        assert loop.calls[0][0] == standby_host.EXIT_LISTEN_S
        assert rt.exit_listen_handle is not None

    def test_expired_exits_listening_and_enters_standby(
        self, std_env, monkeypatch: pytest.MonkeyPatch
    ):
        std_env["runtime"].listen_ctrl = _listen_ctrl(std_env)
        enters = []
        monkeypatch.setattr(standby_host, "enter_standby", lambda via: enters.append(via))
        standby_host._on_listen_expired()
        assert std_env["force_exits"] == [1]
        assert std_env["aftermath"] == [("standby", False)]  # 不问整理
        assert enters == ["listen_timeout"]

    def test_expired_listening_inactive_noop(self, std_env, monkeypatch: pytest.MonkeyPatch):
        std_env["runtime"].listen_ctrl = _listen_ctrl(std_env, active=False)
        enters = []
        monkeypatch.setattr(standby_host, "enter_standby", lambda via: enters.append(via))
        standby_host._on_listen_expired()
        assert enters == [] and std_env["aftermath"] == []

    def test_expired_during_standby_still_exits_listening(self, std_env,
                                                          monkeypatch: pytest.MonkeyPatch):
        """2026-09-16:待命不早退——待命期起的聆听超时照常 force_exit
        (否则聆听卡死在待命里);enter_standby 对已待命幂等。"""
        std_env["runtime"].standby = True
        std_env["runtime"].listen_ctrl = _listen_ctrl(std_env)
        enters = []
        monkeypatch.setattr(standby_host, "enter_standby", lambda via: enters.append(via))
        standby_host._on_listen_expired()
        assert std_env["force_exits"] == [1]
        assert std_env["aftermath"] == [("standby", False)]
        assert enters == ["listen_timeout"]


# ── web_ui_agent 轮次门:语义退出 > duplex(待命不设门 2026-09-16) ────────


@pytest.fixture
def web_env(monkeypatch: pytest.MonkeyPatch) -> dict:
    logs = []
    monkeypatch.setattr(web_ui_agent, "_append_turn_log", lambda line: logs.append(line))
    return {"logs": logs}


@pytest.fixture
def gates(web_env: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
    """on_user_turn_completed/handle_manual_text 门环境:符号替换 + 录制器。"""
    rec = {"logs": web_env["logs"], "resets": 0, "enters": [], "broadcasts": []}
    monkeypatch.setattr(web_ui_agent, "EXIT_ENABLE", True)  # 钉死:不随宿主 env 漂
    monkeypatch.setattr(web_ui_agent, "exit_reset_idle_timer",
                        lambda: rec.__setitem__("resets", rec["resets"] + 1))
    monkeypatch.setattr(web_ui_agent, "enter_standby",
                        lambda via: rec["enters"].append(via))
    monkeypatch.setattr(web_ui_agent, "broadcast",
                        lambda msg: rec["broadcasts"].append(msg))
    monkeypatch.setattr(web_ui_agent.runtime, "overlap_turn_state",
                        {"user_spoke_over_agent": False})
    return rec


def _turn_agent(cfg=None):
    """SimpleNamespace 绑定桩:真 _finalize_g3_user_message(气泡经 broadcast 落)。"""
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
    agent._duplex_off = (
        web_ui_agent.VoiceAgent._duplex_off.__get__(agent)
    )
    # 2026-09-16 欢迎语统一到 attach:attach/旧入口共用 _voice_welcome_said
    # 去重旗标;_voice_welcome_when_ready 是 attach 起的延迟开口任务。
    agent._voice_welcome_said = False
    agent._voice_welcome_when_ready = (
        web_ui_agent.VoiceAgent._voice_welcome_when_ready.__get__(agent)
    )
    agent.on_user_turn_completed = (
        web_ui_agent.VoiceAgent.on_user_turn_completed.__get__(agent))
    return agent


def _duplex_off_cfg():
    return config_store.AgentConfig(
        persona_name="小歌",
        persona_traits=config_store.DEFAULT_PERSONA_TRAITS,
        tts_voice=config_store.DEFAULT_CONFIG.tts_voice,
        duplex_enabled=False)


def _msg(text):
    return types.SimpleNamespace(text_content=text, content=[text])


@pytestmark_web
class TestTurnGates:
    def test_standby_turn_processes_normally(self, gates,
                                             monkeypatch: pytest.MonkeyPatch):
        """2026-09-16 待命=纯通知态:待命中的语音轮走全管线——不吞、照常重臂。"""
        monkeypatch.setattr(web_ui_agent.runtime, "standby", True)
        agent = _turn_agent()
        agent._handle_listening_turn = (
            lambda turn_ctx, new_message, original: (original, False))
        agent._maybe_add_continuation_instruction = lambda turn_ctx, original: original
        agent._maybe_auto_enter_listening = lambda original, spoke: None

        async def noop_filters(original, spoke, finalize_user_message=True):
            return None

        agent._apply_turn_filters = noop_filters
        asyncio.run(agent.on_user_turn_completed(object(), _msg("还在吗")))
        assert gates["resets"] == 1  # 待命期输入照常是锚点:重臂回满额
        assert gates["enters"] == []  # 不因待命静默退出/再进
        assert not any("STANDBY_SWALLOW" in l for l in gates["logs"])  # 门已拆

    def test_semantic_exit_silent_standby(self, gates):
        agent = _turn_agent()
        with pytest.raises(web_ui_agent.StopResponse):
            asyncio.run(agent.on_user_turn_completed(object(), _msg("拜拜。")))
        # 该句照常显示为 user 气泡;其后零回复(静默退出)
        assert agent.finalize_calls == [("拜拜。", True, "exit")]
        assert any(m.get("role") == "user" and m.get("text") == "拜拜。"
                   for m in gates["broadcasts"])
        assert not any(m.get("role") == "assistant" for m in gates["broadcasts"])
        assert gates["enters"] == ["semantic"]
        assert gates["resets"] == 0  # 退出分支先于 reset:不重臂
        assert any("EXIT_PHRASE_HIT" in l for l in gates["logs"])

    def test_exit_phrase_still_wins_during_standby(self, gates,
                                                   monkeypatch: pytest.MonkeyPatch):
        """待命不设门后退出语照常命中:显示气泡+静默吞轮(真 enter_standby 幂等)。"""
        monkeypatch.setattr(web_ui_agent.runtime, "standby", True)
        agent = _turn_agent()
        with pytest.raises(web_ui_agent.StopResponse):
            asyncio.run(agent.on_user_turn_completed(object(), _msg("拜拜")))
        assert gates["enters"] == ["semantic"]
        assert agent.finalize_calls == [("拜拜", True, "exit")]
        assert any("EXIT_PHRASE_HIT" in l for l in gates["logs"])
        assert not any("STANDBY_SWALLOW" in l for l in gates["logs"])

    def test_gate_order_exit_over_duplex_off(self, gates, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(web_ui_agent.runtime, "duplex_enabled", False)
        agent = _turn_agent(_duplex_off_cfg())
        with pytest.raises(web_ui_agent.StopResponse):
            asyncio.run(agent.on_user_turn_completed(object(), _msg("拜拜")))
        assert gates["enters"] == ["semantic"]
        assert agent.finalize_calls == [("拜拜", True, "exit")]
        assert not any("DUPLEX_OFF_TURN" in l for l in gates["logs"])

    def test_exit_enable_off_falls_to_duplex(self, gates, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(web_ui_agent, "EXIT_ENABLE", False)  # env 软关
        monkeypatch.setattr(web_ui_agent.runtime, "duplex_enabled", False)
        agent = _turn_agent(_duplex_off_cfg())
        with pytest.raises(web_ui_agent.StopResponse):
            asyncio.run(agent.on_user_turn_completed(object(), _msg("拜拜")))
        assert gates["enters"] == []
        assert gates["resets"] == 1  # reset 在 duplex 门之前
        assert any("DUPLEX_OFF_TURN" in l for l in gates["logs"])

    def test_normal_turn_resets_idle(self, gates):
        agent = _turn_agent()
        agent._handle_listening_turn = (
            lambda turn_ctx, new_message, original: (original, False))
        agent._maybe_add_continuation_instruction = lambda turn_ctx, original: original
        agent._maybe_auto_enter_listening = lambda original, spoke: None

        async def noop_filters(original, spoke, finalize_user_message=True):
            return None

        agent._apply_turn_filters = noop_filters
        asyncio.run(agent.on_user_turn_completed(object(), _msg("讲个冷笑话")))
        assert gates["resets"] == 1  # 用户输入锚点:重臂回满额
        assert gates["enters"] == []


@pytestmark_web
class TestManualTextStandby:
    def _agent(self):
        agent = _turn_agent()
        agent.handle_manual_text = (
            web_ui_agent.VoiceAgent.handle_manual_text.__get__(agent))
        agent.generated = []
        agent.session = types.SimpleNamespace(
            generate_reply=lambda **kw: agent.generated.append(kw))
        return agent

    def test_standby_text_gets_reply(self, gates, monkeypatch: pytest.MonkeyPatch):
        """2026-09-16:待命期文字照常回复——唤醒是客户端逻辑,不是服务端吞字。"""
        monkeypatch.setattr(web_ui_agent.runtime, "standby", True)
        agent = self._agent()

        async def noop_filters(original, spoke):
            return None

        agent._apply_turn_filters = noop_filters
        asyncio.run(agent.handle_manual_text("还在吗"))
        assert agent.generated == [{"user_input": "还在吗", "input_modality": "text"}]
        assert gates["resets"] == 1  # 文字输入照常重臂 idle
        assert gates["enters"] == []
        assert not any("MANUAL_TEXT_SUPPRESSED standby" in l for l in gates["logs"])

    def test_exit_phrase_text_enters_standby(self, gates):
        agent = self._agent()
        asyncio.run(agent.handle_manual_text("拜拜"))
        assert any(m.get("role") == "user" and m.get("text") == "拜拜"
                   for m in gates["broadcasts"])
        assert gates["enters"] == ["semantic"]
        assert agent.generated == []
        assert any("EXIT_PHRASE_HIT" in l for l in gates["logs"])

    def test_normal_text_resets_then_generates(self, gates):
        agent = self._agent()

        async def noop_filters(original, spoke):
            return None

        agent._apply_turn_filters = noop_filters
        asyncio.run(agent.handle_manual_text("你好"))
        assert gates["resets"] == 1  # 文字也是输入:先重臂再生成
        assert agent.generated == [{"user_input": "你好", "input_modality": "text"}]


# ── _handle_stt_event 气泡抑制(聆听尾巴期;待命期照常弹 2026-09-16) ──────


@pytestmark_taps
class TestSttSuppress:
    def _wiring(self, rec):
        return types.SimpleNamespace(
            session=types.SimpleNamespace(
                interrupt=lambda force=False: rec.append(("interrupt", force))),
            live_from_main=True,
            live=types.SimpleNamespace(
                feed_full=lambda t: rec.append(("full", t)),
                feed_commit=lambda t: rec.append(("commit", t)),
            ),
            turn_trace={},
            online_state={},
        )

    def _env(self, monkeypatch: pytest.MonkeyPatch):
        from app.session_state import runtime
        monkeypatch.setattr(setup_taps, "listen_tail_pending", lambda: False)
        monkeypatch.setattr(setup_taps, "should_ignore_user_turn", lambda t: False)
        monkeypatch.setattr(setup_taps, "is_overlap_ack", lambda t: False)
        monkeypatch.setattr(setup_taps, "_log", lambda line: None)
        monkeypatch.setattr(runtime, "listen_ctrl", None)
        monkeypatch.setattr(runtime, "music_player", None)
        monkeypatch.setattr(runtime, "overlap_turn_state",
                            {"user_spoke_over_agent": False})

    def test_active_feeds_interim_and_final(self, monkeypatch: pytest.MonkeyPatch):
        rec = []
        w = self._wiring(rec)
        self._env(monkeypatch)
        setup_taps._handle_stt_event(w, types.SimpleNamespace(is_final=False,
                                                              transcript="你"))
        setup_taps._handle_stt_event(w, types.SimpleNamespace(is_final=True,
                                                              transcript="你好"))
        assert ("full", "你") in rec and ("commit", "你好") in rec

    def test_standby_still_feeds_display(self, monkeypatch: pytest.MonkeyPatch):
        """2026-09-16:待命不再抑制气泡——端侧若继续发音频,轮次照常显示。"""
        from app.session_state import runtime
        rec = []
        w = self._wiring(rec)
        self._env(monkeypatch)
        monkeypatch.setattr(runtime, "standby", True)
        setup_taps._handle_stt_event(w, types.SimpleNamespace(is_final=False,
                                                              transcript="你"))
        setup_taps._handle_stt_event(w, types.SimpleNamespace(is_final=True,
                                                              transcript="你好"))
        assert ("full", "你") in rec and ("commit", "你好") in rec

    def test_listening_tail_suppresses_display(self, monkeypatch: pytest.MonkeyPatch):
        """聆听尾巴期照旧抑制(挡滞后到达的尾巴;唯一留下的抑制门)。

        listen_ctrl 必须非 None——`ctrl is not None` 先短路,置 None 时
        listen_tail_pending() 根本不会被读到。
        """
        from app.session_state import runtime
        rec = []
        w = self._wiring(rec)
        self._env(monkeypatch)
        monkeypatch.setattr(runtime, "listen_ctrl",
                            types.SimpleNamespace(active=False))
        monkeypatch.setattr(setup_taps, "listen_tail_pending", lambda: True)
        setup_taps._handle_stt_event(w, types.SimpleNamespace(is_final=False,
                                                              transcript="你"))
        setup_taps._handle_stt_event(w, types.SimpleNamespace(is_final=True,
                                                              transcript="你好"))
        assert rec == []

    def test_listening_active_suppresses_display(self, monkeypatch: pytest.MonkeyPatch):
        """聆听在态照旧抑制(ctrl.active 腿;尾巴腿见上一测试)。"""
        from app.session_state import runtime
        rec = []
        w = self._wiring(rec)
        self._env(monkeypatch)
        monkeypatch.setattr(runtime, "listen_ctrl",
                            types.SimpleNamespace(active=True))
        setup_taps._handle_stt_event(w, types.SimpleNamespace(is_final=False,
                                                              transcript="你"))
        setup_taps._handle_stt_event(w, types.SimpleNamespace(is_final=True,
                                                              transcript="你好"))
        assert rec == []


# ── 真实会话 attach 重置(2026-09-15 幽灵待命修复) ──────────────────────


@pytestmark_web
class TestSessionAttachReset:
    """handle_session_attach:per-session 重置 = 热应用配置 + 清 standby +
    撤遗留定时器 + 同步 duplex 镜像 + 重臂满额 idle + SESSION_ATTACH_RESET 埋点。"""

    def _agent(self, cfg=None):
        agent = _turn_agent(cfg)
        agent.handle_session_attach = (
            web_ui_agent.VoiceAgent.handle_session_attach.__get__(agent))
        return agent

    def test_attach_reset_full_sequence(self, gates, monkeypatch: pytest.MonkeyPatch):
        rec = {"applies": 0, "cancels": 0, "arms": 0}
        monkeypatch.setattr(web_ui_agent, "standby_cancel_all",
                            lambda: rec.__setitem__("cancels", rec["cancels"] + 1))
        monkeypatch.setattr(web_ui_agent, "exit_arm_idle_timer",
                            lambda delay=None: rec.__setitem__("arms", rec["arms"] + 1))
        agent = self._agent()

        async def apply() -> None:
            rec["applies"] += 1

        agent._maybe_apply_agent_config = apply
        asyncio.run(agent.handle_session_attach())
        assert rec["applies"] == 1      # 先热应用(音色/人设/duplex 跟手)
        assert rec["cancels"] == 1      # 清脏 standby + 撤幽灵定时器
        assert rec["arms"] == 1         # 重臂满额 idle
        assert any("SESSION_ATTACH_RESET duplex=on" in l for l in gates["logs"])

    def test_attach_resets_duplex_to_project_default(self, gates, monkeypatch: pytest.MonkeyPatch):
        """全双工开关仅当前会话生效:新会话 attach 恢复**项目默认**(由
        sys_xiaoge_project_config.duplex_enabled 决定,2026-09 项目配置接管后
        不再硬编码开;原 test 断言"一律恢复开"是过期语义,2026-10-12 修正)。"""
        monkeypatch.setattr(web_ui_agent, "standby_cancel_all", lambda: None)
        monkeypatch.setattr(web_ui_agent, "exit_arm_idle_timer", lambda delay=None: None)
        monkeypatch.setattr(web_ui_agent.runtime, "duplex_enabled", True)  # 上一会话开过
        agent = self._agent(_duplex_off_cfg())                            # 项目默认 off
        asyncio.run(agent.handle_session_attach())
        assert web_ui_agent.runtime.duplex_enabled is False
        assert any("SESSION_ATTACH_RESET duplex=off" in l for l in gates["logs"])

    def test_attach_pushes_duplex_state_to_panel(self, gates, monkeypatch: pytest.MonkeyPatch):
        """面板开关恢复(2026-10-12):attach 落项目默认后推 ctrl.duplex_state 帧,
        面板下拉按真实态对齐——项目默认 off 时不再错显"开启"。"""
        monkeypatch.setattr(web_ui_agent, "standby_cancel_all", lambda: None)
        monkeypatch.setattr(web_ui_agent, "exit_arm_idle_timer", lambda delay=None: None)
        agent = self._agent(_duplex_off_cfg())
        asyncio.run(agent.handle_session_attach())
        frames = [m for m in gates["broadcasts"] if m.get("type") == "ctrl.duplex_state"]
        assert frames, "attach 应推 ctrl.duplex_state"
        assert frames[-1]["enabled"] is False    # 项目默认 off 照实推
        agent = self._agent()                    # 默认配置 = on
        asyncio.run(agent.handle_session_attach())
        frames = [m for m in gates["broadcasts"] if m.get("type") == "ctrl.duplex_state"]
        assert frames[-1]["enabled"] is True

    def test_attach_resets_session_persona(self, gates, monkeypatch: pytest.MonkeyPatch):
        """会话性格(2026-09-22,仅 -97)同 duplex 口径:新会话 attach 落回默认档。

        三档化改版(用户指定):冷面幽默 witty 是默认档(PERSONA_KEYS 第一项),
        attach 不再清 None——上一会话选过 sunny,新会话必须回到 witty。
        """
        monkeypatch.setattr(web_ui_agent, "standby_cancel_all", lambda: None)
        monkeypatch.setattr(web_ui_agent, "exit_arm_idle_timer", lambda delay=None: None)
        monkeypatch.setattr(web_ui_agent.runtime, "session_persona", "sunny")  # 上一会话选过
        agent = self._agent()
        asyncio.run(agent.handle_session_attach())
        assert web_ui_agent.runtime.session_persona == "witty"
        assert web_ui_agent.runtime.session_persona == config_store.DEFAULT_PERSONA_KEY
        assert any("SESSION_ATTACH_RESET duplex=on persona=witty" in l
                   for l in gates["logs"])


@pytestmark_web
class TestAttachWelcome:
    """连接欢迎语统一到 attach(2026-09-16):每个 ctrl.hello 恰一句短欢迎语。

    语义:_voice_welcome_said 是实例级旗标,但 attach 恒说并重置它——池化
    worker 跨会话复用,上一会话留下的 True 不能吃掉新会话的欢迎语;旗标
    只负责让旧入口(网页音频 WS 连接/按钮唤醒)在本会话内 no-op。
    """

    def _agent(self, cfg=None):
        agent = _turn_agent(cfg)
        agent.handle_session_attach = (
            web_ui_agent.VoiceAgent.handle_session_attach.__get__(agent))
        agent.handle_voice_welcome = (
            web_ui_agent.VoiceAgent.handle_voice_welcome.__get__(agent))
        return agent

    def _patch(self, gates, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(web_ui_agent, "standby_cancel_all", lambda: None)
        monkeypatch.setattr(web_ui_agent, "exit_arm_idle_timer",
                            lambda delay=None: None)
        monkeypatch.setattr(web_ui_agent, "_ENTRYPOINT_DONE", True)
        said = []
        monkeypatch.setattr(web_ui_agent, "say_voice_welcome",
                            lambda voice=None: said.append(voice))
        return said

    def test_attach_says_one_short_welcome(self, gates, monkeypatch: pytest.MonkeyPatch):
        said = self._patch(gates, monkeypatch)
        agent = self._agent()

        async def scenario():
            await agent.handle_session_attach()
            await asyncio.sleep(0.3)  # 让轮询任务起跑开口

        asyncio.run(scenario())
        assert said == [config_store.DEFAULT_CONFIG.tts_voice]  # 恰一句
        # 旧入口(音频 WS 连接/按钮唤醒)在 attach 之后触发 → no-op
        asyncio.run(agent.handle_voice_welcome())
        assert said == [config_store.DEFAULT_CONFIG.tts_voice]

    def test_second_attach_on_reused_worker_says_again(
            self, gates, monkeypatch: pytest.MonkeyPatch):
        """池化 worker 会话复用:第二个 ctrl.hello(新会话)照常说欢迎语。"""
        said = self._patch(gates, monkeypatch)
        agent = self._agent()

        async def scenario():
            await agent.handle_session_attach()  # 会话 1(attach 后旗标=True)
            await asyncio.sleep(0.3)
            await agent.handle_session_attach()  # 会话 2(同 worker 复用,旗标仍 True)
            await asyncio.sleep(0.3)

        asyncio.run(scenario())
        assert len(said) == 2  # 每个会话起点恰一句

    def test_attach_welcome_waits_entrypoint(self, gates,
                                             monkeypatch: pytest.MonkeyPatch):
        """按需 spawn:attach 先于 entrypoint 收尾到达 → 欢迎任务轮询等待。"""
        monkeypatch.setattr(web_ui_agent, "standby_cancel_all", lambda: None)
        monkeypatch.setattr(web_ui_agent, "exit_arm_idle_timer",
                            lambda delay=None: None)
        monkeypatch.setattr(web_ui_agent, "_ENTRYPOINT_DONE", False)
        said = []
        monkeypatch.setattr(web_ui_agent, "say_voice_welcome",
                            lambda voice=None: said.append(voice))
        agent = self._agent()

        async def scenario():
            await agent.handle_session_attach()
            await asyncio.sleep(0.3)
            assert said == []  # entrypoint 未收尾:先不开口
            web_ui_agent._ENTRYPOINT_DONE = True
            await asyncio.sleep(0.5)
            assert said == [config_store.DEFAULT_CONFIG.tts_voice]

        asyncio.run(scenario())

    def test_attach_welcome_duplex_off_skipped(self, gates,
                                               monkeypatch: pytest.MonkeyPatch):
        """当前会话关 duplex 后旧欢迎入口安静;attach 本身会把新会话重置为开。"""
        said = self._patch(gates, monkeypatch)
        monkeypatch.setattr(web_ui_agent.runtime, "duplex_enabled", False)
        agent = self._agent()
        asyncio.run(agent.handle_voice_welcome())
        assert said == []
        assert any("VOICE_WELCOME_SKIP duplex_off" in l for l in gates["logs"])


@pytestmark_wps
class TestServerAttachWiring:
    """webpanel.server:ctrl.hello → _schedule_session_attach_reset marshal 链。"""

    def test_battery_result_speech_ignores_charging_state(self):
        """电量结果协议已移除充电状态:旧端侧带 chg/charging 也不口播。"""
        assert wp_server._battery_result_speech({
            "result": {"type": "cap", "value": 67, "gauge_id": 1, "chg": False}
        }) == "电量67%"
        assert wp_server._battery_result_speech({
            "result": {"gauge_id": 2, "charging": "正在充电"}
        }) == ""

    def test_hello_triggers_attach_reset(self, monkeypatch: pytest.MonkeyPatch):
        """回归 e2e 33/40 的 7 失败:ctrl.hello 必须触发会话 attach 重置。"""
        fired = []

        async def fake_ready(*a, **kw):
            return None

        monkeypatch.setattr(wp_server, "append_turn_log", lambda line: None)
        monkeypatch.setattr(wp_server, "_send_ctrl_ready_and_state", fake_ready)
        monkeypatch.setattr(wp_server, "_schedule_session_attach_reset",
                            lambda: fired.append(1))
        ws = types.SimpleNamespace()

        async def scenario():
            await wp_server._process_session_payload(
                ws, {"type": "ctrl.hello"}, {}, "s1")

        asyncio.run(scenario())
        assert fired == [1]

    def test_handler_registered_marshal(self, monkeypatch: pytest.MonkeyPatch):
        """注册了 session_attach_handler → run_coroutine_threadsafe 进 agent 循环。"""
        marshalled = []
        monkeypatch.setattr(wp_server.asyncio, "run_coroutine_threadsafe",
                            lambda coro, loop: marshalled.append((coro, loop))
                            or types.SimpleNamespace(cancel=lambda: None))
        calls = []

        async def handler():
            calls.append(1)

        loop = types.SimpleNamespace(is_running=lambda: True)
        monkeypatch.setattr(wp_server.runtime, "session_attach_handler", handler)
        monkeypatch.setattr(wp_server.runtime, "agent_loop", loop)
        wp_server._schedule_session_attach_reset()
        assert calls == []          # 协程只被调度,web 线程不直跑 handler 体
        assert marshalled and marshalled[0][1] is loop
        coro = marshalled[0][0]
        assert coro.__name__ == "handler"   # 是 handler() 的协程
        coro.close()                # 防 "never awaited" 警告

    def test_handler_missing_falls_back_to_cancel(self, monkeypatch: pytest.MonkeyPatch):
        """旧 agent 代码没注册 handler → 回落 call_soon_threadsafe(standby_cancel_all)。"""
        rec_calls = []
        loop = types.SimpleNamespace(
            is_running=lambda: True,
            call_soon_threadsafe=lambda cb: rec_calls.append(cb))
        cancels = []
        monkeypatch.setattr(wp_server.runtime, "session_attach_handler", None)
        monkeypatch.setattr(wp_server.runtime, "agent_loop", loop)
        monkeypatch.setattr(wp_server, "standby_cancel_all",
                            lambda: cancels.append(1))
        wp_server._schedule_session_attach_reset()
        assert len(rec_calls) == 1   # marshal 恰一次进 agent 循环
        assert cancels == []         # 只调度,不在 web 线程直跑

    def test_loop_down_noop(self, monkeypatch: pytest.MonkeyPatch):
        """agent 循环未就绪 → 静默跳过(不炸 attach 握手)。"""
        monkeypatch.setattr(wp_server.runtime, "session_attach_handler", None)
        monkeypatch.setattr(wp_server.runtime, "agent_loop", None)
        wp_server._schedule_session_attach_reset()  # 不抛即过


@pytestmark_wps
class TestServerPersonaWiring:
    """webpanel.server:ctrl.persona 帧 → _schedule_session_persona → _set_session_persona 链(2026-09-22,仅 -97)。"""

    def test_set_session_persona_sanitize_and_log(self, monkeypatch: pytest.MonkeyPatch):
        logs = []
        monkeypatch.setattr(wp_server, "append_turn_log", lambda line: logs.append(line))
        monkeypatch.setattr(wp_server.runtime, "session_persona", None)
        wp_server._set_session_persona("sunny")
        assert wp_server.runtime.session_persona == "sunny"
        assert logs == ["SESSION_PERSONA_SET persona=sunny"]
        wp_server._set_session_persona("")   # 空串 = 隐藏回落路径(全局人设,面板三档化后不再发送)
        assert wp_server.runtime.session_persona is None
        assert logs[-1] == "SESSION_PERSONA_SET persona=none"
        wp_server._set_session_persona("nope")  # 非法 key 双保险 → none
        assert wp_server.runtime.session_persona is None

    def test_schedule_persona_marshals_into_agent_loop(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(wp_server, "append_turn_log", lambda line: None)
        rec = []
        loop = types.SimpleNamespace(
            is_running=lambda: True,
            call_soon_threadsafe=lambda cb, arg=None: rec.append((cb, arg)))
        monkeypatch.setattr(wp_server.runtime, "agent_loop", loop)
        monkeypatch.setattr(wp_server.runtime, "session_persona", None)
        assert wp_server._schedule_session_persona("sunny") is True
        assert len(rec) == 1
        cb, arg = rec[0]  # agent 循环线程侧执行(带 key 参数)
        cb(arg)
        assert wp_server.runtime.session_persona == "sunny"
        monkeypatch.setattr(wp_server.runtime, "agent_loop", None)
        assert wp_server._schedule_session_persona("sunny") is False

    def test_persona_frame_roundtrip_ack(self, monkeypatch: pytest.MonkeyPatch):
        """ctrl.persona 帧 → 调度 + ctrl.persona_state 原样回显(空串照回空串)。"""
        monkeypatch.setattr(wp_server, "append_turn_log", lambda line: None)
        acks = []

        class _Ws:
            async def send_str(self, s):
                acks.append(json.loads(s))

        scheduled = []

        def fake_schedule(persona):
            scheduled.append(persona)
            return True

        monkeypatch.setattr(wp_server, "_schedule_session_persona", fake_schedule)

        async def scenario():
            await wp_server._process_session_payload(
                _Ws(), {"type": "ctrl.persona", "persona": "sunny"},
                {"trace_id": "t1"}, "s1")
            await wp_server._process_session_payload(
                _Ws(), {"type": "ctrl.persona", "persona": ""},
                {"trace_id": "t2"}, "s2")

        asyncio.run(scenario())
        assert scheduled == ["sunny", ""]
        assert [a["type"] for a in acks] == ["ctrl.persona_state", "ctrl.persona_state"]
        assert acks[0]["persona"] == "sunny" and acks[0]["session_id"] == "s1"
        assert acks[1]["persona"] == ""

    def test_persona_frame_invalid_key_no_ack(self, monkeypatch: pytest.MonkeyPatch):
        """非法 key(P0 已 4400 拒,分支双保险):不调度不回显。"""
        monkeypatch.setattr(wp_server, "append_turn_log", lambda line: None)

        class _Ws:
            async def send_str(self, s):
                raise AssertionError("invalid persona must not ack")

        scheduled = []
        monkeypatch.setattr(wp_server, "_schedule_session_persona",
                            lambda p: scheduled.append(p) or True)

        async def scenario():
            await wp_server._process_session_payload(
                _Ws(), {"type": "ctrl.persona", "persona": "nope"},
                {"trace_id": "t1"}, "s1")

        asyncio.run(scenario())
        assert scheduled == []

    def test_p0_validation_persona(self):
        """_is_p0_client_frame:三档 key/空串/None 过,其余拒(4400 关连接)。"""
        def ok(p):
            return wp_server._is_p0_client_frame({"type": "ctrl.persona", "persona": p})
        for good in ("sunny", "gentle", "witty", "", None):
            assert ok(good), good
        for bad in ("nope", 123, True):
            assert not ok(bad), bad

    def test_session_config_set_and_reset(self, monkeypatch: pytest.MonkeyPatch):
        logs = []
        monkeypatch.setattr(wp_server, "append_turn_log", logs.append)
        wp_server._set_session_config("sunny", "longanlang_v3")
        assert wp_server.runtime.session_persona == "sunny"
        assert wp_server.runtime.session_persona_explicit is True
        assert wp_server.runtime.session_voice == "longanlang_v3"
        assert wp_server.runtime.session_voice_explicit is True
        wp_server._set_session_config(None, None, reset=True)
        assert wp_server.runtime.session_persona is None
        assert wp_server.runtime.session_voice is None
        assert wp_server.runtime.session_persona_explicit is False
        assert wp_server.runtime.session_voice_explicit is False
        assert logs[-1].endswith("reset=True")

    def test_p0_validation_session_config(self):
        assert wp_server._is_p0_client_frame({
            "type": "ctrl.session_config", "persona": "witty", "voice": "longxiaochun_v3"
        })
        assert wp_server._is_p0_client_frame({"type": "ctrl.session_config", "reset": True})
        for payload in (
            {"type": "ctrl.session_config", "persona": "bad", "voice": "longxiaochun_v3"},
            {"type": "ctrl.session_config", "persona": "witty", "voice": "bad"},
            {"type": "ctrl.session_config", "persona": "witty"},
            {"type": "ctrl.session_config", "reset": "yes"},
        ):
            assert not wp_server._is_p0_client_frame(payload)

    def test_session_config_frame_ack(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(wp_server, "append_turn_log", lambda line: None)
        acks = []

        class _Ws:
            async def send_str(self, s):
                acks.append(json.loads(s))

        scheduled = []
        monkeypatch.setattr(wp_server, "_schedule_session_config",
                            lambda p: scheduled.append(p) or True)

        async def scenario():
            await wp_server._process_session_payload(
                _Ws(), {"type": "ctrl.session_config", "persona": "gentle",
                        "voice": "longyan_v3"}, {"trace_id": "t1"}, "s1")

        asyncio.run(scenario())
        assert scheduled[0]["persona"] == "gentle"
        assert acks == [{"type": "ctrl.session_config_state", "trace_id": "t1",
                         "session_id": "s1", "persona": "gentle", "voice": "longyan_v3",
                         "ts_ms": acks[0]["ts_ms"]}]


# ── LLM 工具 not_addressed(2026-10-10):判定"这轮不是在跟小歌说话" ──────


@pytestmark_web
class TestNotAddressedTool:
    def test_hit_enters_standby_and_swallows(self, gates):
        """LLM 判非对话对象→静默吞轮 + enter_standby("not_addressed")。

        端侧收 standby on 帧停发音频(由真 enter_standby 广播,桩只录 via);
        工具自身不再 finalize/播报——语音轮的 user 气泡在守卫链已落。
        """
        tool = web_ui_agent.VoiceAgent.not_addressed.__get__(_turn_agent())
        assert tool.info.name == "not_addressed"  # LLM 可见 schema 就位
        assert "不是在跟你说" in tool.info.description
        with pytest.raises(web_ui_agent.StopResponse):
            asyncio.run(tool(object()))
        assert gates["enters"] == ["not_addressed"]
        assert any("NOT_ADDRESSED_HIT" in l for l in gates["logs"])
        assert gates["resets"] == 0  # 不重臂:进待命即停计时

    def test_exit_disabled_still_swallows(self, gates,
                                          monkeypatch: pytest.MonkeyPatch):
        """EXIT_ENABLE=0(退出机制总关):只吞轮不进待命——同语义退出门口径。"""
        monkeypatch.setattr(web_ui_agent, "EXIT_ENABLE", False)
        tool = web_ui_agent.VoiceAgent.not_addressed.__get__(_turn_agent())
        with pytest.raises(web_ui_agent.StopResponse):
            asyncio.run(tool(object()))
        assert gates["enters"] == []  # 不广播 standby
        assert any("NOT_ADDRESSED_HIT" in l for l in gates["logs"])  # 轨迹仍留

    def test_prompt_wiring(self):
        """提示词护栏就位:工具进 CAPABILITY_GUARD 清单 + 固定规则段有判定线。"""
        assert "not_addressed" in config_store.CAPABILITY_GUARD_INSTRUCTIONS
        assert "not_addressed" in config_store.FIXED_AGENT_INSTRUCTIONS
        # 2026-10-13 对话中旁聊碎片:GUARD 的 not_addressed 条目也覆盖
        # 对话进行中接不上的破碎碎片(DB override 只替换 FIXED,不替换 GUARD)。
        assert "对话进行中也一样" in config_store.CAPABILITY_GUARD_INSTRUCTIONS
