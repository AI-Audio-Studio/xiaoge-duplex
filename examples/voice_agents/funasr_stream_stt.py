"""FunASR 2pass 流式主 STT(判停/主STT 改造 · optimized 栈)。

设计见 TURN_STT_DESIGN.md。要点:
  - 流式:一条 WS 贯穿整轮会话,边说边出 interim/final,长句不丢字(替代离线+VAD 切段)。
  - **内置 VAD 输出门控防幽灵**:静音/底噪期 ASR 蹦出的文本一律丢弃(不累加/不发)。
  - **GAP 轮次聚合**:以"最后一帧有声"起算静默,静默 ≥ GAP 才发一条 FINAL(=一轮),
    中途短停顿只累加 → 一段连续话 = 一轮 = 一次回复。
  - 上游 endpointing/turn detector 不改:无 FINAL 时上游 EOU 空 transcript 早返回。

零改上游;opt-in(STT_BACKEND=funasr-stream / XIAOGE_STACK=optimized)。
内置 VAD 为**独立的 silero 实例**(不与 AgentSession 的 VAD 共享模型状态)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import ssl
import time

import aiohttp

from livekit.agents import APIConnectOptions, LanguageCode, stt
from livekit.agents._exceptions import APIConnectionError
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr
from livekit.agents.vad import VADEventType

logger = logging.getLogger("funasr-stream")

_SAMPLE_RATE = 16000
_WATCHDOG_INTERVAL = 0.05


def _env_f(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    try:
        return float(v)
    except ValueError:
        return default


class FunASRStreamSTT(stt.STT):
    """FunASR 2pass 流式主 STT(内置 VAD 门控 + GAP 聚合)。"""

    def __init__(
        self,
        *,
        ws_url: str | None = None,
        verify_ssl: bool | None = None,
        gap_s: float | None = None,
        vad_activation: float | None = None,
        vad=None,  # 可注入独立 silero VAD;None 则自建
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True, interim_results=True, offline_recognize=False
            )
        )
        self._ws_url = ws_url or os.getenv("FUNASR_WS_URL", "wss://60.205.197.165:10090")
        self._verify_ssl = (
            verify_ssl
            if verify_ssl is not None
            else os.getenv("FUNASR_VERIFY_SSL", "0").strip().lower() in {"1", "true", "yes", "on"}
        )
        self._gap_s = gap_s if gap_s is not None else _env_f("XIAOGE_AGG_GAP", 1.5)
        self._vad_activation = (
            vad_activation
            if vad_activation is not None
            else _env_f("XIAOGE_STREAM_VAD_ACTIVATION", 0.5)
        )
        # 独立 silero VAD(只读逐帧概率做门控/最后有声时刻;不与 AgentSession VAD 共享状态)
        if vad is None:
            from livekit.plugins import silero

            vad = silero.VAD.load()
        self._vad = vad

    @property
    def model(self) -> str:
        return "funasr-2pass"

    @property
    def provider(self) -> str:
        return "FunASR-Stream"

    async def _recognize_impl(self, buffer, *, language=NOT_GIVEN, conn_options):  # type: ignore[override]
        raise NotImplementedError("FunASRStreamSTT is streaming-only (use stream())")

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.RecognizeStream:
        return _FunASRStreamStream(stt=self, conn_options=conn_options)

    async def aclose(self) -> None:
        return


class _FunASRStreamStream(stt.RecognizeStream):
    def __init__(self, *, stt: FunASRStreamSTT, conn_options: APIConnectOptions) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=_SAMPLE_RATE)
        self._impl = stt
        self._gap = stt._gap_s
        self._activation = stt._vad_activation
        # 聚合状态(单事件循环,无需锁)
        self._prefix = ""  # 已收尾段落
        self._seg = ""  # 当前段在线增量
        self._last_voiced: float | None = None
        self._voiced = False
        self._ghost_chars = 0  # 幽灵率 KPI:被门控丢弃的字数

    async def _run(self) -> None:
        ssl_ctx = None
        if self._impl._ws_url.startswith("wss://") and not self._impl._verify_ssl:
            ssl_ctx = ssl._create_unverified_context()
        session = aiohttp.ClientSession()
        vad_stream = self._impl._vad.stream()
        ws: aiohttp.ClientWSResponse | None = None
        try:
            ws = await session.ws_connect(self._impl._ws_url, ssl=ssl_ctx, heartbeat=30)
            await ws.send_str(
                json.dumps(
                    {
                        "mode": "2pass",
                        "chunk_size": [5, 8, 4],
                        "chunk_interval": 10,
                        "wav_name": "funasr-stream",
                        "wav_format": "pcm",
                        "audio_fs": _SAMPLE_RATE,
                        "is_speaking": True,
                        "itn": False,
                    },
                    ensure_ascii=False,
                )
            )
            recv_task = asyncio.create_task(self._funasr_recv(ws), name="funasr-recv")
            vad_task = asyncio.create_task(self._vad_consume(vad_stream), name="funasr-vad")
            wd_task = asyncio.create_task(self._gap_watchdog(), name="funasr-gap")
            try:
                await self._forward(ws, vad_stream)  # 输入结束才返回
            finally:
                with contextlib.suppress(Exception):
                    await ws.send_str(json.dumps({"is_speaking": False}))
                for t in (recv_task, wd_task, vad_task):
                    t.cancel()
                await asyncio.gather(recv_task, wd_task, vad_task, return_exceptions=True)
        finally:
            with contextlib.suppress(Exception):
                await vad_stream.aclose()
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
            with contextlib.suppress(Exception):
                await session.close()

    async def _forward(self, ws, vad_stream) -> None:
        """读输入帧:**实时即送**(帧到即发,无节流),同帧喂内置 VAD。

        关键:输入帧本身已是实时节奏(`async for` 被帧到达驱动),所以直接发送即等于
        实时发送,不会堆积;任何人为节流(sleep)都会让发送慢于实时 → backlog 累积 →
        说得越久显示越落后。参照在线 tap(队列即取即发)。
        """
        async for frame in self._input_ch:
            if isinstance(frame, self._FlushSentinel):
                continue
            with contextlib.suppress(Exception):
                vad_stream.push_frame(frame)  # 基类已重采样到 16k
            await ws.send_bytes(bytes(frame.data))

    async def _vad_consume(self, vad_stream) -> None:
        """逐帧读 VAD 概率,维护 voiced / last_voiced(门控 + GAP 起算)。"""
        async for ev in vad_stream:
            if ev.type == VADEventType.INFERENCE_DONE:
                self._voiced = ev.probability >= self._activation
                if self._voiced:
                    self._last_voiced = time.monotonic()

    def _accepting(self) -> bool:
        """门控:有声、或处于最后有声后的 GAP 窗内(容忍识别延迟的尾巴)。"""
        if self._voiced:
            return True
        return self._last_voiced is not None and (time.monotonic() - self._last_voiced) < self._gap

    async def _funasr_recv(self, ws) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    break
                if msg.type == aiohttp.WSMsgType.ERROR:
                    raise APIConnectionError("FunASR 2pass websocket error")
                continue
            payload = json.loads(msg.data)
            mode = payload.get("mode", "")
            text = (payload.get("text") or "").strip()
            if not text:
                continue
            if not self._accepting():
                self._ghost_chars += len(text)  # 静音期文本 = 幽灵,丢弃
                continue
            if mode == "2pass-online":
                self._seg = self._seg + text
            elif mode == "2pass-offline":
                self._prefix = (self._prefix + text).strip()
                self._seg = ""
            else:
                continue
            self._emit_interim()

    async def _gap_watchdog(self) -> None:
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            pending = (self._prefix + self._seg).strip()
            if (
                pending
                and self._last_voiced is not None
                and (time.monotonic() - self._last_voiced) >= self._gap
            ):
                self._emit_final(pending)
                self._prefix = ""
                self._seg = ""
                self._last_voiced = None

    def _emit_interim(self) -> None:
        text = (self._prefix + self._seg).strip()
        if text:
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language=LanguageCode("zh"), text=text)],
                )
            )

    def _emit_final(self, text: str) -> None:
        self._event_ch.send_nowait(
            stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(language=LanguageCode("zh"), text=text)],
            )
        )
