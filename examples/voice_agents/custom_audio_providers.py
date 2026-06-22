from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import queue
import ssl
import threading
import time
from dataclasses import dataclass

import aiohttp
import dashscope
from dashscope.audio.qwen_tts_realtime.qwen_tts_realtime import (
    AudioFormat as BailianAudioFormat,
    QwenTtsRealtime,
    QwenTtsRealtimeCallback,
)
from dashscope.audio.tts_v2 import (
    AudioFormat as CosyAudioFormat,
    ResultCallback as CosyResultCallback,
    SpeechSynthesizer as CosySpeechSynthesizer,
    SpeechSynthesizerObjectPool as CosySynthPool,
)

from livekit import rtc
from livekit.agents import APIConnectOptions, LanguageCode, stt, tts, utils
from livekit.agents._exceptions import APIConnectionError, APIStatusError
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr
from livekit.agents.utils import shortuuid

logger = logging.getLogger("custom-audio-providers")


class _WebsocketCloseNoiseFilter(logging.Filter):
    """只丢弃 websocket 库对"服务端正常关闭帧"(opcode=8 Bye/goodbye)的 ERROR 噪音。

    CosyVoice 预热池每 ~30s 主动 renew 连接时,旧连接会收到服务端正常关闭帧,被
    websocket-client 库按 ERROR 打印,看着像错误其实无害(合成全程正常)。这里只过滤
    这一种正常关闭噪音,**其他 websocket 错误(握手失败/异常断开等)照常放行**,不掩盖真问题。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not ("opcode=8" in msg and ("goodbye" in msg or "Bye" in msg))


# 安装到 websocket 库的 logger(仅作用于该 logger 的记录,精准、可逆)。
logging.getLogger("websocket").addFilter(_WebsocketCloseNoiseFilter())

# Max seconds to wait for an ASR WebSocket to connect. Without this, switching to
# an unreachable backend would stall each recognition on the TCP connect (~21s on
# Windows). Keep it short so a dead backend fails fast and recovery is quick.
_WS_CONNECT_TIMEOUT = float(os.getenv("ASR_WS_CONNECT_TIMEOUT", "5"))

# Same idea for HTTP TTS: bound the connect so an unreachable endpoint fails fast
# (~5s) instead of hanging on the OS TCP connect (~21s on Windows). Only the
# connect is bounded; streaming the audio body can still take as long as needed.
_TTS_CONNECT_TIMEOUT = float(os.getenv("TTS_CONNECT_TIMEOUT", "5"))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# 短停止词（尤其单字"停"）默认常被识别成"嗯/哦/行"，整轮 0 召回（实测）。
# FunASR WS 协议握手支持热词加权（hotwords 字段，JSON 字符串 词->权重，建议 10~100），
# 把停止词喂进去拉升短词召回。可用 FUNASR_HOTWORDS 覆盖，格式 词:权重|词:权重。
_DEFAULT_HOTWORDS: dict[str, int] = {
    "停": 40,
    "停下": 30,
    "停一下": 30,
    "别说了": 30,
    "别讲了": 30,
    "不要讲了": 20,
    "等等": 20,
    "等一下": 20,
    "继续": 20,
}


def _funasr_hotwords() -> str:
    raw = os.getenv("FUNASR_HOTWORDS", "").strip()
    if not raw:
        return json.dumps(_DEFAULT_HOTWORDS, ensure_ascii=False)
    words: dict[str, int] = {}
    for token in raw.split("|"):
        word, _, weight = token.partition(":")
        word = word.strip()
        if not word:
            continue
        try:
            words[word] = int(weight)
        except ValueError:
            words[word] = 20
    return json.dumps(words, ensure_ascii=False) if words else ""


def _acquire_http_session() -> tuple[aiohttp.ClientSession, bool]:
    """Return (session, owns). Prefer LiveKit's shared http session; fall back to a
    private ClientSession the caller is responsible for closing (owns=True)."""
    try:
        return utils.http_context.http_session(), False
    except RuntimeError:
        return aiohttp.ClientSession(), True


def _resample_pcm(buffer: utils.AudioBuffer, target_rate: int) -> bytes:
    """Merge an AudioBuffer and resample to mono 16-bit PCM at target_rate."""
    frame = utils.merge_frames(buffer) if isinstance(buffer, list) else buffer
    if frame.sample_rate != target_rate or frame.num_channels != 1:
        resampler = rtc.AudioResampler(
            input_rate=frame.sample_rate,
            output_rate=target_rate,
            num_channels=1,
            quality=rtc.AudioResamplerQuality.HIGH,
        )
        frames = resampler.push(frame)
        frames.extend(resampler.flush())
        frame = rtc.combine_audio_frames(frames)
    return bytes(frame.data)


@dataclass
class FunASROptions:
    websocket_url: str
    sample_rate: int = 16000
    verify_ssl: bool = False
    language: str = "zh"
    chunk_size: int = 3200


class FunASROfflineSTT(stt.STT):
    def __init__(
        self,
        *,
        websocket_url: str | None = None,
        sample_rate: int = 16000,
        verify_ssl: bool | None = None,
        language: str = "zh",
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
                aligned_transcript=False,
                offline_recognize=True,
            )
        )
        self._opts = FunASROptions(
            websocket_url=websocket_url or os.getenv("FUNASR_WS_URL", "wss://60.205.197.165:10090"),
            sample_rate=sample_rate,
            verify_ssl=verify_ssl
            if verify_ssl is not None
            else _env_bool("FUNASR_VERIFY_SSL", False),
            language=language,
        )
        self._session = session
        self._owns_session = False
        # 跨轮复用的持久 WS：每轮新建连接要付 ~190ms TCP+TLS+upgrade。实测远端
        # 支持同一连接连续多段 offline 识别，故连一次反复用。recognize 按轮串行，
        # 用 _ws_lock 串起来；发送/接收失败或服务端关闭则重连一次重试。
        self._ws: aiohttp.ClientWSResponse | None = None
        self._ws_lock = asyncio.Lock()

    @property
    def model(self) -> str:
        return "funasr-offline"

    @property
    def provider(self) -> str:
        return "FunASR"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session, owns = _acquire_http_session()
            if owns:
                self._owns_session = True
        return self._session

    def _resample_to_pcm(self, buffer: utils.AudioBuffer) -> bytes:
        return _resample_pcm(buffer, self._opts.sample_rate)

    async def _ensure_ws(self) -> aiohttp.ClientWSResponse:
        ws = self._ws
        if ws is not None and not ws.closed:
            return ws
        ssl_ctx = None
        # 内网自签证书端点:仅当 verify_ssl=False(由 FUNASR_VERIFY_SSL 控制,默认 false)时
        # 才跳过校验。生产/公网请置 true。见 .env.example。
        if self._opts.websocket_url.startswith("wss://") and not self._opts.verify_ssl:
            ssl_ctx = ssl._create_unverified_context()
        session = self._ensure_session()
        self._ws = await asyncio.wait_for(
            session.ws_connect(
                self._opts.websocket_url,
                ssl=ssl_ctx,
                heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_receive=30.0),
            ),
            timeout=_WS_CONNECT_TIMEOUT,
        )
        return self._ws

    async def _reset_ws(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _recognize_once(self, pcm: bytes, conn_options: APIConnectOptions) -> tuple[str, str]:
        """单段 offline 识别，复用持久 ws。返回 (transcript, request_id)。
        失败（服务端关闭/出错）抛异常，由调用方重连重试。"""
        ws = await self._ensure_ws()
        init_payload = {
            "mode": "offline",
            "chunk_size": [5, 10, 5],
            "chunk_interval": 10,
            "wav_name": "livekit-console",
            "wav_format": "pcm",
            "audio_fs": self._opts.sample_rate,
            "is_speaking": True,
            "itn": False,
        }
        if hotwords := _funasr_hotwords():
            init_payload["hotwords"] = hotwords
        await ws.send_str(json.dumps(init_payload, ensure_ascii=False))

        # 离线模式：服务端攒齐 is_speaking:False 才识别，client 端限速毫无意义、
        # 只会拖慢"说完"信号。全速上传（仍分片以免单帧过大）。
        for i in range(0, len(pcm), self._opts.chunk_size):
            await ws.send_bytes(pcm[i : i + self._opts.chunk_size])

        await ws.send_str(json.dumps({"is_speaking": False}, ensure_ascii=False))

        transcript = ""
        request_id = ""
        got_final = False
        deadline = time.monotonic() + max(5.0, conn_options.timeout)
        while time.monotonic() < deadline:
            timeout = max(0.1, deadline - time.monotonic())
            try:
                msg = await ws.receive(timeout=timeout)
            except asyncio.TimeoutError:
                break

            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = json.loads(msg.data)
                request_id = payload.get("wav_name", request_id)
                if payload.get("text"):
                    transcript = payload["text"].strip()
                if payload.get("is_final") is True:
                    got_final = True
                    break
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                raise APIConnectionError("FunASR websocket closed mid-recognition")
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise APIConnectionError("FunASR websocket closed with error")

        # 没拿到 is_final（超时）说明这条连接状态不明，关掉它让下一轮重连，避免残留帧串台。
        if not got_final:
            await self._reset_ws()
        return transcript, request_id

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        pcm = self._resample_to_pcm(buffer)
        if not pcm:
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(language=LanguageCode("zh"), text="")],
            )

        async with self._ws_lock:
            try:
                transcript, request_id = await self._recognize_once(pcm, conn_options)
            except (APIConnectionError, aiohttp.ClientError, ConnectionError, OSError):
                # 持久连接可能已被服务端/中间设备闲置断开：重连一次重试整段。
                await self._reset_ws()
                transcript, request_id = await self._recognize_once(pcm, conn_options)

        event_language = (
            LanguageCode(language)
            if language is not NOT_GIVEN
            else LanguageCode(self._opts.language)
        )
        logger.info(
            "funasr final transcript request_id=%s text=%r",
            request_id,
            transcript,
        )
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=request_id,
            alternatives=[stt.SpeechData(language=event_language, text=transcript)],
        )

    async def aclose(self) -> None:
        await self._reset_ws()
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._owns_session = False


class Qwen3ASROfflineSTT(stt.STT):
    """Offline STT backed by Qwen3-ASR growing-buffer WebSocket service.

    支持预热连接（prewarm_connection）：用户开始说话时提前建好 WS，把握手延迟
    藏进说话期间，用户说完后直接复用已建连接发音频。
    """

    def __init__(
        self,
        *,
        websocket_url: str | None = None,
        sample_rate: int = 16000,
        chunk_size: int = 3200,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
                aligned_transcript=False,
                offline_recognize=True,
            )
        )
        self._url = websocket_url or os.getenv(
            "QWEN3_ASR_WS_URL", "ws://60.205.197.165:10091/ws/transcribe"
        )
        self._sample_rate = sample_rate
        self._chunk_size = chunk_size
        self._session = session
        self._owns_session = False
        self._warm_ws: aiohttp.ClientWebSocketResponse | None = None
        self._prewarming: bool = False

    @property
    def model(self) -> str:
        return "qwen3-asr"

    @property
    def provider(self) -> str:
        return "Qwen3ASR"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session, owns = _acquire_http_session()
            if owns:
                self._owns_session = True
        return self._session

    def _resample_to_pcm(self, buffer: utils.AudioBuffer) -> bytes:
        return _resample_pcm(buffer, self._sample_rate)

    async def prewarm_connection(self) -> None:
        """用户开始说话时调用：提前建好 WS，将握手延迟藏进说话期间。

        asyncio 单线程：无需锁。prewarm 未完成时 _recognize_once 直接开新连接，
        不会等待 prewarm——避免连接慢时阻塞识别。
        """
        if self._warm_ws is not None and not self._warm_ws.closed:
            return
        if self._prewarming:
            return
        self._prewarming = True
        try:
            ws = await asyncio.wait_for(
                self._ensure_session().ws_connect(
                    self._url, timeout=aiohttp.ClientWSTimeout(ws_close=5.0)
                ),
                timeout=_WS_CONNECT_TIMEOUT,
            )
            self._warm_ws = ws
            logger.info("asr prewarm: connected to %s", self._url)
        except Exception as exc:
            logger.info("asr prewarm failed: %s", exc)
        finally:
            self._prewarming = False

    async def _send_audio_and_recv(
        self, ws: aiohttp.ClientWebSocketResponse, pcm: bytes, conn_options: APIConnectOptions
    ) -> str:
        """在已建立的 WS 上发送音频、等待识别结果。"""
        for i in range(0, len(pcm), self._chunk_size):
            await ws.send_bytes(pcm[i : i + self._chunk_size])
        await ws.send_str(json.dumps({"action": "finalize"}))

        transcript = ""
        deadline = time.monotonic() + max(5.0, conn_options.timeout)
        got_response = False

        while time.monotonic() < deadline:
            recv_timeout = 0.4 if got_response else max(0.1, deadline - time.monotonic())
            try:
                msg = await ws.receive(timeout=recv_timeout)
            except asyncio.TimeoutError:
                break

            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = json.loads(msg.data)
                full_text = (payload.get("full_text") or payload.get("text") or "").strip()
                if full_text:
                    transcript = full_text
                got_response = True
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise APIConnectionError("Qwen3-ASR websocket error")

        return transcript

    async def _recognize_once(self, pcm: bytes, conn_options: APIConnectOptions) -> str:
        # 无锁取预热连接：asyncio 单线程，两行赋值之间无 yield，天然原子。
        # prewarm 若还在 await ws_connect() 中，_warm_ws 仍为 None，直接走新连接，
        # 不等待——这是关键：recognition 绝不因 prewarm 慢而阻塞。
        warm_ws = self._warm_ws
        self._warm_ws = None

        if warm_ws is not None and not warm_ws.closed:
            logger.info("asr: using prewarmed connection")
            try:
                return await self._send_audio_and_recv(warm_ws, pcm, conn_options)
            finally:
                with contextlib.suppress(Exception):
                    await warm_ws.close()

        logger.info("asr: opening new connection (no prewarm, warm_ws=%s)", warm_ws)
        ws = await asyncio.wait_for(
            self._ensure_session().ws_connect(
                self._url, timeout=aiohttp.ClientWSTimeout(ws_close=5.0)
            ),
            timeout=_WS_CONNECT_TIMEOUT,
        )
        try:
            return await self._send_audio_and_recv(ws, pcm, conn_options)
        finally:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        pcm = self._resample_to_pcm(buffer)
        if not pcm:
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(language=LanguageCode("zh"), text="")],
            )

        try:
            transcript = await self._recognize_once(pcm, conn_options)
        except (aiohttp.ClientError, ConnectionError, OSError) as e:
            raise APIConnectionError(f"Qwen3-ASR connection failed: {e}") from e

        lang = LanguageCode(language) if language is not NOT_GIVEN else LanguageCode("zh")
        request_id = shortuuid("qwen3-asr-")
        logger.info("qwen3-asr final transcript request_id=%s text=%r", request_id, transcript)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=request_id,
            alternatives=[stt.SpeechData(language=lang, text=transcript)],
        )

    async def aclose(self) -> None:
        warm_ws = self._warm_ws
        self._warm_ws = None
        if warm_ws is not None:
            with contextlib.suppress(Exception):
                await warm_ws.close()
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._owns_session = False


class FunASRStreamingSTT(stt.STT):
    """Streaming FunASR (2pass) STT: emits interim (2pass-online) + final (2pass-offline).

    Interim transcripts make the `min_words` interruption gate functional under VAD-only
    interruption, so short backchannels ("嗯") can be blocked while multi-word real
    interruptions still cut playback mid-speech.
    """

    def __init__(
        self,
        *,
        websocket_url: str | None = None,
        sample_rate: int = 16000,
        verify_ssl: bool | None = None,
        language: str = "zh",
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
                aligned_transcript=False,
                offline_recognize=False,
            )
        )
        self._opts = FunASROptions(
            websocket_url=websocket_url or os.getenv("FUNASR_WS_URL", "wss://60.205.197.165:10090"),
            sample_rate=sample_rate,
            verify_ssl=verify_ssl
            if verify_ssl is not None
            else _env_bool("FUNASR_VERIFY_SSL", False),
            language=language,
        )
        self._session = session
        self._owns_session = False

    @property
    def model(self) -> str:
        return "funasr-2pass"

    @property
    def provider(self) -> str:
        return "FunASR"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session, owns = _acquire_http_session()
            if owns:
                self._owns_session = True
        return self._session

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        raise NotImplementedError("FunASRStreamingSTT only supports stream()")

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> _FunASRStream:
        return _FunASRStream(
            stt=self,
            conn_options=conn_options,
            opts=self._opts,
            session=self._ensure_session(),
        )

    async def aclose(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._owns_session = False


class _FunASRStream(stt.RecognizeStream):
    def __init__(
        self,
        *,
        stt: FunASRStreamingSTT,
        conn_options: APIConnectOptions,
        opts: FunASROptions,
        session: aiohttp.ClientSession,
    ) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=opts.sample_rate)
        self._opts = opts
        self._session = session

    async def _run(self) -> None:
        ssl_ctx = None
        if self._opts.websocket_url.startswith("wss://") and not self._opts.verify_ssl:
            ssl_ctx = ssl._create_unverified_context()

        init_payload = {
            "mode": "2pass",
            "chunk_size": [5, 10, 5],
            "chunk_interval": 10,
            "wav_name": "livekit-stream",
            "wav_format": "pcm",
            "audio_fs": self._opts.sample_rate,
            "is_speaking": True,
            "itn": False,
        }
        if hotwords := _funasr_hotwords():
            init_payload["hotwords"] = hotwords

        ws_timeout = aiohttp.ClientWSTimeout(ws_receive=30.0)
        async with self._session.ws_connect(
            self._opts.websocket_url,
            ssl=ssl_ctx,
            heartbeat=30,
            timeout=ws_timeout,
        ) as ws:
            await ws.send_str(json.dumps(init_payload, ensure_ascii=False))

            async def send_task() -> None:
                # FunASR 2pass does its own VAD segmentation, so FlushSentinel is ignored;
                # is_speaking:false is only sent once input ends to flush the final segment.
                try:
                    async for data in self._input_ch:
                        if isinstance(data, rtc.AudioFrame):
                            await ws.send_bytes(bytes(data.data))
                finally:
                    with contextlib.suppress(Exception):
                        await ws.send_str(json.dumps({"is_speaking": False}, ensure_ascii=False))

            # FunASR 2pass 常给碎片 final 加前导标点（"，这"/"。这片子"），判停模型会
            # 当成"句子没说完"而干等到 max_delay。剥掉前导标点让 EOU 判断更准。
            lead_punct = "，,。.、！!？?；;：:～~ \t\n"

            async def recv_task() -> None:
                language = LanguageCode(self._opts.language)
                speaking = False
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        payload = json.loads(msg.data)
                        mode = payload.get("mode")
                        text = (payload.get("text") or "").strip().lstrip(lead_punct)
                        if mode == "2pass-online":
                            if not text:
                                continue
                            if not speaking:
                                speaking = True
                                self._event_ch.send_nowait(
                                    stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH)
                                )
                            self._event_ch.send_nowait(
                                stt.SpeechEvent(
                                    type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                                    alternatives=[stt.SpeechData(language=language, text=text)],
                                )
                            )
                        elif mode == "2pass-offline":
                            if text:
                                self._event_ch.send_nowait(
                                    stt.SpeechEvent(
                                        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                        alternatives=[stt.SpeechData(language=language, text=text)],
                                    )
                                )
                            if speaking:
                                speaking = False
                                self._event_ch.send_nowait(
                                    stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
                                )
                        if payload.get("is_final") is True:
                            break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                        break
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        raise APIConnectionError("FunASR websocket closed with error")

            tasks = [
                asyncio.create_task(send_task(), name="funasr_send"),
                asyncio.create_task(recv_task(), name="funasr_recv"),
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                await utils.aio.cancel_and_wait(*tasks)


@dataclass
class BailianTTSOptions:
    model: str
    voice: str
    sample_rate: int
    speech_rate: float


class _BailianCallback(QwenTtsRealtimeCallback):
    def __init__(self) -> None:
        self.audio = bytearray()
        self.done = threading.Event()
        self.error: Exception | None = None

    def on_open(self) -> None:
        return

    def on_close(self, close_status_code, close_msg) -> None:
        if not self.done.is_set():
            self.done.set()

    def on_event(self, message: dict) -> None:
        event_type = message.get("type")
        if event_type == "response.audio.delta":
            delta = message.get("delta") or message.get("response", {}).get("audio", {}).get(
                "delta"
            )
            if delta:
                self.audio.extend(base64.b64decode(delta))
        elif event_type in ("error", "response.error"):
            self.error = APIStatusError(str(message))
            self.done.set()
        elif event_type == "response.done":
            self.done.set()


class BailianRealtimeTTS(tts.TTS):
    def __init__(
        self,
        *,
        model: str | None = None,
        voice: str | None = None,
        sample_rate: int = 24000,
        speech_rate: float = 1.0,
        api_key: str | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not key:
            raise ValueError("DASHSCOPE_API_KEY is required for Bailian TTS")

        self._api_key = key
        self._opts = BailianTTSOptions(
            model=model or os.getenv("BAILIAN_TTS_MODEL", "qwen-tts-realtime"),
            voice=voice or os.getenv("BAILIAN_TTS_VOICE", "Ethan"),
            sample_rate=sample_rate,
            speech_rate=speech_rate,
        )

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "Bailian"

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return _BailianChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    async def aclose(self) -> None:
        return

    def _synthesize_sync(self, text: str) -> bytes:
        # NOTE: dashscope.api_key 是进程级全局(SDK 设计如此)。当前单 key 场景没问题;
        # 若将来一个进程内并存多个不同 DASHSCOPE_API_KEY,这里会相互覆盖/竞争。
        dashscope.api_key = self._api_key
        callback = _BailianCallback()
        realtime = QwenTtsRealtime(model=self._opts.model, callback=callback)
        realtime.connect()
        try:
            realtime.update_session(
                voice=self._opts.voice,
                response_format=BailianAudioFormat.PCM_24000HZ_MONO_16BIT,
                speech_rate=self._opts.speech_rate,
                sample_rate=self._opts.sample_rate,
            )
            realtime.append_text(text)
            realtime.commit()
            if not callback.done.wait(timeout=30):
                raise APIConnectionError("Bailian TTS timed out")
            if callback.error:
                raise callback.error
            return bytes(callback.audio)
        finally:
            try:
                realtime.finish()
            except Exception:
                pass
            realtime.close()


class _BailianChunkedStream(tts.ChunkedStream):
    def __init__(
        self, *, tts: BailianRealtimeTTS, input_text: str, conn_options: APIConnectOptions
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        audio_bytes = await asyncio.to_thread(self._tts._synthesize_sync, self.input_text)
        if not audio_bytes:
            raise APIConnectionError("Bailian TTS returned empty audio")

        output_emitter.initialize(
            request_id=utils.shortuuid("bailian-tts-"),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
        )
        output_emitter.push(audio_bytes)
        output_emitter.flush()


class _QwenStreamCallback(QwenTtsRealtimeCallback):
    """Callback that bridges Qwen TTS audio data from WebSocket thread to asyncio.

    目标 queue/event 通过 bind() 在每轮开始时挂入，构造时为空——这样连接可以提前
    预热（connect+update_session）而不绑定到某一轮的 queue。预热到 bind 之间不会
    append_text/commit，服务端不吐 audio.delta，悬空的 delta 直接丢弃即可。
    """

    def __init__(self) -> None:
        self.audio_queue: queue.Queue[bytes | Exception | None] | None = None
        self.audio_done: threading.Event | None = None

    def bind(
        self, audio_queue: queue.Queue[bytes | Exception | None], audio_done: threading.Event
    ) -> None:
        self.audio_queue = audio_queue
        self.audio_done = audio_done

    def on_event(self, message: dict) -> None:
        event_type = message.get("type")
        if event_type == "response.audio.delta":
            q = self.audio_queue
            if q is None:
                return  # 预热连接未绑定到某轮，丢弃悬空音频
            delta = message.get("delta") or message.get("response", {}).get("audio", {}).get(
                "delta"
            )
            if delta:
                try:
                    q.put(base64.b64decode(delta))
                except Exception as e:
                    q.put(Exception(f"Failed to decode audio: {e}"))
        elif event_type == "response.done":
            if self.audio_done is not None:
                self.audio_done.set()
        elif event_type in ("error", "response.error"):
            if self.audio_queue is not None:
                self.audio_queue.put(Exception(f"Qwen TTS error: {message}"))

    def on_close(self, close_status_code, close_msg) -> None:
        pass  # drain handles shutdown via audio_done + queue sentinel


class QwenStreamingTTS(tts.TTS):
    """Streaming TTS using Qwen TTS Realtime.

    每轮一条连接：connect()/update_session() -> 按句 append_text() -> finish()
    合成并收尾 -> close()。打断时直接 close() 中止服务端合成。
    每轮独立连接 + 独立 callback，杜绝跨轮串台，打断语义干净可靠。
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        voice: str | None = None,
        sample_rate: int = 24000,
        speech_rate: float = 1.0,
        api_key: str | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=1,
        )
        key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not key:
            raise ValueError("DASHSCOPE_API_KEY is required for Qwen TTS")

        self._api_key = key
        self._opts = BailianTTSOptions(
            model=model or os.getenv("BAILIAN_TTS_MODEL", "qwen-tts-realtime"),
            voice=voice or os.getenv("BAILIAN_TTS_VOICE", "Ethan"),
            sample_rate=sample_rate,
            speech_rate=speech_rate,
        )
        # 预热连接：池 size=1。connect()+update_session() 的 ~1s 握手是每轮 tts_ttfb 的
        # 大头，提前在用户说话期间建好，下一轮 _run 直接取用。超过 TTL 视为可能被服务端
        # 闲置关闭，丢弃重建（宁可慢一轮也不用半死连接）。
        self._warm_lock = threading.Lock()
        self._warm_conn: tuple[QwenTtsRealtime, _QwenStreamCallback] | None = None
        self._warm_at = 0.0
        self._warm_ttl = float(os.getenv("BAILIAN_TTS_WARM_TTL", "20"))

    def _build_connection(self) -> tuple[QwenTtsRealtime, _QwenStreamCallback]:
        dashscope.api_key = self._api_key
        opts = self._opts
        callback = _QwenStreamCallback()
        synth = QwenTtsRealtime(model=opts.model, callback=callback)
        synth.connect()
        synth.update_session(
            voice=opts.voice,
            response_format=BailianAudioFormat.PCM_24000HZ_MONO_16BIT,
            speech_rate=opts.speech_rate,
            sample_rate=opts.sample_rate,
        )
        return synth, callback

    def take_connection(self) -> tuple[QwenTtsRealtime, _QwenStreamCallback]:
        """取一条就绪连接：命中预热则零握手，否则现建（与改前同延迟）。"""
        with self._warm_lock:
            conn = self._warm_conn
            self._warm_conn = None
            fresh = conn is not None and (time.monotonic() - self._warm_at) <= self._warm_ttl
        if conn is not None and not fresh:
            with contextlib.suppress(Exception):
                conn[0].close()
            conn = None
        if conn is not None:
            return conn
        return self._build_connection()

    def prewarm_connection(self) -> None:
        """后台预建一条连接挂起。已有则跳过；建多了关掉富余的。阻塞调用，请丢线程里跑。"""
        with self._warm_lock:
            if self._warm_conn is not None and (time.monotonic() - self._warm_at) <= self._warm_ttl:
                return
        try:
            conn = self._build_connection()
        except Exception as exc:  # noqa: BLE001
            logger.debug("tts prewarm skipped: %s", exc)
            return
        stale: tuple[QwenTtsRealtime, _QwenStreamCallback] | None = None
        with self._warm_lock:
            if self._warm_conn is None:
                self._warm_conn = conn
                self._warm_at = time.monotonic()
            else:
                stale = conn
        if stale is not None:
            with contextlib.suppress(Exception):
                stale[0].close()

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "Bailian"

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return _BailianChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.SynthesizeStream:
        return _QwenSynthesizeStream(tts=self, conn_options=conn_options)

    def _synthesize_sync(self, text: str) -> bytes:
        dashscope.api_key = self._api_key
        opts = self._opts
        callback = _BailianCallback()
        realtime = QwenTtsRealtime(model=opts.model, callback=callback)
        realtime.connect()
        try:
            realtime.update_session(
                voice=opts.voice,
                response_format=BailianAudioFormat.PCM_24000HZ_MONO_16BIT,
                speech_rate=opts.speech_rate,
                sample_rate=opts.sample_rate,
            )
            realtime.append_text(text)
            realtime.commit()
            if not callback.done.wait(timeout=30):
                raise APIConnectionError("Qwen TTS timed out")
            if callback.error:
                raise callback.error
            return bytes(callback.audio)
        finally:
            with contextlib.suppress(Exception):
                realtime.finish()
            with contextlib.suppress(Exception):
                realtime.close()

    async def aclose(self) -> None:
        with self._warm_lock:
            conn = self._warm_conn
            self._warm_conn = None
        if conn is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(conn[0].close)


class _QwenSynthesizeStream(tts.SynthesizeStream):
    """SynthesizeStream using Qwen TTS with single-commit strategy.

    Buffers all incoming text and commits once when input ends. This is
    reliable but has the same latency profile as non-streaming TTS.
    """

    def __init__(
        self,
        *,
        tts: QwenStreamingTTS,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        opts = self._tts._opts

        # 当轮专属连接 + 专属 callback：取一条就绪连接（命中预热则零握手）后把当轮
        # queue/event 绑上去。连接仍是每轮独占、用完即关，杜绝残留音频串台——预热只是
        # 把握手提前到上一轮播放期，不改变"不跨轮复用"的语义。
        audio_queue: queue.Queue[bytes | Exception | None] = queue.Queue()
        audio_done = threading.Event()

        synth, callback = await asyncio.to_thread(self._tts.take_connection)
        callback.bind(audio_queue, audio_done)

        output_emitter.initialize(
            request_id=shortuuid("qwen-tts-"),
            sample_rate=opts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=False,
        )

        async def _drain() -> None:
            """Drain audio data from callback queue and push to LiveKit."""
            try:
                while True:
                    data = await asyncio.to_thread(audio_queue.get)
                    if data is None:
                        break
                    if isinstance(data, Exception):
                        raise data
                    output_emitter.push(data)
            finally:
                output_emitter.flush()

        drain_task = asyncio.create_task(_drain())
        any_text = False

        try:
            # 按句边界增量 append，让第一句的合成与 LLM 后续生成重叠，
            # 而不是攒齐整段再发（首包延迟从"等整段"降到"等第一句"）。
            pending = ""
            async for data in self._input_ch:
                if not isinstance(data, str):
                    continue
                pending += data
                if pending and pending[-1] in "。！？!?；;\n":
                    await asyncio.to_thread(synth.append_text, pending)
                    any_text = True
                    pending = ""

            if pending.strip():
                await asyncio.to_thread(synth.append_text, pending)
                any_text = True

            if any_text:
                # commit() 触发合成（与 _synthesize_sync 同一条经过验证的路径）；
                # 等 response.done 落定后再 finish()+close() 收尾。
                await asyncio.to_thread(synth.commit)
                done = await asyncio.to_thread(audio_done.wait, 30)
                if not done:
                    raise TimeoutError("Qwen TTS synthesis timed out")

            audio_queue.put(None)

        except BaseException:
            # 打断/异常（含 asyncio.CancelledError——框架打断时 cancel 本 _run）。
            # 每轮独立连接，直接 close() 即可确定性中止服务端合成、丢弃在途音频，
            # 不会污染下一轮（下一轮本就是全新连接）。
            audio_queue.put(None)
            drain_task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(synth.close)
            raise

        with contextlib.suppress(Exception):
            await asyncio.to_thread(synth.finish)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(synth.close)
        await drain_task


# ─── CosyVoice (DashScope tts_v2) — A/B 实测用的可切换后端 ────────────────────
# 与 QwenStreamingTTS 并列的另一条 TTS 后端,走 dashscope tts_v2 的 SpeechSynthesizer
# (CosyVoice 系列),原生支持 streaming_call / streaming_cancel 与 instruction(情感/
# 风格)。每轮借一条连接(命中预热池则零握手)、用完即归还,打断即 streaming_cancel()
# 中止;借还语义对齐 Qwen 的"不跨轮复用"。

_COSY_PCM_FORMATS = {
    16000: CosyAudioFormat.PCM_16000HZ_MONO_16BIT,
    22050: CosyAudioFormat.PCM_22050HZ_MONO_16BIT,
    24000: CosyAudioFormat.PCM_24000HZ_MONO_16BIT,
    48000: CosyAudioFormat.PCM_48000HZ_MONO_16BIT,
}

# 官方对象池(进程级单例):提前建好 WS 连接消除每轮握手,后台线程自动续连/renew。
# 预建的是与 model 无关的纯 WS 握手,borrow 时再套用我们的 model/voice/format/callback。
# **只构造一次**(单例的 __init__ 每次都会重跑并另起线程),故用模块级守卫 + 锁。
_cosy_pool: CosySynthPool | None = None
_cosy_pool_lock = threading.Lock()


def _ensure_cosy_pool(api_key: str, max_size: int) -> CosySynthPool | None:
    """幂等创建预热池。构造会阻塞(逐个开 max_size 条连接),请在后台线程里调。"""
    global _cosy_pool
    if _cosy_pool is not None:
        return _cosy_pool
    with _cosy_pool_lock:
        if _cosy_pool is None:
            dashscope.api_key = api_key
            _cosy_pool = CosySynthPool(max_size=max_size)
    return _cosy_pool


@dataclass
class CosyVoiceTTSOptions:
    model: str
    voice: str
    sample_rate: int
    speech_rate: float
    pitch_rate: float
    instruction: str | None


class _CosyVoiceCallback(CosyResultCallback):
    """把 CosyVoice WS 线程的裸 PCM 桥接到 asyncio(线程安全 queue + done 事件)。
    每轮一个实例(不预热复用),不存在悬空音频问题。"""

    def __init__(self) -> None:
        self.audio_queue: queue.Queue[bytes | Exception | None] = queue.Queue()
        self.audio_done = threading.Event()

    def on_open(self) -> None:
        return

    def on_data(self, data: bytes) -> None:
        if data:
            self.audio_queue.put(bytes(data))

    def on_complete(self) -> None:
        self.audio_done.set()

    def on_error(self, message: object) -> None:
        self.audio_queue.put(APIStatusError(f"CosyVoice TTS error: {message}"))
        self.audio_done.set()

    def on_close(self) -> None:
        return


class CosyVoiceStreamingTTS(tts.TTS):
    """Streaming TTS using DashScope CosyVoice (tts_v2.SpeechSynthesizer).

    与 QwenStreamingTTS 并列,供面板一键切换做 A/B。每轮从官方对象池借一条连接(命中
    预热则零握手)、streaming_call() 按句增量送字、streaming_complete() 收尾,用完归还;
    打断时 streaming_cancel() 确定性中止后归还(池会自动 renew 这条失效连接)。
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        voice: str | None = None,
        sample_rate: int = 24000,
        speech_rate: float = 1.0,
        pitch_rate: float = 1.0,
        instruction: str | None = None,
        api_key: str | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=1,
        )
        key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not key:
            raise ValueError("DASHSCOPE_API_KEY is required for CosyVoice TTS")
        if sample_rate not in _COSY_PCM_FORMATS:
            raise ValueError(f"unsupported CosyVoice sample_rate: {sample_rate}")

        self._api_key = key
        self._opts = CosyVoiceTTSOptions(
            model=model or os.getenv("COSYVOICE_MODEL", "cosyvoice-v3-flash"),
            # 默认女声;候选见 web_ui_agent._make_tts_backend(longanwen_v3/longanrou_v3/longanli_v3)。
            voice=voice or os.getenv("COSYVOICE_VOICE", "longxiaochun_v3"),
            sample_rate=sample_rate,
            speech_rate=speech_rate,
            pitch_rate=pitch_rate,
            instruction=instruction or (os.getenv("COSYVOICE_INSTRUCTION") or None),
        )
        self._pool_size = max(1, int(os.getenv("COSYVOICE_POOL_SIZE", "3")))

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "CosyVoice"

    def prewarm_connection(self) -> None:
        """预建 WS 连接池消除每轮握手。幂等(池为进程级单例,只建一次,后台自动续连)。
        建池会阻塞,故由调用方丢线程里跑(web_ui_agent 在用户说话时 to_thread 调用);
        失败则跳过,后续退回冷连接,绝不影响主流程。"""
        try:
            _ensure_cosy_pool(self._api_key, self._pool_size)
        except Exception as exc:  # noqa: BLE001
            logger.debug("cosyvoice prewarm skipped: %s", exc)

    def take_synth(self, callback: CosyResultCallback) -> tuple[CosySpeechSynthesizer, bool]:
        """取一条就绪连接:命中预热池则零握手(返回 pooled=True 需归还);否则现建冷连接。"""
        opts = self._opts
        pool = _cosy_pool
        if pool is not None:
            try:
                synth = pool.borrow_synthesizer(
                    model=opts.model,
                    voice=opts.voice,
                    format=_COSY_PCM_FORMATS[opts.sample_rate],
                    speech_rate=opts.speech_rate,
                    pitch_rate=opts.pitch_rate,
                    instruction=opts.instruction or None,
                    callback=callback,
                )
                return synth, True
            except Exception as exc:  # noqa: BLE001
                logger.debug("cosyvoice borrow failed, cold build: %s", exc)
        return self._build_synth(callback), False

    @staticmethod
    def _release_synth(synth: CosySpeechSynthesizer, pooled: bool) -> None:
        """归还(池连接,即便已 cancel——池会自动 renew)或关闭(冷连接)。"""
        pool = _cosy_pool
        if pooled and pool is not None:
            with contextlib.suppress(Exception):
                pool.return_synthesizer(synth)
            return
        with contextlib.suppress(Exception):
            synth.close()

    def _build_synth(self, callback: CosyResultCallback | None) -> CosySpeechSynthesizer:
        dashscope.api_key = self._api_key
        opts = self._opts
        kwargs: dict[str, object] = {
            "model": opts.model,
            "voice": opts.voice,
            "format": _COSY_PCM_FORMATS[opts.sample_rate],
            "speech_rate": opts.speech_rate,
            "pitch_rate": opts.pitch_rate,
            "callback": callback,
        }
        if opts.instruction:
            kwargs["instruction"] = opts.instruction
        return CosySpeechSynthesizer(**kwargs)

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return _CosyVoiceChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.SynthesizeStream:
        return _CosyVoiceSynthesizeStream(tts=self, conn_options=conn_options)

    def _synthesize_sync(self, text: str) -> bytes:
        synth = self._build_synth(None)
        try:
            audio = synth.call(text)
        finally:
            with contextlib.suppress(Exception):
                synth.close()
        return bytes(audio) if audio else b""

    async def aclose(self) -> None:
        return


class _CosyVoiceChunkedStream(tts.ChunkedStream):
    def __init__(
        self, *, tts: CosyVoiceStreamingTTS, input_text: str, conn_options: APIConnectOptions
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        audio_bytes = await asyncio.to_thread(self._tts._synthesize_sync, self.input_text)
        if not audio_bytes:
            raise APIConnectionError("CosyVoice TTS returned empty audio")
        output_emitter.initialize(
            request_id=shortuuid("cosyvoice-tts-"),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
        )
        output_emitter.push(audio_bytes)
        output_emitter.flush()


class _CosyVoiceSynthesizeStream(tts.SynthesizeStream):
    """按句增量 streaming_call,让第一句合成与 LLM 后续生成重叠以降首包延迟。"""

    def __init__(self, *, tts: CosyVoiceStreamingTTS, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        callback = _CosyVoiceCallback()
        synth, pooled = await asyncio.to_thread(self._tts.take_synth, callback)

        output_emitter.initialize(
            request_id=shortuuid("cosyvoice-tts-"),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=False,
        )

        async def _drain() -> None:
            try:
                while True:
                    data = await asyncio.to_thread(callback.audio_queue.get)
                    if data is None:
                        break
                    if isinstance(data, Exception):
                        raise data
                    output_emitter.push(data)
            finally:
                output_emitter.flush()

        drain_task = asyncio.create_task(_drain())
        any_text = False

        try:
            pending = ""
            async for data in self._input_ch:
                if not isinstance(data, str):
                    continue
                pending += data
                if pending and pending[-1] in "。！？!?；;\n":
                    await asyncio.to_thread(synth.streaming_call, pending)
                    any_text = True
                    pending = ""

            if pending.strip():
                await asyncio.to_thread(synth.streaming_call, pending)
                any_text = True

            if any_text:
                # 阻塞至服务端合成完成(on_complete 落定),此时全部 on_data 已入队。
                await asyncio.to_thread(synth.streaming_complete)

            callback.audio_queue.put(None)

        except BaseException:
            # 打断/异常(含 CancelledError):streaming_cancel() 确定性中止、丢弃在途音频,
            # 然后归还连接(池会 renew 这条已关闭的连接);不污染下一轮。
            callback.audio_queue.put(None)
            drain_task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(synth.streaming_cancel)
            await asyncio.to_thread(self._tts._release_synth, synth, pooled)
            raise

        await asyncio.to_thread(self._tts._release_synth, synth, pooled)
        await drain_task


class HttpStreamingTTS(tts.TTS):
    """TTS backed by a streaming HTTP POST endpoint.

    POST /tts  {"text": ..., "speaker": ..., "speed": ...}
    返回 audio/L16 PCM 流，24000Hz 单声道 16-bit。
    用 HTTP_TTS_URL 环境变量覆盖默认地址。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        speaker: str = "default",
        speed: float = 1.0,
        sample_rate: int = 24000,
        chunk_size: int = 4096,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._url = (base_url or os.getenv("HTTP_TTS_URL", "http://10.212.164.230:8001")).rstrip(
            "/"
        ) + "/tts"
        self._speaker = speaker
        self._speed = float(os.getenv("HTTP_TTS_SPEED", str(speed)))
        self._chunk_size = chunk_size

    @property
    def model(self) -> str:
        return "http-tts"

    @property
    def provider(self) -> str:
        return "HttpTTS"

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return _HttpChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.SynthesizeStream:
        return _HttpSynthesizeStream(tts=self, conn_options=conn_options)

    async def aclose(self) -> None:
        return

    def _get_session(self) -> tuple[aiohttp.ClientSession, bool]:
        """返回 (session, owns_session)；owns_session=True 时调用方负责关闭。"""
        try:
            return utils.http_context.http_session(), False
        except RuntimeError:
            return aiohttp.ClientSession(), True

    async def _post_push(
        self, session: aiohttp.ClientSession, text: str, output_emitter: tts.AudioEmitter
    ) -> None:
        """POST 单句文本，把 PCM 块逐一 push 到 emitter。不调用 flush()，由调用方决定时机。"""
        payload = {"text": text, "speaker": self._speaker, "speed": self._speed}
        timeout = aiohttp.ClientTimeout(
            connect=_TTS_CONNECT_TIMEOUT, sock_connect=_TTS_CONNECT_TIMEOUT
        )
        async with session.post(self._url, json=payload, timeout=timeout) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise APIConnectionError(f"HTTP TTS returned {resp.status}: {body}")
            async for chunk in resp.content.iter_chunked(self._chunk_size):
                if chunk:
                    output_emitter.push(chunk)


class _HttpChunkedStream(tts.ChunkedStream):
    def __init__(
        self, *, tts: HttpStreamingTTS, input_text: str, conn_options: APIConnectOptions
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid("http-tts-"),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
        )
        session, owns = self._tts._get_session()
        try:
            await self._tts._post_push(session, self.input_text, output_emitter)
            output_emitter.flush()
        finally:
            if owns:
                await session.close()


class _HttpSynthesizeStream(tts.SynthesizeStream):
    def __init__(self, *, tts: HttpStreamingTTS, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid("http-tts-"),
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=False,
        )

        session, owns = self._tts._get_session()
        any_text = False
        try:
            # 按句边界逐句发 POST：LLM 生成第一句结束就立即合成，
            # 收音频期间 LLM 继续生成后续句，首包延迟从"等整段"降到"等第一句"。
            pending = ""
            async for data in self._input_ch:
                if not isinstance(data, str):
                    continue
                pending += data
                if pending and pending[-1] in "。！？!?；;":
                    text = pending.strip()
                    pending = ""
                    if text:
                        await self._tts._post_push(session, text, output_emitter)
                        any_text = True

            if pending.strip():
                await self._tts._post_push(session, pending.strip(), output_emitter)
                any_text = True

            if any_text:
                output_emitter.flush()
        finally:
            if owns:
                await session.close()
