"""讯飞 RTASR 流式 STT(判停/主STT 改造 阶段2)。

原生流式:一条 WS 贯穿整轮会话,边说边出 interim/final,长句不丢内容——替代
"离线FunASR + VAD + StreamAdapter"那条会"超长段超时→空→静默丢弃"的妥协路径。

协议(已对真服务验证):
  - URL wss://rtasr.xfyun.cn/v1/ws?appid=&ts=&signa=(signa 需 URL 编码)
  - signa = base64(hmac_sha1(key=APIKey, msg=md5_hex(appid+ts)))
  - 音频 PCM 16k/16bit/单声道,1280 字节/帧、~40ms 间隔,裸二进制;结束发 {"end":true}
  - 结果 {action:"result", data:"<json>"};data.cn.st.type 0=final/1=partial;
    文本 = data.cn.st.rt[].ws[].cw[].w 拼接;action:"started"=鉴权成功

约束:opt-in(STT_BACKEND=iflytek 才用);异常向上抛由基类按 conn_options 重试(重连),
绝不静默丢内容;严格 ~40ms 节流避免引擎报错。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import quote

import aiohttp

from livekit.agents import APIConnectOptions, LanguageCode, stt
from livekit.agents._exceptions import APIConnectionError, APIStatusError
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr

logger = logging.getLogger("iflytek-rtasr")

_BASE_URL = "wss://rtasr.xfyun.cn/v1/ws"
_SAMPLE_RATE = 16000
_CHUNK_BYTES = 1280  # 40ms @ 16k/16bit/mono
_SEND_INTERVAL = 0.04
_CONNECT_TIMEOUT = float(os.getenv("IFLYTEK_CONNECT_TIMEOUT", "8"))


def _signa(appid: str, key: str, ts: str) -> str:
    md5 = hashlib.md5((appid + ts).encode()).hexdigest()
    return base64.b64encode(hmac.new(key.encode(), md5.encode(), hashlib.sha1).digest()).decode()


def _extract(data_str: str) -> tuple[str, str]:
    """从 result.data(json 串)取 (type, text)。type: '0'=final / '1'=partial。"""
    d = json.loads(data_str)
    st = d["cn"]["st"]
    text = "".join(cw["w"] for rt in st["rt"] for ws in rt["ws"] for cw in ws["cw"])
    return str(st.get("type", "")), text


class IFlyTekRTASR(stt.STT):
    """讯飞实时语音转写,流式主STT(只支持 streaming,不支持 offline recognize)。"""

    def __init__(self, *, appid: str | None = None, api_key: str | None = None) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True, interim_results=True, offline_recognize=False
            )
        )
        self._appid = appid or os.getenv("IFLYTEK_APPID")
        self._key = api_key or os.getenv("IFLYTEK_API_KEY")
        if not self._appid or not self._key:
            raise ValueError("IFLYTEK_APPID / IFLYTEK_API_KEY required for iFlyTek RTASR")

    @property
    def model(self) -> str:
        return "rtasr"

    @property
    def provider(self) -> str:
        return "iFlyTek"

    async def _recognize_impl(self, buffer, *, language=NOT_GIVEN, conn_options):  # type: ignore[override]
        raise NotImplementedError("iFlyTek RTASR is streaming-only (use stream())")

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.RecognizeStream:
        return _IFlyTekStream(stt=self, conn_options=conn_options)

    async def aclose(self) -> None:
        return


class _IFlyTekStream(stt.RecognizeStream):
    """一条 WS 贯穿整个流:发送任务推 1280 字节/40ms,接收任务把结果转 SpeechEvent。"""

    def __init__(self, *, stt: IFlyTekRTASR, conn_options: APIConnectOptions) -> None:
        # sample_rate=16000 让基类自动把输入帧重采样到 16k(讯飞要求)。
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=_SAMPLE_RATE)
        self._impl = stt

    async def _run(self) -> None:
        appid, key = self._impl._appid, self._impl._key
        ts = str(int(time.time()))
        url = f"{_BASE_URL}?appid={appid}&ts={ts}&signa={quote(_signa(appid, key, ts))}"

        session = aiohttp.ClientSession()
        ws: aiohttp.ClientWSResponse | None = None
        try:
            ws = await asyncio.wait_for(
                session.ws_connect(url, heartbeat=20), timeout=_CONNECT_TIMEOUT
            )
            send_task = asyncio.create_task(self._send_loop(ws), name="iflytek-send")
            try:
                await self._recv_loop(ws)
            finally:
                send_task.cancel()
                await asyncio.gather(send_task, return_exceptions=True)
        finally:
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
            with contextlib.suppress(Exception):
                await session.close()

    async def _send_loop(self, ws: aiohttp.ClientWSResponse) -> None:
        buf = bytearray()
        last = 0.0
        async for frame in self._input_ch:
            if isinstance(frame, self._FlushSentinel):
                continue  # 段间 flush:不结束讯飞会话,继续推流
            buf.extend(bytes(frame.data))  # 基类已重采样到 16k 单声道
            while len(buf) >= _CHUNK_BYTES:
                chunk = bytes(buf[:_CHUNK_BYTES])
                del buf[:_CHUNK_BYTES]
                # 节流:不快于 ~40ms/帧(发太快讯飞引擎会报错)
                if last:
                    wait = _SEND_INTERVAL - (time.monotonic() - last)
                    if wait > 0:
                        await asyncio.sleep(wait)
                last = time.monotonic()
                await ws.send_bytes(chunk)
        # 输入结束(会话关闭):补发剩余 + 结束信号
        if buf:
            with contextlib.suppress(Exception):
                await ws.send_bytes(bytes(buf))
        with contextlib.suppress(Exception):
            await ws.send_str(json.dumps({"end": True}))

    async def _recv_loop(self, ws: aiohttp.ClientWSResponse) -> None:
        while True:
            msg = await ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                m = json.loads(msg.data)
                action = m.get("action")
                if action == "result":
                    typ, text = _extract(m["data"])
                    if not text:
                        continue
                    ev_type = (
                        stt.SpeechEventType.FINAL_TRANSCRIPT
                        if typ == "0"
                        else stt.SpeechEventType.INTERIM_TRANSCRIPT
                    )
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(
                            type=ev_type,
                            request_id=str(m.get("sid", "")),
                            alternatives=[stt.SpeechData(language=LanguageCode("zh"), text=text)],
                        )
                    )
                elif action == "error":
                    raise APIStatusError(f"iFlyTek RTASR error: {m}")
                # action == "started": 鉴权成功,忽略
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
            ):
                break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise APIConnectionError("iFlyTek RTASR websocket error")
