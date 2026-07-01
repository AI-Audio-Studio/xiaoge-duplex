"""小歌全双工语音 · Python 客户端 SDK。

对接服务端的 `/ws/audio` WebSocket(服务端需 `WEB_AUDIO=1`)。协议见 ../PROTOCOL.md:
上行=连续发 16kHz/单声道/16-bit 小端裸 PCM;下行=同格式 PCM(TTS)+ 文本控制
`{"type":"ready"|"clear"|"busy"}`。本模块只做协议与收发,音频采集/播放由调用方在回调里处理。

用法::

    client = XiaogeClient("60.205.197.165", 10099, tls=True)  # 当前部署(wss)
    client.on_audio = lambda pcm: speaker.write(pcm)   # 播放 TTS
    client.on_clear = lambda: speaker.flush()          # 打断:清空播放
    async def feed():
        while True:
            await client.send_pcm(mic.read())          # 上行麦克风 PCM
    await asyncio.gather(client.run(), feed())
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import websockets

logger = logging.getLogger("xiaoge.client")

SAMPLE_RATE = 16_000
NUM_CHANNELS = 1
SAMPLE_WIDTH = 2  # int16 little-endian


class XiaogeClient:
    """连接小歌 `/ws/audio` 的全双工音频客户端。

    回调(均可选,默认 no-op;在事件循环线程被调用):
        on_ready(sample_rate: int) — 收到握手 ready。
        on_audio(pcm: bytes)       — 收到一帧 TTS PCM(16k/单声道/int16 小端)。
        on_clear()                 — 服务端要求清空本地播放(打断)。
        on_busy(message: str)      — 服务端忙、连接被拒。
    """

    def __init__(
        self, host: str, port: int = 8787, *, tls: bool = False, ssl: object | None = None
    ) -> None:
        self.host = host
        self.port = port
        self.sample_rate = SAMPLE_RATE
        self._url = f"{'wss' if tls else 'ws'}://{host}:{port}/ws/audio"
        self._ssl = ssl  # wss 时可传 ssl.SSLContext(自签/自定义 CA);None=默认校验
        self._ws: websockets.ClientConnection | None = None
        self.on_ready: Callable[[int], None] | None = None
        self.on_audio: Callable[[bytes], None] | None = None
        self.on_clear: Callable[[], None] | None = None
        self.on_busy: Callable[[str], None] | None = None

    async def send_pcm(self, pcm: bytes) -> None:
        """发送一段上行 PCM(16kHz/单声道/int16 小端)。未连接时静默丢弃。"""
        ws = self._ws
        if ws is not None and pcm:
            await ws.send(pcm)

    async def run(self) -> None:
        """连接并阻塞运行接收循环,直到连接关闭。"""
        async with websockets.connect(self._url, ssl=self._ssl, max_size=None) as ws:
            self._ws = ws
            logger.info("connected %s", self._url)
            try:
                async for message in ws:
                    self._dispatch(message)
            finally:
                self._ws = None
                logger.info("disconnected")

    async def close(self) -> None:
        """主动关闭连接。"""
        ws = self._ws
        if ws is not None:
            await ws.close()

    def _dispatch(self, message: str | bytes) -> None:
        if isinstance(message, (bytes, bytearray)):
            self._call(self.on_audio, bytes(message))
        else:
            self._handle_text(message)

    def _handle_text(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except (ValueError, TypeError):
            logger.warning("non-JSON text frame: %r", text)
            return
        kind = msg.get("type")
        if kind == "ready":
            self.sample_rate = int(msg.get("sample_rate", SAMPLE_RATE))
            self._call(self.on_ready, self.sample_rate)
        elif kind == "clear":
            self._call(self.on_clear)
        elif kind == "busy":
            self._call(self.on_busy, str(msg.get("message", "server busy")))
        else:
            logger.debug("ignored control message: %s", kind)

    @staticmethod
    def _call(cb: Callable | None, *args: object) -> None:
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:  # 回调异常不应中断接收循环
            logger.exception("client callback error")
