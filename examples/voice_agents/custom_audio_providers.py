"""兼容 shim:实现已拆到 providers/ 包(阶段2重构)。

新代码请直接 `from providers import ...`(类)与
`from providers.config import funasr_hotwords`(热词)。本模块仅做 re-export,
保证旧引用/文档示例不失效;待外部引用清零后可删除。
"""

from providers import (  # noqa: F401
    BailianRealtimeTTS,
    CosyVoiceStreamingTTS,
    FunASROfflineSTT,
    FunASRStreamingSTT,
    HttpStreamingTTS,
    IFlyTekRTASR,
    Qwen3ASROfflineSTT,
    QwenStreamingTTS,
)
from providers.config import (  # noqa: F401
    DEFAULT_HOTWORDS as _DEFAULT_HOTWORDS,
    BailianTTSOptions,
    CosyVoiceTTSOptions,
    FunASROptions,
    funasr_hotwords as _funasr_hotwords,
)
