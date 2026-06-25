"""LLM 输出净化:去除 markdown / 排版符号,保证语音(TTS)与显示都是纯口语。

小模型(Qwen3-4B)常无视"不要用 markdown"的否定指令,输出 ** / ### / --- / - / → 等。
语音场景下这些会被读出来或读得怪。这里在管线里**确定性地**剥掉,与模型是否听话无关。

- strip_markdown(str)   : 对完整文本净化。
- sanitize_stream(aiter): 流式净化(按句缓冲后净化再吐,保留近实时,正确处理跨块的 ** 等)。

只删排版符号,**不动正常内容**:单个连字符(13-15)、中文顿号编号(1、2、3)均保留。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, AsyncIterator

# 行首结构(需在"换行→空格"之前处理)
_LINE_RULES = (
    (re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*"), ""),  # ### 标题
    (re.compile(r"(?m)^[ \t]*[-*+•][ \t]+"), ""),  # - / * 列表项
    (re.compile(r"(?m)^[ \t]*\d+[.)][ \t]+"), ""),  # 1. / 2) 有序列表(行首)
    (re.compile(r"(?m)^[ \t]*\|?[ \t:|\-]*-{2,}[ \t:|\-]*$"), ""),  # |---|---| 表格分隔行
    (re.compile(r"(?m)^[ \t]*[-=*_]{3,}[ \t]*$"), ""),  # --- 分隔线
    (re.compile(r"(?m)^[ \t]*>[ \t]?"), ""),  # > 引用
)
_DASHRUN = re.compile(r"-{2,}")  # 行内 -- / ---(单个连字符不动)
_INLINE = re.compile(r"[*_`~#>]")  # 残余行内标记
_NL = re.compile(r"[ \t]*\n[ \t]*")
_WS = re.compile(r"[ \t]{2,}")
_ARROWS = ("→", "►", "▸", "▶", "•", "·")
# 句界:吐给 TTS 的缓冲在这些处冲刷
_BOUNDARY = re.compile(r"[。!！?？;；\n]")


def strip_markdown(text: str) -> str:
    if not text:
        return text
    t = text
    for rx, rep in _LINE_RULES:
        t = rx.sub(rep, t)
    t = t.replace("**", "").replace("__", "").replace("~~", "")
    t = _DASHRUN.sub(" ", t)
    t = _INLINE.sub("", t)
    for ch in _ARROWS:
        t = t.replace(ch, " ")
    t = t.replace("|", " ")  # 表格单元格竖线 → 空格(分隔行已在 _LINE_RULES 删除)
    t = _NL.sub(" ", t)
    t = _WS.sub(" ", t)
    return t.strip()


async def sanitize_stream(src: AsyncIterable[str]) -> AsyncIterator[str]:
    """按句缓冲净化:跨块的 ** / 行首 - 等都能正确处理;近实时(逐句吐)。"""
    buf = ""
    async for chunk in src:
        buf += chunk
        last = None
        for m in _BOUNDARY.finditer(buf):
            last = m
        if last is not None:
            head, buf = buf[: last.end()], buf[last.end() :]
            cleaned = strip_markdown(head)
            if cleaned:
                yield cleaned
    if buf.strip():
        cleaned = strip_markdown(buf)
        if cleaned:
            yield cleaned
