"""Reusable agent tool helpers.

These helpers keep tool execution/result formatting out of the main VoiceAgent
orchestration while preserving the current LiveKit @function_tool surface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("web-ui-agent")


@dataclass(frozen=True)
class ToolResult:
    """Structured tool result for agent-internal helpers."""

    ok: bool
    code: str
    text: str
    speak_hint: str | None = None


async def query_knowledge_text(index: Any, query: str) -> ToolResult:
    """Query the product knowledge index and return grounded context.

    ``text`` is LLM-readable tool context. ``speak_hint`` is safe to say directly
    on deterministic reply-only paths that cannot run an LLM synthesis step.
    """

    if index is None:
        direct = "我现在还查不到产品知识库，所以这部分先不确定。"
        return ToolResult(
            ok=False,
            code="knowledge_disabled",
            text="知识库未启用。告诉用户你不清楚这个问题,不要编造。",
            speak_hint=direct,
        )
    try:
        hits = await index.query(query)
    except Exception as exc:
        logger.exception("query_knowledge_text failed")
        direct = "检索知识库时出错了，请稍后再试。"
        return ToolResult(
            ok=False,
            code="knowledge_error",
            text=f"检索出错了:{exc}",
            speak_hint=direct,
        )
    if not hits:
        direct = "我暂时没在产品知识库里查到相关内容，可以换个问法再试试。"
        return ToolResult(
            ok=False,
            code="knowledge_no_hits",
            text="没查到相关内容。告诉用户知识库里没有这部分,不要编造。",
            speak_hint=direct,
        )

    lines: list[str] = [f"知识库命中 {len(hits)} 条(按相关度降序):"]
    for i, hit in enumerate(hits, 1):
        lines.append(
            f"\n[{i}] (score={hit.score:.2f} source={hit.source} title={hit.title})\n{hit.text}"
        )
    lines.append(
        "\n请基于以上内容用一句到三句口语化回答用户。不要复述 score/source/标题等元信息,"
        "不要说'根据知识库'之类的话。如果内容不足以回答,如实说不确定。"
    )
    direct = _direct_answer_from_hits(hits)
    return ToolResult(ok=True, code="knowledge_hits", text="\n".join(lines), speak_hint=direct)


def _direct_answer_from_hits(hits: list[Any]) -> str:
    """Build a short direct answer when no LLM synthesis step is available."""

    first = hits[0]
    text = str(getattr(first, "text", "")).strip()
    title = str(getattr(first, "title", "")).strip()
    if not text:
        return "我查到了相关条目，但内容不够完整，暂时不能确定。"
    sentence = text.replace("\r", "").replace("\n", " ").strip()
    for sep in ("。", "！", "？", ".", "!", "?"):
        if sep in sentence:
            sentence = sentence.split(sep, 1)[0].strip() + sep
            break
    if len(sentence) > 120:
        sentence = sentence[:117].rstrip() + "..."
    if title and title not in sentence:
        return f"{title}：{sentence}"
    return sentence
