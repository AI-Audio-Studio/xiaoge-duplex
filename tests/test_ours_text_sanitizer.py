"""行为锁定测试:text_sanitizer(strip_markdown / sanitize_stream)。

重构护栏(阶段0):断言当前净化行为——只删排版符号,不动正常内容。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

from text_sanitizer import sanitize_stream, strip_markdown  # noqa: E402


class TestStripMarkdown:
    def test_empty_and_plain(self) -> None:
        assert strip_markdown("") == ""
        assert strip_markdown("今天天气不错。") == "今天天气不错。"

    def test_bold_and_inline_marks(self) -> None:
        assert strip_markdown("**加粗**内容") == "加粗内容"
        assert strip_markdown("__下划线__和~~删除~~") == "下划线和删除"
        assert strip_markdown("`代码`片段") == "代码片段"

    def test_headings_and_lists(self) -> None:
        assert strip_markdown("### 标题\n内容") == "标题 内容"
        assert strip_markdown("- 项目一\n- 项目二") == "项目一 项目二"
        assert strip_markdown("1. 第一\n2) 第二") == "第一 第二"

    def test_quote_and_hr(self) -> None:
        assert strip_markdown("> 引用内容") == "引用内容"
        assert strip_markdown("上文\n---\n下文") == "上文 下文"

    def test_arrows_replaced(self) -> None:
        assert strip_markdown("甲→乙") == "甲 乙"

    def test_dash_runs_collapsed_single_hyphen_kept(self) -> None:
        assert strip_markdown("13-15") == "13-15"
        assert strip_markdown("a--b") == "a b"

    def test_chinese_enumeration_kept(self) -> None:
        assert strip_markdown("1、2、3") == "1、2、3"

    def test_newlines_become_spaces(self) -> None:
        assert strip_markdown("第一行\n第二行") == "第一行 第二行"


def _collect(chunks: list[str]) -> list[str]:
    async def _src() -> AsyncIterator[str]:
        for c in chunks:
            yield c

    async def _run() -> list[str]:
        return [piece async for piece in sanitize_stream(_src())]

    return asyncio.run(_run())


class TestSanitizeStream:
    def test_flush_on_sentence_boundary(self) -> None:
        # 跨块的 ** 在句界冲刷时被正确剥掉
        out = _collect(["**你", "好**。今天", "天气"])
        assert out == ["你好。", "今天天气"]

    def test_trailing_buffer_flushed(self) -> None:
        out = _collect(["没有句号的尾巴"])
        assert out == ["没有句号的尾巴"]

    def test_empty_stream(self) -> None:
        assert _collect([]) == []

    def test_multiple_boundaries_in_one_chunk(self) -> None:
        out = _collect(["第一句。第二句！第三"])
        # 冲刷到最后一个句界,余下留缓冲
        assert out == ["第一句。第二句！", "第三"]
