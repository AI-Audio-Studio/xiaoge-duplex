"""STT/TTS 后端注册表 + 工厂 + build_llm —— 扩展点单一来源。

加一个新后端:providers/ 下加一个模块,这里在注册表补一行(key/tab_id/工厂),
面板 tab 由服务端按注册表生成、/api/{asr,tts} 校验与构造走同一张表。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import openai
from common.config_utils import env_bool
from providers import (
    CosyVoiceStreamingTTS,
    FunASROfflineSTT,
    HttpStreamingTTS,
    Qwen3ASROfflineSTT,
    QwenStreamingTTS,
)

from app.session_state import runtime
from app.switchable import SwitchableSTT, SwitchableTTS
from livekit.agents import stt as agents_stt, tts
from livekit.plugins import openai as lk_openai

logger = logging.getLogger("web-ui-agent")


class Qwen3StreamSTT(Qwen3ASROfflineSTT):
    """Same growing-buffer WebSocket protocol as Qwen3ASROfflineSTT, different server."""

    @property
    def model(self) -> str:
        return "qwen3-asr-stream"

    @property
    def provider(self) -> str:
        return "Qwen3ASR-Stream"


def _make_funasr() -> agents_stt.STT:
    url = os.getenv("FUNASR_WS_URL", "wss://60.205.197.165:10090")
    logger.info("STT backend: FunASR  url=%s", url)
    return FunASROfflineSTT(websocket_url=url)


def _make_qwen3() -> agents_stt.STT:
    url = os.getenv("QWEN3_ASR_WS_URL", "ws://60.205.197.165:10091/ws/transcribe")
    logger.info("STT backend: Qwen3-ASR  url=%s", url)
    return Qwen3ASROfflineSTT(websocket_url=url)


def _make_qwen3_stream() -> agents_stt.STT:
    url = os.getenv("QWEN3_ASR_STREAM_WS_URL", "ws://10.212.164.230:10091/ws/transcribe")
    logger.info("STT backend: Qwen3-ASR-Stream  url=%s", url)
    return Qwen3StreamSTT(websocket_url=url)


def _make_tts_http() -> tts.TTS:
    url = os.getenv("HTTP_TTS_URL", "http://10.212.164.230:8001")
    logger.info("TTS backend: HttpStreamingTTS  url=%s/tts", url)
    return HttpStreamingTTS(base_url=url)


def _make_tts_cosyvoice() -> tts.TTS:
    model = os.getenv("COSYVOICE_MODEL", "cosyvoice-v3-flash")
    # 默认女声(贴小歌"暖心知己"人设)。可用 COSYVOICE_VOICE 覆盖切换试听。
    # 其他候选女声(同为 cosyvoice-v3 系列):
    #   longanwen_v3 龙安温 优雅知性女 · longanrou_v3 龙安柔 温柔闺蜜女
    #   longanli_v3  龙安莉 利落从容女
    voice = os.getenv("COSYVOICE_VOICE", "longxiaochun_v3")
    logger.info("TTS backend: CosyVoiceStreamingTTS  model=%s voice=%s", model, voice)
    return CosyVoiceStreamingTTS(model=model, voice=voice)


def _make_tts_qwen() -> tts.TTS:
    logger.info("TTS backend: QwenStreamingTTS")
    return QwenStreamingTTS()


@dataclass(frozen=True)
class BackendSpec:
    key: str
    tab_id: str  # 面板隐藏按钮 id(现有后端保持原 id,JS 高亮逻辑依赖)
    make: Callable[[], agents_stt.STT] | Callable[[], tts.TTS]


# 注册表顺序 = 面板 tab 顺序(与拆分前 HTML 一致)。
STT_BACKENDS: dict[str, BackendSpec] = {
    "funasr": BackendSpec("funasr", "tabFunasr", _make_funasr),
    "qwen3": BackendSpec("qwen3", "tabQwen3", _make_qwen3),
    "qwen3-stream": BackendSpec("qwen3-stream", "tabQwen3Stream", _make_qwen3_stream),
}
TTS_BACKENDS: dict[str, BackendSpec] = {
    "cosyvoice": BackendSpec("cosyvoice", "tabTtsCosy", _make_tts_cosyvoice),
    "qwen": BackendSpec("qwen", "tabTtsQwen", _make_tts_qwen),
    "http": BackendSpec("http", "tabTtsHttp", _make_tts_http),
}


def make_stt_backend(backend: str) -> agents_stt.STT:
    """Construct a (non-switchable) STT backend. Single source of truth shared by
    build_stt() and the /api/asr switch handler."""
    return STT_BACKENDS[backend].make()


def make_tts_backend(backend: str) -> tts.TTS:
    """Construct a (non-switchable) TTS backend. Single source of truth shared by
    build_tts() and the /api/tts switch handler."""
    return TTS_BACKENDS[backend].make()


def backend_tabs_html() -> str:
    """按注册表生成面板隐藏 tab 按钮(替换 index.html 的 <!--BACKEND_TABS-->)。"""
    lines = [
        f"""<button id="{s.tab_id}" onclick="switchASR('{s.key}')"></button>"""
        for s in STT_BACKENDS.values()
    ] + [
        f"""<button id="{s.tab_id}" onclick="switchTTS('{s.key}')"></button>"""
        for s in TTS_BACKENDS.values()
    ]
    return "\n  ".join(lines)


def build_stt() -> SwitchableSTT:
    backend = (os.getenv("STT_BACKEND") or "funasr").strip().lower()
    if backend not in STT_BACKENDS:
        logger.warning("unknown STT_BACKEND=%r, falling back to funasr", backend)
        backend = "funasr"
    return SwitchableSTT(make_stt_backend(backend))


def build_tts() -> SwitchableTTS:
    backend = os.getenv("TTS_BACKEND", "cosyvoice").strip().lower()
    if backend not in TTS_BACKENDS:
        logger.warning("unknown TTS_BACKEND=%r, falling back to cosyvoice", backend)
        backend = "cosyvoice"
    runtime.tts_backend_key = backend
    return SwitchableTTS(make_tts_backend(backend))


def build_llm() -> lk_openai.LLM:
    base_url = os.getenv("QWEN_BASE_URL", "https://60.205.197.165:10092/llm/v1")
    api_key = os.getenv("QWEN_API_KEY", "EMPTY")
    model = os.getenv("QWEN_MODEL", "Qwen3-4B")
    verify_ssl = env_bool("QWEN_VERIFY_SSL", False)

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
