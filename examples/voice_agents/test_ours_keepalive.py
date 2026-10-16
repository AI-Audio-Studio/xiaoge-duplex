"""保活协议单测 — 2026-10-12(客户端 ctrl.ping → 服务端 ctrl.pong)。

覆盖:
- webpanel.server._is_p0_client_frame:ctrl.ping 的 ts_ms 可省;带则必须
  非负 int(bool 显式拒——bool 是 int 子类);额外字段不拒(向后兼容)。
- _send_ctrl_pong:应答帧字段(type/ts_ms 必带;合法 ts 回显 client_ts_ms;
  trace_id/session_id 有则带、None 不带)——/ws(merged)与 /ws/audio 共用。
- _process_session_payload:ctrl.ping 分发 → pong 带 session_info 的
  trace_id + 会话号。
- _process_audio_text_frame(/ws/audio 文本帧):ping→pong(只带 X-XG-Session
  头的 session_id,无 trace_id)/坏 ts 丢弃/非法 JSON 或非对象或未知类型
  忽略不关连接(音频通道是设备唯一生命线,坏帧不断音频)。
- ctrl.frontend_state 已退役(2026-10-13):服务端零唤醒/零欢迎语/零忽略
  日志,P0 校验仍放行(固件停发前删校验会 4400 断设备连接);/ws/audio 侧
  落 AUDIO_WS_TEXT_DROP。V1/V2 行为一致,见 TestAudioTextFrame 退役用例。

网关零改动:proxy 对客户端 TEXT 帧原样转发,本文件不测网关。
本地无 livekit/aiohttp → wp_server 相关整类 skip(与 test_ours_exit_standby
同款 stub 舞);服务器:cd examples/voice_agents && ../../.venv/bin/python
-m pytest test_ours_keepalive.py -q。
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

# web_ui_agent import 前的 stub 舞(与 test_ours_agent_config 一致;
# webpanel.server 传递依赖 web_ui_agent 链)。
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
    import web_ui_agent  # noqa: E402,F401  (webpanel.server 的传递依赖)
except Exception:  # pragma: no cover - 本地无 livekit
    web_ui_agent = None

try:
    import webpanel.server as wp_server  # noqa: E402
except Exception:  # pragma: no cover - 本地缺 aiohttp/webpanel 链
    wp_server = None

try:
    from app import interaction_host  # noqa: E402
except Exception:  # pragma: no cover - 本地缺 aiohttp/webpanel 链
    interaction_host = None

pytestmark_wps = pytest.mark.skipif(wp_server is None, reason="webpanel deps missing locally")


class _Ws:
    """send_str/close 录制桩;close 被调即断言失败(证明没关连接)。"""

    def __init__(self):
        self.sent = []
        self.closed = []

    async def send_str(self, s):
        self.sent.append(json.loads(s))

    async def close(self, code=None, message=b""):
        self.closed.append((code, message))
        raise AssertionError("audio text frames must never close the device lifeline")


# ── P0 校验:ctrl.ping ts_ms 口径 ────────────────────────────────────────


@pytestmark_wps
class TestPingValidation:
    def _ok(self, payload):
        return wp_server._is_p0_client_frame(payload)

    def test_ts_omitted(self):
        assert self._ok({"type": "ctrl.ping"})

    @pytest.mark.parametrize("ts", [0, 1, 1728000000000])
    def test_ts_valid(self, ts):
        assert self._ok({"type": "ctrl.ping", "ts_ms": ts})

    @pytest.mark.parametrize("ts", [-1, 1.5, "123", True, [1], {"ms": 1}])
    def test_ts_invalid(self, ts):
        assert not self._ok({"type": "ctrl.ping", "ts_ms": ts})

    def test_extra_fields_tolerated(self):
        # 额外字段不拒:端侧以后加字段不会因此被 4400(白名单只管 ts_ms 口径)
        assert self._ok({"type": "ctrl.ping", "ts_ms": 5, "tag": "watchdog"})


# ── 应答帧构造:_send_ctrl_pong ──────────────────────────────────────────


@pytestmark_wps
class TestPongFrame:
    def _pong(self, payload, trace_id=None, session_id=None):
        ws = _Ws()

        async def scenario():
            await wp_server._send_ctrl_pong(ws, payload, trace_id, session_id)

        asyncio.run(scenario())
        assert len(ws.sent) == 1
        return ws.sent[0]

    def test_echo_client_ts(self):
        pong = self._pong({"ts_ms": 1728000000123}, "t1", "s1")
        assert pong["type"] == "ctrl.pong"
        assert pong["client_ts_ms"] == 1728000000123  # 回显 → 端侧算 RTT
        assert pong["trace_id"] == "t1" and pong["session_id"] == "s1"
        assert isinstance(pong["ts_ms"], int)

    def test_no_ts_no_echo(self):
        pong = self._pong({}, "t1", "s1")
        assert "client_ts_ms" not in pong

    def test_bad_ts_no_echo(self):
        # 坏 ts 本应在 P0/分支层被拒;helper 双保险也不回显
        pong = self._pong({"ts_ms": -1}, "t1", "s1")
        assert "client_ts_ms" not in pong

    def test_ids_optional(self):
        pong = self._pong({"ts_ms": 7})
        assert pong["type"] == "ctrl.pong"
        assert "trace_id" not in pong and "session_id" not in pong


# ── merged WS(/ws)分发:_process_session_payload ────────────────────────


@pytestmark_wps
class TestSessionPingDispatch:
    def test_ping_gets_pong_with_session_context(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(wp_server, "append_turn_log", lambda line: None)
        ws = _Ws()
        session_info = {"trace_id": "trace-9", "session_id": "s-9"}

        async def scenario():
            await wp_server._process_session_payload(
                ws, {"type": "ctrl.ping", "ts_ms": 42}, session_info, "s-9"
            )

        asyncio.run(scenario())
        assert len(ws.sent) == 1
        pong = ws.sent[0]
        assert pong["type"] == "ctrl.pong"
        assert pong["trace_id"] == "trace-9" and pong["session_id"] == "s-9"
        assert pong["client_ts_ms"] == 42


# ── /ws/audio 文本帧:_process_audio_text_frame ──────────────────────────


@pytestmark_wps
class TestAudioTextFrame:
    """设备唯一生命线:坏帧只记日志,绝不 4400 关连接。"""

    def _env(self, monkeypatch: pytest.MonkeyPatch) -> list:
        logs = []
        monkeypatch.setattr(wp_server, "append_turn_log", logs.append)
        return logs

    def test_ping_pong_with_xg_session(self, monkeypatch: pytest.MonkeyPatch):
        self._env(monkeypatch)
        ws = _Ws()

        async def scenario():
            await wp_server._process_audio_text_frame(
                ws, json.dumps({"type": "ctrl.ping", "ts_ms": 9}), "sess-device"
            )

        asyncio.run(scenario())
        assert len(ws.sent) == 1
        pong = ws.sent[0]
        assert pong["type"] == "ctrl.pong"
        assert pong["session_id"] == "sess-device"   # 只带 X-XG-Session 头的会话号
        assert "trace_id" not in pong                # 音频通道无 trace 上下文
        assert pong["client_ts_ms"] == 9

    def test_ping_without_session_header(self, monkeypatch: pytest.MonkeyPatch):
        self._env(monkeypatch)
        ws = _Ws()

        async def scenario():
            await wp_server._process_audio_text_frame(
                ws, json.dumps({"type": "ctrl.ping"}), None
            )

        asyncio.run(scenario())
        assert [p["type"] for p in ws.sent] == ["ctrl.pong"]
        assert "session_id" not in ws.sent[0]
        assert "client_ts_ms" not in ws.sent[0]

    def test_ping_bad_ts_dropped(self, monkeypatch: pytest.MonkeyPatch):
        logs = self._env(monkeypatch)
        ws = _Ws()

        async def scenario():
            await wp_server._process_audio_text_frame(
                ws, json.dumps({"type": "ctrl.ping", "ts_ms": -5}), "s1"
            )

        asyncio.run(scenario())
        assert ws.sent == [] and ws.closed == []
        assert any("AUDIO_WS_PING_DROP reason=bad_ts" in l for l in logs)

    def test_bad_json_ignored_not_closed(self, monkeypatch: pytest.MonkeyPatch):
        logs = self._env(monkeypatch)
        ws = _Ws()

        async def scenario():
            await wp_server._process_audio_text_frame(ws, "not json{", "s1")

        asyncio.run(scenario())
        assert ws.sent == [] and ws.closed == []
        assert any("AUDIO_WS_TEXT_DROP reason=bad_json" in l for l in logs)

    def test_non_object_json_ignored(self, monkeypatch: pytest.MonkeyPatch):
        logs = self._env(monkeypatch)
        ws = _Ws()

        async def scenario():
            await wp_server._process_audio_text_frame(ws, "[1,2]", "s1")

        asyncio.run(scenario())
        assert ws.sent == [] and ws.closed == []
        assert any("reason=not_object" in l for l in logs)

    def test_unknown_type_ignored(self, monkeypatch: pytest.MonkeyPatch):
        """合法 P0 帧但不在音频通道白名单(data.text 只属 merged WS)→ 忽略。"""
        logs = self._env(monkeypatch)
        ws = _Ws()

        async def scenario():
            await wp_server._process_audio_text_frame(
                ws, json.dumps({"type": "data.text", "text": "hi"}), "s1"
            )

        asyncio.run(scenario())
        assert ws.sent == [] and ws.closed == []
        assert any("AUDIO_WS_TEXT_DROP type=data.text" in l for l in logs)

    def test_oversize_dropped(self, monkeypatch: pytest.MonkeyPatch):
        logs = self._env(monkeypatch)
        ws = _Ws()
        raw = "x" * (wp_server._FRAME_JSON_MAX_BYTES + 1)

        async def scenario():
            await wp_server._process_audio_text_frame(ws, raw, "s1")

        asyncio.run(scenario())
        assert ws.sent == [] and ws.closed == []
        assert any("reason=oversize" in l for l in logs)

    def test_p0_validation_still_accepts_frontend_state(self):
        """帧已退役(2026-10-13)但 P0 校验仍放行:固件停发前删校验会让
        merged WS 对连接即发的 seq:1 帧 4400 断连;seq/ttl_ms 负值仍拒。"""
        assert wp_server._is_p0_client_frame(
            {"type": "ctrl.frontend_state", "seq": 1, "ttl_ms": 60000}
        )
        assert not wp_server._is_p0_client_frame(
            {"type": "ctrl.frontend_state", "seq": -1, "ttl_ms": 60000}
        )
        assert not wp_server._is_p0_client_frame(
            {"type": "ctrl.frontend_state", "seq": 1, "ttl_ms": -1}
        )

    @pytest.mark.parametrize("v2", [True, False])
    def test_frontend_state_wake_frame_narrow(
        self, monkeypatch: pytest.MonkeyPatch, v2: bool
    ):
        """唤醒帧窄处理(2026-10-13 按键死锁修复 A):awake+authoritative+
        button 帧带 P0 必填字段(seq/ttl_ms)——V2 marshal v2_wake_on_input
        回 agent 循环(不 exit、不关生命线、不落 DROP);V1 仍零动作落
        AUDIO_WS_TEXT_DROP(V1 按键唤醒回滚须还原 .bak_20261012_v2mode 的
        server.py)。"""
        logs = self._env(monkeypatch)
        if interaction_host is not None:  # 钉死开关:行为随模式分流
            monkeypatch.setattr(interaction_host, "_MODE_V2", v2)
        monkeypatch.setattr(wp_server.runtime, "standby", True)
        marshalled = []
        loop = types.SimpleNamespace(
            is_running=lambda: True,
            call_soon_threadsafe=lambda cb, arg=None: marshalled.append((cb, arg)),
        )
        monkeypatch.setattr(wp_server.runtime, "agent_loop", loop)
        from app import standby_host
        exits = []
        monkeypatch.setattr(standby_host, "exit_standby", lambda via: exits.append(via))
        ws = _Ws()

        async def scenario():
            await wp_server._process_audio_text_frame(
                ws,
                json.dumps(
                    {
                        "type": "ctrl.frontend_state",
                        "wake_state": "awake",
                        "trust_level": "authoritative",
                        "wake_event": "button",
                        "seq": 2,
                        "ttl_ms": 1000,
                    }
                ),
                "s1",
            )

        asyncio.run(scenario())
        assert ws.sent == [] and ws.closed == []
        assert exits == []
        if v2:
            assert len(marshalled) == 1
            cb, arg = marshalled[0]
            assert cb.__name__ == "v2_wake_on_input" and arg == "frontend_button"
            assert any("V2_FRONTEND_WAKE_ACCEPTED wake_event=button src=audio"
                       in l for l in logs)
            assert not any("AUDIO_WS_TEXT_DROP" in l for l in logs)
        else:
            assert marshalled == []
            assert not any("V2_FRONTEND_WAKE_ACCEPTED" in l for l in logs)
            assert any("AUDIO_WS_TEXT_DROP type=ctrl.frontend_state" in l for l in logs)
