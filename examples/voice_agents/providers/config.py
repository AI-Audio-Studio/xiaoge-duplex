"""后端配置集中处:超时、FunASR 热词/握手载荷、各后端 Options dataclass。

原则:每个 os.getenv 都带内置默认(缺 .env 也能跑),默认值与拆分前逐字节一致。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

# ASR WS 建连上限:不可达后端快速失败(否则 Windows 上 TCP 连接卡 ~21s),
# 切到坏后端最多"暂时没反应",切回即恢复。
WS_CONNECT_TIMEOUT = float(os.getenv("ASR_WS_CONNECT_TIMEOUT", "5"))

# HTTP TTS 同理:只限建连,音频流本体不限时。
TTS_CONNECT_TIMEOUT = float(os.getenv("TTS_CONNECT_TIMEOUT", "5"))

# 短停止词(尤其单字"停")默认常被识别成"嗯/哦/行",整轮 0 召回(实测)。
# FunASR WS 协议握手支持热词加权(hotwords 字段,JSON 字符串 词->权重,建议 10~100),
# 把停止词喂进去拉升短词召回。可用 FUNASR_HOTWORDS 覆盖,格式 词:权重|词:权重。
DEFAULT_HOTWORDS: dict[str, int] = {
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


def funasr_hotwords() -> str:
    raw = os.getenv("FUNASR_HOTWORDS", "").strip()
    if not raw:
        return json.dumps(DEFAULT_HOTWORDS, ensure_ascii=False)
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


def funasr_init_payload(
    *, mode: str, wav_name: str, sample_rate: int, chunk_size: list[int]
) -> dict:
    """FunASR WS 握手 JSON(offline / 2pass 共用骨架;热词按 env 注入)。"""
    payload: dict = {
        "mode": mode,
        "chunk_size": chunk_size,
        "chunk_interval": 10,
        "wav_name": wav_name,
        "wav_format": "pcm",
        "audio_fs": sample_rate,
        "is_speaking": True,
        "itn": False,
    }
    if hotwords := funasr_hotwords():
        payload["hotwords"] = hotwords
    return payload


@dataclass
class FunASROptions:
    websocket_url: str
    sample_rate: int = 16000
    verify_ssl: bool = False
    language: str = "zh"
    chunk_size: int = 3200


@dataclass
class BailianTTSOptions:
    model: str
    voice: str
    sample_rate: int
    speech_rate: float


@dataclass
class CosyVoiceTTSOptions:
    model: str
    voice: str
    sample_rate: int = 24000
    speech_rate: float = 1.0
    pitch_rate: float = 1.0
    instruction: str | None = None

    @classmethod
    def from_env(cls, *, model: str | None = None, voice: str | None = None) -> CosyVoiceTTSOptions:
        return cls(
            model=model or os.getenv("COSYVOICE_MODEL", "cosyvoice-v3-flash"),
            # 默认女声;候选见应用层 _make_tts_backend(longanwen_v3/longanrou_v3/longanli_v3)。
            voice=voice or os.getenv("COSYVOICE_VOICE", "longxiaochun_v3"),
            instruction=os.getenv("COSYVOICE_INSTRUCTION") or None,
        )
