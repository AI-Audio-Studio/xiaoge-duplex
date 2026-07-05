"""行为锁定测试:providers.helpers 的分句聚合(拆分前内嵌在 3 个 TTS 流里)。

边界集合必须与拆分前逐字符一致:
  Qwen/CosyVoice: "。！？!?；;\\n"(SENTENCE_BOUNDARY)
  HTTP TTS      : "。！？!?；;"(不含换行,providers/tts/http.py 的 _HTTP_BOUNDARY)
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

from providers.helpers import SENTENCE_BOUNDARY, iter_sentence_chunks  # noqa: E402


def _collect(chunks: list, boundary: str = SENTENCE_BOUNDARY) -> list[str]:
    async def _src() -> AsyncIterator:
        for c in chunks:
            yield c

    async def _run() -> list[str]:
        return [s async for s in iter_sentence_chunks(_src(), boundary=boundary)]

    return asyncio.run(_run())


class TestBoundarySet:
    def test_boundary_constant(self) -> None:
        assert SENTENCE_BOUNDARY == "。！？!?；;\n"


class TestIterSentenceChunks:
    def test_flush_at_each_boundary(self) -> None:
        assert _collect(["你好。", "今天", "天气！", "如何"]) == ["你好。", "今天天气！", "如何"]

    def test_chunks_accumulate_until_boundary(self) -> None:
        assert _collect(["第一", "句还没", "完。"]) == ["第一句还没完。"]

    def test_trailing_remainder_kept_raw(self) -> None:
        # 残余原样产出(不 strip),与拆分前 append_text(pending) 一致
        assert _collect(["ab。", " tail "]) == ["ab。", " tail "]

    def test_whitespace_only_remainder_dropped(self) -> None:
        assert _collect(["ab。", "   "]) == ["ab。"]

    def test_non_str_items_ignored(self) -> None:
        assert _collect(["a", object(), "b。"]) == ["ab。"]

    def test_newline_is_boundary_by_default(self) -> None:
        assert _collect(["第一行\n", "第二行。"]) == ["第一行\n", "第二行。"]

    def test_http_boundary_excludes_newline(self) -> None:
        out = _collect(["第一行\n第二行。"], boundary="。！？!?；;")
        assert out == ["第一行\n第二行。"]

    def test_empty_stream(self) -> None:
        assert _collect([]) == []
