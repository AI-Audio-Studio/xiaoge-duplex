"""Simulink 兜底桥(B 方案):TCP ↔ 小歌 /ws/audio 中继。

Simulink 用原生 TCP 块即可对接(无需 Java):
  - 上行:Simulink → 本桥 `--up` 端口(发 16k/单声道/int16 小端裸 PCM)→ 转发给小歌。
  - 下行:小歌 TTS → 本桥 `--down` 端口 → Simulink 收(同格式)。
  - 打断:小歌发 clear 时,桥仅记录(下行为直通流,无需清缓冲)。

用法::  python xiaoge_bridge.py <xiaoge_host> <xiaoge_port> [--up 5001] [--down 5002] [--tls] [--insecure]
  --tls / --insecure:小歌为 wss(HTTPS)时用;自签证书加 --insecure。
依赖:websockets(+ 同级 ../../python/xiaoge_client.py)。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
from xiaoge_client import XiaogeClient  # noqa: E402  (需先插 sys.path)

logging.basicConfig(level=logging.INFO, format="%(asctime)s bridge: %(message)s")
log = logging.getLogger("xiaoge.bridge")


class Bridge:
    """把一条 TCP 上行 + 一条 TCP 下行 桥到一个 XiaogeClient。"""

    def __init__(
        self, host: str, port: int, *, tls: bool = False, ssl: object | None = None
    ) -> None:
        self.client = XiaogeClient(host, port, tls=tls, ssl=ssl)
        self._down: asyncio.StreamWriter | None = None
        self.client.on_audio = self._to_down
        self.client.on_clear = lambda: log.info("clear(打断)")
        self.client.on_busy = lambda m: log.warning("服务器忙: %s", m)

    def _to_down(self, pcm: bytes) -> None:
        w = self._down
        if w is not None and not w.is_closing():
            w.write(pcm)

    async def on_up(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log.info("上行已连接 %s", writer.get_extra_info("peername"))
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                await self.client.send_pcm(data)
        finally:
            writer.close()
            log.info("上行断开")

    async def on_down(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log.info("下行已连接 %s", writer.get_extra_info("peername"))
        self._down = writer
        try:
            await reader.read()  # 持有到对端关闭
        finally:
            if self._down is writer:
                self._down = None
            writer.close()
            log.info("下行断开")

    async def serve(self, up_port: int, down_port: int) -> None:
        up = await asyncio.start_server(self.on_up, "0.0.0.0", up_port)
        down = await asyncio.start_server(self.on_down, "0.0.0.0", down_port)
        log.info("桥就绪:上行 TCP :%d,下行 TCP :%d", up_port, down_port)
        async with up, down:
            await asyncio.gather(self.client.run(), up.serve_forever(), down.serve_forever())


def _ssl_context(insecure: bool) -> object | None:
    if not insecure:
        return None
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def main() -> None:
    p = argparse.ArgumentParser(description="Simulink ↔ 小歌 TCP/WS 桥")
    p.add_argument("host")
    p.add_argument("port", type=int)
    p.add_argument("--up", type=int, default=5001)
    p.add_argument("--down", type=int, default=5002)
    p.add_argument("--tls", action="store_true", help="用 wss")
    p.add_argument("--insecure", action="store_true", help="wss 不校验证书(自签)")
    a = p.parse_args()
    bridge = Bridge(a.host, a.port, tls=a.tls, ssl=_ssl_context(a.insecure))
    try:
        asyncio.run(bridge.serve(a.up, a.down))
    except KeyboardInterrupt:
        print("\n桥已退出")


if __name__ == "__main__":
    main()
