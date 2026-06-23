"""录音回放注入(自动化测试 阶段1)。

把一段 wav 当作"用户说的话"按真实节奏注入会话:在所有 tap 之前替换
session.input.audio。让判停问题可复现,供阶段2/3 的 A/B 与扫参用同一段输入。

硬约束:opt-in(仅 AGENT_SCENARIO 设了才启用,默认正常麦克风、零影响)、非阻塞
(逐帧 await sleep、绝对时刻对齐防漂移)、解耦(只依赖 livekit io/rtc)、稳定。

行为:lead 静音(让开场白先放完)→ wav 帧(末帧补零)→ 之后持续静音(让 VAD 判到
说完→触发回复;会话保持存活供观察/录音)。**不结束迭代**(结束会让会话误判输入关闭)。
"""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path

import numpy as np

from livekit import rtc
from livekit.agents.voice import io


def _read_wav_mono(path: str) -> tuple[np.ndarray, int]:
    """读 16-bit PCM wav → (mono int16, rate)。立体声下混单声道。"""
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        nframes = w.getnframes()
        raw = w.readframes(nframes)
    if sampwidth != 2:
        raise ValueError(f"only 16-bit PCM wav supported, got sampwidth={sampwidth}")
    arr = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        arr = arr.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return arr, int(rate)


def _resolve(path: str | Path) -> Path:
    """解析路径:存在则用之;否则相对路径回退到仓库根(agent 运行 cwd 在
    examples/voice_agents,而 runs/ 在仓库根 —— 让 AGENT_SCENARIO=runs/... 也能用)。"""
    p = Path(path)
    if p.exists() or p.is_absolute():
        return p
    root = Path(__file__).resolve().parents[2]
    cand = root / p
    return cand if cand.exists() else p


def load_scenario(path: str) -> dict:
    """加载场景:.wav 直接当音频;.json 支持 {wav, expect?, lead_silence_s?, frame_ms?}。
    json 里的 wav 相对路径按 json 所在目录解析,找不到再回退仓库根。"""
    p = _resolve(path)
    if p.suffix.lower() == ".json":
        cfg = json.loads(p.read_text(encoding="utf-8"))
        wav = cfg["wav"]
        wavp = Path(wav)
        if not wavp.is_absolute():
            wavp = p.parent / wav
        if not wavp.exists():
            wavp = _resolve(wav)
        return {
            "wav": str(wavp),
            "expect": cfg.get("expect"),
            "lead_silence_s": float(cfg.get("lead_silence_s", 4.0)),
            "frame_ms": int(cfg.get("frame_ms", 10)),
        }
    return {"wav": str(_resolve(p)), "expect": None, "lead_silence_s": 4.0, "frame_ms": 10}


class ScriptedAudioInput(io.AudioInput):
    """自生成的音频输入:按真实节奏把 wav 帧喂进会话,放完后持续吐静音。"""

    def __init__(
        self,
        samples: np.ndarray,
        rate: int,
        *,
        frame_ms: int = 10,
        lead_silence_s: float = 4.0,
        expect: str | None = None,
    ) -> None:
        super().__init__(label="scripted-audio")
        self.expect = expect
        self._rate = int(rate)
        self._fs = max(1, int(self._rate * frame_ms / 1000))
        self._frame_dur = self._fs / self._rate
        self._silence = bytes(self._fs * 2)  # 一帧静音(int16)

        frames: list[bytes] = []
        lead = int(round(lead_silence_s / self._frame_dur)) if self._frame_dur > 0 else 0
        frames.extend([self._silence] * lead)
        s = np.asarray(samples, dtype=np.int16).reshape(-1)
        for i in range(0, len(s), self._fs):
            chunk = s[i : i + self._fs]
            if len(chunk) < self._fs:
                chunk = np.concatenate([chunk, np.zeros(self._fs - len(chunk), dtype=np.int16)])
            frames.append(chunk.tobytes())
        self._frames = frames
        self._idx = 0
        self._n = 0
        self._t0: float | None = None

    @classmethod
    def from_scenario(cls, path: str) -> ScriptedAudioInput:
        cfg = load_scenario(path)
        samples, rate = _read_wav_mono(cfg["wav"])
        return cls(
            samples,
            rate,
            frame_ms=cfg["frame_ms"],
            lead_silence_s=cfg["lead_silence_s"],
            expect=cfg["expect"],
        )

    async def __anext__(self) -> rtc.AudioFrame:
        loop = asyncio.get_running_loop()
        if self._t0 is None:
            self._t0 = loop.time()
        # 绝对时刻对齐:目标 = 起点 + n×帧长,防止逐帧 sleep 的累计漂移
        target = self._t0 + self._n * self._frame_dur
        delay = target - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        self._n += 1
        if self._idx < len(self._frames):
            data = self._frames[self._idx]
            self._idx += 1
        else:
            data = self._silence  # wav 放完:持续静音(不结束迭代)
        return rtc.AudioFrame(
            data=data,
            sample_rate=self._rate,
            num_channels=1,
            samples_per_channel=self._fs,
        )
