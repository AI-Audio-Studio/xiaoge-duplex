"""诊断版桥(详尽日志 + 可离线自测),用于定位「服务器回了、MATLAB 没收到」断在哪一层。

两种模式:
  1) 真机模式:桥连小歌,转发上/下行,并把从小歌收到的音频另存 bridge_recv.wav
     (证明桥确实收到了下行,与 MATLAB 是否收到解耦):
       python bridge_debug.py 60.205.197.165 10099 --tls --insecure --up 5001 --down 5002
  2) 自测模式(--selftest):不连小歌,只要有下行客户端连上 5002,就持续下发一段
     440Hz 正弦音,用来单独验证「桥 → MATLAB」这段 TCP 读取是否正常:
       python bridge_debug.py --selftest --up 5001 --down 5002

每秒打印一次计数:ws 连接、上行字节、下行是否连、收到音频帧/字节、写往下行字节、
以及「收到音频但下行未连」的丢弃字节。日志实时刷新。
"""

from __future__ import annotations

import argparse
import asyncio
import math
import struct
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
from xiaoge_client import XiaogeClient  # noqa: E402


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _tone_frame(phase: int) -> bytes:
    """320 样本(20ms@16k)440Hz 正弦,int16 小端。"""
    out = bytearray()
    for i in range(320):
        v = int(6000 * math.sin(2 * math.pi * 440 * (phase + i) / 16000))
        out += struct.pack("<h", v)
    return bytes(out)


class DebugBridge:
    def __init__(self, host: str, port: int, tls: bool, insecure: bool, selftest: bool) -> None:
        self.host, self.port, self.tls, self.insecure = host, port, tls, insecure
        self.selftest = selftest
        self._down: asyncio.StreamWriter | None = None
        self.up_bytes = 0
        self.audio_frames = 0
        self.audio_bytes = 0
        self.down_written = 0
        self.down_dropped = 0
        self.ready = False
        self.sink = bytearray()
        self.client: XiaogeClient | None = None

    # ---- 小歌侧回调 ----
    def on_ready(self, sr: int) -> None:
        self.ready = True
        log(f"小歌 READY sample_rate={sr}")

    def on_audio(self, pcm: bytes) -> None:
        self.audio_frames += 1
        self.audio_bytes += len(pcm)
        self.sink.extend(pcm)
        if self.audio_frames <= 3:
            log(f"收到小歌音频帧#{self.audio_frames} {len(pcm)}B")
        w = self._down
        if w is not None and not w.is_closing():
            try:
                w.write(pcm)
                self.down_written += len(pcm)
            except Exception as e:  # noqa: BLE001
                log(f"!! 写下行异常: {e!r}")
        else:
            self.down_dropped += len(pcm)  # 收到音频但此刻没有下行客户端

    def _make_ssl(self):
        if self.tls and self.insecure:
            import ssl

            c = ssl.create_default_context()
            c.check_hostname = False
            c.verify_mode = ssl.CERT_NONE
            return c
        return None

    # ---- TCP 上行(MATLAB → 桥 → 小歌)----
    async def on_up(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log(f"上行 TCP 已连接 {writer.get_extra_info('peername')}")
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                self.up_bytes += len(data)
                if self.client is not None:
                    await self.client.send_pcm(data)
        finally:
            writer.close()
            log("上行 TCP 断开")

    # ---- TCP 下行(小歌/自测 → 桥 → MATLAB)----
    async def on_down(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log(f"下行 TCP 已连接 {writer.get_extra_info('peername')}  (self-test={self.selftest})")
        self._down = writer
        try:
            if self.selftest:
                phase = 0
                while not writer.is_closing():
                    writer.write(_tone_frame(phase))
                    self.down_written += 640
                    phase += 320
                    await writer.drain()
                    await asyncio.sleep(0.02)  # 实时下发
            else:
                await reader.read()  # 持有到对端关闭
        except Exception as e:  # noqa: BLE001
            log(f"下行处理异常: {e!r}")
        finally:
            if self._down is writer:
                self._down = None
            writer.close()
            log("下行 TCP 断开")

    async def ticker(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            log(
                f"状态 ws_ready={self.ready} up={self.up_bytes}B "
                f"down_connected={self._down is not None} "
                f"audio_recv={self.audio_frames}帧/{self.audio_bytes}B "
                f"down_written={self.down_written}B dropped={self.down_dropped}B"
            )

    async def serve(self, up_port: int, down_port: int) -> None:
        tasks = []
        if not self.selftest:
            self.client = XiaogeClient(self.host, self.port, tls=self.tls, ssl=self._make_ssl())
            self.client.on_ready = self.on_ready
            self.client.on_audio = self.on_audio
            self.client.on_clear = lambda: log("小歌 CLEAR(打断)")
            self.client.on_busy = lambda m: log(f"小歌 BUSY: {m}")
            tasks.append(self.client.run())
        up = await asyncio.start_server(self.on_up, "0.0.0.0", up_port)
        down = await asyncio.start_server(self.on_down, "0.0.0.0", down_port)
        log(f"诊断桥就绪:上行 :{up_port} 下行 :{down_port}  模式={'自测' if self.selftest else '真机'}")
        tasks += [up.serve_forever(), down.serve_forever(), self.ticker()]
        async with up, down:
            await asyncio.gather(*tasks)

    def dump_wav(self) -> None:
        if self.sink and not self.selftest:
            with wave.open("bridge_recv.wav", "wb") as w:
                w.setframerate(16000)
                w.setnchannels(1)
                w.setsampwidth(2)
                w.writeframes(bytes(self.sink))
            log(f"已把桥收到的小歌音频存为 bridge_recv.wav ({len(self.sink)}B)")


def main() -> None:
    p = argparse.ArgumentParser(description="诊断版 Simulink↔小歌 桥")
    p.add_argument("host", nargs="?", default="")
    p.add_argument("port", nargs="?", type=int, default=0)
    p.add_argument("--up", type=int, default=5001)
    p.add_argument("--down", type=int, default=5002)
    p.add_argument("--tls", action="store_true")
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--selftest", action="store_true", help="不连小歌,只对下行发正弦音")
    a = p.parse_args()
    if not a.selftest and (not a.host or not a.port):
        raise SystemExit("真机模式需要 <host> <port>;或用 --selftest")
    b = DebugBridge(a.host, a.port, a.tls, a.insecure, a.selftest)
    try:
        asyncio.run(b.serve(a.up, a.down))
    except KeyboardInterrupt:
        log("退出中…")
        b.dump_wav()


if __name__ == "__main__":
    main()
