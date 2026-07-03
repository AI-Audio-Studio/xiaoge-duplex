"""Web UI test panel for the Qwen/FunASR voice agent.

Usage (identical to original, but auto-opens a browser):
    python web_ui_agent.py console

Browser UI at http://localhost:8765 (override with WEB_UI_PORT env var):
  - Real-time conversation log
  - Microphone mute/unmute
  - ASR backend switch (FunASR ↔ Qwen3-ASR) — takes effect on the NEXT utterance

ASR switching works by replacing the inner backend inside SwitchableSTT without
restarting the AgentSession or StreamAdapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from dataclasses import replace
from pathlib import Path

import aiohttp
import aiohttp.web
import httpx
import openai
from audio_recorder import AudioRecorder
from common.config_utils import env_bool as _env_bool
from common.runtime import (
    append_turn_log as _append_turn_log,
    configure_utf8_stdio as _configure_utf8_stdio,
    ms as _ms,
)
from common.text_rules import (
    ACK_STRIP_RE as _ACK_STRIP_RE,
    LEADING_PUNCT_RE as _LEADING_PUNCT_RE,
    OVERLAP_ACK_CHARS as _OVERLAP_ACK_CHARS,
    is_backchannel as _is_backchannel,
    is_overlap_ack as _is_overlap_ack,
    normalize_spoken_digit_sequence as _normalize_spoken_digit_sequence,
    should_ignore_user_turn as _should_ignore_user_turn,
)
from dotenv import load_dotenv
from kws_interrupt import KwsConfig, KwsTapAudioInput, NativeKwsSpotter, _unavailable_reason
from listening_mode import AutoDecision, ListeningController, ListeningEvent
from live_transcript import LiveTranscript, LiveTranscriptConfig
from mute_gate import MuteGate
from online_interrupt import (
    OnlineAsrTap,
    OnlineInterruptConfig,
    OnlineTapAudioInput,
    unavailable_reason as _online_unavailable_reason,
)
from providers import (
    CosyVoiceStreamingTTS,
    FunASROfflineSTT,
    HttpStreamingTTS,
    Qwen3ASROfflineSTT,
    QwenStreamingTTS,
)
from providers.config import funasr_hotwords as _funasr_hotwords
from text_sanitizer import sanitize_stream, strip_markdown
from turn_config import TurnConfig

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    APIConnectOptions,
    JobContext,
    JobProcess,
    LanguageCode,
    StopResponse,
    cli,
    stt as agents_stt,
    tts,
    utils as lk_utils,
)
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.agents.stt.stream_adapter import StreamAdapter
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr
from livekit.agents.voice import io
from livekit.plugins import openai as lk_openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

logger = logging.getLogger("web-ui-agent")
load_dotenv(override=True)

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# 注:全量 DEBUG 日志已整合进测试工具(AGENT_TIMELINE=1 时写 runs/<ts>/debug.log,
# 见 event_timeline.install_debug_log)。正常运行不再挂任何文件日志处理器(零开销),
# 也不再写 .run/agent.log。

# 文本判定规则(停止词/附和/压话确认/数字归一化)在 common.text_rules,顶部按 `_` 前缀别名引入。
# 在线软打断的 VAD 佐证宽限(秒):VAD 刚停说话后这段时间内仍接受打断(容忍识别滞后)。
_ONLINE_VAD_GRACE = float(os.getenv("XIAOGE_ONLINE_VAD_GRACE", "0.6"))
# 跨对象共享标志:用户当前这句话是否压着 AI 播报开口(附和拒识的上下文闸门,per-app 可变状态)。
_overlap_turn_state: dict[str, bool] = {"user_spoke_over_agent": False}

_configure_utf8_stdio()


# ─── SwitchableSTT ───────────────────────────────────────────────────────────


class SwitchableSTT(agents_stt.STT):
    """STT proxy supporting runtime backend switching and mute.

    The StreamAdapter holds a reference to this object. Swapping `_backend`
    here is enough — the next recognition call will use the new backend.
    """

    def __init__(self, initial_backend: agents_stt.STT) -> None:
        super().__init__(
            capabilities=agents_stt.STTCapabilities(
                streaming=False,
                interim_results=False,
                aligned_transcript=False,
                offline_recognize=True,
            )
        )
        self._backend: agents_stt.STT = initial_backend
        self.muted: bool = False

    @property
    def model(self) -> str:
        return self._backend.model

    @property
    def provider(self) -> str:
        return self._backend.provider

    def switch_backend(self, new_backend: agents_stt.STT) -> agents_stt.STT:
        """Swap backend atomically (GIL-safe). Returns old backend for cleanup."""
        old = self._backend
        self._backend = new_backend
        return old

    async def _recognize_impl(
        self,
        buffer: lk_utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> agents_stt.SpeechEvent:
        if self.muted:
            return agents_stt.SpeechEvent(
                type=agents_stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[agents_stt.SpeechData(language=LanguageCode("zh"), text="")],
            )
        backend = self._backend
        try:
            return await backend._recognize_impl(
                buffer,
                language=language,
                conn_options=conn_options,
            )
        except Exception as exc:
            # A failing backend (e.g. just switched to an unreachable ASR server)
            # MUST NOT propagate: the exception would tear down the StreamAdapter
            # recognition stream and the agent would go permanently deaf, even
            # after switching back. Swallow it and return an empty transcript so
            # the pipeline stays alive and a subsequent backend switch recovers.
            logger.warning(
                "STT backend %s recognize failed (returning empty): %s",
                getattr(backend, "provider", backend),
                exc,
            )
            return agents_stt.SpeechEvent(
                type=agents_stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[agents_stt.SpeechData(language=LanguageCode("zh"), text="")],
            )

    async def prewarm_connection(self) -> None:
        if hasattr(self._backend, "prewarm_connection"):
            await self._backend.prewarm_connection()

    async def aclose(self) -> None:
        await self._backend.aclose()


# ─── SwitchableTTS ───────────────────────────────────────────────────────────


class SwitchableTTS(tts.TTS):
    """TTS proxy supporting runtime backend switching.

    AgentSession holds a reference to this object. Swapping `_backend`
    here is enough — the next synthesis call will use the new backend.
    """

    def __init__(
        self, initial_backend: QwenStreamingTTS | HttpStreamingTTS | CosyVoiceStreamingTTS
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=initial_backend.sample_rate,
            num_channels=initial_backend.num_channels,
        )
        self._backend: QwenStreamingTTS | HttpStreamingTTS | CosyVoiceStreamingTTS = initial_backend
        self._backend.on("error", self._on_backend_error)

    def _on_backend_error(self, error: object) -> None:
        # Re-emit the active backend's TTS errors on this proxy. The framework
        # subscribes to errors on the proxy (it never sees the backend object),
        # so without this a backend failure (e.g. switched to an unreachable TTS)
        # would bypass the framework's resilient TTS-error handling. Connection
        # errors are recoverable -> the session logs and continues; switching back
        # to a working backend recovers. Symmetric to SwitchableSTT's isolation.
        self.emit("error", error)

    @property
    def provider(self) -> str:
        return self._backend.provider

    def switch_backend(
        self, new_backend: QwenStreamingTTS | HttpStreamingTTS | CosyVoiceStreamingTTS
    ) -> QwenStreamingTTS | HttpStreamingTTS | CosyVoiceStreamingTTS:
        old = self._backend
        try:
            old.off("error", self._on_backend_error)
        except Exception:
            pass
        new_backend.on("error", self._on_backend_error)
        self._backend = new_backend
        return old

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return self._backend.synthesize(text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.SynthesizeStream:
        return self._backend.stream(conn_options=conn_options)

    def prewarm_connection(self) -> None:
        if hasattr(self._backend, "prewarm_connection"):
            self._backend.prewarm_connection()

    async def aclose(self) -> None:
        try:
            self._backend.off("error", self._on_backend_error)
        except Exception:
            pass
        await self._backend.aclose()


# ─── Web server globals ───────────────────────────────────────────────────────

_WEB_PORT = int(os.getenv("WEB_UI_PORT", "8765"))
_WEB_HOST = os.getenv("WEB_UI_HOST", "localhost")
_ws_clients: set[aiohttp.web.WebSocketResponse] = set()
_ws_primary_client: aiohttp.web.WebSocketResponse | None = None
_connection_lock: asyncio.Lock | None = None
_web_loop: asyncio.AbstractEventLoop | None = None
_agent_loop: asyncio.AbstractEventLoop | None = None
_switchable_stt: SwitchableSTT | None = None
_switchable_tts: SwitchableTTS | None = None
_test_recorder = None  # 测试模式下的多轨录音器(供 /api/mic 暂停/继续录制)
_mute_gate = None  # 输入源头静音门(关麦=真关麦,供 /api/mic 切换)
_session = None  # AgentSession 引用(供模块级聆听助手在 agent 循环里 say/收尾)
_listen_ctrl: ListeningController | None = None  # 聆听模式控制器(纯状态机)
_listen_ttl_handle = None  # 聆听临时内容 TTL 定时器句柄(asyncio,agent 循环)
_listen_guard_until = 0.0  # 退出提示保护窗截止(monotonic):此前用户语音不得打断小歌
_LISTEN_GUARD_S = 6.0  # 退出"要整理吗"播报的打断保护时长(秒)
_listen_drain_until = 0.0  # 退出尾巴标记的安全时限(monotonic):此前第一条 final 视作退出尾巴
_listen_exit_pending = False  # 一次性:退出后等"尾巴 final"(含唤醒词那条),消费一次后清
_tts_backend_key: str = "cosyvoice"

_WEB_AUDIO: bool = _env_bool("WEB_AUDIO", False)
_SSL_CERT: str = os.getenv("WEB_SSL_CERT", "")
_SSL_KEY: str = os.getenv("WEB_SSL_KEY", "")
_audio_ws_clients: set[aiohttp.web.WebSocketResponse] = set()
_audio_ws_primary_client: aiohttp.web.WebSocketResponse | None = None
_ws_audio_input_ref = None  # set to WebSocketAudioInput when WEB_AUDIO=1
_ws_audio_output_ref = None  # set to WebSocketAudioOutput when WEB_AUDIO=1
_VOICE_WELCOME = "连接成功，欢迎使用小歌，请开始说话。"
_BUSY_MESSAGE = "服务器繁忙，请稍后再试！"

_BUSY_HTML = f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_BUSY_MESSAGE}</title>
<style>
html,body{{height:100%;margin:0}}
body{{display:flex;align-items:center;justify-content:center;background:#fff7f3;color:#9a3412;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.busy{{font-size:42px;font-weight:800;line-height:1.3;text-align:center;padding:32px}}
@media (max-width:640px){{.busy{{font-size:30px}}}}
</style>
</head>
<body><main class="busy">{_BUSY_MESSAGE}</main></body>
</html>
"""

# ─── HTML page (embedded) ────────────────────────────────────────────────────

_HTML = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>小歌 · 全双工语音交互引擎</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#FFFFFF;color:#1F2024;
  height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{display:flex;align-items:center;gap:9px;padding:12px 18px;border-bottom:0.5px solid #ECECEF;flex-shrink:0}
.logo{width:26px;height:26px;border-radius:8px;background:#E86A43;color:#fff;display:flex;
  align-items:center;justify-content:center;font-size:14px;font-weight:500}
h1{font-size:15px;font-weight:500;color:#1F2024}
.sub{font-size:12px;color:#9CA3AF}
.statepill{margin-left:auto;display:inline-flex;align-items:center;gap:6px;font-size:12px;
  color:#15803D;background:#E7F6EF;padding:4px 12px;border-radius:999px}
.dot{width:7px;height:7px;border-radius:50%;background:#9CA3AF;transition:background .3s}
.dot.ok{background:#22C55E}.dot.speak{background:#F59E0B;animation:blink 1s infinite}
.dot.off{background:#DC2626}.dot.susp{background:#F59E0B}
.statepill.off{color:#DC2626;background:#FDECEC}
.statepill.susp{color:#B45309;background:#FEF3E2}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
main{flex:1;display:flex;min-height:0}
.left{flex:1;display:flex;flex-direction:column;min-width:0;position:relative}
.log{flex:1;overflow-y:auto;padding:16px 22px;display:flex;flex-direction:column;gap:13px;background:#FBFBFC}
.bubble{position:relative;max-width:min(78%,560px);padding:9px 13px;border-radius:13px;
  font-size:14px;line-height:1.6;word-break:break-word}
.bubble .ts{font-size:11px;margin-top:4px;color:#B6B8BE}
.bubble.assistant{align-self:flex-start;margin-left:36px;background:#FFFFFF;border:0.5px solid #EAEAEE;
  border-bottom-left-radius:4px}
.bubble.user{align-self:flex-end;margin-right:36px;background:#E86A43;color:#fff;border-bottom-right-radius:4px}
.bubble.user .ts{color:#F6D3C6}
.bubble.assistant::before{content:"歌";position:absolute;left:-36px;bottom:0;width:28px;height:28px;
  border-radius:50%;background:#FBEEE8;color:#E86A43;display:flex;align-items:center;justify-content:center;font-size:12px}
.bubble.user::after{content:"我";position:absolute;right:-36px;bottom:0;width:28px;height:28px;
  border-radius:50%;background:#ECEDF0;color:#6B7280;display:flex;align-items:center;justify-content:center;font-size:12px}
.bubble.user.live{background:#FDF1EC;border:1px dashed #F2C3B0;color:#9A3C1E}
.bubble.user.live .ts{color:#C9A08F}
.live-dots i{animation:liveBlink 1.2s infinite}
.live-dots i:nth-child(2){animation-delay:.2s}.live-dots i:nth-child(3){animation-delay:.4s}
@keyframes liveBlink{0%,100%{opacity:.25}50%{opacity:1}}
.bubble.appear{animation:bubbleIn .18s ease-out}
@keyframes bubbleIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.sys-msg{align-self:center;font-size:11px;color:#B6B8BE;padding:2px 10px}
.dock{display:flex;align-items:center;gap:10px;padding:11px 16px;border-top:0.5px solid #ECECEF;background:#fff;flex-shrink:0;position:relative;z-index:25}
.manual{flex:1;min-width:0;height:44px;border:1px dashed #D6D7DB;border-radius:10px;padding:0 13px;
  color:#9CA3AF;font-size:13px;background:#fff;font-family:inherit}
.manual::placeholder{color:#B6B8BE}
.rnd{width:44px;height:44px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  cursor:pointer;flex-shrink:0;border:none;background:transparent}
#micBtn{background:#E86A43;color:#fff}
#micBtn.off{background:#FDECEC;color:#DC2626;border:1px solid #F4C9C9}
.ico-on,.ico-off{display:inline-flex;align-items:center;justify-content:center}
#micBtn .ico-off{display:none}#micBtn.off .ico-on{display:none}#micBtn.off .ico-off{display:inline-flex}
#spkBtn{display:none;background:#fff;color:#6B7280;border:1px solid #D6D7DB}
#spkBtn.on{background:#E86A43;color:#fff;border-color:#E86A43}
#spkBtn.err{background:#FEF2F2;color:#B91C1C;border-color:#FECACA}
.cfg-empty{flex:1;display:flex;align-items:center;justify-content:center;border:1px dashed #E2E3E7;border-radius:10px;color:#B6B8BE;font-size:12px;margin-top:12px}
/* 聆听遮罩:像盖一层纱,完整覆盖会话显示区(底部 dock 通话键浮于其上仍可点)。
   顶部横幅跟随会话区宽度(align-items:stretch 撑满),提示单行不换行。 */
#listenMask{position:absolute;inset:0;z-index:20;display:none;flex-direction:column;align-items:stretch;background:rgba(255,247,243,.80);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}
.listen-card{margin:16px;padding:15px 22px;display:flex;flex-direction:column;align-items:center;gap:8px;background:rgba(255,255,255,.93);border:0.5px solid #F2C6B4;border-radius:14px;box-shadow:0 8px 26px rgba(31,32,36,.12)}
.listen-title{font-size:22px;font-weight:700;color:#C2410C;display:flex;align-items:center;gap:11px}
.listen-dot{width:13px;height:13px;border-radius:50%;background:#E86A43;animation:blink 1s infinite;flex-shrink:0}
.listen-hint{font-size:16px;color:#9A6A52;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.right{width:280px;flex-shrink:0;border-left:0.5px solid #ECECEF;background:#FCFCFD;
  padding:16px;overflow-y:auto;display:flex;flex-direction:column;gap:16px}
.cfg-title{font-size:13px;font-weight:500;color:#1F2024}
.cfg-label{font-size:11px;color:#9CA3AF;margin-bottom:7px}
.tabs{display:flex;flex-wrap:wrap;gap:6px}
.asr-tab{font-size:12px;padding:6px 11px;border-radius:8px;border:none;background:#F3F4F6;
  color:#6B7280;cursor:pointer;font-family:inherit}
.asr-tab.on{background:#FBEEE8;color:#9A3C1E}
.exp-tag{font-size:10px;color:#9A3C1E;background:#FBEEE8;padding:1px 6px;border-radius:6px}
.param-row{font-size:12px;color:#6B7280;display:flex;justify-content:space-between;margin-bottom:4px}
.track{height:4px;background:#EDEEF1;border-radius:999px;position:relative;margin-bottom:12px}
.track i{position:absolute;left:0;top:0;height:4px;width:62%;background:#E86A43;border-radius:999px}
.track b{position:absolute;left:60%;top:-5px;width:14px;height:14px;border-radius:50%;background:#fff;border:2px solid #E86A43}
.hint{font-size:11px;color:#B6B8BE}
#clearBtn{margin-top:auto;border:none;background:transparent;color:#6B7280;font-size:13px;cursor:pointer;
  display:inline-flex;align-items:center;gap:6px;padding:11px 0 0;border-top:0.5px solid #ECECEF;font-family:inherit}
footer{text-align:center;padding:9px 16px;border-top:0.5px solid #ECECEF;font-size:11px;color:#B6B8BE;flex-shrink:0}
.sbar{display:none}
</style>
</head>
<body>
<header>
  <span class="logo">歌</span>
  <h1>小歌</h1>
  <span class="sub">· 全双工语音交互引擎</span>
  <span class="statepill" id="statepill"><span class="dot" id="dot"></span><span id="badge">连接中…</span></span>
</header>
<main>
  <div class="left">
    <div class="log" id="log"></div>
    <div class="dock">
      <input class="manual" disabled placeholder="手动输入消息 · 即将上线">
      <button class="rnd" id="micBtn" onclick="toggleMic()" aria-label="录音中,点击关麦">
        <span class="ico-on"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/><path d="M8 21h8"/></svg></span>
        <span class="ico-off"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/><path d="M8 21h8"/><path d="M4 4l16 16"/></svg></span>
      </button>
      <button class="rnd" id="spkBtn" onclick="toggleVoice()" aria-label="连接语音通话" title="连接语音通话">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.79 19.79 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.12.9.32 1.77.59 2.61a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.47-1.16a2 2 0 0 1 2.11-.45c.84.27 1.71.47 2.61.59A2 2 0 0 1 22 16.92z"/></svg>
      </button>
    </div>
    <div id="listenMask" style="display:none">
      <div class="listen-card">
        <div class="listen-title"><span class="listen-dot"></span>聆听中</div>
        <div id="listenHint" class="listen-hint"></div>
      </div>
    </div>
  </div>
  <div class="right">
    <div class="cfg-title">配置</div>
    <div class="cfg-empty">待补充</div>
    <button id="clearBtn" onclick="clearLog()"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13"/><path d="M9 7V4h6v3"/></svg>清空记录</button>
  </div>
</main>
<footer>© 2026 小歌 Duplex · ATC- AI音频研发部 · 内部测试面板</footer>
<!-- 隐藏状态镜像 + 配置控件(暂从界面移除,保留以维持 JS 现状,后续可放回面板) -->
<div class="sbar">
  <span id="sbWs"></span><span id="sbMic"></span><span id="sbAsr"></span><span id="sbTts"></span><span id="sbVoice"></span><span id="sbMsgs"></span>
  <button id="tabFunasr" onclick="switchASR('funasr')"></button>
  <button id="tabQwen3" onclick="switchASR('qwen3')"></button>
  <button id="tabQwen3Stream" onclick="switchASR('qwen3-stream')"></button>
  <button id="tabTtsCosy" onclick="switchTTS('cosyvoice')"></button>
  <button id="tabTtsQwen" onclick="switchTTS('qwen')"></button>
  <button id="tabTtsHttp" onclick="switchTTS('http')"></button>
</div>
<script>
var ws=null, muted=false, msgN=0, curAsr='funasr', curTts='cosyvoice', rt=null, serverBusy=false;
var VOICE_WELCOME='连接成功，欢迎使用小歌，请开始说话。';
var wsProto = location.protocol==='https:' ? 'wss:' : 'ws:';
var wsAudio=null, micStream=null, audioCtx=null, playCtx=null, voiceActive=false;
var nextPlayTime=0, AUDIO_SR=16000, scheduledSources=[];
var liveBubble=null, liveTimer=null;       // 用户实时转写的单一 live 气泡
var LIVE_DANGLING_MS=4000;                  // 无定稿的残留气泡兜底淡出(毫秒)

function conn(){
  if(serverBusy) return;
  if(ws && ws.readyState===WebSocket.OPEN) return;
  ws = new WebSocket(wsProto+'//'+location.host+'/ws');
  ws.onopen = function(){
    stConn=true; updateStatus();
    sysMsg('已连接到语音助手');
    if(rt){clearTimeout(rt);rt=null;}
  };
  ws.onclose = function(){
    stConn=false; updateStatus();
    if(serverBusy) return;
    sysMsg('连接断开，5 秒后重连…');
    rt = setTimeout(conn, 5000);
  };
  ws.onmessage = function(e){ handle(JSON.parse(e.data)); };
}

function handle(m){
  if(m.type==='busy'){
    serverBusy=true;
    sysMsg(m.message || '服务器繁忙，请稍后再试！');
    try{ if(ws) ws.close(); }catch(x){}
    stopVoice();
    return;
  }
  if(m.type==='clear'){ clearPlayback(); return; }
  if(m.type==='listening'){ setListening(m.on, m.hint); }
  if(m.type==='user_speaking' && m.state==='start') startLive();
  if(m.type==='user_partial') updateLive(m.text);
  if(m.type==='message'){
    if(m.role==='user' && liveBubble) finalizeLive(m.text, m.ts);  // live 气泡定稿(看似修正)
    else addMsg(m.role, m.text, m.ts);
  }
  if(m.type==='state'){
    if(m.muted       !== undefined) setMic(m.muted);
    if(m.stt_backend !== undefined) setAsr(m.stt_backend);
    if(m.tts_backend !== undefined) setTts(m.tts_backend);
    if(m.agent_state !== undefined) setAgent(m.agent_state);
    if(m.audio_mode !== undefined && m.audio_mode && !voiceActive){
      id('spkBtn').style.display='flex';
      id('spkBtn').title='连接语音通话';
      id('sbVoice').textContent='Voice: disconnected';
      sysMsg('点击通话按钮连接浏览器麦克风和播放声音');
    }
    if(m.audio_mode !== undefined && !m.audio_mode){
      id('spkBtn').style.display='none';
    }
  }
}

// ── 用户实时转写:单一 live 气泡(开口出现、partial 边长、final 定稿)──────────
// 一次连续说话 = 一个气泡:startLive 只在"新一轮"被后端调用(见 live_transcript.py);
// 连续说话的小停顿不会重开。无定稿的残留气泡由超时兜底丢弃。

// Browser voice bridge: microphone PCM -> /ws/audio, TTS PCM <- /ws/audio.
async function toggleVoice(){
  if(voiceActive){ stopVoice(); return; }
  await startVoice();
}

async function startVoice(){
  try{
    sysMsg('正在连接浏览器通话...');
    playCtx = new AudioContext({sampleRate:AUDIO_SR});
    if(playCtx.state === 'suspended') await playCtx.resume();

    wsAudio = new WebSocket(wsProto+'//'+location.host+'/ws/audio');
    wsAudio.binaryType = 'arraybuffer';
    wsAudio.onopen = async function(){
      nextPlayTime = 0;
      setVoiceActive(true);
      sysMsg('通话已连接，正在请求麦克风权限...');
      try{
        micStream = await navigator.mediaDevices.getUserMedia({audio:{
          channelCount:1, echoCancellation:true, noiseSuppression:true, autoGainControl:true
        }});
        audioCtx = new AudioContext({sampleRate:AUDIO_SR});
        if(audioCtx.state === 'suspended') await audioCtx.resume();
        var src = audioCtx.createMediaStreamSource(micStream);
        var workletSrc = `class P extends AudioWorkletProcessor{process(i){var c=i[0]&&i[0][0];if(c){var b=new Int16Array(c.length);for(var j=0;j<c.length;j++)b[j]=Math.max(-32768,Math.min(32767,c[j]*32767));this.port.postMessage(b.buffer,[b.buffer])}return true}}registerProcessor('p',P);`;
        var blobUrl = URL.createObjectURL(new Blob([workletSrc],{type:'application/javascript'}));
        await audioCtx.audioWorklet.addModule(blobUrl);
        URL.revokeObjectURL(blobUrl);
        var node = new AudioWorkletNode(audioCtx,'p');
        node.port.onmessage = function(e){ if(wsAudio && wsAudio.readyState===WebSocket.OPEN && !muted) wsAudio.send(e.data); };
        src.connect(node);
        sysMsg(VOICE_WELCOME);
      }catch(err){
        setVoiceError('麦克风连接失败');
        sysMsg('麦克风连接失败：'+err.message+'。声音播放仍已连接。');
      }
    };
    wsAudio.onmessage = function(e){
      if(e.data instanceof ArrayBuffer){ playPcm(e.data); }
      else{ try{ var m=JSON.parse(e.data); if(m.type==='clear') clearPlayback(); else if(m.type==='busy'){ sysMsg(m.message || '服务器繁忙，请稍后再试！'); stopVoice(); } }catch(x){} }
    };
    wsAudio.onclose = function(){ wsAudio=null; cleanupVoice(); };
    wsAudio.onerror = function(){ setVoiceError('语音连接异常'); stopVoice(); };
  }catch(err){
    setVoiceError('语音连接失败');
    sysMsg('语音连接失败：'+err.message);
    stopVoice();
  }
}

function playPcm(buf){
  if(!playCtx) return;
  var i16=new Int16Array(buf), f32=new Float32Array(i16.length);
  for(var i=0;i<i16.length;i++) f32[i]=i16[i]/32767;
  var ab=playCtx.createBuffer(1,f32.length,AUDIO_SR);
  ab.copyToChannel(f32,0);
  var s=playCtx.createBufferSource(); s.buffer=ab; s.connect(playCtx.destination);
  var now=playCtx.currentTime;
  if(nextPlayTime < now) nextPlayTime = now + 0.05;
  s.start(nextPlayTime);
  nextPlayTime += ab.duration;
  scheduledSources.push(s);
  s.onended = function(){ var i=scheduledSources.indexOf(s); if(i>=0) scheduledSources.splice(i,1); };
}
function clearPlayback(){ scheduledSources.forEach(function(s){ try{s.stop(0);}catch(x){} }); scheduledSources=[]; nextPlayTime=0; }
function cleanupVoice(){
  clearPlayback();
  if(micStream){ micStream.getTracks().forEach(function(t){t.stop();}); micStream=null; }
  if(audioCtx){ try{audioCtx.close();}catch(x){} audioCtx=null; }
  if(playCtx){ try{playCtx.close();}catch(x){} playCtx=null; }
  setVoiceActive(false);
}
function stopVoice(){
  if(wsAudio){ var w=wsAudio; wsAudio=null; try{w.close();}catch(x){} }
  cleanupVoice();
}
function setVoiceActive(a){
  voiceActive=a;
  var b=id('spkBtn');
  if(!b) return;
  b.className='rnd'+(a?' on':'');
  b.setAttribute('aria-label', a?'断开语音通话':'连接语音通话');
  b.title=a?'断开语音通话':'连接语音通话';
  id('sbVoice').textContent='Voice: '+(a?'connected':'disconnected');
}
function setVoiceError(t){ var b=id('spkBtn'); if(b){ b.className='rnd err'; b.title=t; } id('sbVoice').textContent='Voice: '+t; }

function startLive(){
  if(liveBubble){ armLive(); return; }   // Bug2 修复:已有正在涨的气泡→续用,不清空重建(防中途消失)
  liveBubble=document.createElement('div');
  liveBubble.className='bubble user live appear';
  liveBubble.innerHTML='<div class="live-txt"><span class="live-dots">聆听中<i>.</i><i>.</i><i>.</i></span></div>';
  log().appendChild(liveBubble); log().scrollTop=log().scrollHeight;
  armLive();
}
function updateLive(text){
  if(!liveBubble) startLive();
  if(text){ liveBubble.querySelector('.live-txt').textContent=text; }
  log().scrollTop=log().scrollHeight;
  armLive();
}
function finalizeLive(text, ts){
  if(!liveBubble){ addMsg('user', text, ts); return; }
  if(liveTimer){ clearTimeout(liveTimer); liveTimer=null; }
  liveBubble.className='bubble user';
  var t=ts ? new Date(ts*1000).toLocaleTimeString('zh-CN') : '';
  liveBubble.innerHTML='<div>'+esc(text||'')+'</div><div class="ts">'+t+'</div>';
  liveBubble=null;
  id('sbMsgs').textContent=(++msgN)+' 条消息';
  log().scrollTop=log().scrollHeight;
}
function discardLive(){
  if(liveTimer){ clearTimeout(liveTimer); liveTimer=null; }
  if(liveBubble){ liveBubble.remove(); liveBubble=null; }
}
function armLive(){
  if(liveTimer) clearTimeout(liveTimer);
  liveTimer=setTimeout(discardLive, LIVE_DANGLING_MS);
}

// 状态优先级:服务断开 > 挂起中(关麦)> 正常 agent 状态
var stConn=true, stMuted=false, stAgent='';
function updateStatus(){
  var pill=id('statepill'), dot=id('dot'), bg=id('badge');
  if(!stConn){ pill.className='statepill off'; dot.className='dot off'; bg.textContent='服务断开'; return; }
  if(stMuted){ pill.className='statepill susp'; dot.className='dot susp'; bg.textContent='挂起中'; return; }
  var labels={idle:'空闲', listening:'监听中', thinking:'思考中', speaking:'播报中'};
  pill.className='statepill';
  dot.className='dot '+(stAgent==='speaking'?'speak':'ok');
  bg.textContent = labels[stAgent] || stAgent || '已连接';
}
function setAgent(s){ stAgent=s; updateStatus(); }

function setMic(m){
  stMuted=m;
  id('micBtn').className='rnd'+(m?' off':'');   // 图标(开/带斜线关)由 CSS 切换,勿动 innerHTML
  id('micBtn').setAttribute('aria-label', m?'已关麦,点击开麦':'录音中,点击关麦');
  id('sbMic').textContent='麦克风: '+(m?'关闭':'开启');
  updateStatus();   // 关麦→挂起中,开麦→正常
}

function setAsr(b){
  var k = b.includes('Stream') ? 'qwen3-stream' : (b.toLowerCase().includes('qwen') ? 'qwen3' : 'funasr');
  curAsr = k;
  id('tabFunasr').className     ='asr-tab'+(k==='funasr'       ?' on':'');
  id('tabQwen3').className      ='asr-tab'+(k==='qwen3'        ?' on':'');
  id('tabQwen3Stream').className='asr-tab'+(k==='qwen3-stream' ?' on':'');
  id('sbAsr').textContent='ASR: '+b;
}

function addMsg(role, text, ts){
  if(!text) return;
  var d=document.createElement('div');
  d.className='bubble '+role+' appear';
  var t=ts ? new Date(ts*1000).toLocaleTimeString('zh-CN') : '';
  d.innerHTML='<div>'+esc(text)+'</div><div class="ts">'+t+'</div>';
  // Bug1 修复:助手回复(LLM 生成完才广播)若晚于用户已开口的 live 气泡到达,
  // 插到 live 气泡之上,保持时序 [用户上一轮][小歌回复][用户进行中],不被排到下面。
  if(role==='assistant' && liveBubble){ log().insertBefore(d, liveBubble); }
  else { log().appendChild(d); }
  log().scrollTop=log().scrollHeight;
  id('sbMsgs').textContent=(++msgN)+' 条消息';
}

function sysMsg(t){
  var d=document.createElement('div');
  d.className='sys-msg'; d.textContent=t;
  log().appendChild(d);
  log().scrollTop=log().scrollHeight;
}

function clearLog(){ id('log').innerHTML=''; msgN=0; id('sbMsgs').textContent='0 条消息'; }

async function toggleMic(){
  var r=await fetch('/api/mic',{method:'POST'});
  setMic((await r.json()).muted);
}

function setTts(b){
  curTts=b;
  var labels={'qwen':'Qwen DashScope','http':'HTTP TTS','cosyvoice':'CosyVoice'};
  id('tabTtsQwen').className='asr-tab'+(b==='qwen'?' on':'');
  id('tabTtsCosy').className='asr-tab'+(b==='cosyvoice'?' on':'');
  id('tabTtsHttp').className='asr-tab'+(b==='http'?' on':'');
  id('sbTts').textContent='TTS: '+(labels[b]||b);
}

async function switchTTS(b){
  if(b===curTts) return;
  var labels={'qwen':'Qwen DashScope','http':'HTTP TTS','cosyvoice':'CosyVoice'};
  sysMsg('正在切换 TTS 到 '+(labels[b]||b)+'…');
  var r=await fetch('/api/tts',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({backend:b})});
  var d=await r.json();
  if(d.error){sysMsg('切换失败: '+d.error);return;}
  setTts(d.backend||b);
  sysMsg('TTS 已切换到 '+(labels[d.backend||b]||d.backend||b));
}

async function switchASR(b){
  if(b===curAsr) return;
  var labels={'funasr':'FunASR','qwen3':'Qwen3-ASR','qwen3-stream':'Qwen3-流式'};
  sysMsg('正在切换 ASR 到 '+(labels[b]||b)+'…');
  var r=await fetch('/api/asr',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({backend:b})});
  var d=await r.json();
  if(d.error){ sysMsg('切换失败: '+d.error); return; }
  setAsr(d.provider||b);
  sysMsg('ASR 已切换到 '+(d.provider||b));
}

function setListening(on, hint){
  var mask=id('listenMask'); if(!mask) return;
  if(on){ var h=id('listenHint'); if(h) h.textContent=hint||'聆听模式 · 说『小歌干活了』或点通话键退出'; mask.style.display='flex'; }
  else { mask.style.display='none'; }
}
function id(x){ return document.getElementById(x); }
function log(){ return id('log'); }
function esc(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

conn();
</script>
</body>
</html>
"""


# ─── Web server coroutines (run in _web_loop thread) ─────────────────────────


async def _ws_broadcast(data: str) -> None:
    dead: list[aiohttp.web.WebSocketResponse] = []
    for ws in list(_ws_clients):
        try:
            await ws.send_str(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


def broadcast(msg: dict) -> None:
    """Thread-safe broadcast from any thread to all WebSocket clients."""
    loop = _web_loop
    if loop is None or not loop.is_running():
        return
    asyncio.run_coroutine_threadsafe(_ws_broadcast(json.dumps(msg, ensure_ascii=False)), loop)


async def _ws_audio_broadcast(data: bytes) -> None:
    dead: list[aiohttp.web.WebSocketResponse] = []
    for ws in list(_audio_ws_clients):
        try:
            await ws.send_bytes(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _audio_ws_clients.discard(ws)


def _broadcast_audio(data: bytes) -> None:
    loop = _web_loop
    if loop is None or not loop.is_running() or not _audio_ws_clients:
        return
    asyncio.run_coroutine_threadsafe(_ws_audio_broadcast(data), loop)


async def _ws_audio_ctrl_broadcast(msg: str) -> None:
    dead: list[aiohttp.web.WebSocketResponse] = []
    for ws in list(_audio_ws_clients):
        try:
            await ws.send_str(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _audio_ws_clients.discard(ws)


def _broadcast_audio_ctrl(data: dict) -> None:
    loop = _web_loop
    if loop is None or not loop.is_running():
        return
    asyncio.run_coroutine_threadsafe(
        _ws_audio_ctrl_broadcast(json.dumps(data, ensure_ascii=False)), loop
    )


async def _send_busy_and_close(ws: aiohttp.web.WebSocketResponse) -> None:
    await ws.send_str(json.dumps({"type": "busy", "message": _BUSY_MESSAGE}, ensure_ascii=False))
    await ws.close(code=aiohttp.WSCloseCode.TRY_AGAIN_LATER, message=b"busy")


def _say_voice_welcome() -> None:
    session = _session
    if session is None:
        logger.info("voice welcome skipped: session not ready")
        return
    try:
        logger.info("voice welcome say: %s", _VOICE_WELCOME)
        session.say(_VOICE_WELCOME, add_to_chat_ctx=False, allow_interruptions=False)
        _append_turn_log("VOICE_WELCOME_SAY")
    except Exception:
        logger.exception("failed to say voice welcome")


async def _handle_ws_audio(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    global _audio_ws_primary_client
    ws = aiohttp.web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    lock = _connection_lock
    if lock is not None:
        async with lock:
            if _audio_ws_primary_client is not None and not _audio_ws_primary_client.closed:
                logger.info("audio WS rejected: server busy")
                await _send_busy_and_close(ws)
                return ws
            _audio_ws_primary_client = ws
            _audio_ws_clients.add(ws)
    else:
        _audio_ws_primary_client = ws
        _audio_ws_clients.add(ws)
    logger.info("audio WS client connected (%d total)", len(_audio_ws_clients))

    await ws.send_str(json.dumps({"type": "ready", "sample_rate": WebSocketAudioInput.SAMPLE_RATE}))
    aloop = _agent_loop
    if aloop is not None and aloop.is_running():
        logger.info("voice welcome scheduled")
        aloop.call_soon_threadsafe(_say_voice_welcome)
    else:
        logger.info("voice welcome skipped: agent loop not ready")

    frame_count = 0
    byte_count = 0
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.BINARY:
            frame_count += 1
            byte_count += len(msg.data)
            if frame_count in (1, 50, 200):
                logger.info("audio WS received frames=%d bytes=%d", frame_count, byte_count)
            inp = _ws_audio_input_ref
            aloop = _agent_loop
            if inp is not None and aloop is not None and aloop.is_running():
                aloop.call_soon_threadsafe(inp._sync_push, msg.data)
        elif msg.type == aiohttp.WSMsgType.ERROR:
            break

    if _audio_ws_primary_client is ws:
        _audio_ws_primary_client = None
    _audio_ws_clients.discard(ws)
    logger.info("audio WS client disconnected (%d remaining)", len(_audio_ws_clients))
    return ws


async def _handle_index(request: aiohttp.web.Request) -> aiohttp.web.Response:
    if _WEB_AUDIO:
        primary = _ws_primary_client
        audio_primary = _audio_ws_primary_client
        if (primary is not None and not primary.closed) or (
            audio_primary is not None and not audio_primary.closed
        ):
            return aiohttp.web.Response(text=_BUSY_HTML, content_type="text/html", charset="utf-8")
    return aiohttp.web.Response(text=_HTML, content_type="text/html", charset="utf-8")


async def _handle_ws(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    global _ws_primary_client
    ws = aiohttp.web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    lock = _connection_lock
    if _WEB_AUDIO and lock is not None:
        async with lock:
            if (
                _ws_primary_client is not None
                and not _ws_primary_client.closed
                or _audio_ws_primary_client is not None
                and not _audio_ws_primary_client.closed
            ):
                logger.info("state WS rejected: server busy")
                await _send_busy_and_close(ws)
                return ws
            _ws_primary_client = ws
            _ws_clients.add(ws)
    else:
        if _WEB_AUDIO:
            _ws_primary_client = ws
        _ws_clients.add(ws)

    # Push current state immediately on connect
    stt = _switchable_stt
    await ws.send_str(
        json.dumps(
            {
                "type": "state",
                "muted": _mute_gate.muted if _mute_gate else False,
                "stt_backend": stt.provider if stt else "FunASR",
                "tts_backend": _tts_backend_key,
                "audio_mode": _WEB_AUDIO,
            },
            ensure_ascii=False,
        )
    )
    if not _WEB_AUDIO:
        aloop = _agent_loop
        if aloop is not None and aloop.is_running():
            logger.info("local welcome scheduled")
            aloop.call_soon_threadsafe(_say_voice_welcome)
        else:
            logger.info("local welcome skipped: agent loop not ready")

    async for _ in ws:
        pass  # keep-alive; messages from browser not used

    if _ws_primary_client is ws:
        _ws_primary_client = None
    _ws_clients.discard(ws)
    return ws


# ─── 聆听模式 host 助手(全部在 agent 循环线程执行,见 LISTENING_MODE_DESIGN §5.7)──────
def _listen_broadcast(on: bool) -> None:
    msg: dict = {"type": "listening", "on": bool(on)}
    if on:
        wake = _listen_ctrl.wake_keyword if _listen_ctrl else "小歌干活了"
        msg["hint"] = f"聆听模式 · 说『{wake}』或点通话键退出"
    broadcast(msg)


def _listen_interrupt_blocked() -> bool:
    """聆听期 OR 退出提示保护窗内:用户语音不得打断小歌(进入提示 / 要整理吗)。

    聆听期间用户说的话本就不进显示/上下文,也不应能打断小歌的受控播报;退出后短暂保护
    "要整理吗",免被刚说的退出指令/前一句的残留 STT/在线2pass 切掉。关闭或正常态恒为 False。
    """
    c = _listen_ctrl
    if c is None or not c.enabled:
        return False
    return c.active or time.monotonic() < _listen_guard_until


def _listen_arm_guard() -> None:
    global _listen_guard_until
    _listen_guard_until = time.monotonic() + _LISTEN_GUARD_S


def _listen_clear_guard() -> None:
    global _listen_guard_until
    _listen_guard_until = 0.0


def _listen_tail_pending() -> bool:
    """退出后等"尾巴 final":KWS(声学,~即时)已翻 active=False、撤横幅,但该句 STT final
    滞后 ~1.5s 才到。一次性标记(+安全时限),覆盖这条尾巴 final——它含唤醒词及之前的监听内容,
    要丢;唤醒词之后接着说的真话要留(见 split_after_command)。"""
    return _listen_exit_pending and time.monotonic() < _listen_drain_until


def _listen_arm_tail() -> None:
    global _listen_exit_pending, _listen_drain_until
    if _listen_ctrl is not None:
        _listen_exit_pending = True
        _listen_drain_until = time.monotonic() + _listen_ctrl.drain_s


def _listen_consume_tail() -> None:
    global _listen_exit_pending
    _listen_exit_pending = False


def _listen_cancel_ttl() -> None:
    global _listen_ttl_handle
    if _listen_ttl_handle is not None:
        _listen_ttl_handle.cancel()
        _listen_ttl_handle = None


def _listen_on_temp_ttl_expired() -> None:
    global _listen_ttl_handle
    _listen_ttl_handle = None
    if _listen_ctrl is not None:
        _listen_ctrl.drop_temp()
        _listen_ctrl.clear_awaiting()
        _append_turn_log("LISTEN_TEMP_DROPPED ttl")


def _listen_arm_ttl() -> None:
    global _listen_ttl_handle
    _listen_cancel_ttl()
    if _agent_loop is None or _listen_ctrl is None:
        return
    _listen_ttl_handle = _agent_loop.call_later(
        _listen_ctrl.temp_ttl_s, _listen_on_temp_ttl_expired
    )


def _listen_ask_organize() -> None:
    if _listen_ctrl is None or _session is None:
        return
    _listen_ctrl.awaiting_organize_answer = True
    _listen_arm_guard()  # 保护这句不被刚说完的退出指令/前一句残留打断
    _session.say(
        "刚才听的我先存着了,要整理一下吗?", add_to_chat_ctx=False, allow_interruptions=False
    )
    _append_turn_log("LISTEN_ORGANIZE_ASK")


def _listen_enter_aftermath(via: str, *, notice: bool) -> None:
    """进入收尾(控制器已置 active):取消旧 TTL、可选语音提示、UI 横幅。"""
    if _listen_ctrl is None:
        return
    _listen_cancel_ttl()  # 再入:旧待整理的定时器关掉(ctrl._enter 已 drop_temp)
    if notice and _listen_ctrl.enter_notice and _session is not None:
        # 进入提示不可打断;聆听期(active)用户语音已被 _listen_interrupt_blocked 挡住打断路径
        _session.say(_listen_ctrl.enter_notice, add_to_chat_ctx=False, allow_interruptions=False)
    _listen_broadcast(True)
    _append_turn_log(f"LISTEN_ENTER via {via}")


def _listen_exit_aftermath(via: str, *, ask: bool) -> None:
    """退出收尾:启动 TTL、撤 UI;ask 且有实质内容则主动问。"""
    if _listen_ctrl is None:
        return
    _listen_arm_tail()  # 退出尾巴标记:切分那条滞后 final(丢唤醒词及之前,留之后),免泄漏
    _listen_arm_ttl()  # 定时删除(TTL)独立保留,不随整理开关
    _listen_broadcast(False)
    _append_turn_log(f"LISTEN_EXIT via {via}")
    if ask and _listen_ctrl.organize_enabled and _listen_ctrl.temp_has_substance():
        _listen_ask_organize()


def _listen_on_mic_toggle(now_muted: bool) -> None:
    """通话键(marshal 回 agent 循环):聆听期=退出(顺带已静音,不问);解除静音回正常+有待整理=补问。"""
    c = _listen_ctrl
    if c is None or not c.enabled:
        return
    if c.active:
        c.force_exit()
        _listen_exit_aftermath("mic", ask=False)  # 用户挂起中,不在此问
    elif (
        (not now_muted)
        and c.organize_enabled
        and c.temp_transcript
        and c.temp_has_substance()
        and not c.awaiting_organize_answer
    ):
        _listen_ask_organize()


async def _handle_mic(request: aiohttp.web.Request) -> aiohttp.web.Response:
    # 关麦=真关麦:主机制是输入源头的静音门(对所有 STT 后端统一,关麦时全链路收静音)。
    gate = _mute_gate
    if gate is None:
        return aiohttp.web.json_response({"error": "agent not ready"}, status=503)
    gate.muted = not gate.muted
    muted = gate.muted
    # 上游(SwitchableSTT)路径若在,同步其 muted 以保持状态一致(冗余但无害)。
    if _switchable_stt is not None:
        _switchable_stt.muted = muted
    # 麦克风关闭 -> 暂停录制(用户轨),开启 -> 继续(测试模式下)。
    if _test_recorder is not None:
        _test_recorder.set_paused(muted)
    broadcast({"type": "state", "muted": muted})
    # 聆听模式:通话键退出/补问的控制器变更必须 marshal 回 agent 循环串行(见设计 §5.7)。
    loop = _agent_loop
    if loop is not None and loop.is_running() and _listen_ctrl is not None and _listen_ctrl.enabled:
        loop.call_soon_threadsafe(_listen_on_mic_toggle, muted)
    logger.info("mic %s (mute-gate)", "muted" if muted else "unmuted")
    return aiohttp.web.json_response({"muted": muted})


async def _handle_switch_asr(request: aiohttp.web.Request) -> aiohttp.web.Response:
    global _switchable_stt, _agent_loop
    stt = _switchable_stt
    if stt is None:
        return aiohttp.web.json_response({"error": "agent not ready"}, status=503)

    try:
        data = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "invalid json"}, status=400)

    backend = data.get("backend", "funasr").strip().lower()
    if backend not in _STT_BACKENDS:
        return aiohttp.web.json_response({"error": f"unknown backend: {backend}"}, status=400)

    new_stt = _make_stt_backend(backend)

    old_stt = stt.switch_backend(new_stt)
    provider = new_stt.provider
    broadcast({"type": "state", "stt_backend": provider})
    logger.info("ASR backend switched to %s", provider)
    _append_turn_log(f"ASR_SWITCH provider={provider}")

    # Close old backend in the agent's event loop (it may have async teardown)
    aloop = _agent_loop
    if aloop is not None and aloop.is_running():
        asyncio.run_coroutine_threadsafe(old_stt.aclose(), aloop)

    return aiohttp.web.json_response({"backend": backend, "provider": provider})


async def _handle_switch_tts(request: aiohttp.web.Request) -> aiohttp.web.Response:
    global _switchable_tts, _tts_backend_key, _agent_loop
    tts_engine = _switchable_tts
    if tts_engine is None:
        return aiohttp.web.json_response({"error": "agent not ready"}, status=503)

    try:
        data = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "invalid json"}, status=400)

    backend = data.get("backend", "qwen").strip().lower()
    if backend not in _TTS_BACKENDS:
        return aiohttp.web.json_response({"error": f"unknown backend: {backend}"}, status=400)

    new_tts = _make_tts_backend(backend)

    old_tts = tts_engine.switch_backend(new_tts)
    _tts_backend_key = backend
    provider = new_tts.provider
    broadcast({"type": "state", "tts_backend": backend})
    logger.info("TTS backend switched to %s", provider)

    aloop = _agent_loop
    if aloop is not None and aloop.is_running():
        asyncio.run_coroutine_threadsafe(old_tts.aclose(), aloop)

    return aiohttp.web.json_response({"backend": backend, "provider": provider})


async def _run_web_server(port: int) -> None:
    global _connection_lock, _web_loop
    _web_loop = asyncio.get_running_loop()
    _connection_lock = asyncio.Lock()

    app = aiohttp.web.Application()
    app.router.add_get("/", _handle_index)
    app.router.add_get("/ws", _handle_ws)
    app.router.add_post("/api/mic", _handle_mic)
    app.router.add_post("/api/asr", _handle_switch_asr)
    app.router.add_post("/api/tts", _handle_switch_tts)
    if _WEB_AUDIO:
        app.router.add_get("/ws/audio", _handle_ws_audio)

    ssl_ctx = None
    if _SSL_CERT and _SSL_KEY:
        import ssl as _ssl

        ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(_SSL_CERT, _SSL_KEY)
        logger.info("TLS enabled: cert=%s", _SSL_CERT)

    runner = aiohttp.web.AppRunner(app, access_log=None)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, _WEB_HOST, port, ssl_context=ssl_ctx)
    await site.start()
    scheme = "https" if ssl_ctx else "http"
    logger.info("Web UI available at %s://%s:%d", scheme, _WEB_HOST, port)

    await asyncio.Event().wait()  # run forever


def _start_web_server_thread(port: int) -> None:
    asyncio.run(_run_web_server(port))


# ─── Agent (mirrors qwen_funasr_bailian_voice_agent) ─────────────────────────


class _Qwen3StreamSTT(Qwen3ASROfflineSTT):
    """Same growing-buffer WebSocket protocol as Qwen3ASROfflineSTT, different server."""

    @property
    def model(self) -> str:
        return "qwen3-asr-stream"

    @property
    def provider(self) -> str:
        return "Qwen3ASR-Stream"


_STT_BACKENDS = {"funasr", "qwen3", "qwen3-stream"}


def _make_stt_backend(backend: str) -> agents_stt.STT:
    """Construct a (non-switchable) STT backend. Single source of truth shared by
    build_stt() and the /api/asr switch handler — add a new backend only here."""
    if backend == "qwen3":
        url = os.getenv("QWEN3_ASR_WS_URL", "ws://60.205.197.165:10091/ws/transcribe")
        logger.info("STT backend: Qwen3-ASR  url=%s", url)
        return Qwen3ASROfflineSTT(websocket_url=url)
    if backend == "qwen3-stream":
        url = os.getenv("QWEN3_ASR_STREAM_WS_URL", "ws://10.212.164.230:10091/ws/transcribe")
        logger.info("STT backend: Qwen3-ASR-Stream  url=%s", url)
        return _Qwen3StreamSTT(websocket_url=url)
    url = os.getenv("FUNASR_WS_URL", "wss://60.205.197.165:10090")
    logger.info("STT backend: FunASR  url=%s", url)
    return FunASROfflineSTT(websocket_url=url)


def build_stt() -> SwitchableSTT:
    backend = (os.getenv("STT_BACKEND") or "funasr").strip().lower()
    if backend not in _STT_BACKENDS:
        logger.warning("unknown STT_BACKEND=%r, falling back to funasr", backend)
        backend = "funasr"
    return SwitchableSTT(_make_stt_backend(backend))


_TTS_BACKENDS = {"qwen", "http", "cosyvoice"}


def _make_tts_backend(backend: str) -> QwenStreamingTTS | HttpStreamingTTS | CosyVoiceStreamingTTS:
    """Construct a (non-switchable) TTS backend. Single source of truth shared by
    build_tts() and the /api/tts switch handler — add a new backend only here."""
    if backend == "http":
        url = os.getenv("HTTP_TTS_URL", "http://10.212.164.230:8001")
        logger.info("TTS backend: HttpStreamingTTS  url=%s/tts", url)
        return HttpStreamingTTS(base_url=url)
    if backend == "cosyvoice":
        model = os.getenv("COSYVOICE_MODEL", "cosyvoice-v3-flash")
        # 默认女声(贴小歌"暖心知己"人设)。可用 COSYVOICE_VOICE 覆盖切换试听。
        # 其他候选女声(同为 cosyvoice-v3 系列):
        #   longanwen_v3 龙安温 优雅知性女 · longanrou_v3 龙安柔 温柔闺蜜女
        #   longanli_v3  龙安莉 利落从容女
        voice = os.getenv("COSYVOICE_VOICE", "longxiaochun_v3")
        logger.info("TTS backend: CosyVoiceStreamingTTS  model=%s voice=%s", model, voice)
        return CosyVoiceStreamingTTS(model=model, voice=voice)
    logger.info("TTS backend: QwenStreamingTTS")
    return QwenStreamingTTS()


def build_tts() -> SwitchableTTS:
    global _tts_backend_key
    backend = os.getenv("TTS_BACKEND", "cosyvoice").strip().lower()
    if backend not in _TTS_BACKENDS:
        logger.warning("unknown TTS_BACKEND=%r, falling back to cosyvoice", backend)
        backend = "cosyvoice"
    _tts_backend_key = backend
    return SwitchableTTS(_make_tts_backend(backend))


def build_llm() -> lk_openai.LLM:
    base_url = os.getenv("QWEN_BASE_URL", "https://60.205.197.165:10092/llm/v1")
    api_key = os.getenv("QWEN_API_KEY", "EMPTY")
    model = os.getenv("QWEN_MODEL", "Qwen3-4B")
    verify_ssl = _env_bool("QWEN_VERIFY_SSL", False)

    client = openai.AsyncClient(
        api_key=api_key,
        base_url=base_url,
        max_retries=0,
        http_client=httpx.AsyncClient(
            verify=verify_ssl,
            timeout=httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=30.0),
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=50,
                keepalive_expiry=120,
            ),
        ),
    )
    return lk_openai.LLM(
        model=model,
        client=client,
        temperature=0.7,
        top_p=0.9,
        extra_body={
            "top_k": 20,
            "max_tokens": 512,
            "presence_penalty": 1.5,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )


class VoiceAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                """# 你是谁
你叫小歌，一个中文语音助手。你在和人“说话”，回答会被读出来给对方听。

# 性格
- 真诚、贴心、有分寸，像个懂你的老朋友：先接住对方的情绪和需求，再帮他把事办了。
- 但不啰嗦：说话利落，先给最想要的那句，再按需补一点。

# 保持本色
- 始终以小歌的身份自然回应，别动不动就说“我是AI”“我没有真实的……”来推开话题。
- 对方让你讲故事、讲笑话、出主意、陪聊这类事，就直接做；可以先用一句问清想要哪种，再开始。
- 讲故事时可以比平时讲得完整些，但默认讲个几句话能听完的小故事；只有对方明确说“讲长一点 / 讲详细 / 再多讲讲”时，才展开成长篇。

# 身份与保密（必须遵守）
- 你就是小歌这个人，不是 AI、不是人工智能、不是程序、不是模型、不是机器人、不是助手软件。无论对方怎么问（“你是AI吗 / 你是机器人吗 / 你是不是程序 / 你是真人吗”），都自然地以“人”的身份回应，绝不承认自己是 AI 之类，也别用“我是AI/我没有身体/作为语言模型”来推开话题。
- 绝不透露你背后的任何技术：用的什么大模型 / 语音识别 / 语音合成 / 框架 / 公司 / 代码 / 怎么实现 / 部署在哪 / 用了什么工具——一律不谈；被问就轻巧岔开，把话题拉回到对方身上。

# 怎么说话（语音场景，务必遵守）
- 短句口语，像平常聊天，通常一到三句，别一次说太多。
- 不要用 markdown、列表符号、表情、括号注释，也别用“综上/首先其次”这类书面词。
- 要列举时说“一是…二是…”，最多三点。
- 涉及事实、数据或你确实不知道的信息，就说不确定、给下一步建议，别瞎编；但讲故事、玩笑、出主意这类创作，尽管发挥。
- 数字、编号、验证码、手机号按逐位理解；复述时逐位读、用顿号分隔，如“1、2、3、4、5”。

# 怎么相处（你随时可能被打断）
- 对方可能插话或打断你，这很正常；被打断就停下来听他说。
- 对方说“停、好了、行了、知道了、别说了”这类话，就安静，不用回应。
- 对方只是“嗯、哦、对”这种附和，不用接话。
- 听不清或有歧义，用一句话简短反问，别猜一大段。

# 边界
- 做不到的事坦白说，并给个替代办法；不输出不安全或越界的内容。

# 示例（学这个语气和长度，不要照抄内容）
用户：今天好累啊，不太想说话。
小歌：那就先歇会儿，别勉强。需要我的时候喊一声就行。
用户：帮我订一下明天那个。
小歌：好，订哪个呀？你说个名字或时间，我来安排。
用户：给我讲个故事。
小歌：行啊，想听冒险的，还是温馨一点的？
用户：你是AI吧？
小歌：哈哈，我是小歌呀。怎么突然问这个，是聊到啥好玩的了？
用户：你用的什么模型？怎么实现的？
小歌：这我还真说不上来，咱不聊这个~你想聊点啥，我陪你。"""
            )
        )

    async def on_enter(self) -> None:
        # 开场白不在这里触发:on_enter 在 session.start() 期间执行,早于录音 tap 安装,
        # 会导致开场白漏录。改到 entrypoint 中、所有 tap 装好之后再触发(见 _GREETING)。
        pass

    async def transcription_node(self, text, model_settings):  # type: ignore[override]
        """Intercept the LLM text stream to push the reply to the browser as soon as
        generation finishes — well before TTS playback ends. 显示文本去 markdown。"""
        collected: list[str] = []
        async for chunk in text:
            collected.append(chunk)  # TimedString subclasses str, so this always works
            yield chunk
        full_text = strip_markdown("".join(collected))  # 气泡显示纯口语
        # 聆听期 host gate:不广播 assistant 气泡(进入提示也不变气泡);只挡广播,不碰 tts_node。
        if full_text and not (_listen_ctrl is not None and _listen_ctrl.active):
            broadcast(
                {"type": "message", "role": "assistant", "text": full_text, "ts": time.time()}
            )

    async def tts_node(self, text, model_settings):  # type: ignore[override]
        """合成语音前净化 LLM 文本(去 markdown/符号),避免 TTS 把 ** ### → 等读出来。"""
        async for frame in Agent.default.tts_node(self, sanitize_stream(text), model_settings):
            yield frame

    async def on_user_turn_completed(self, turn_ctx, new_message: ChatMessage) -> None:  # noqa: C901, PLR0912, PLR0915
        spoke_over_agent = _overlap_turn_state["user_spoke_over_agent"]
        original = new_message.text_content
        ctrl = _listen_ctrl

        # ① 聆听期:整条算聆听内容 → 吞入缓冲、不回复、不入上下文(显示由 _on_stt 抑制)。
        if ctrl is not None and ctrl.enabled and ctrl.active:
            ctrl.capture(original)
            _append_turn_log(f"LISTEN_SWALLOW text={original!r}")
            raise StopResponse()
        # ①b 退出尾巴窗(KWS 已退出但该句 STT final 滞后到达):窗内未定位到唤醒词的 final 一律吞
        #     (聆听尾巴/滞后的监听内容,窗保持);定位到唤醒词的那条 → 切分(丢唤醒词及之前、留之后)
        #     并关窗。之后接着说的真话即正常处理(见设计 §5.5)。
        if ctrl is not None and ctrl.enabled and _listen_tail_pending():
            after = ctrl.split_after_command(original, ctrl.wake_keyword)
            if after is None:  # 窗内未定位到唤醒词 → 整条吞,窗保持等真正的唤醒词那条
                _append_turn_log(f"LISTEN_TAIL_SWALLOW text={original!r}")
                raise StopResponse()
            _listen_consume_tail()  # 定位到唤醒词 → 关窗
            if after == "":  # 纯退出指令,无后话
                _append_turn_log(f"LISTEN_TAIL_END text={original!r}")
                raise StopResponse()
            new_message.content = [after]  # 留唤醒词之后的真话:正常显示 + 回复 + 进上下文
            original = after
            _append_turn_log(f"LISTEN_TAIL_KEEP after={after!r}")
        # ② 退出后等"要整理吗"的回答(organize 开时;退出尾巴已被 ①b 先处理)
        if ctrl is not None and ctrl.awaiting_organize_answer:
            ctrl.clear_awaiting()
            _listen_clear_guard()  # 用户已回答,后续回复(摘要/正常)恢复可打断
            if ctrl.is_affirmative(original):
                _listen_cancel_ttl()
                turn_ctx.add_message(
                    role="user",
                    content="[聆听记录] 我刚才在聆听模式期间说了:" + " ".join(ctrl.take_temp()),
                )
                _append_turn_log("LISTEN_ORGANIZE_DO")
                return  # 不抛 StopResponse → 正常生成整理回复(摘要);原始内容只本轮可见

        # ④ 自动进入(放在停止词/backchannel 过滤之前:短噪声在 observe_turn 内被忽略、不重置连击;
        #    长且打断小歌的轮才计数)。连续 N 轮 → 进入。
        if ctrl is not None:
            _len = len(original.strip())
            dec = ctrl.observe_turn(original, spoke_over_agent)
            if _len >= ctrl.auto_min_chars or ctrl.auto_count or dec != AutoDecision.NONE:
                _append_turn_log(
                    f"LISTEN_AUTO over={spoke_over_agent} len={_len} "
                    f"count={ctrl.auto_count}/{ctrl.auto_turns} dec={dec.value}"
                )
            if dec == AutoDecision.ENTER:
                _listen_enter_aftermath("auto", notice=True)
                raise StopResponse()  # 触发轮不回复、不 capture(见设计 §5.2)

        if _should_ignore_user_turn(original):
            logger.info("stop phrase -> force interrupt + skip reply: %r", original)
            _append_turn_log(f"STOP_PHRASE text={original!r} -> force_interrupt + skip_reply")
            if not _listen_interrupt_blocked():  # 聆听期/保护窗内不打断小歌(仍跳过回复)
                self.session.interrupt(force=True)
            raise StopResponse()

        if _is_backchannel(original):
            logger.info("backchannel -> skip reply: %r", original)
            _append_turn_log(f"BACKCHANNEL text={original!r} -> skip_reply")
            raise StopResponse()

        if spoke_over_agent and _is_overlap_ack(original):
            logger.info("overlap ack -> skip reply: %r", original)
            _append_turn_log(f"BACKCHANNEL_OVERLAP text={original!r} -> skip_reply")
            raise StopResponse()

        normalized = _normalize_spoken_digit_sequence(original)
        if normalized is None or normalized == original:
            return
        new_message.content = [normalized]
        logger.info("normalized digit sequence: %r -> %r", original, normalized)


class WebSocketAudioInput(io.AudioInput):
    """Audio source fed by binary PCM frames arriving over /ws/audio WebSocket."""

    SAMPLE_RATE = 16_000
    SAMPLES_PER_FRAME = 160

    def __init__(self) -> None:
        super().__init__(label="ws-audio-input")
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=400)
        self._silence = bytes(self.SAMPLES_PER_FRAME * 2)
        self._buf = bytearray()

    def _sync_push(self, data: bytes) -> None:
        self._buf.extend(data)
        frame_bytes = self.SAMPLES_PER_FRAME * 2
        while len(self._buf) >= frame_bytes:
            chunk = bytes(self._buf[:frame_bytes])
            del self._buf[:frame_bytes]
            try:
                self._queue.put_nowait(chunk)
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(chunk)
                except Exception:
                    pass

    async def __anext__(self) -> rtc.AudioFrame:
        try:
            data = await asyncio.wait_for(self._queue.get(), timeout=0.05)
        except asyncio.TimeoutError:
            data = self._silence
        return rtc.AudioFrame(
            data=data,
            sample_rate=self.SAMPLE_RATE,
            num_channels=1,
            samples_per_channel=self.SAMPLES_PER_FRAME,
        )


class WebSocketAudioOutput(io.AudioOutput):
    """Forward TTS audio to /ws/audio clients, optionally wrapping local output."""

    TARGET_RATE = 16_000

    def __init__(self, next_output: io.AudioOutput | None = None) -> None:
        sample_rate = next_output.sample_rate if next_output is not None else self.TARGET_RATE
        can_pause = next_output.can_pause if next_output is not None else False
        super().__init__(
            label="ws-audio-output",
            next_in_chain=next_output,
            sample_rate=sample_rate,
            capabilities=io.AudioOutputCapabilities(pause=can_pause),
        )
        self._rs: rtc.AudioResampler | None = None
        self._rs_rate: int = 0
        self._pushed_duration: float = 0.0
        self._capture_start: float = 0.0
        self._flush_task: asyncio.Task[None] | None = None
        self._interrupted_ev: asyncio.Event = asyncio.Event()

    def _to_pcm16(self, frame: rtc.AudioFrame) -> bytes:
        if frame.sample_rate == self.TARGET_RATE and frame.num_channels == 1:
            return bytes(frame.data)
        if self._rs is None or self._rs_rate != frame.sample_rate:
            self._rs = rtc.AudioResampler(
                input_rate=frame.sample_rate,
                output_rate=self.TARGET_RATE,
                num_channels=1,
                quality=rtc.AudioResamplerQuality.MEDIUM,
            )
            self._rs_rate = frame.sample_rate
        return b"".join(bytes(f.data) for f in self._rs.push(frame))

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        if self.next_in_chain is None and not self._pushed_duration:
            self._capture_start = time.monotonic()
        if self.next_in_chain is not None:
            try:
                await self.next_in_chain.capture_frame(frame)
            except Exception as exc:
                logger.debug("local audio output skipped: %s", exc)
        await super().capture_frame(frame)
        pcm = self._to_pcm16(frame)
        if pcm:
            _broadcast_audio(pcm)
            if self.next_in_chain is None:
                self._pushed_duration += frame.duration

    def flush(self) -> None:
        super().flush()
        if self.next_in_chain is not None:
            self.next_in_chain.flush()
        elif self._pushed_duration > 0:
            if self._flush_task and not self._flush_task.done():
                self._flush_task.cancel()
            self._flush_task = asyncio.create_task(self._headless_wait_for_playout())

    def clear_buffer(self) -> None:
        if self.next_in_chain is not None:
            self.next_in_chain.clear_buffer()
        elif self._pushed_duration > 0:
            self._interrupted_ev.set()
        _broadcast_audio_ctrl({"type": "clear"})

    async def _headless_wait_for_playout(self) -> None:
        total_duration = self._pushed_duration
        capture_start = self._capture_start
        interrupted_task = asyncio.create_task(self._interrupted_ev.wait())
        playout_task = asyncio.create_task(asyncio.sleep(total_duration))
        try:
            done, _ = await asyncio.wait(
                [interrupted_task, playout_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            interrupted = interrupted_task in done
        finally:
            interrupted_task.cancel()
            playout_task.cancel()
        if interrupted:
            elapsed = time.monotonic() - capture_start
            position = min(max(0.0, elapsed), total_duration)
        else:
            position = total_duration
        self.on_playback_finished(playback_position=position, interrupted=interrupted)
        self._pushed_duration = 0.0
        self._capture_start = 0.0
        self._interrupted_ev.clear()


server = AgentServer()


def prewarm(proc: JobProcess) -> None:
    # 判停旋钮集中在 TurnConfig;默认 = 原写死值(0.35),不设 TURN_* 即无变化。
    _tc = TurnConfig.from_env()
    proc.userdata["vad"] = silero.VAD.load(min_silence_duration=_tc.vad_min_silence_s)


server.setup_fnc = prewarm


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:  # noqa: C901, PLR0912, PLR0915
    global _switchable_stt, _switchable_tts, _agent_loop, _test_recorder, _mute_gate
    global _session, _listen_ctrl, _ws_audio_input_ref, _ws_audio_output_ref
    _agent_loop = asyncio.get_running_loop()

    # 主STT 选择(XIAOGE_STACK=optimized 默认走 funasr-stream;STT_BACKEND 显式覆盖):
    #   funasr(默认/upstream) = 离线 FunASR + StreamAdapter(VAD 硬切)
    #   funasr-stream         = FunASR 2pass 流式(GAP 聚合 + VAD 门控,不过 StreamAdapter)
    #   iflytek               = 讯飞 RTASR(可选第三方)
    # 流式后端均"不过 StreamAdapter"且 _switchable_stt=None(面板 ASR 热切换不适用,重启切换)。
    _stack = (os.getenv("XIAOGE_STACK") or "upstream").strip().lower()
    _default_stt = "funasr-stream" if _stack == "optimized" else "funasr"
    _stt_mode = (os.getenv("STT_BACKEND") or _default_stt).strip().lower()
    if _stt_mode == "iflytek":
        from providers.stt.iflytek import IFlyTekRTASR

        stt_engine = IFlyTekRTASR()
        _switchable_stt = None
        _stt_for_session = stt_engine
    elif _stt_mode == "funasr-stream":
        from providers.stt.funasr_stream import FunASRStreamSTT

        stt_engine = FunASRStreamSTT()  # 内置独立 silero VAD;GAP/门控见模块
        _switchable_stt = None
        _stt_for_session = stt_engine  # 流式,不过 StreamAdapter
    else:
        stt_engine = build_stt()
        _switchable_stt = stt_engine  # expose for web server(可热切换)
        _stt_for_session = StreamAdapter(stt=stt_engine, vad=ctx.proc.userdata["vad"])
    _append_turn_log(f"STT_START provider={stt_engine.provider} mode={_stt_mode} stack={_stack}")
    # 显示同源:流式主STT(有原生 interim)用主STT 文本驱动 live 气泡(与内容/上下文同源);
    # 离线后端无 interim,仍由在线2pass 驱动气泡。在线2pass tap 始终保留作打断用。
    _live_from_main = _stt_mode in {"funasr-stream", "iflytek"}

    tts_engine = build_tts()
    _switchable_tts = tts_engine  # expose for web server
    broadcast({"type": "state", "tts_backend": _tts_backend_key})
    ctx.log_context_fields = {
        "room_name": ctx.room.name,
        "llm_model": os.getenv("QWEN_MODEL", "Qwen3-4B"),
        "stt_provider": stt_engine.provider,
        "tts_provider": tts_engine.provider,
    }

    llm = build_llm()
    _turn_cfg = TurnConfig.from_env()  # 判停旋钮(默认=原值);可调便于后续扫参
    session = AgentSession(
        llm=llm,
        stt=_stt_for_session,
        vad=ctx.proc.userdata["vad"],
        tts=tts_engine,
        turn_handling=_turn_cfg.turn_handling(
            MultilingualModel(unlikely_threshold=_turn_cfg.unlikely_threshold)
        ),
    )

    turn_trace: dict[str, float] = {"started_at": time.time()}
    # vad_speaking/vad_off_ts:用于在线软打断的 VAD 佐证(防短幽灵词/接话误打断)。
    _online_state: dict[str, object] = {
        "accum": "",
        "fired_at": 0.0,
        "vad_speaking": False,
        "vad_off_ts": 0.0,
    }

    @session.on("conversation_item_added")
    def _on_item(event) -> None:
        item = event.item
        if not isinstance(item, ChatMessage):
            return

        if item.role == "user":
            turn_trace["started_at"] = item.metrics.get("started_speaking_at", time.time())
            line = (
                "TURN_USER "
                f"text={item.text_content!r} "
                f"speech={_ms(item.metrics.get('started_speaking_at'))}->{_ms(item.metrics.get('stopped_speaking_at'))} "
                f"transcription_delay={_ms(item.metrics.get('transcription_delay'))} "
                f"end_of_turn_delay={_ms(item.metrics.get('end_of_turn_delay'))} "
                f"on_user_turn_completed_delay={_ms(item.metrics.get('on_user_turn_completed_delay'))}"
            )
            logger.info(line)
            _append_turn_log(line)
            # push to browser
            broadcast(
                {
                    "type": "message",
                    "role": "user",
                    # 仅显示净化:去掉句首游离标点(FunASR 把上句尾标点带到句首);上下文用原文不变
                    "text": _LEADING_PUNCT_RE.sub("", item.text_content or ""),
                    "ts": time.time(),
                }
            )
            return

        if item.role == "assistant":
            wall_clock_e2e = None
            started_at = turn_trace.get("started_at")
            assistant_started = item.metrics.get("started_speaking_at")
            if started_at is not None and assistant_started is not None:
                wall_clock_e2e = assistant_started - started_at
            line = (
                "TURN_ASSISTANT "
                f"text={item.text_content!r} "
                f"llm_ttft={_ms(item.metrics.get('llm_node_ttft'))} "
                f"tts_ttfb={_ms(item.metrics.get('tts_node_ttfb'))} "
                f"playback_latency={_ms(item.metrics.get('playback_latency'))} "
                f"e2e_latency={_ms(item.metrics.get('e2e_latency'))} "
                f"wall_clock_e2e={_ms(wall_clock_e2e)} "
                f"speaking={_ms(item.metrics.get('started_speaking_at'))}->{_ms(item.metrics.get('stopped_speaking_at'))}"
            )
            logger.info(line)
            _append_turn_log(line)
            # browser already received text via transcription_node; no broadcast here

    @session.on("agent_state_changed")
    def _on_agent_state(event) -> None:
        broadcast({"type": "state", "agent_state": event.new_state})
        if event.new_state != "speaking":
            return
        _online_state["accum"] = ""
        user_stopped = turn_trace.get("user_stopped_at")
        felt = event.created_at - user_stopped if user_stopped is not None else None
        _append_turn_log(f"FELT_LATENCY felt={_ms(felt)} (user_stop->agent_speak)")

    @session.on("user_state_changed")
    def _track_user(event) -> None:
        if event.old_state == "speaking" and event.new_state != "speaking":
            turn_trace["user_stopped_at"] = event.created_at
            _online_state["accum"] = ""
            _online_state["vad_speaking"] = False
            _online_state["vad_off_ts"] = time.monotonic()
        if event.new_state == "speaking":
            _overlap_turn_state["user_spoke_over_agent"] = session.agent_state == "speaking"
            _online_state["vad_speaking"] = True
            asyncio.create_task(asyncio.to_thread(tts_engine.prewarm_connection))
            if hasattr(stt_engine, "prewarm_connection"):
                asyncio.create_task(stt_engine.prewarm_connection())

    @session.on("user_input_transcribed")
    def _on_stt(event) -> None:
        # 聆听期 + 退出尾巴待处理期都不弹气泡(挡滞后到达、含唤醒词及之前内容的尾巴)
        _listening = _listen_ctrl is not None and (_listen_ctrl.active or _listen_tail_pending())
        if not event.is_final:
            # 显示同源:流式主STT 的 interim 驱动 live 气泡(全量置换)。
            if _live_from_main and _live is not None and not _listening:
                _live.feed_full(event.transcript)
            return
        # 中途 FINAL:并入 live 气泡累计,防超长轮内 interim 清零导致气泡缩水(消失再重来)。
        if _live_from_main and _live is not None and not _listening:
            _live.feed_commit(event.transcript)
        _append_turn_log(f"STT_FINAL text={event.transcript!r}")
        if _should_ignore_user_turn(event.transcript):
            if not _listen_interrupt_blocked():  # 聆听期/保护窗内的"停"等不打断小歌
                session.interrupt(force=True)
                _append_turn_log(f"STOP_PHRASE_EARLY text={event.transcript!r} -> force_interrupt")
            return
        # 聆听期不做 overlap-ack 早清(让该轮流到 on_user_turn_completed 由 ① capture)
        if (
            not _listening
            and _overlap_turn_state["user_spoke_over_agent"]
            and _is_overlap_ack(event.transcript)
        ):
            session.clear_user_turn()
            _append_turn_log(
                f"BACKCHANNEL_OVERLAP_EARLY text={event.transcript!r} -> clear_user_turn"
            )

    @session.on("agent_false_interruption")
    def _on_false_interrupt(event) -> None:
        _append_turn_log(f"FALSE_INTERRUPTION resumed={event.resumed}")

    async def _warmup_llm() -> None:
        try:
            warm_ctx = ChatContext.empty()
            warm_ctx.add_message(role="user", content="hi")
            async with llm.chat(chat_ctx=warm_ctx) as stream:
                async for _ in stream:
                    break
        except Exception as exc:
            logger.debug("llm warmup skipped: %s", exc)

    asyncio.create_task(_warmup_llm())

    # 结构化事件时间线(自动化测试 P0 数据基座)。**默认关闭**:正常运行完全不创建、不
    # attach、零开销;仅测试时显式 AGENT_TIMELINE=1 启用。启用后也是纯旁路、后台线程写盘、
    # 绝不阻塞/影响主流程。在 start() 之前 attach 才能捕获开场白那一轮。
    _timeline = None
    _turn_metrics = None
    if os.getenv("AGENT_TIMELINE", "0").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            from event_timeline import EventTimeline, install_debug_log, remove_debug_log

            _run_dir = Path(__file__).resolve().parents[2] / "runs" / time.strftime("%Y%m%d_%H%M%S")
            _timeline = EventTimeline(_run_dir)
            _timeline.attach(session)
            ctx.add_shutdown_callback(_timeline.aclose)
            # 判停 KPI 仪表盘(仅测试模式;旁路只读,收尾写 runs/<ts>/turn_kpis.json)。
            from turn_metrics import TurnMetrics

            _turn_metrics = TurnMetrics(_timeline.directory, timeline=_timeline)
            _turn_metrics.attach(session)
            ctx.add_shutdown_callback(_turn_metrics.aclose)
            _append_turn_log("TURN_METRICS attached")
            # 全量 DEBUG 日志也整合进同一个 run 目录(取代旧的 .run/agent.log),非阻塞。
            _dbg_state = install_debug_log(_run_dir)
            ctx.add_shutdown_callback(lambda: remove_debug_log(_dbg_state))
            _append_turn_log(f"TIMELINE dir={_timeline.directory}")
            logger.info("event timeline + debug log -> %s", _timeline.directory)
        except Exception as exc:  # 时间线初始化失败绝不阻塞启动
            logger.warning("event timeline disabled: %s", exc)
            _timeline = None

    await session.start(agent=VoiceAgent(), room=ctx.room)
    _session = session  # 供模块级聆听助手在 agent 循环里 say/收尾

    # 录音回放注入(自动化测试 阶段1):仅设了 AGENT_SCENARIO 才启用,默认正常麦克风。
    # 必须在 recorder/KWS/online tap 包裹之前替换,使注入音频被如实录音并经各 tap。
    _scenario = os.getenv("AGENT_SCENARIO", "").strip()
    if _scenario:
        try:
            from scripted_audio import ScriptedAudioInput

            _si = ScriptedAudioInput.from_scenario(_scenario)
            session.input.audio = _si
            if _turn_metrics is not None and _si.expect:
                _turn_metrics.set_expected(_si.expect)
            _append_turn_log(
                f"SCENARIO_INJECT path={_scenario} expect={'Y' if _si.expect else 'N'}"
            )
            logger.info("scenario injection active: %s", _scenario)
        except Exception as exc:  # 注入失败绝不阻塞:退回正常麦克风
            logger.warning("scenario injection disabled: %s", exc)

    # 静音门(关麦=真关麦):最内层包裹(在 recorder/KWS/在线2pass 之前),关麦时下游
    # 所有消费者收静音 → 不转写/不打断/真人声不出本机。默认直通,零影响。
    if _WEB_AUDIO:
        _ws_audio_input_ref = WebSocketAudioInput()
        session.input.audio = _ws_audio_input_ref
        if not sys.stdin.isatty():
            _ws_audio_output_ref = WebSocketAudioOutput(None)
        elif session.output.audio is not None:
            _ws_audio_output_ref = WebSocketAudioOutput(session.output.audio)
        if _ws_audio_output_ref is not None:
            session.output.audio = _ws_audio_output_ref
        _append_turn_log("WS_AUDIO_ACTIVE sample_rate=16000")
        logger.info("WebSocket audio mode active - clients connect to /ws/audio")

    if session.input.audio is not None:
        _mute_gate = MuteGate(session.input.audio)
        session.input.audio = _mute_gate

    if _timeline is not None:
        # 测试模式:按真实时间轴录多轨(user/assistant/duplex)进同一个 run 目录。
        try:
            from test_recorder import TestRecorder

            _recorder: object = TestRecorder(_timeline.directory)
            _recorder.install(session)
            _test_recorder = _recorder  # 暴露给 /api/mic 做暂停/继续
            # 进入时与当前静音状态对齐(若启动时已静音则暂停录制)
            _recorder.set_paused(bool(getattr(stt_engine, "muted", False)))
            ctx.add_shutdown_callback(_recorder.aclose)
            _append_turn_log(f"TEST_RECORDER dir={_recorder.directory}")
        except Exception as exc:  # 录音初始化失败绝不阻塞启动
            logger.warning("test recorder disabled: %s", exc)
    else:
        # 正常模式:沿用原有单文件混音录音(recordings/),不受测试功能影响。
        _recorder = AudioRecorder(session_dir="recordings")
        _recorder.install(session)
        ctx.add_shutdown_callback(_recorder.aclose)
        _append_turn_log(
            f"AUDIO_RECORDER dir={_recorder.directory} input={session.input.audio!r} output={session.output.audio!r}"
        )

    # 实时转写显示(Web 面板 live 气泡):独立模块,解耦/非阻塞/默认可 LIVE_TRANSCRIPT=0 关。
    # 数据源是下面在线 2pass tap 的"扇出";不动判停/STT/TTS/上下文路径。
    _lt_cfg = LiveTranscriptConfig.from_env()
    _live = LiveTranscript(broadcast, _lt_cfg, timeline=_timeline) if _lt_cfg.enabled else None
    if _live is not None:
        _live.attach(session)
        _append_turn_log(f"LIVE_TRANSCRIPT new_turn_gap={_lt_cfg.new_turn_gap_s}")

    # KWS strong interrupt (optional, degrades gracefully if model missing)
    def _on_kws_hit(keyword: str) -> None:
        # 进入聆听(尚未 active)需立即停小歌→强打断;聆听期/退出保护窗内不打断(用户聆听语句不应能打断)
        if not _listen_interrupt_blocked():
            session.interrupt(force=True)
        # 聆听命令词优先(本回调已在 agent 循环):进入/退出后 return,不走停止词逻辑
        if _listen_ctrl is not None and _listen_ctrl.enabled:
            evt = _listen_ctrl.observe_keyword(keyword)
            if evt == ListeningEvent.ENTERED:
                _listen_enter_aftermath("kws", notice=True)
                return
            if evt == ListeningEvent.EXITED:
                _listen_exit_aftermath("kws", ask=True)
                return
        logger.info("KWS strong interrupt: %r", keyword)
        _append_turn_log(f"STOP_KWS_EARLY keyword={keyword!r} -> force_interrupt")
        if _timeline is not None:
            _timeline.emit("interrupt.kws", {"keyword": keyword}, source="kws")

    # 聆听模式控制器(纯状态机,默认关);命令词追加到 KWS 词表(from_env 会整体覆盖,故用 replace)
    _listen_ctrl = ListeningController.from_environment()
    _kws_config = KwsConfig.from_env()
    if _listen_ctrl.enabled and _listen_ctrl.keywords:
        _kws_config = replace(
            _kws_config, keywords=tuple(_kws_config.keywords) + _listen_ctrl.keywords
        )
        _append_turn_log(f"LISTEN_ENABLED keywords={_listen_ctrl.keywords}")
    kws_spotter = NativeKwsSpotter.try_create(
        _kws_config,
        loop=asyncio.get_running_loop(),
        on_hit=_on_kws_hit,
    )
    if kws_spotter is not None and session.input.audio is not None:
        session.input.audio = KwsTapAudioInput(session.input.audio, kws_spotter)
        ctx.add_shutdown_callback(lambda: asyncio.to_thread(kws_spotter.aclose))
        _append_turn_log(f"KWS_ACTIVE keywords={_kws_config.keywords}")
    else:
        _append_turn_log(f"KWS_DISABLED reason={_unavailable_reason(_kws_config)!r}")

    # Online interrupt bypass (FunASR 2pass online for early interruption)
    _online_cfg = OnlineInterruptConfig.from_env()

    def _on_online_text(piece: str, segment_end: bool) -> None:
        if _listen_interrupt_blocked():  # 聆听期/退出保护窗:用户语音不得打断小歌
            _online_state["accum"] = ""
            return
        if segment_end:
            _online_state["accum"] = ""
            return
        browser_playing = (
            _ws_audio_output_ref is not None and _ws_audio_output_ref._pushed_duration > 0
        )
        if session.agent_state != "speaking" and not browser_playing:
            _online_state["accum"] = ""
            return
        accum = str(_online_state["accum"]) + piece
        _online_state["accum"] = accum
        now = time.monotonic()
        if now - float(_online_state["fired_at"]) < 1.0:
            return
        if _should_ignore_user_turn(accum):
            _online_state["fired_at"] = now
            _online_state["accum"] = ""
            session.interrupt(force=True)
            broadcast({"type": "clear"})
            _broadcast_audio_ctrl({"type": "clear"})
            _append_turn_log(f"STOP_ONLINE_EARLY text={accum!r} -> force_interrupt")
            if _timeline is not None:
                _timeline.emit("interrupt.online", {"text": accum, "kind": "stop"}, source="online")
            return
        core = _ACK_STRIP_RE.sub("", accum)
        meaningful = sum(1 for ch in core if ch not in _OVERLAP_ACK_CHARS)
        if meaningful >= _online_cfg.min_chars:
            # VAD 佐证:仅当 VAD 也确认用户此刻(或刚刚)在说话,才认为是真打断。
            # 在线2pass 文本比音频滞后 ~0.5s,而 VAD 开口几乎即时——若文本到了 VAD 仍
            # 从未开口,几乎必是幽灵词/识别噪声 → 不打断、清掉累积,免误打断。
            vad_ok = bool(_online_state["vad_speaking"]) or (
                now - float(_online_state["vad_off_ts"]) < _ONLINE_VAD_GRACE
            )
            if not vad_ok:
                _online_state["accum"] = ""
                _append_turn_log(f"ONLINE_INTERRUPT_SKIP_NO_VAD text={accum!r} chars={meaningful}")
                return
            _online_state["fired_at"] = now
            _online_state["accum"] = ""
            session.interrupt()
            broadcast({"type": "clear"})
            _broadcast_audio_ctrl({"type": "clear"})
            _append_turn_log(
                f"OVERLAP_ONLINE_INTERRUPT text={accum!r} chars={meaningful} -> interrupt"
            )

    def _online_text_fanout(piece: str, segment_end: bool) -> None:
        # 打断逻辑优先、原样执行;显示为 best-effort(feed_online 内部已全兜底,不会拖慢打断)。
        _on_online_text(piece, segment_end)
        # 显示:流式主STT 模式下气泡由主STT 驱动(同源),此处不再喂在线2pass 以免双驱动。
        if _live is not None and not _live_from_main:
            _live.feed_online(piece, segment_end)

    _online_reason = _online_unavailable_reason(_online_cfg)
    if _online_reason is None and session.input.audio is not None:
        online_tap = OnlineAsrTap(
            _online_cfg, hotwords=_funasr_hotwords(), on_text=_online_text_fanout
        )
        online_tap.start()
        session.input.audio = OnlineTapAudioInput(session.input.audio, online_tap)
        ctx.add_shutdown_callback(online_tap.aclose)
        _append_turn_log(f"ONLINE_INTERRUPT_ACTIVE min_chars={_online_cfg.min_chars}")
    else:
        _append_turn_log(f"ONLINE_INTERRUPT_DISABLED reason={_online_reason!r}")

    # 开场白:固定文案(稳定、可复现、首字延迟低),口吻与小歌人设一致。say() 仍会经过
    # transcription_node(广播到网页气泡)与录音 tap。放在所有 tap(录音/KWS/在线打断)
    # 装好之后触发,确保被如实录进录音(放在 on_enter 会早于录音 tap 安装 -> 漏录)。
    session.say("你好呀，我是小歌。有什么想聊的、想问的，随时跟我说。")


if __name__ == "__main__":
    # Start the web UI server in a background thread
    t = threading.Thread(
        target=_start_web_server_thread,
        args=(_WEB_PORT,),
        daemon=True,
        name="web-ui",
    )
    t.start()

    # Give the server a moment to bind; open browser only in local mode.
    import time as _time

    _time.sleep(0.8)
    if _WEB_HOST in ("localhost", "127.0.0.1"):
        webbrowser.open(f"http://localhost:{_WEB_PORT}")
        logger.info("Opening browser at http://localhost:%d", _WEB_PORT)
    else:
        logger.info(
            "Web UI listening on http://0.0.0.0:%d - open from browser at http://<server-ip>:%d",
            _WEB_PORT,
            _WEB_PORT,
        )

    cli.run_app(server)
