"""音频旁路 tap 基类:透传 + 观察。

此前 KWS / 在线打断 / 录音 / 测试录音 各自重写同一套 `__anext__` / `capture_frame`
透传样板(6 处),收敛到两个基类,子类只实现 `_on_frame()` 观察钩子。

承载式不变量(ARCHITECTURE §6.5,基类负责保证,子类不得破坏):
  - 输入 tap 的 `__anext__` **必须原样 return 帧**,否则下游 STT/VAD 断粮、agent 变聋;
  - 输出 tap **必须转发 `flush` / `clear_buffer`**,否则打断切不断播放。
`_on_frame()` 必须快(非阻塞:入队/加锁追加级别),重活下真线程。

注:MuteGate 会**替换**帧(变换器而非观察者),不属此基类,保持独立实现。
"""

from __future__ import annotations

from livekit import rtc
from livekit.agents.voice import io


class TapAudioInput(io.AudioInput):
    """输入旁路:每帧原样透传给管线,同时调 `_on_frame` 观察。

    依赖 io.AudioInput 基类:__anext__ / on_attached / on_detached 都委托给 source,
    所以 session.input.audio setter 的 detach->attach 不会切断底层输入。
    """

    def __init__(self, source: io.AudioInput, *, label: str) -> None:
        super().__init__(label=label, source=source)

    async def __anext__(self) -> rtc.AudioFrame:
        frame = await super().__anext__()
        self._on_frame(frame)
        return frame

    def _on_frame(self, frame: rtc.AudioFrame) -> None:
        """子类实现:旁路观察(必须快、必须不抛;帧只读不改)。"""
        raise NotImplementedError


class TapAudioOutput(io.AudioOutput):
    """输出旁路:先转发下游(播放优先),再调 `_on_frame` 观察;转发 flush/clear_buffer。"""

    def __init__(self, next_output: io.AudioOutput, *, label: str) -> None:
        super().__init__(
            label=label,
            next_in_chain=next_output,
            sample_rate=next_output.sample_rate,
            capabilities=io.AudioOutputCapabilities(pause=next_output.can_pause),
        )

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        if self.next_in_chain:
            await self.next_in_chain.capture_frame(frame)
        await super().capture_frame(frame)
        self._on_frame(frame)

    def flush(self) -> None:
        super().flush()
        if self.next_in_chain:
            self.next_in_chain.flush()

    def clear_buffer(self) -> None:
        if self.next_in_chain:
            self.next_in_chain.clear_buffer()

    def _on_frame(self, frame: rtc.AudioFrame) -> None:
        """子类实现:旁路观察(必须快、必须不抛;帧只读不改)。"""
        raise NotImplementedError
