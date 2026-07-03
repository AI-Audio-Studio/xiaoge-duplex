"""行为锁定测试:turn_config.TurnConfig(默认值 / env 覆盖 / 坏值回退 / turn_handling 形状)。

重构护栏(阶段0):默认值 = 当前线上生效值,重构后必须逐项一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

from turn_config import TurnConfig  # noqa: E402

_ENV_VARS = (
    "TURN_VAD_MIN_SILENCE",
    "TURN_ENDPOINT_MIN_DELAY",
    "TURN_ENDPOINT_MAX_DELAY",
    "TURN_PREEMPTIVE_TTS",
    "TURN_INTR_MIN_WORDS",
    "TURN_INTR_MIN_DURATION",
    "TURN_INTR_BACKCHANNEL",
    "TURN_UNLIKELY_THRESHOLD",
)


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestDefaults:
    def test_from_env_without_env_equals_defaults(self, clean_env: pytest.MonkeyPatch) -> None:
        cfg = TurnConfig.from_env()
        assert cfg.vad_min_silence_s == 0.35
        assert cfg.endpoint_min_delay_s == 0.3
        assert cfg.endpoint_max_delay_s == 0.6
        assert cfg.preemptive_tts is True
        assert cfg.interruption_min_words == 3
        assert cfg.interruption_min_duration_s == 2.0
        assert cfg.backchannel_boundary == (1.8, 3.5)
        assert cfg.unlikely_threshold is None


class TestEnvOverride:
    def test_overrides(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("TURN_VAD_MIN_SILENCE", "0.5")
        clean_env.setenv("TURN_PREEMPTIVE_TTS", "0")
        clean_env.setenv("TURN_INTR_MIN_WORDS", "5")
        clean_env.setenv("TURN_INTR_BACKCHANNEL", "1.0, 2.0")
        clean_env.setenv("TURN_UNLIKELY_THRESHOLD", "0.2")
        cfg = TurnConfig.from_env()
        assert cfg.vad_min_silence_s == 0.5
        assert cfg.preemptive_tts is False
        assert cfg.interruption_min_words == 5
        assert cfg.backchannel_boundary == (1.0, 2.0)
        assert cfg.unlikely_threshold == 0.2

    def test_bad_values_fall_back_to_defaults(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("TURN_VAD_MIN_SILENCE", "abc")
        clean_env.setenv("TURN_INTR_MIN_WORDS", "3.5")
        clean_env.setenv("TURN_INTR_BACKCHANNEL", "oops")
        clean_env.setenv("TURN_UNLIKELY_THRESHOLD", "xx")
        cfg = TurnConfig.from_env()
        assert cfg.vad_min_silence_s == 0.35
        assert cfg.interruption_min_words == 3
        assert cfg.backchannel_boundary == (1.8, 3.5)
        assert cfg.unlikely_threshold is None

    def test_empty_string_means_default(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("TURN_VAD_MIN_SILENCE", "  ")
        cfg = TurnConfig.from_env()
        assert cfg.vad_min_silence_s == 0.35


class TestTurnHandling:
    def test_shape_and_values(self) -> None:
        cfg = TurnConfig()
        sentinel = object()
        handling = cfg.turn_handling(sentinel)
        assert handling == {
            "turn_detection": sentinel,
            "interruption": {
                "min_words": 3,
                "min_duration": 2.0,
                "backchannel_boundary": (1.8, 3.5),
            },
            "endpointing": {"min_delay": 0.3, "max_delay": 0.6},
            "preemptive_generation": {"preemptive_tts": True},
        }
