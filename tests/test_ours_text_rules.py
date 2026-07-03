"""行为锁定测试:web_ui_agent 的停止词/附和/ack/数字归一化纯逻辑。

重构护栏(阶段0):断言**当前**行为,公共层抽取(common/text_rules.py)前后必须同绿。
只测纯函数,不起会话、不连网络。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

import web_ui_agent as wua  # noqa: E402
from common.runtime import ms as _ms  # noqa: E402


class TestIsOverlapAck:
    def test_none_and_empty(self) -> None:
        assert wua._is_overlap_ack(None) is False
        assert wua._is_overlap_ack("") is False

    def test_pure_punct_is_not_ack(self) -> None:
        assert wua._is_overlap_ack("，。！") is False

    def test_typical_acks(self) -> None:
        for text in ("嗯", "嗯嗯", "对对对", "好的", "好的呀", "嗯，好的。", "是的", "行"):
            assert wua._is_overlap_ack(text) is True, text

    def test_content_is_not_ack(self) -> None:
        for text in ("对啊我觉得", "好的明天见", "你好"):
            assert wua._is_overlap_ack(text) is False, text


class TestIsBackchannel:
    def test_none_and_empty(self) -> None:
        assert wua._is_backchannel(None) is False
        assert wua._is_backchannel("") is False
        assert wua._is_backchannel("   ") is False

    def test_typical_backchannels(self) -> None:
        for text in ("嗯", "嗯嗯。", "哦？", "啊"):
            assert wua._is_backchannel(text) is True, text

    def test_ack_words_are_not_backchannel(self) -> None:
        # “好/对/是”属于 overlap-ack 字符集,但不属于 backchannel 字符集
        for text in ("好", "对", "是的", "你好", "嗯好"):
            assert wua._is_backchannel(text) is False, text


class TestShouldIgnoreUserTurn:
    def test_none_and_empty(self) -> None:
        assert wua._should_ignore_user_turn(None) is False
        assert wua._should_ignore_user_turn("") is False
        assert wua._should_ignore_user_turn("，。") is False

    def test_bare_stop_words(self) -> None:
        for text in ("停", "停下", "别说了", "闭嘴", "等一下", "休庭"):
            assert wua._should_ignore_user_turn(text) is True, text

    def test_stop_word_with_lead_in_and_tail(self) -> None:
        for text in ("那你停一下吧。", "就停吧", "请稍等", "停下了"):
            assert wua._should_ignore_user_turn(text) is True, text

    def test_stop_word_mixed_with_ack_segments(self) -> None:
        assert wua._should_ignore_user_turn("嗯，停") is True
        assert wua._should_ignore_user_turn("好的，别说了") is True

    def test_pure_ack_without_stop_word(self) -> None:
        assert wua._should_ignore_user_turn("嗯嗯") is False
        assert wua._should_ignore_user_turn("好的") is False

    def test_stop_word_followed_by_content(self) -> None:
        assert wua._should_ignore_user_turn("停一下，今天天气怎么样") is False
        assert wua._should_ignore_user_turn("等等我想问个问题") is False


class TestNormalizeSpokenDigitSequence:
    def test_none_passthrough(self) -> None:
        assert wua._normalize_spoken_digit_sequence(None) is None

    def test_pure_digits_expanded(self) -> None:
        assert wua._normalize_spoken_digit_sequence("12") == "1、2"
        assert wua._normalize_spoken_digit_sequence("12345") == "1、2、3、4、5"

    def test_stripped_before_match(self) -> None:
        assert wua._normalize_spoken_digit_sequence(" 123 ") == "1、2、3"

    def test_single_digit_unchanged(self) -> None:
        assert wua._normalize_spoken_digit_sequence("7") == "7"

    def test_too_long_unchanged(self) -> None:
        digits = "1" * 17
        assert wua._normalize_spoken_digit_sequence(digits) == digits

    def test_mixed_text_unchanged(self) -> None:
        assert wua._normalize_spoken_digit_sequence("电话是12345") == "电话是12345"


class TestMsFormat:
    def test_none(self) -> None:
        assert _ms(None) == "-"

    def test_value(self) -> None:
        assert _ms(0.1234) == "123.4ms"
        assert _ms(0) == "0.0ms"
