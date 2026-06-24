"""输入源头静音门(关麦 = 真关麦)。

最内层包裹 session.input.audio(在 recorder / KWS / 在线2pass tap **之前**):
`muted` 时把真实麦克风帧**替换为等长静音帧**。于是关麦时下游所有消费者(主STT /
在线2pass / KWS / 录音)一致拿到静音 → **不转写、不打断、真人声不出本机、各 WS 静音帧保活**。

设计见 TURN_STT_DESIGN.md §5.7。`muted` 为简单 bool(单写[web 线程] + 单读[agent loop],
GIL-safe,不引锁);默认 False 直通 → 不开即零影响。
"""

from __future__ import annotations

from livekit import rtc
from livekit.agents.voice import io


class MuteGate(io.AudioInput):
    def __init__(self, source: io.AudioInput) -> None:
        super().__init__(label="mute-gate", source=source)
        self.muted = False

    async def __anext__(self) -> rtc.AudioFrame:
        frame = await super().__anext__()  # 仍从源拉真实帧(推进源;真音频止于此)
        if not self.muted:
            return frame
        # 关麦:同形状静音帧(零),真实人声不向下游传播
        return rtc.AudioFrame(
            data=bytes(len(bytes(frame.data))),
            sample_rate=frame.sample_rate,
            num_channels=frame.num_channels,
            samples_per_channel=frame.samples_per_channel,
        )
