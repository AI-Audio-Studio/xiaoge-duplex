"""Offline stream-isolation regression tests using real SDK model types."""

import asyncio

import pytest
from app.output_sanitizer import _MAX_DEPTH, _TAG_STEMS, _TextFilter, sanitize_llm_stream

from livekit.agents import llm
from livekit.agents.types import FlushSentinel


async def source(items):
    for item in items:
        yield item


async def collect(items):
    return [item async for item in sanitize_llm_stream(source(items))]


def text(items):
    parts = []
    for item in items:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, llm.ChatChunk) and item.delta:
            parts.append(item.delta.content or "")
    return "".join(parts)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('你好<tool_call>{"name":"secret"}</tool_call>再见', "你好再见"),
        ("前<toolcall>secret</toolcall>后", "前后"),
        ("前<TOOL_CALL >secret</Tool_Call>后", "前后"),
        ("前<tool_call name='a>b'>secret</tool_call>后", "前后"),
        ("前<tool_call>secret", "前"),
        ("前<tool_call attr='unfinished", "前"),
        ("前</tool_call>中</toolcall>后", "前中后"),
        ("<tool_call>a<toolcall>b</toolcall>c</tool_call>后", "后"),
        (
            'JSON {"name":"play_music","arguments":{"id":1}}',
            'JSON {"name":"play_music","arguments":{"id":1}}',
        ),
        (
            "a < b <tool_callback>x</tool_callback> <toolcalls>y",
            "a < b <tool_callback>x</tool_callback> <toolcalls>y",
        ),
        ("<other>普通</other> <<toolcall>x</toolcall>", "<other>普通</other> <"),
        ("", ""),
    ],
)
def test_every_two_chunk_boundary_and_character_chunks(raw, expected):
    async def scenario():
        for split in range(len(raw) + 1):
            assert text(await collect([raw[:split], raw[split:]])) == expected
        assert text(await collect(list(raw))) == expected

    asyncio.run(scenario())


@pytest.mark.parametrize("stem", _TAG_STEMS)
def test_all_truncated_tag_prefixes_are_discarded(stem):
    async def scenario():
        for end in range(1, len(stem) + 1):
            assert text(await collect(["正文", stem[:end]])) == "正文"

    asyncio.run(scenario())


def test_chunk_copy_preserves_tools_metadata_and_source_models():
    async def scenario():
        tool = llm.FunctionToolCall(name="allowed", arguments='{"id":1}', call_id="call")
        delta = llm.ChoiceDelta(
            role="assistant",
            content="前<tool_call>secret",
            tool_calls=[tool],
            extra={"signature": "keep"},
        )
        usage = llm.CompletionUsage(completion_tokens=3, prompt_tokens=2, total_tokens=5)
        chunk = llm.ChatChunk(id="req", delta=delta, usage=usage)
        original = chunk.model_dump()
        result = await collect([chunk, "</tool_call>后"])
        copied = result[0]
        assert text(result) == "前后"
        assert copied is not chunk and copied.delta is not delta
        assert copied.delta.content == "前"
        assert copied.delta.tool_calls is delta.tool_calls
        assert copied.delta.extra is delta.extra
        assert copied.usage is usage and copied.id == chunk.id
        assert copied.delta.role == delta.role
        assert chunk.model_dump() == original

    asyncio.run(scenario())


def test_metadata_and_flush_do_not_reset_partial_tag_or_block():
    async def scenario():
        sentinel = FlushSentinel()
        metadata = llm.ChatChunk(id="usage")
        tool = llm.FunctionToolCall(name="allowed", arguments="{}", call_id="call")
        tools = llm.ChatChunk(id="tools", delta=llm.ChoiceDelta(tool_calls=[tool]))
        chunks = [
            "前<tool_",
            sentinel,
            metadata,
            "call>secret",
            tools,
            sentinel,
            llm.ChatChunk(id="end", delta=llm.ChoiceDelta(content="</toolcall>后")),
        ]
        result = await collect(chunks)
        assert text(result) == "前后"
        assert result[1] is sentinel and result[2] is metadata
        assert result[4] is tools and result[5] is sentinel

    asyncio.run(scenario())


def test_unchanged_chunks_keep_identity_and_mixed_text_shares_state():
    async def scenario():
        chunk = llm.ChatChunk(id="plain", delta=llm.ChoiceDelta(content="正常{}"))
        empty = llm.ChatChunk(id="empty", delta=llm.ChoiceDelta(content=""))
        result = await collect(
            [
                chunk,
                empty,
                "<tool",
                llm.ChatChunk(
                    id="split", delta=llm.ChoiceDelta(content="call>secret</tool_call>好")
                ),
            ]
        )
        assert result[0] is chunk and result[1] is empty
        assert text(result) == "正常{}好"
        assert text(await collect(["新一轮{}"])) == "新一轮{}"

    asyncio.run(scenario())


def test_concurrent_generations_have_independent_state():
    async def scenario():
        first, second = await asyncio.gather(
            collect(["前<toolcall>", "hidden"]), collect(["正常", "{}"])
        )
        assert text(first) == "前" and text(second) == "正常{}"

    asyncio.run(scenario())


def test_state_is_bounded_for_long_bodies_attributes_and_excessive_nesting():
    state = _TextFilter()
    assert state.feed("前<toolcall attr='") == "前"
    for _ in range(1000):
        assert state.feed("x" * 1000) == ""
        assert len(state.pending) <= max(map(len, _TAG_STEMS))
    assert state.feed("'>") == ""
    assert state.feed("<toolcall>" * (_MAX_DEPTH + 1)) == ""
    assert state.depth <= _MAX_DEPTH and state.quarantined
    assert state.feed("</toolcall>" * 100 + "secret") == ""


class TrackedStream:
    def __init__(self, items=(), error=None, close_error=None):
        self.items = iter(items)
        self.error = error
        self.close_error = close_error
        self.closed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.items)
        except StopIteration:
            if self.error:
                raise self.error from None
            raise StopAsyncIteration from None

    async def aclose(self):
        self.closed += 1
        if self.close_error:
            raise self.close_error


@pytest.mark.parametrize("error", [None, RuntimeError("upstream"), asyncio.CancelledError()])
def test_upstream_errors_and_cancellation_propagate_and_close(error):
    async def scenario():
        upstream = TrackedStream(["safe", "<toolcall>secret"], error=error)
        wrapped = sanitize_llm_stream(upstream)
        if error is None:
            assert text([item async for item in wrapped]) == "safe"
        else:
            with pytest.raises(type(error)) as caught:
                _ = [item async for item in wrapped]
            assert caught.value is error
        assert upstream.closed == 1

    asyncio.run(scenario())


def test_explicit_early_close_propagates_upstream():
    async def scenario():
        upstream = TrackedStream(["safe", "never requested"])
        wrapped = sanitize_llm_stream(upstream)
        assert await wrapped.__anext__() == "safe"
        await wrapped.aclose()
        assert upstream.closed == 1
        assert next(upstream.items) == "never requested"

    asyncio.run(scenario())


def test_close_error_propagates_without_masking_primary_error():
    async def scenario():
        closing = RuntimeError("close")
        upstream = TrackedStream(close_error=closing)
        with pytest.raises(RuntimeError) as caught:
            _ = [item async for item in sanitize_llm_stream(upstream)]
        assert caught.value is closing
        primary = ValueError("primary")
        upstream = TrackedStream(error=primary, close_error=closing)
        with pytest.raises(ValueError) as caught:
            _ = [item async for item in sanitize_llm_stream(upstream)]
        assert caught.value is primary and upstream.closed == 1

    asyncio.run(scenario())


def test_task_cancellation_closes_waiting_upstream():
    async def scenario():
        entered = asyncio.Event()
        closed = asyncio.Event()

        async def waiting():
            try:
                entered.set()
                await asyncio.Event().wait()
                yield "unreachable"
            finally:
                closed.set()

        async def consume():
            return [item async for item in sanitize_llm_stream(waiting())]

        task = asyncio.create_task(consume())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()

    asyncio.run(scenario())


def test_plain_iterable_without_close_is_supported():
    class Iterable:
        def __aiter__(self):
            return source(["前<toolcall>", "hidden</toolcall>后"])

    async def scenario():
        assert text([item async for item in sanitize_llm_stream(Iterable())]) == "前后"

    asyncio.run(scenario())


def test_generator_finally_runs_on_explicit_close():
    async def scenario():
        closed = []

        async def generated():
            try:
                yield "safe"
                yield "never"
            finally:
                closed.append(True)

        wrapped = sanitize_llm_stream(generated())
        assert await wrapped.__anext__() == "safe"
        await wrapped.aclose()
        assert closed == [True]

    asyncio.run(scenario())


def test_explicit_close_error_is_not_swallowed():
    async def scenario():
        error = RuntimeError("close failed")
        upstream = TrackedStream(["safe"], close_error=error)
        wrapped = sanitize_llm_stream(upstream)
        await wrapped.__anext__()
        with pytest.raises(RuntimeError) as caught:
            await wrapped.aclose()
        assert caught.value is error

    asyncio.run(scenario())


def test_iterable_and_distinct_iterator_are_both_closed():
    async def scenario():
        iterator = TrackedStream(["ok"])

        class Iterable:
            closed = False

            def __aiter__(self):
                return iterator

            async def aclose(self):
                self.closed = True

        upstream = Iterable()
        assert text([item async for item in sanitize_llm_stream(upstream)]) == "ok"
        assert iterator.closed == 1 and upstream.closed

    asyncio.run(scenario())
