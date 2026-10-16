"""Isolate textual tool-call markup without touching structured SDK tool calls."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterable, AsyncIterator

from livekit.agents import llm
from livekit.agents.types import FlushSentinel

_StreamItem = str | llm.ChatChunk | FlushSentinel
_TAG_STEMS = ("<tool_call", "<toolcall", "</tool_call", "</toolcall")
_MAX_DEPTH = 32


class _TextFilter:
    """Bounded incremental scanner; never retain tool bodies or tag attributes.

    Tag names are case-insensitive, with optional whitespace/attributes after
    the name. Nested blocks are suppressed. Excessive nesting fails closed for
    the rest of this generation. A truncated tag prefix/body is discarded at
    EOF; FlushSentinel is not an EOF and must not reset the scanner.
    """

    def __init__(self) -> None:
        self.pending = ""
        self.depth = 0
        self.tag: str | None = None
        self.quote = ""
        self.quarantined = False

    def feed(self, text: str) -> str:
        output: list[str] = []
        for char in text:
            if self.quarantined:
                break
            if self.tag is not None:
                self._tag_character(char)
            else:
                self._text_character(char, output)
        return "".join(output)

    def _text_character(self, char: str, output: list[str]) -> None:
        self.pending += char
        candidate = self.pending.lower()
        stem = candidate[:-1]
        if stem in _TAG_STEMS and (char == ">" or char.isspace()):
            self.tag = stem
            self.pending = ""
            if char == ">":
                self._finish_tag()
            return
        if any(tag.startswith(candidate) for tag in _TAG_STEMS):
            return
        # Only '<' can start another candidate: keep it for the next character.
        keep = int(char == "<")
        visible = self.pending[:-1] if keep else self.pending
        if not self.depth:
            output.append(visible)
        self.pending = "<" if keep else ""

    def _tag_character(self, char: str) -> None:
        if self.quote:
            if char == self.quote:
                self.quote = ""
        elif char in ("'", '"'):
            self.quote = char
        elif char == ">":
            self._finish_tag()

    def _finish_tag(self) -> None:
        assert self.tag is not None
        if self.tag.startswith("</"):
            self.depth = max(0, self.depth - 1)
        elif self.depth == _MAX_DEPTH:
            self.quarantined = True
        else:
            self.depth += 1
        self.tag = None


def _sanitize_item(item: _StreamItem, state: _TextFilter) -> _StreamItem:
    if isinstance(item, str):
        return state.feed(item)
    if isinstance(item, llm.ChatChunk) and item.delta is not None:
        content = item.delta.content
        if content is not None:
            clean = state.feed(content)
            if clean != content:
                delta = item.delta.model_copy(update={"content": clean})
                return item.model_copy(update={"delta": delta})
    return item


async def _close_stream(stream: object, *, preserve_error: bool) -> None:
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except BaseException:
        if not preserve_error:
            raise


async def sanitize_llm_stream(stream: AsyncIterable[_StreamItem]) -> AsyncIterator[_StreamItem]:
    """Wrap one generation, changing only text and closing upstream on exit.

    Every ChatChunk (including empty deltas, usage and structured tool calls)
    and FlushSentinel is retained. Modified chunks/deltas are shallow copies;
    input models are never mutated. Strings can become empty. Pending markup
    is never flushed on errors, cancellation or EOF. The caller must close this
    iterator when abandoning it early, as with any SDK async generator node.
    """
    state = _TextFilter()
    iterator = stream.__aiter__()
    try:
        async for item in iterator:
            yield _sanitize_item(item, state)
    finally:
        preserve_error = sys.exc_info()[0] not in (None, GeneratorExit)
        try:
            await _close_stream(iterator, preserve_error=preserve_error)
        finally:
            if iterator is not stream:
                await _close_stream(
                    stream, preserve_error=sys.exc_info()[0] not in (None, GeneratorExit)
                )
