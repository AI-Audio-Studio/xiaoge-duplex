"""STT/TTS 远程模型适配器包(阶段2,由单文件 custom_audio_providers.py 拆分而来)。

结构:
  config.py   后端配置 dataclass + 热词/超时(env 集中)
  helpers.py  共享积木:HTTP 会话/重采样/SSL/分句聚合/音频队列排水
  stt/        funasr_offline · funasr_2pass · funasr_stream(带内置VAD) · qwen3 · iflytek
  tts/        cosyvoice(默认) · bailian(qwen-tts-realtime 系) · http

加新后端:在 stt/ 或 tts/ 加一个模块(继承 livekit 的 stt.STT / tts.TTS,
复用 helpers/config),再到本文件 re-export、并在应用层注册表登记一行。
"""

from providers.stt.funasr_2pass import FunASRStreamingSTT
from providers.stt.funasr_offline import FunASROfflineSTT
from providers.stt.funasr_stream import FunASRStreamSTT
from providers.stt.iflytek import IFlyTekRTASR
from providers.stt.qwen3 import Qwen3ASROfflineSTT
from providers.tts.bailian import BailianRealtimeTTS
from providers.tts.cosyvoice import CosyVoiceStreamingTTS
from providers.tts.http import HttpStreamingTTS
from providers.tts.qwen_stream import QwenStreamingTTS

__all__ = [
    "BailianRealtimeTTS",
    "CosyVoiceStreamingTTS",
    "FunASROfflineSTT",
    "FunASRStreamSTT",
    "FunASRStreamingSTT",
    "HttpStreamingTTS",
    "IFlyTekRTASR",
    "Qwen3ASROfflineSTT",
    "QwenStreamingTTS",
]
