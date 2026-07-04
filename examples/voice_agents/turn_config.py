"""判停(turn-taking)相关参数的集中配置。

把原本写死/散落的 VAD 静音、endpointing 收尾窗口、打断阈值、抢跑开关收口成一处,
全部 env 可覆盖,**默认值 = 当前线上写死值**——不设任何 TURN_* 环境变量时,行为与
改动前逐字节一致(零回归)。这是后续"判停调优 / 扫参"的统一旋钮面;调好的值最终
通过 .env / 默认值发布到正式版(与"测试用 KPI 仪表盘"分开,后者只在测试模式挂载)。

设计:纯数据 + from_env(),无副作用、不依赖其他模块(解耦);沿用 KwsConfig /
OnlineInterruptConfig / LiveTranscriptConfig 的惯例。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.config_utils import env_bool, env_float, env_float_opt, env_int, env_pair


@dataclass
class TurnConfig:
    """判停旋钮(默认 = 当前线上值;不设 env 即无变化)。"""

    # VAD:多长静音算"说完一段"。**主切分源**(写死在 prewarm 的 silero.VAD.load)。
    vad_min_silence_s: float = 0.35
    # endpointing:轮次收尾的最短/最长等待。
    endpoint_min_delay_s: float = 0.3
    endpoint_max_delay_s: float = 0.6
    # 抢跑(在 final 前预生成回复,会放大"被续话打断后留下残片回复"的观感)。
    preemptive_tts: bool = True
    # 打断阈值(次要,先一并引出便于调)。
    interruption_min_words: int = 3
    interruption_min_duration_s: float = 2.0
    backchannel_boundary: tuple[float, float] = (1.8, 3.5)
    # turn detector 判定阈值:EOU 概率 < 此值视作"没说完"→ 等 max_delay(更耐心)。
    # None=用模型默认。调大=更多情况判"没说完"=更少过早提交(但延迟可能升)。
    unlikely_threshold: float | None = None

    @classmethod
    def from_env(cls) -> TurnConfig:
        return cls(
            vad_min_silence_s=env_float("TURN_VAD_MIN_SILENCE", 0.35),
            endpoint_min_delay_s=env_float("TURN_ENDPOINT_MIN_DELAY", 0.3),
            endpoint_max_delay_s=env_float("TURN_ENDPOINT_MAX_DELAY", 0.6),
            preemptive_tts=env_bool("TURN_PREEMPTIVE_TTS", True),
            interruption_min_words=env_int("TURN_INTR_MIN_WORDS", 3),
            interruption_min_duration_s=env_float("TURN_INTR_MIN_DURATION", 2.0),
            backchannel_boundary=env_pair("TURN_INTR_BACKCHANNEL", (1.8, 3.5)),
            unlikely_threshold=env_float_opt("TURN_UNLIKELY_THRESHOLD"),
        )

    def turn_handling(self, turn_detection: Any) -> dict[str, Any]:
        """组装 AgentSession 的 turn_handling 字典(turn_detection 由调用方传入)。"""
        return {
            "turn_detection": turn_detection,
            "interruption": {
                "min_words": self.interruption_min_words,
                "min_duration": self.interruption_min_duration_s,
                "backchannel_boundary": self.backchannel_boundary,
            },
            "endpointing": {
                "min_delay": self.endpoint_min_delay_s,
                "max_delay": self.endpoint_max_delay_s,
            },
            "preemptive_generation": {"preemptive_tts": self.preemptive_tts},
        }
