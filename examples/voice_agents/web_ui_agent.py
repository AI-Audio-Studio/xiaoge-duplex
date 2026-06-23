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
import re
import sys
import threading
import time
import webbrowser
from pathlib import Path

import aiohttp
import aiohttp.web
import httpx
import openai
from audio_recorder import AudioRecorder
from custom_audio_providers import (
    CosyVoiceStreamingTTS,
    FunASROfflineSTT,
    HttpStreamingTTS,
    Qwen3ASROfflineSTT,
    QwenStreamingTTS,
    _funasr_hotwords,
)
from dotenv import load_dotenv
from kws_interrupt import KwsConfig, KwsTapAudioInput, NativeKwsSpotter, _unavailable_reason
from live_transcript import LiveTranscript, LiveTranscriptConfig
from online_interrupt import (
    OnlineAsrTap,
    OnlineInterruptConfig,
    OnlineTapAudioInput,
    unavailable_reason as _online_unavailable_reason,
)
from turn_config import TurnConfig

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
from livekit.plugins import openai as lk_openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

logger = logging.getLogger("web-ui-agent")
load_dotenv(override=True)

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# 注:全量 DEBUG 日志已整合进测试工具(AGENT_TIMELINE=1 时写 runs/<ts>/debug.log,
# 见 event_timeline.install_debug_log)。正常运行不再挂任何文件日志处理器(零开销),
# 也不再写 .run/agent.log。

_TURN_METRICS_LOG = Path(os.getenv("TURN_METRICS_LOG", "qwen_voice_turn_metrics.log")).resolve()
_PURE_DIGIT_RE = re.compile(r"^\d{2,16}$")
_STOP_WORDS = (
    "停",
    "停下",
    "停一下",
    "暂停",
    "好了",
    "行了",
    "别说",
    "别说了",
    "别讲",
    "别讲了",
    "别念了",
    "等一下",
    "等等",
    "等下",
    "稍等",
    "知道了",
    "我知道了",
    "闭嘴",
    "安静",
    "不听了",
    "不用了",
    "不要了",
    "休庭",
)
_STOP_LEAD_IN = r"(?:那|你|就|请|先|那你|那就)?"
_STOP_REPLY_PATTERNS = tuple(
    re.compile(rf"^\s*{_STOP_LEAD_IN}{re.escape(w)}[一下吧呢啊呀了嘛]*\s*[。.！!，,、\s]*$")
    for w in _STOP_WORDS
)
_BACKCHANNEL_CHARS = "嗯哦噢喔啊呃唉唔诶哼呢"
_BACKCHANNEL_RE = re.compile(rf"^[{_BACKCHANNEL_CHARS}][{_BACKCHANNEL_CHARS}，,。.、！!？?～~\s]*$")
_OVERLAP_ACK_CHARS = _BACKCHANNEL_CHARS + "对好是行的呀嘛"
_ACK_STRIP_RE = re.compile(r"[\s，,。.、！!？?～~；;：:]+")
_SEGMENT_SPLIT_RE = re.compile(r"[\s，,。.、！!？?～~；;：:]+")
_overlap_turn_state: dict[str, bool] = {"user_spoke_over_agent": False}


def _is_overlap_ack(text: str | None) -> bool:
    if text is None:
        return False
    core = _ACK_STRIP_RE.sub("", text.strip())
    return bool(core) and all(ch in _OVERLAP_ACK_CHARS for ch in core)


def _configure_utf8_stdio() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass


_configure_utf8_stdio()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _should_ignore_user_turn(text: str | None) -> bool:
    if text is None:
        return False
    segments = [seg for seg in _SEGMENT_SPLIT_RE.split(text.strip()) if seg]
    if not segments:
        return False
    has_stop_word = False
    for seg in segments:
        if any(pattern.fullmatch(seg) for pattern in _STOP_REPLY_PATTERNS):
            has_stop_word = True
            continue
        if all(ch in _OVERLAP_ACK_CHARS for ch in seg):
            continue
        return False
    return has_stop_word


def _is_backchannel(text: str | None) -> bool:
    if text is None:
        return False
    normalized = text.strip()
    return bool(normalized) and bool(_BACKCHANNEL_RE.fullmatch(normalized))


def _normalize_spoken_digit_sequence(text: str | None) -> str | None:
    if text is None:
        return None
    stripped = text.strip()
    if not _PURE_DIGIT_RE.fullmatch(stripped):
        return text
    return "、".join(stripped)


def _ms(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 1000:.1f}ms"


def _append_turn_log(line: str) -> None:
    now = time.time()
    ts = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}.{int((now % 1) * 1000):03d}"
    with _TURN_METRICS_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {line}\n")


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
_ws_clients: set[aiohttp.web.WebSocketResponse] = set()
_web_loop: asyncio.AbstractEventLoop | None = None
_agent_loop: asyncio.AbstractEventLoop | None = None
_switchable_stt: SwitchableSTT | None = None
_switchable_tts: SwitchableTTS | None = None
_test_recorder = None  # 测试模式下的多轨录音器(供 /api/mic 暂停/继续录制)
_tts_backend_key: str = "cosyvoice"

# ─── HTML page (embedded) ────────────────────────────────────────────────────

_HTML = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>小歌语音助手 · 测试面板</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#0d0d14;color:#e0e0ee;
  height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{background:#131320;padding:12px 20px;display:flex;align-items:center;gap:10px;
  border-bottom:1px solid #232340;flex-shrink:0}
.dot{width:9px;height:9px;border-radius:50%;background:#333;transition:background .3s}
.dot.ok{background:#4ade80}.dot.speak{background:#fbbf24;animation:blink 1s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
h1{font-size:16px;font-weight:600;flex:1}
#badge{font-size:12px;padding:3px 10px;border-radius:10px;background:#1e1e30;color:#6b7280}
.ctrl{background:#131320;padding:10px 20px;display:flex;gap:10px;align-items:center;
  border-bottom:1px solid #232340;flex-shrink:0;flex-wrap:wrap}
.btn{padding:7px 15px;border:none;border-radius:7px;cursor:pointer;
  font-size:13px;font-weight:500;transition:all .2s}
#micBtn{background:#22c55e;color:#000}
#micBtn.muted{background:#ef4444;color:#fff}
.asr-grp{display:flex;gap:6px;align-items:center}
.asr-grp label{font-size:12px;color:#6b7280}
.asr-tab{padding:5px 12px;border:1px solid #2a2a50;border-radius:6px;
  background:transparent;color:#6b7280;cursor:pointer;font-size:12px;transition:all .2s}
.asr-tab.on{background:#6d28d9;border-color:#6d28d9;color:#fff}
#clearBtn{background:#1a1a2a;color:#6b7280;margin-left:auto}
.log{flex:1;overflow-y:auto;padding:16px 20px;display:flex;flex-direction:column;gap:10px}
.bubble{max-width:72%;padding:10px 14px;border-radius:12px;font-size:14px;line-height:1.55;word-break:break-all}
.bubble.user{align-self:flex-end;background:#5b21b6;color:#f0f0ff;border-bottom-right-radius:3px}
.bubble.assistant{align-self:flex-start;background:#1a1a2e;border:1px solid #252545;
  border-bottom-left-radius:3px}
.bubble .ts{font-size:11px;margin-top:4px;opacity:.4}
.bubble.appear{animation:bubbleIn .18s ease-out}
@keyframes bubbleIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.bubble.user.live{opacity:.7}
.live-dots i{animation:liveBlink 1.2s infinite}
.live-dots i:nth-child(2){animation-delay:.2s}.live-dots i:nth-child(3){animation-delay:.4s}
@keyframes liveBlink{0%,100%{opacity:.25}50%{opacity:1}}
.sys-msg{align-self:center;font-size:11px;color:#374151;padding:2px 10px}
.sbar{background:#0a0a11;padding:6px 20px;font-size:11px;color:#374151;display:flex;
  gap:16px;border-top:1px solid #131320;flex-shrink:0}
</style>
</head>
<body>
<header>
  <div class="dot" id="dot"></div>
  <h1>小歌语音助手</h1>
  <span id="badge">未连接</span>
</header>
<div class="ctrl">
  <button class="btn" id="micBtn" onclick="toggleMic()">&#127908; 麦克风开启</button>
  <div class="asr-grp">
    <label>ASR 模型：</label>
    <button class="asr-tab"    id="tabQwen3Stream" onclick="switchASR('qwen3-stream')">Qwen3-流式</button>
    <button class="asr-tab"    id="tabQwen3"       onclick="switchASR('qwen3')">Qwen3-ASR</button>
    <button class="asr-tab on" id="tabFunasr"      onclick="switchASR('funasr')">FunASR</button>
  </div>
  <div class="asr-grp">
    <label>TTS 模型：</label>
    <button class="asr-tab"    id="tabTtsQwen" onclick="switchTTS('qwen')">Qwen DashScope</button>
    <button class="asr-tab on" id="tabTtsCosy" onclick="switchTTS('cosyvoice')">CosyVoice</button>
    <button class="asr-tab"    id="tabTtsHttp" onclick="switchTTS('http')">HTTP TTS</button>
  </div>
  <button class="btn" id="clearBtn" onclick="clearLog()">清空记录</button>
</div>
<div class="log" id="log"></div>
<div class="sbar">
  <span id="sbWs">WS: 断开</span>
  <span id="sbMic">麦克风: 开启</span>
  <span id="sbAsr">ASR: Qwen3-流式</span>
  <span id="sbTts">TTS: CosyVoice</span>
  <span id="sbMsgs" style="margin-left:auto">0 条消息</span>
</div>
<script>
var ws=null, muted=false, msgN=0, curAsr='funasr', curTts='cosyvoice', rt=null;
var liveBubble=null, liveTimer=null;       // 用户实时转写的单一 live 气泡
var LIVE_DANGLING_MS=4000;                  // 无定稿的残留气泡兜底淡出(毫秒)

function conn(){
  if(ws && ws.readyState===WebSocket.OPEN) return;
  ws = new WebSocket('ws://localhost:'+location.port+'/ws');
  ws.onopen = function(){
    id('dot').className='dot ok';
    id('sbWs').textContent='WS: 已连接';
    sysMsg('已连接到语音助手');
    if(rt){clearTimeout(rt);rt=null;}
  };
  ws.onclose = function(){
    id('dot').className='dot';
    id('sbWs').textContent='WS: 断开';
    sysMsg('连接断开，5 秒后重连…');
    rt = setTimeout(conn, 5000);
  };
  ws.onmessage = function(e){ handle(JSON.parse(e.data)); };
}

function handle(m){
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
  }
}

// ── 用户实时转写:单一 live 气泡(开口出现、partial 边长、final 定稿)──────────
// 一次连续说话 = 一个气泡:startLive 只在"新一轮"被后端调用(见 live_transcript.py);
// 连续说话的小停顿不会重开。无定稿的残留气泡由超时兜底丢弃。
function startLive(){
  discardLive();
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

function setAgent(s){
  var labels={idle:'空闲', listening:'监听中', thinking:'思考中', speaking:'播报中'};
  id('badge').textContent = labels[s]||s;
  id('dot').className = 'dot '+(s==='speaking'?'speak':'ok');
}

function setMic(m){
  muted=m;
  id('micBtn').className='btn'+(m?' muted':'');
  id('micBtn').textContent=(m?'&#128263; 麦克风关闭':'&#127908; 麦克风开启');
  id('sbMic').textContent='麦克风: '+(m?'关闭':'开启');
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
  log().appendChild(d);
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


async def _handle_index(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.Response(text=_HTML, content_type="text/html", charset="utf-8")


async def _handle_ws(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    ws = aiohttp.web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _ws_clients.add(ws)

    # Push current state immediately on connect
    stt = _switchable_stt
    await ws.send_str(
        json.dumps(
            {
                "type": "state",
                "muted": stt.muted if stt else False,
                "stt_backend": stt.provider if stt else "FunASR",
                "tts_backend": _tts_backend_key,
            },
            ensure_ascii=False,
        )
    )

    async for _ in ws:
        pass  # keep-alive; messages from browser not used

    _ws_clients.discard(ws)
    return ws


async def _handle_mic(request: aiohttp.web.Request) -> aiohttp.web.Response:
    stt = _switchable_stt
    if stt is None:
        return aiohttp.web.json_response({"error": "agent not ready"}, status=503)
    stt.muted = not stt.muted
    # 麦克风关闭 -> 暂停录制(用户轨),开启 -> 继续(测试模式下)。
    if _test_recorder is not None:
        _test_recorder.set_paused(stt.muted)
    broadcast({"type": "state", "muted": stt.muted})
    logger.info("mic %s", "muted" if stt.muted else "unmuted")
    return aiohttp.web.json_response({"muted": stt.muted})


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
    global _web_loop
    _web_loop = asyncio.get_running_loop()

    app = aiohttp.web.Application()
    app.router.add_get("/", _handle_index)
    app.router.add_get("/ws", _handle_ws)
    app.router.add_post("/api/mic", _handle_mic)
    app.router.add_post("/api/asr", _handle_switch_asr)
    app.router.add_post("/api/tts", _handle_switch_tts)

    runner = aiohttp.web.AppRunner(app, access_log=None)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "localhost", port)
    await site.start()
    logger.info("Web UI available at http://localhost:%d", port)

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
小歌：行啊，想听冒险的，还是温馨一点的？"""
            )
        )

    async def on_enter(self) -> None:
        # 开场白不在这里触发:on_enter 在 session.start() 期间执行,早于录音 tap 安装,
        # 会导致开场白漏录。改到 entrypoint 中、所有 tap 装好之后再触发(见 _GREETING)。
        pass

    async def transcription_node(self, text, model_settings):  # type: ignore[override]
        """Intercept the LLM text stream to push the reply to the browser as soon as
        generation finishes — well before TTS playback ends."""
        collected: list[str] = []
        async for chunk in text:
            collected.append(chunk)  # TimedString subclasses str, so this always works
            yield chunk
        full_text = "".join(collected).strip()
        if full_text:
            broadcast(
                {"type": "message", "role": "assistant", "text": full_text, "ts": time.time()}
            )

    async def on_user_turn_completed(self, turn_ctx, new_message: ChatMessage) -> None:
        spoke_over_agent = _overlap_turn_state["user_spoke_over_agent"]
        original = new_message.text_content

        if _should_ignore_user_turn(original):
            logger.info("stop phrase -> force interrupt + skip reply: %r", original)
            _append_turn_log(f"STOP_PHRASE text={original!r} -> force_interrupt + skip_reply")
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


server = AgentServer()


def prewarm(proc: JobProcess) -> None:
    # 判停旋钮集中在 TurnConfig;默认 = 原写死值(0.35),不设 TURN_* 即无变化。
    _tc = TurnConfig.from_env()
    proc.userdata["vad"] = silero.VAD.load(min_silence_duration=_tc.vad_min_silence_s)


server.setup_fnc = prewarm


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    global _switchable_stt, _switchable_tts, _agent_loop, _test_recorder
    _agent_loop = asyncio.get_running_loop()

    stt_engine = build_stt()
    _switchable_stt = stt_engine  # expose for web server
    _append_turn_log(f"STT_START provider={stt_engine.provider}")

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
        stt=StreamAdapter(stt=stt_engine, vad=ctx.proc.userdata["vad"]),
        vad=ctx.proc.userdata["vad"],
        tts=tts_engine,
        turn_handling=_turn_cfg.turn_handling(MultilingualModel()),
    )

    turn_trace: dict[str, float] = {"started_at": time.time()}
    _online_state: dict[str, object] = {"accum": "", "fired_at": 0.0}

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
                    "text": item.text_content or "",
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
        if event.new_state == "speaking":
            _overlap_turn_state["user_spoke_over_agent"] = session.agent_state == "speaking"
            asyncio.create_task(asyncio.to_thread(tts_engine.prewarm_connection))
            asyncio.create_task(stt_engine.prewarm_connection())

    @session.on("user_input_transcribed")
    def _on_stt(event) -> None:
        if not event.is_final:
            return
        _append_turn_log(f"STT_FINAL text={event.transcript!r}")
        if _should_ignore_user_turn(event.transcript):
            session.interrupt(force=True)
            _append_turn_log(f"STOP_PHRASE_EARLY text={event.transcript!r} -> force_interrupt")
            return
        if _overlap_turn_state["user_spoke_over_agent"] and _is_overlap_ack(event.transcript):
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
        session.interrupt(force=True)
        logger.info("KWS strong interrupt: %r", keyword)
        _append_turn_log(f"STOP_KWS_EARLY keyword={keyword!r} -> force_interrupt")
        if _timeline is not None:
            _timeline.emit("interrupt.kws", {"keyword": keyword}, source="kws")

    _kws_config = KwsConfig.from_env()
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
        if segment_end:
            _online_state["accum"] = ""
            return
        if session.agent_state != "speaking":
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
            _append_turn_log(f"STOP_ONLINE_EARLY text={accum!r} -> force_interrupt")
            if _timeline is not None:
                _timeline.emit("interrupt.online", {"text": accum, "kind": "stop"}, source="online")
            return
        core = _ACK_STRIP_RE.sub("", accum)
        meaningful = sum(1 for ch in core if ch not in _OVERLAP_ACK_CHARS)
        if meaningful >= _online_cfg.min_chars:
            _online_state["fired_at"] = now
            _online_state["accum"] = ""
            session.interrupt()
            _append_turn_log(
                f"OVERLAP_ONLINE_INTERRUPT text={accum!r} chars={meaningful} -> interrupt"
            )

    def _online_text_fanout(piece: str, segment_end: bool) -> None:
        # 打断逻辑优先、原样执行;显示为 best-effort(feed_online 内部已全兜底,不会拖慢打断)。
        _on_online_text(piece, segment_end)
        if _live is not None:
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

    # Give the server a moment to bind, then open the browser
    import time as _time

    _time.sleep(0.8)
    webbrowser.open(f"http://localhost:{_WEB_PORT}")
    logger.info("Opening browser at http://localhost:%d", _WEB_PORT)

    cli.run_app(server)
