"""STT/TTS 热切换代理:AgentSession/StreamAdapter 持有代理引用,switch_backend()
原子换内部 _backend(GIL 安全),下一句生效,无需重启会话/适配器。

失败不致命:STT 吞异常返回空(抛出会杀死 StreamAdapter 识别流→永久变聋);
TTS 把后端 error 事件转发到代理,框架"可恢复错误→记录并继续"逻辑生效。
"""

from __future__ import annotations

import logging

from livekit.agents import (
    APIConnectOptions,
    LanguageCode,
    stt as agents_stt,
    tts,
    utils as lk_utils,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr

logger = logging.getLogger("web-ui-agent")


class SwitchableSTT(agents_stt.STT):
    """STT proxy supporting runtime backend switching and mute.

    The StreamAdapter holds a reference to this object. Swapping `_backend`
    here is enough — the next recognition call will use the new backend.
    """

    def __init__(self, initial_backend: agents_stt.STT) -> None:
        super().__init__(
            capabilities=agents_stt.STTCapabilities(
                streaming=False,
                interim_results=False,
                aligned_transcript=False,
                offline_recognize=True,
            )
        )
        self._backend: agents_stt.STT = initial_backend
        self.muted: bool = False

    @property
    def model(self) -> str:
        return self._backend.model

    @property
    def provider(self) -> str:
        return self._backend.provider

    def switch_backend(self, new_backend: agents_stt.STT) -> agents_stt.STT:
        """Swap backend atomically (GIL-safe). Returns old backend for cleanup."""
        old = self._backend
        self._backend = new_backend
        return old

    async def _recognize_impl(
        self,
        buffer: lk_utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> agents_stt.SpeechEvent:
        if self.muted:
            return agents_stt.SpeechEvent(
                type=agents_stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[agents_stt.SpeechData(language=LanguageCode("zh"), text="")],
            )
        backend = self._backend
        try:
            return await backend._recognize_impl(
                buffer,
                language=language,
                conn_options=conn_options,
            )
        except Exception as exc:
            # A failing backend (e.g. just switched to an unreachable ASR server)
            # MUST NOT propagate: the exception would tear down the StreamAdapter
            # recognition stream and the agent would go permanently deaf, even
            # after switching back. Swallow it and return an empty transcript so
            # the pipeline stays alive and a subsequent backend switch recovers.
            logger.warning(
                "STT backend %s recognize failed (returning empty): %s",
                getattr(backend, "provider", backend),
                exc,
            )
            return agents_stt.SpeechEvent(
                type=agents_stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[agents_stt.SpeechData(language=LanguageCode("zh"), text="")],
            )

    async def prewarm_connection(self) -> None:
        if hasattr(self._backend, "prewarm_connection"):
            await self._backend.prewarm_connection()

    async def aclose(self) -> None:
        await self._backend.aclose()


class SwitchableTTS(tts.TTS):
    """TTS proxy supporting runtime backend switching.

    AgentSession holds a reference to this object. Swapping `_backend`
    here is enough — the next synthesis call will use the new backend.
    """

    def __init__(self, initial_backend: tts.TTS) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=initial_backend.sample_rate,
            num_channels=initial_backend.num_channels,
        )
        self._backend: tts.TTS = initial_backend
        self._backend.on("error", self._on_backend_error)

    def _on_backend_error(self, error: object) -> None:
        # Re-emit the active backend's TTS errors on this proxy. The framework
        # subscribes to errors on the proxy (it never sees the backend object),
        # so without this a backend failure (e.g. switched to an unreachable TTS)
        # would bypass the framework's resilient TTS-error handling. Connection
        # errors are recoverable -> the session logs and continues; switching back
        # to a working backend recovers. Symmetric to SwitchableSTT's isolation.
        self.emit("error", error)

    @property
    def provider(self) -> str:
        return self._backend.provider

    def switch_backend(self, new_backend: tts.TTS) -> tts.TTS:
        old = self._backend
        try:
            old.off("error", self._on_backend_error)
        except Exception:
            pass
        new_backend.on("error", self._on_backend_error)
        self._backend = new_backend
        return old

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return self._backend.synthesize(text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.SynthesizeStream:
        return self._backend.stream(conn_options=conn_options)

    def prewarm_connection(self) -> None:
        if hasattr(self._backend, "prewarm_connection"):
            self._backend.prewarm_connection()

    async def aclose(self) -> None:
        try:
            self._backend.off("error", self._on_backend_error)
        except Exception:
            pass
        await self._backend.aclose()
