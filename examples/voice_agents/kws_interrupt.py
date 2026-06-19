"""本地 sherpa-onnx 关键词强打断（KWS）。

直接在音频流上识别"停/别说了"等关键词，比等 FunASR 出 final 早 ~0.5-1.5s。
源自 duplexMVP2 的 native KWS 路径，只保留音频识别这一半——文本停止词回退
仍由 agent 自带的 _STOP_WORDS / on_user_turn_completed 兜底。

缺模型或缺依赖（sherpa_onnx / pypinyin / numpy）时自动降级为 no-op，不阻塞启动。
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from livekit import rtc
from livekit.agents.voice import io

try:  # 可选依赖，缺了就降级
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore[assignment]

try:
    import sherpa_onnx
except Exception:  # pragma: no cover
    sherpa_onnx = None  # type: ignore[assignment]

try:
    from pypinyin import Style, pinyin
except Exception:  # pragma: no cover
    Style = None  # type: ignore[assignment]
    pinyin = None  # type: ignore[assignment]


logger = logging.getLogger("kws-interrupt")

# Bundled KWS model shipped under <repo>/models/kws/. Resolved relative to this
# file (not the cwd, which is examples/voice_agents at runtime) so native KWS
# works out of the box without any environment configuration.
_DEFAULT_KWS_MODEL_DIR = (
    Path(__file__).resolve().parents[2]
    / "models"
    / "kws"
    / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
)


def _default_model_dir() -> str | None:
    return str(_DEFAULT_KWS_MODEL_DIR) if _DEFAULT_KWS_MODEL_DIR.is_dir() else None


DEFAULT_KEYWORDS: tuple[str, ...] = (
    "停",
    "停下",
    "停一下",
    "别说了",
    "你别说了",
    "别讲了",
    "等等",
    "等一下",
    "不要讲了",
)


@dataclass(slots=True)
class KwsConfig:
    enable: bool = False
    model_dir: str | None = None
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    keywords_file: str | None = None
    sample_rate: int = 16_000
    num_threads: int = 2
    feature_dim: int = 80
    max_active_paths: int = 4
    keywords_score: float = 1.0
    keywords_threshold: float = 0.18
    num_trailing_blanks: int = 1
    provider: str = "cpu"
    debounce_ms: int = 800

    @classmethod
    def from_env(cls) -> "KwsConfig":
        keywords = tuple(
            token.strip()
            for token in os.getenv("XIAOGE_KWS_KEYWORDS", "").split("|")
            if token.strip()
        ) or DEFAULT_KEYWORDS
        return cls(
            enable=_parse_bool(os.getenv("XIAOGE_KWS_ENABLE_NATIVE", "1")),
            model_dir=(os.getenv("XIAOGE_KWS_MODEL_DIR", "").strip() or _default_model_dir()),
            keywords=keywords,
            keywords_file=os.getenv("XIAOGE_KWS_KEYWORDS_FILE", "").strip() or None,
            keywords_score=_parse_float(os.getenv("XIAOGE_KWS_KEYWORDS_SCORE"), 1.0),
            keywords_threshold=_parse_float(os.getenv("XIAOGE_KWS_KEYWORDS_THRESHOLD"), 0.18),
            num_trailing_blanks=_parse_int(os.getenv("XIAOGE_KWS_NUM_TRAILING_BLANKS"), 1),
            debounce_ms=_parse_int(os.getenv("XIAOGE_KWS_DEBOUNCE_MS"), 800),
        )


class NativeKwsSpotter:
    """音频流喂入 sherpa-onnx，命中关键词时在事件循环上回调 on_hit。

    解码跑在独立线程里，push() 只做非阻塞入队，不拖累音频管线。
    """

    def __init__(
        self,
        *,
        spotter: object,
        stream: object,
        debounce_ms: int,
        loop: asyncio.AbstractEventLoop,
        on_hit: Callable[[str], None],
    ) -> None:
        self._spotter = spotter
        self._stream = stream
        self._debounce_ms = debounce_ms
        self._loop = loop
        self._on_hit = on_hit
        self._queue: queue.Queue[tuple[bytes, int, int] | None] = queue.Queue(maxsize=256)
        self._thread = threading.Thread(target=self._run, name="kws-spotter", daemon=True)
        self._last_hit_keyword: str | None = None
        self._last_hit_at_ms = 0.0
        self._closed = False

    @classmethod
    def try_create(
        cls,
        config: KwsConfig,
        *,
        loop: asyncio.AbstractEventLoop,
        on_hit: Callable[[str], None],
    ) -> "NativeKwsSpotter | None":
        reason = _unavailable_reason(config)
        if reason is not None:
            logger.info("native KWS disabled: %s", reason)
            return None

        model_root = Path(config.model_dir or "")
        artifacts = _find_model_artifacts(model_root)
        assert artifacts is not None  # _unavailable_reason already checked
        keywords_file = (
            Path(config.keywords_file)
            if config.keywords_file
            else model_root / "generated-keywords.txt"
        )
        _write_keywords_file(keywords_file, config.keywords)
        spotter = sherpa_onnx.KeywordSpotter(  # type: ignore[union-attr]
            tokens=str(artifacts["tokens"]),
            encoder=str(artifacts["encoder"]),
            decoder=str(artifacts["decoder"]),
            joiner=str(artifacts["joiner"]),
            keywords_file=str(keywords_file),
            num_threads=config.num_threads,
            sample_rate=int(config.sample_rate),
            feature_dim=config.feature_dim,
            max_active_paths=config.max_active_paths,
            keywords_score=config.keywords_score,
            keywords_threshold=config.keywords_threshold,
            num_trailing_blanks=config.num_trailing_blanks,
            provider=config.provider,
        )
        stream = spotter.create_stream()
        self = cls(
            spotter=spotter,
            stream=stream,
            debounce_ms=config.debounce_ms,
            loop=loop,
            on_hit=on_hit,
        )
        self._thread.start()
        logger.info("native KWS active: keywords=%s model=%s", config.keywords, model_root)
        return self

    def push(self, frame: rtc.AudioFrame) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait((bytes(frame.data), frame.sample_rate, frame.num_channels))
        except queue.Full:
            # 解码跟不上时丢最旧的，保证不积压拖延
            try:
                self._queue.get_nowait()
                self._queue.put_nowait((bytes(frame.data), frame.sample_rate, frame.num_channels))
            except queue.Empty:
                pass

    def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            audio, sample_rate, channels = item
            keyword = self._decode(audio, sample_rate=sample_rate, channels=channels)
            if keyword is not None:
                self._loop.call_soon_threadsafe(self._on_hit, keyword)

    def _decode(self, audio: bytes, *, sample_rate: int, channels: int) -> str | None:
        if np is None:
            return None
        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)

        self._stream.accept_waveform(sample_rate, samples)  # type: ignore[attr-defined]
        hit: str | None = None
        while self._spotter.is_ready(self._stream):  # type: ignore[attr-defined]
            self._spotter.decode_stream(self._stream)  # type: ignore[attr-defined]
            result = self._spotter.get_result(self._stream).strip()  # type: ignore[attr-defined]
            if result:
                hit = result
        if not hit:
            return None

        now_ms = time.monotonic() * 1000.0
        if hit == self._last_hit_keyword and now_ms - self._last_hit_at_ms < self._debounce_ms:
            self._spotter.reset_stream(self._stream)  # type: ignore[attr-defined]
            return None
        self._last_hit_keyword = hit
        self._last_hit_at_ms = now_ms
        self._spotter.reset_stream(self._stream)  # type: ignore[attr-defined]
        return hit


class KwsTapAudioInput(io.AudioInput):
    """透传包装：每帧原样返回给管线，同时旁路喂给 KWS。

    依赖 io.AudioInput 基类：__anext__ / on_attached / on_detached 都委托给 source，
    所以 session.input.audio setter 的 detach->attach 不会切断底层输入。
    """

    def __init__(self, source: io.AudioInput, spotter: NativeKwsSpotter) -> None:
        super().__init__(label="kws-tap", source=source)
        self._spotter = spotter

    async def __anext__(self) -> rtc.AudioFrame:
        frame = await super().__anext__()
        self._spotter.push(frame)
        return frame


def _unavailable_reason(config: KwsConfig) -> str | None:
    if not config.enable:
        return "not requested (XIAOGE_KWS_ENABLE_NATIVE)"
    if not config.model_dir:
        return "model dir missing (XIAOGE_KWS_MODEL_DIR)"
    if sherpa_onnx is None:
        return "sherpa_onnx not installed"
    if np is None:
        return "numpy not installed"
    if pinyin is None or Style is None:
        return "pypinyin not installed"
    if _find_model_artifacts(Path(config.model_dir)) is None:
        return f"model files missing in {config.model_dir}"
    return None


def _find_model_artifacts(model_root: Path) -> dict[str, Path] | None:
    if not model_root.is_dir():
        return None
    tokens = model_root / "tokens.txt"
    encoder = model_root / "encoder-epoch-13-avg-2-chunk-8-left-64.onnx"
    decoder = model_root / "decoder-epoch-13-avg-2-chunk-8-left-64.onnx"
    joiner = model_root / "joiner-epoch-13-avg-2-chunk-8-left-64.onnx"
    if not all(path.is_file() for path in (tokens, encoder, decoder, joiner)):
        return None
    return {"tokens": tokens, "encoder": encoder, "decoder": decoder, "joiner": joiner}


def _write_keywords_file(path: Path, keywords: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [line for phrase in keywords if (line := _phrase_to_keyword_line(phrase))]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _phrase_to_keyword_line(phrase: str) -> str:
    if pinyin is None or Style is None:
        return ""
    clean = "".join(ch for ch in phrase.strip() if not ch.isspace() and ch not in "，,。！？!?；;、")
    if not clean:
        return ""
    initials = pinyin(clean, style=Style.INITIALS, strict=False)
    finals = pinyin(clean, style=Style.FINALS_TONE, strict=False)
    tokens: list[str] = []
    for initial_parts, final_parts in zip(initials, finals, strict=True):
        initial = str(initial_parts[0] or "").strip()
        final = str(final_parts[0] or "").strip()
        if initial and initial != clean:
            tokens.append(initial)
        if final:
            tokens.append(final)
    return f"{' '.join(tokens)} @{clean}"


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_float(value: str | None, default: float) -> float:
    if not value:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
