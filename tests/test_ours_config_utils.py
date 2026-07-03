"""行为锁定测试:common.config_utils(统一 env 解析,重点锁"空串"语义差异)。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

from common.config_utils import (  # noqa: E402
    env_bool,
    env_float,
    env_float_opt,
    env_int,
    env_pair,
    env_str,
)

_VAR = "XIAOGE_TEST_CONFIG_VAR"


@pytest.fixture()
def clean(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv(_VAR, raising=False)
    return monkeypatch


class TestEnvBool:
    def test_missing_gives_default(self, clean: pytest.MonkeyPatch) -> None:
        assert env_bool(_VAR, True) is True
        assert env_bool(_VAR, False) is False

    def test_truthy_values(self, clean: pytest.MonkeyPatch) -> None:
        for v in ("1", "true", "YES", " on "):
            clean.setenv(_VAR, v)
            assert env_bool(_VAR, False) is True, v

    def test_blank_is_false_by_default(self, clean: pytest.MonkeyPatch) -> None:
        # 多数模块的历史语义:设了但为空 -> 假
        clean.setenv(_VAR, "")
        assert env_bool(_VAR, True) is False

    def test_blank_is_default_optin(self, clean: pytest.MonkeyPatch) -> None:
        # listening_mode 的历史语义:设了但为空 -> 当没设
        clean.setenv(_VAR, "  ")
        assert env_bool(_VAR, True, blank_is_default=True) is True


class TestNumbers:
    def test_int_and_float(self, clean: pytest.MonkeyPatch) -> None:
        clean.setenv(_VAR, "42")
        assert env_int(_VAR, 7) == 42
        clean.setenv(_VAR, "0.5")
        assert env_float(_VAR, 1.0) == 0.5

    def test_bad_and_blank_fall_back(self, clean: pytest.MonkeyPatch) -> None:
        clean.setenv(_VAR, "abc")
        assert env_int(_VAR, 7) == 7
        assert env_float(_VAR, 1.5) == 1.5
        clean.setenv(_VAR, " ")
        assert env_int(_VAR, 7) == 7

    def test_float_opt(self, clean: pytest.MonkeyPatch) -> None:
        assert env_float_opt(_VAR) is None
        clean.setenv(_VAR, "0.2")
        assert env_float_opt(_VAR) == 0.2
        clean.setenv(_VAR, "bad")
        assert env_float_opt(_VAR) is None


class TestStrAndPair:
    def test_str_keeps_blank(self, clean: pytest.MonkeyPatch) -> None:
        assert env_str(_VAR, "d") == "d"
        clean.setenv(_VAR, "")
        assert env_str(_VAR, "d") == ""  # 空串是合法值(如"提示语=不出声")

    def test_pair(self, clean: pytest.MonkeyPatch) -> None:
        assert env_pair(_VAR, (1.0, 2.0)) == (1.0, 2.0)
        clean.setenv(_VAR, "1.5, 3")
        assert env_pair(_VAR, (1.0, 2.0)) == (1.5, 3.0)
        clean.setenv(_VAR, "oops")
        assert env_pair(_VAR, (1.0, 2.0)) == (1.0, 2.0)
