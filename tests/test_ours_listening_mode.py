"""行为锁定测试:listening_mode.ListeningController(纯状态机主路径)。

重构护栏(阶段0):进入/退出、自动进入连击、尾巴切分、整理回答判定、临时缓冲。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

from listening_mode import (  # noqa: E402
    AutoDecision,
    ListeningController,
    ListeningEvent,
)

_ENV_VARS = (
    "XIAOGE_LISTEN_ENABLE",
    "XIAOGE_LISTEN_COMMAND",
    "XIAOGE_LISTEN_WAKE",
    "XIAOGE_LISTEN_AUTO_ENABLE",
    "XIAOGE_LISTEN_AUTO_TURNS",
    "XIAOGE_LISTEN_AUTO_MINCHARS",
    "XIAOGE_LISTEN_TEMP_TTL",
    "XIAOGE_LISTEN_MIN_ORGANIZE_CHARS",
    "XIAOGE_LISTEN_ORGANIZE",
    "XIAOGE_LISTEN_DRAIN",
    "XIAOGE_LISTEN_ENTER_NOTICE",
)


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestFromEnvironment:
    def test_defaults(self, clean_env: pytest.MonkeyPatch) -> None:
        c = ListeningController.from_environment()
        assert c.enabled is False
        assert c.command_keyword == "小歌聆听模式"
        assert c.command_aliases == ("小歌进入聆听模式", "聆听模式")
        assert c.wake_keyword == "小歌干活了"
        assert c.auto_enabled is True
        assert c.auto_turns == 3
        assert c.auto_min_chars == 20
        assert c.temp_ttl_s == 120.0
        assert c.organize_enabled is False
        assert c.drain_s == 2.5

    def test_command_pipe_split(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("XIAOGE_LISTEN_COMMAND", "开始聆听| 进入聆听 |听着")
        c = ListeningController.from_environment()
        assert c.command_keyword == "开始聆听"
        assert c.command_aliases == ("进入聆听", "听着")

    def test_keywords_empty_when_disabled(self, clean_env: pytest.MonkeyPatch) -> None:
        c = ListeningController.from_environment()
        assert c.keywords == ()


class TestObserveKeyword:
    def _ctrl(self) -> ListeningController:
        return ListeningController(enabled=True)

    def test_disabled_is_noop(self) -> None:
        c = ListeningController(enabled=False)
        assert c.observe_keyword("小歌聆听模式") is ListeningEvent.NONE
        assert c.active is False

    def test_enter_via_main_and_alias(self) -> None:
        c = self._ctrl()
        assert c.observe_keyword("小歌聆听模式") is ListeningEvent.ENTERED
        assert c.active is True
        c2 = self._ctrl()
        assert c2.observe_keyword("聆听模式") is ListeningEvent.ENTERED

    def test_enter_normalizes_punctuation(self) -> None:
        c = self._ctrl()
        assert c.observe_keyword("小歌,聆听模式。") is ListeningEvent.ENTERED

    def test_wake_exits_only_when_active(self) -> None:
        c = self._ctrl()
        assert c.observe_keyword("小歌干活了") is ListeningEvent.NONE
        c.observe_keyword("小歌聆听模式")
        assert c.observe_keyword("小歌干活了") is ListeningEvent.EXITED
        assert c.active is False

    def test_enter_keyword_while_active_is_noop(self) -> None:
        c = self._ctrl()
        c.observe_keyword("小歌聆听模式")
        assert c.observe_keyword("小歌聆听模式") is ListeningEvent.NONE
        assert c.active is True


class TestAutoEnter:
    def test_auto_enter_after_streak(self) -> None:
        c = ListeningController(enabled=True, auto_turns=2, auto_min_chars=5)
        long_text = "这是一段足够长的自说自话内容"
        assert c.observe_turn(long_text, interrupted_agent=True) is AutoDecision.NONE
        assert c.auto_count == 1
        assert c.observe_turn(long_text, interrupted_agent=True) is AutoDecision.ENTER
        assert c.active is True
        assert c.auto_count == 0

    def test_short_text_neither_counts_nor_resets(self) -> None:
        c = ListeningController(enabled=True, auto_turns=2, auto_min_chars=10)
        c.observe_turn("这是一段足够长的自说自话内容", interrupted_agent=True)
        assert c.auto_count == 1
        assert c.observe_turn("嗯", interrupted_agent=True) is AutoDecision.NONE
        assert c.auto_count == 1

    def test_long_non_interrupt_resets(self) -> None:
        c = ListeningController(enabled=True, auto_turns=3, auto_min_chars=5)
        c.observe_turn("这是一段足够长的内容啊", interrupted_agent=True)
        assert c.auto_count == 1
        c.observe_turn("这也是一段足够长的内容", interrupted_agent=False)
        assert c.auto_count == 0

    def test_noop_when_active_or_disabled(self) -> None:
        c = ListeningController(enabled=True, auto_min_chars=1)
        c.observe_keyword("小歌聆听模式")
        assert c.observe_turn("很长的内容很长的内容", interrupted_agent=True) is AutoDecision.NONE


class TestBufferAndExit:
    def test_capture_moves_to_temp_on_exit(self) -> None:
        c = ListeningController(enabled=True)
        c.observe_keyword("小歌聆听模式")
        c.capture("第一句")
        c.capture("  ")  # 空白不入缓冲
        c.capture("第二句")
        assert c.force_exit() is True
        assert c.active is False
        assert c.temp_transcript == ["第一句", "第二句"]

    def test_force_exit_when_inactive(self) -> None:
        c = ListeningController(enabled=True)
        assert c.force_exit() is False

    def test_reenter_drops_previous_temp(self) -> None:
        c = ListeningController(enabled=True)
        c.observe_keyword("小歌聆听模式")
        c.capture("旧内容")
        c.force_exit()
        assert c.temp_transcript == ["旧内容"]
        c.observe_keyword("小歌聆听模式")
        assert c.temp_transcript == []

    def test_take_temp_clears(self) -> None:
        c = ListeningController(enabled=True)
        c.temp_transcript = ["a", "b"]
        assert c.take_temp() == ["a", "b"]
        assert c.temp_transcript == []


class TestSplitAfterCommand:
    def test_exact_with_content_after(self) -> None:
        c = ListeningController()
        assert c.split_after_command("小歌干活了今天天气怎么样", "小歌干活了") == "今天天气怎么样"

    def test_exact_at_end_returns_empty(self) -> None:
        c = ListeningController()
        assert c.split_after_command("刚才说的小歌干活了", "小歌干活了") == ""

    def test_fuzzy_misheard_command(self) -> None:
        c = ListeningController()
        # STT 听岔:小歌→小郭
        out = c.split_after_command("小郭干活了帮我记一下", "小歌干活了")
        assert out == "帮我记一下"

    def test_unlocatable_returns_none(self) -> None:
        c = ListeningController()
        assert c.split_after_command("完全无关的一句话而已", "小歌干活了") is None

    def test_empty_inputs(self) -> None:
        c = ListeningController()
        assert c.split_after_command("", "小歌干活了") is None
        assert c.split_after_command("随便说点什么", "") is None


class TestIsAffirmative:
    def test_affirmatives(self) -> None:
        c = ListeningController()
        for text in ("要", "好", "嗯", "整理一下吧", "帮我总结"):
            assert c.is_affirmative(text) is True, text

    def test_negatives_win(self) -> None:
        c = ListeningController()
        for text in ("不要", "不用了", "算了", "没必要整理"):
            assert c.is_affirmative(text) is False, text

    def test_empty(self) -> None:
        c = ListeningController()
        assert c.is_affirmative("") is False


class TestTempSubstance:
    def test_threshold(self) -> None:
        c = ListeningController(min_organize_chars=6)
        c.temp_transcript = ["三个字", "两字"]  # 3+2=5 < 6
        assert c.temp_has_substance() is False
        c.temp_transcript = ["三个字", "三个字"]  # 6 >= 6
        assert c.temp_has_substance() is True
