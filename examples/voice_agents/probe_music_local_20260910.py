"""探针(2026-09-10): 用户选了本地音乐后, "音乐停止"/"下一首"/"上一首"/"切歌" 现在到底走哪条路。

不预设立场——每个场景打印: 是否 StopResponse / 广播了什么帧 / 口播了什么 / player 调用。
跑在服务器 venv(本地缺 aiohttp/livekit)。
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

backends = types.ModuleType("app.backends")
backends.build_llm = lambda: None
backends.build_semantic_llm = lambda: None
backends.build_tts = lambda: None
backends.build_stt = lambda: None
backends.STT_BACKENDS = {}
backends.TTS_BACKENDS = {}
backends.backend_tabs_html = lambda *args, **kwargs: ""
backends.make_stt_backend = lambda *args, **kwargs: None
backends.make_tts_backend = lambda *a, **kw: None
sys.modules.setdefault("app.backends", backends)
providers_config = types.ModuleType("providers.config")
providers_config.funasr_hotwords = lambda: {}
sys.modules.setdefault("providers.config", providers_config)
silero = types.ModuleType("livekit.plugins.silero")
silero.VAD = types.SimpleNamespace(load=lambda *args, **kwargs: None)
sys.modules.setdefault("livekit.plugins.silero", silero)

import app.setup_taps as setup_taps  # noqa: E402, F401
import web_ui_agent  # noqa: E402
from livekit.agents import StopResponse  # noqa: E402
from livekit.agents.llm import ChatContext  # noqa: E402


class _FakeSession:
    def __init__(self) -> None:
        self.says: list[str] = []

    def say(self, text, *, add_to_chat_ctx=True, allow_interruptions=True):
        self.says.append(text)

    def interrupt(self, force=False):
        pass

    def generate_reply(self, *, user_input, input_modality):
        self.says.append(f"<LLM:{user_input}>")


class _FakePlayer:
    def __init__(self, *, playing=False):
        self.is_playing = playing
        self.calls: list[str] = []

    async def play_for_tool(self, music_id):
        self.calls.append(f"play_for_tool({music_id!r})")
        self.is_playing = True
        return "云端曲子"

    async def resume_last_for_tool(self):
        self.calls.append("resume_last_for_tool")
        self.is_playing = True
        return "云端曲子"

    async def stop_for_tool(self):
        self.calls.append(f"stop_for_tool(was={self.is_playing})")
        self.is_playing = False
        return self.is_playing


def _new_agent():
    session = _FakeSession()
    agent = types.SimpleNamespace(session=session, _semantic_router=None,
                                  _wiring=types.SimpleNamespace(live=None))
    for name in (
        "_music_control_intent", "_apply_turn_filters", "_maybe_handle_g3_protocol_turn",
        "_g3_frames", "_g3_validation_and_frames", "_build_g3_state",
        "_remember_g3_pending_high_risk", "_semantic_frames", "_finalize_g3_user_message",
        "_finish_consumed_voice_turn", "handle_manual_text", "_remember_executed",
        "_dispatch_g3_cmd_frames", "_local_music_frames", "_dispatch_local_music_play",
        "_dispatch_local_music_pause", "_play_cloud_music",
    ):
        setattr(agent, name, getattr(web_ui_agent.VoiceAgent, name).__get__(agent))
    agent._reset_live_transcript = lambda *a, **kw: None

    async def _noop():
        return None
    agent._maybe_apply_agent_config = _noop
    agent._chat_ctx = ChatContext.empty()
    return agent, session


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    for k in ("music_source", "music_source_pending", "music_playing_source"):
        monkeypatch.setattr(web_ui_agent.runtime, k, None)


def _probe(monkeypatch, label, text, *, src, playing_src, cloud_playing=False):
    """src=music_source 记忆, playing_src=music_playing_source, cloud_playing=云端 player 状态。"""
    async def run():
        agent, session = _new_agent()
        player = _FakePlayer(playing=cloud_playing)
        monkeypatch.setattr(web_ui_agent.runtime, "music_player", player)
        monkeypatch.setattr(web_ui_agent.runtime, "music_source", src)
        monkeypatch.setattr(web_ui_agent.runtime, "music_playing_source", playing_src)
        frames: list[dict] = []

        def fake_broadcast(msg):
            if msg.get("type") == "g3_protocol":
                frames.extend(msg.get("frames") or [])
            return True
        monkeypatch.setattr(web_ui_agent, "broadcast", fake_broadcast)

        outcome = "正常聊天"
        try:
            await agent.handle_manual_text(text)
        except StopResponse:
            outcome = "StopResponse(命令路)"
        await asyncio.sleep(0)
        cmds = [(f.get("action"), f.get("params")) for f in frames if f.get("type") == "data.cmd"]
        print(f"\n[{label}] 话术={text!r} src={src} playing_src={playing_src} cloud_playing={cloud_playing}")
        print(f"    路径={outcome}  下发帧={cmds}  口播={session.says}  player调用={player.calls}")
    asyncio.run(run())


def test_probe_local_playing_stop(monkeypatch):
    _probe(monkeypatch, "S1 本地在播·停止", "音乐停止", src="local", playing_src="local")


def test_probe_local_playing_next(monkeypatch):
    _probe(monkeypatch, "S2 本地在播·下一首", "下一首", src="local", playing_src="local")


def test_probe_local_playing_prev(monkeypatch):
    _probe(monkeypatch, "S3 本地在播·上一首", "上一首", src="local", playing_src="local")


def test_probe_local_playing_qiege(monkeypatch):
    _probe(monkeypatch, "S4 本地在播·切歌", "切歌", src="local", playing_src="local")


def test_probe_local_playing_nextge(monkeypatch):
    _probe(monkeypatch, "S5 本地在播·下一首歌", "下一首歌", src="local", playing_src="local")


def test_probe_local_memory_only_stop(monkeypatch):
    _probe(monkeypatch, "S6 仅来源记忆local·停止", "音乐停止", src="local", playing_src=None)


def test_probe_local_memory_only_next(monkeypatch):
    _probe(monkeypatch, "S7 仅来源记忆local·下一首", "下一首", src="local", playing_src=None)


def test_probe_cloud_playing_next(monkeypatch):
    _probe(monkeypatch, "S8 云端在播·下一首", "下一首", src="cloud", playing_src="cloud", cloud_playing=True)


def test_probe_cloud_playing_stop(monkeypatch):
    _probe(monkeypatch, "S9 云端在播·停止", "音乐停止", src="cloud", playing_src="cloud", cloud_playing=True)


def test_probe_local_playing_pause_word(monkeypatch):
    _probe(monkeypatch, "S10 本地在播·暂停", "暂停音乐", src="local", playing_src="local")


# ── 变体矩阵: 本地在播(src=local, playing=local)时哪些话术漏网到 LLM ──
_VARIANTS = [
    "停止音乐", "音乐停止", "别放了", "停止播放", "把音乐停了",
    "下一首", "下一首歌", "下一曲", "播放下一首", "换一首", "换首歌", "切下一首",
    "上一首", "上一首歌", "上一曲", "切歌", "切一下歌",
]


def test_probe_variant_matrix(monkeypatch):
    for text in _VARIANTS:
        _probe(monkeypatch, "V", text, src="local", playing_src="local")
