"""Real AgentSession/child-say integration, offline and with no device entrypoint import.

Run with Python -I; pass trusted application and SDK test fixture paths explicitly.
Only new test files are used; no old replay or implementation is modified.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import importlib.util
import json
import socket
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
NETWORK_ATTEMPTS = []
_LOOP_SOCKETPAIR = False


def network_guard(event, args):
    """Deny Internet sockets/DNS before any SDK/loader import; allow AF_UNIX."""
    if (
        event == "socket.__new__"
        and args[1] != getattr(socket, "AF_UNIX", -1)
        and not _LOOP_SOCKETPAIR
    ):
        NETWORK_ATTEMPTS.append(event)
        raise PermissionError("Offline SDK tests prohibit Internet sockets")
    if event in {"socket.connect", "socket.bind"} and args[0].family in {
        socket.AF_INET,
        socket.AF_INET6,
    }:
        address = args[1]
        if not (_LOOP_SOCKETPAIR and address[0] in {"127.0.0.1", "::1"}):
            NETWORK_ATTEMPTS.append(event)
            raise PermissionError("Offline SDK tests prohibit Internet connections")
    if event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
        NETWORK_ATTEMPTS.append(event)
        raise PermissionError("Offline SDK tests prohibit DNS")


sys.addaudithook(network_guard)

# This Windows Python lacks AF_UNIX: allow only synchronous stdlib socketpair
# creation/bind/connect to numeric loopback for asyncio's self-pipe. All other
# Internet socket creation and connections remain denied; Linux needs no exception.
if sys.platform == "win32" and not hasattr(socket, "AF_UNIX"):
    _socketpair = socket.socketpair

    def loop_socketpair(*args, **kwargs):
        global _LOOP_SOCKETPAIR
        _LOOP_SOCKETPAIR = True
        try:
            return _socketpair(*args, **kwargs)
        finally:
            _LOOP_SOCKETPAIR = False

    socket.socketpair = loop_socketpair

from livekit.agents import AgentSession, StopResponse, llm  # noqa: E402 - guard before SDK

OPTIONS = None
LOADER = None
FAKES = None
GUIDE = "我帮你查一下天气"


def messages(boundary):
    return [
        row["data"]["text"]
        for row in boundary.trace
        if row["event"] == "broadcast" and row["data"].get("role") == "assistant"
    ]


def http_rows(boundary):
    return [row for row in boundary.trace if row["event"].endswith("_http")]


async def wait_until(predicate, timeout=2.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


class Harness:
    def __init__(
        self,
        *,
        dispatch_ok=True,
        audio_duration=0.08,
        tts_delay=None,
        tts_error=None,
        hold_audio=False,
        helper_timeout=None,
    ):
        self.boundary = LOADER.Boundary()
        self.boundary.dispatch_ok = dispatch_ok
        self.agent, self.ns, self.gate_module = LOADER.build_agent(
            OPTIONS.source, self.boundary, OPTIONS.sanitizer
        )
        self.children = []
        self.parents = []
        self.tts_inputs = []
        boundary = self.boundary
        inputs = self.tts_inputs
        self_agent_speech = self.gate_module.bound_speech

        class RecordingTTS(FAKES.tts.FakeTTS):
            def stream(self, **kwargs):
                stream = super().stream(**kwargs)
                original = stream.push_text

                def push_text(text):
                    inputs.append(text)
                    boundary.record("tts_text", text=text)
                    original(text)

                stream.push_text = push_text
                return stream

        class RecordingAudio(FAKES.io.FakeAudioOutput):
            def __init__(self):
                super().__init__()
                self.frames = []
                self.hold = hold_audio
                self.playback_context = None

            async def capture_frame(self, frame):
                self.frames.append(frame)
                boundary.record("audio_frame", duration=frame.duration)
                await super().capture_frame(frame)

            def flush(self):
                boundary.record("audio_flush", speech_id=getattr(self_agent_speech(), "id", None))
                if not self.hold:
                    super().flush()
                elif self.frames:
                    self.playback_context = contextvars.copy_context()

            def release(self):
                self.hold = False
                if self.playback_context is not None:
                    self.playback_context.run(super().flush)
                else:
                    super().flush()

        self.tts = RecordingTTS(
            fake_audio_duration=audio_duration, fake_timeout=tts_delay, fake_exception=tts_error
        )
        self.model = FAKES.llm.FakeLLM()
        self.audio = RecordingAudio()
        self.text = FAKES.io.FakeTextOutput()
        self.session = AgentSession(llm=self.model, tts=self.tts, use_tts_aligned_transcript=False)
        self.session.output.audio = self.audio
        self.session.output.transcription = self.text
        self.audio.on(
            "playback_finished",
            lambda event: boundary.record(
                "playout_finished", position=event.playback_position, interrupted=event.interrupted
            ),
        )
        self.committed = []
        self.session.on("conversation_item_added", lambda event: self.committed.append(event.item))
        original_say = self.session.say

        def say(text, **kwargs):
            child = original_say(text, **kwargs)
            self.children.append(child)
            boundary.record("say_child", speech_id=child.id, text=text)
            return child

        self.session.say = say
        if helper_timeout is not None:
            original_helper = self.ns["say_linked"]

            async def fast_helper(*args, **kwargs):
                kwargs["timeout_s"] = helper_timeout
                return await original_helper(*args, **kwargs)

            self.ns["say_linked"] = fast_helper

    async def start(self):
        await self.session.start(self.agent, record=False)
        return self

    def generate(self, tool, arguments=None, *, content="", user="帮我执行"):
        response = FAKES.llm.FakeLLMResponse(
            input=user,
            content=content,
            ttft=0,
            duration=0,
            tool_calls=[
                llm.FunctionToolCall(
                    call_id="offline-call",
                    name=tool,
                    arguments=json.dumps(arguments or {}, ensure_ascii=False),
                )
            ]
            if tool
            else [],
        )
        self.model.fake_response_map[user] = response
        parent = self.session.generate_reply(user_input=user)
        self.parents.append(parent)
        return parent

    async def finish(self, parent):
        # Parent completion alone is not evidence of child playout.
        await asyncio.wait_for(parent, 3.0)
        for child in self.children:
            await asyncio.wait_for(child, 3.0)
        await asyncio.sleep(0.02)

    async def close(self):
        self.audio.release()
        self.session.interrupt(force=True)
        await asyncio.wait_for(self.session.aclose(), 3.0)

    def assert_linked_output(self, test, parent, *, history):
        test.assertEqual(len(self.children), 1, self.boundary.trace)
        child = self.children[0]
        test.assertIsNot(child, parent)
        test.assertNotEqual(child.id, parent.id)
        test.assertFalse(child.interrupted)
        test.assertTrue(self.audio.frames)
        texts = messages(self.boundary)
        test.assertEqual(len(texts), 1, self.boundary.trace)
        test.assertEqual("".join(self.tts_inputs), texts[0])
        test.assertEqual("".join(self.text._messages), texts[0])
        ended = [r for r in self.boundary.trace if r["event"] == "playout_finished"]
        test.assertTrue(any(r["position"] > 0 and not r["interrupted"] for r in ended))
        child_messages = [
            i.text_content
            for i in child.chat_items
            if isinstance(i, llm.ChatMessage) and i.role == "assistant"
        ]
        test.assertEqual(child_messages, texts if history else [])
        return texts[0]


class OutputSDKTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, **kwargs):
        harness = await Harness(**kwargs).start()
        self.addAsyncCleanup(harness.close)
        return harness

    async def test_photo_child_audio_text_once_and_committed_history(self):
        h = await self.make()
        parent = h.generate("take_photo")
        await h.finish(parent)
        text = h.assert_linked_output(self, parent, history=True)
        self.assertIn("拍张照", text)
        effects = [
            r
            for r in h.boundary.trace
            if r["event"] == "broadcast" and r["data"].get("type") == "g3_protocol"
        ]
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]["data"]["frames"][0]["action"], "take_photo")

    async def test_volume_child_audio_text_once_and_exact_frame(self):
        h = await self.make()
        parent = h.generate("adjust_volume", {"command": "set", "value": 60})
        await h.finish(parent)
        text = h.assert_linked_output(self, parent, history=True)
        self.assertIn("音量调到60", text)
        effects = [
            r["data"]
            for r in h.boundary.trace
            if r["event"] == "broadcast" and r["data"].get("type") == "g3_protocol"
        ]
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]["frames"][0]["params"], {"command": "set", "value": 60})

    async def test_failed_dispatch_confirmation_once(self):
        h = await self.make(dispatch_ok=False)
        parent = h.generate("take_photo")
        await h.finish(parent)
        text = h.assert_linked_output(self, parent, history=True)
        self.assertEqual(text, h.ns["COMMAND_FAILURE"])
        self.assertNotIn("拍张照", text)

    async def test_weather_guide_actual_playout_before_resolve_and_forecast(self):
        h = await self.make(hold_audio=True)
        parent = h.generate("query_weather", {"city": "北京"})
        await wait_until(lambda: bool(h.audio.frames))
        await asyncio.sleep(0.05)
        self.assertEqual(http_rows(h.boundary), [])
        self.assertFalse(parent.done(), "Parent prematurely completed while tool waits for child")
        await wait_until(lambda: h.audio.playback_context is not None)
        h.audio.release()
        await h.finish(parent)
        self.assertEqual(h.assert_linked_output(self, parent, history=False), GUIDE)
        rows = http_rows(h.boundary)
        self.assertEqual(
            [r["event"] for r in rows], ["resolve_http", "forecast_http"], h.boundary.trace
        )
        finished = next(
            r
            for r in h.boundary.trace
            if r["event"] == "playout_finished" and r["position"] > 0 and not r["interrupted"]
        )
        self.assertLess(finished["at"], rows[0]["at"])
        self.assertLess(rows[0]["at"], rows[1]["at"])

    async def test_unrelated_global_playback_event_cannot_certify_failed_child(self):
        from livekit.agents.voice.io import PlaybackFinishedEvent

        h = await self.make(hold_audio=True)
        parent = h.generate("query_weather", {"city": "北京"})
        await wait_until(
            lambda: (
                bool(h.children)
                and bool(h.audio.frames)
                and any(
                    r["event"] == "audio_flush" and r.get("speech_id") == h.children[0].id
                    for r in h.boundary.trace
                )
            )
        )
        # Complete this child's sink segment with zero played audio. A stale unrelated
        # global positive event must not change its proof into successful playout.
        h.audio.on_playback_finished(playback_position=0.0, interrupted=False)
        h.audio.emit(
            "playback_finished",
            PlaybackFinishedEvent(
                playback_position=9.0, interrupted=False, synchronized_transcript="other output"
            ),
        )
        await h.finish(parent)
        self.assertEqual(http_rows(h.boundary), [], h.boundary.trace)

    async def test_weather_final_answer_uses_parent_not_second_guide(self):
        h = await self.make()
        result = h.ns["_format_weather"](
            {"current": {"temperature_2m": 20, "weather_code": 0}}, "北京"
        )
        h.model.fake_response_map[result] = FAKES.llm.FakeLLMResponse(
            input=result, content="北京现在晴天，20度。", ttft=0, duration=0
        )
        parent = h.generate("query_weather", {"city": "北京"})
        await h.finish(parent)
        self.assertEqual(messages(h.boundary), [GUIDE, "北京现在晴天，20度。"])
        self.assertEqual(len(h.children), 1)
        self.assertEqual(h.children[0].chat_items, [])
        parent_texts = [
            item.text_content
            for item in parent.chat_items
            if isinstance(item, llm.ChatMessage) and item.role == "assistant"
        ]
        self.assertEqual(parent_texts, ["北京现在晴天，20度。"])
        self.assertNotIn(GUIDE, parent_texts)

    async def test_normal_generation_still_requires_valid_parent(self):
        h = await self.make()
        parent = h.generate(None, content="正常回答", user="你好")
        await h.finish(parent)
        self.assertEqual(messages(h.boundary), ["正常回答"])
        self.assertTrue(h.audio.frames)
        self.assertEqual(h.children, [])

    def assert_sanitized_parent(self, h, parent, expected, *, linked=False):
        def assistant_text(items):
            return "".join(
                item.text_content or ""
                for item in items
                if isinstance(item, llm.ChatMessage) and item.role == "assistant"
            )

        # Check the actual sinks and both committed-history views, not just the
        # sanitizer's return value. Parent chat_items also catches SDK buffering.
        outputs = {
            "broadcast": "".join(messages(h.boundary)),
            "text": "".join(h.text._messages),
            "tts": "".join(h.tts_inputs),
            "parent_history": assistant_text(parent.chat_items),
            "committed_events": assistant_text(h.committed),
            "session_history": assistant_text(h.session.history.items),
        }
        for sink, text in outputs.items():
            with self.subTest(sink=sink):
                if linked:
                    self.assertIn(expected, text)
                else:
                    self.assertEqual(text, expected)
                self.assertNotIn("tool_call", text.lower())
                self.assertNotIn("toolcall", text.lower())
                self.assertNotIn("HIDDEN_PAYLOAD", text)
        self.assertTrue(h.audio.frames)
        return outputs

    async def test_body_tool_markup_split_chunks_keeps_surrounding_text(self):
        # Existing FakeLLM emits three-character chunks, splitting tag names,
        # attributes and closing delimiters across the real SDK stream.
        for gate_enabled in (True, False):
            for markup in (
                '<tool_call>{"name":"HIDDEN_PAYLOAD"}</tool_call>',
                '<toolcall name="HIDDEN_PAYLOAD">do not speak</toolcall>',
                '<TOOL_CALL id="HIDDEN_PAYLOAD">do not speak</TOOL_CALL>',
            ):
                with self.subTest(gate_enabled=gate_enabled, markup=markup):
                    h = await self.make()
                    h.agent._gate_enabled = lambda enabled=gate_enabled: enabled
                    parent = h.generate(None, content=f"前面的普通文字。{markup}后面的普通文字。")
                    await h.finish(parent)
                    self.assert_sanitized_parent(h, parent, "前面的普通文字。后面的普通文字。")
                    self.assertEqual(h.children, [])
                    self.assertEqual(http_rows(h.boundary), [])

    async def test_body_unclosed_tool_markup_does_not_commit_hidden_tail(self):
        for gate_enabled in (True, False):
            with self.subTest(gate_enabled=gate_enabled):
                h = await self.make()
                h.agent._gate_enabled = lambda enabled=gate_enabled: enabled
                parent = h.generate(
                    None, content='保留这句普通文字。<tool_call>{"name":"HIDDEN_PAYLOAD"}'
                )
                await h.finish(parent)
                self.assert_sanitized_parent(h, parent, "保留这句普通文字。")
                self.assertEqual(h.children, [])
                self.assertEqual(http_rows(h.boundary), [])

    async def test_body_markup_and_structured_call_same_chunk_preserves_call(self):
        h = await self.make()
        original_chat = h.model.chat
        combined = []

        def chat(**kwargs):
            stream = original_chat(**kwargs)
            original_send = stream._send_chunk
            pending = []

            def send_chunk(*, delta=None, tool_calls=None):
                if delta is not None:
                    pending.append(delta)
                if tool_calls is not None:
                    # Reuse FakeLLM's real ChatChunk/ChoiceDelta construction;
                    # coalesce its text and final tool list into the same chunk.
                    text = "".join(pending)
                    combined.append((text, list(tool_calls)))
                    original_send(delta=text, tool_calls=tool_calls)
                    pending.clear()

            stream._send_chunk = send_chunk
            return stream

        h.model.chat = chat
        content = "前面普通文字。<tool_call>HIDDEN_PAYLOAD</tool_call>后面普通文字。"
        parent = h.generate("take_photo", content=content)
        await h.finish(parent)
        self.assertEqual(combined[0][0], content)
        self.assertEqual(len(combined[0][1]), 1)
        self.assertEqual(combined[0][1][0].name, "take_photo")
        outputs = self.assert_sanitized_parent(
            h, parent, "前面普通文字。后面普通文字。", linked=True
        )
        self.assertEqual(len(h.children), 1)
        effects = [
            row["data"]
            for row in h.boundary.trace
            if row["event"] == "broadcast" and row["data"].get("type") == "g3_protocol"
        ]
        self.assertEqual(len(effects), 1, h.boundary.trace)
        self.assertEqual(effects[0]["frames"][0]["action"], "take_photo")
        for sink in ("broadcast", "text", "tts", "committed_events", "session_history"):
            with self.subTest(sink=sink):
                self.assertIn("拍张照", outputs[sink])
        self.assertEqual(http_rows(h.boundary), [])

    async def test_unregistered_say_rejected_by_text_and_tts(self):
        h = await self.make()
        child = h.session.say("禁止未登记内部播报", add_to_chat_ctx=False)
        await asyncio.wait_for(child, 2.0)
        await asyncio.sleep(0.05)
        self.assertEqual(messages(h.boundary), [])
        self.assertEqual(h.audio.frames, [])
        self.assertEqual(h.tts_inputs, [])
        with self.assertRaises(StopResponse):
            h.agent._gate().require_output(child, "禁止", 0)

    async def test_weather_guide_without_audio_cannot_query(self):
        h = await self.make(audio_duration=0, helper_timeout=0.15)
        await h.finish(h.generate("query_weather", {"city": "北京"}))
        self.assertEqual(http_rows(h.boundary), [])
        self.assertEqual(h.audio.frames, [])

    async def test_weather_tts_error_cannot_query(self):
        h = await self.make(
            tts_error=RuntimeError("offline synthetic TTS failure"), helper_timeout=0.2
        )
        await h.finish(h.generate("query_weather", {"city": "北京"}))
        self.assertEqual(http_rows(h.boundary), [])

    async def test_weather_timeout_no_late_text_audio_or_http(self):
        h = await self.make(hold_audio=True, helper_timeout=0.4)
        parent = h.generate("query_weather", {"city": "北京"})
        await wait_until(lambda: bool(h.children))
        await h.finish(parent)
        snapshot = (len(messages(h.boundary)), len(h.audio.frames), len(http_rows(h.boundary)))
        await asyncio.sleep(0.5)
        self.assertEqual(
            (len(messages(h.boundary)), len(h.audio.frames), len(http_rows(h.boundary))), snapshot
        )
        self.assertEqual(http_rows(h.boundary), [])
        self.assertTrue(h.audio.frames)
        self.assertTrue(h.children[0].interrupted)

    async def test_weather_parent_cancel_before_audio_zero_late_effects(self):
        h = await self.make(tts_delay=0.3)
        parent = h.generate("query_weather", {"city": "北京"})
        await wait_until(lambda: bool(h.children))
        parent.interrupt(force=True)
        await h.finish(parent)
        snapshot = (len(messages(h.boundary)), len(h.audio.frames))
        await asyncio.sleep(0.4)
        self.assertEqual((len(messages(h.boundary)), len(h.audio.frames)), snapshot)
        self.assertEqual(http_rows(h.boundary), [])
        self.assertEqual(h.audio.frames, [])
        self.assertTrue(h.children[0].done())
        self.assertEqual(h.agent._gate().outputs.links, {})

    async def test_weather_parent_cancel_during_playout_interrupts_child(self):
        h = await self.make(hold_audio=True)
        parent = h.generate("query_weather", {"city": "北京"})
        await wait_until(
            lambda: (
                bool(h.children)
                and bool(h.audio.frames)
                and any(
                    r["event"] == "audio_flush" and r.get("speech_id") == h.children[0].id
                    for r in h.boundary.trace
                )
            )
        )
        self.assertFalse(h.children[0].done(), h.boundary.trace)
        parent.interrupt(force=True)
        await h.finish(parent)
        snapshot = (len(messages(h.boundary)), len(h.audio.frames))
        await asyncio.sleep(0.15)
        self.assertEqual((len(messages(h.boundary)), len(h.audio.frames)), snapshot)
        self.assertEqual(http_rows(h.boundary), [])
        self.assertTrue(h.children[0].interrupted)

    async def test_weather_reset_interrupts_child_zero_late_effects(self):
        h = await self.make(tts_delay=0.3)
        parent = h.generate("query_weather", {"city": "北京"})
        await wait_until(lambda: bool(h.children))
        gate = h.agent._gate()
        epoch = gate.epoch
        gate.reset()
        self.assertEqual(gate.epoch, epoch + 1)
        await h.finish(parent)
        snapshot = (len(messages(h.boundary)), len(h.audio.frames))
        await asyncio.sleep(0.4)
        self.assertEqual((len(messages(h.boundary)), len(h.audio.frames)), snapshot)
        self.assertEqual(http_rows(h.boundary), [])
        self.assertEqual(h.audio.frames, [])
        self.assertTrue(h.children[0].interrupted)

    async def test_reset_during_resolve_does_not_start_forecast(self):
        h = await self.make()
        h.boundary.http_block = asyncio.Event()
        parent = h.generate("query_weather", {"city": "北京"})
        await asyncio.wait_for(h.boundary.http_entered.wait(), 2.0)
        h.agent._gate().reset()
        h.boundary.http_block.set()
        await h.finish(parent)
        self.assertEqual([r["event"] for r in http_rows(h.boundary)], ["resolve_http"])

    async def test_interrupted_movie_topic_reaches_judge_and_generation(self):
        h = await self.make(tts_delay=0.3)
        gate = h.agent._gate()
        old = "推荐一部科幻电影"
        current = "那部电影有什么亮点？"
        seen = []

        async def judge(prompt, payload, keys):
            seen.append((keys, payload))
            if keys == {"addressed"}:
                if payload["current"] == current:
                    self.assertEqual(payload["user_topics"], [{"role": "user", "text": old}])
                    self.assertEqual(payload["confirmed"], [])
                else:
                    self.assertEqual(payload["user_topics"], [])
                return {"addressed": True}
            self.assertNotIn("user_topics", payload)
            if keys == {"summarize_background"}:
                return {"summarize_background": False}
            return {"music_action": "none", "music_target": "", "direction": "none"}

        gate._judge = judge
        contexts = []
        original_chat = h.model.chat

        def chat(**kwargs):
            contexts.append(kwargs["chat_ctx"].copy())
            return original_chat(**kwargs)

        h.model.chat = chat
        first = h.generate(None, user=old, content="尚未播放的电影推荐")
        await wait_until(lambda: bool(gate.grants) and bool(contexts))
        first.interrupt(force=True)
        await h.finish(first)
        self.assertFalse(any(turn.interacted for turn in gate.turns.values()))
        second = h.generate(None, user=current, content="这部科幻电影的亮点是太空探索。")
        await h.finish(second)
        self.assertEqual(sum(keys == {"addressed"} for keys, _ in seen), 2)
        ctx = contexts[-1]
        self.assertEqual([i.text_content for i in ctx.items if i.role == "user"], [current])
        self.assertFalse(any(i.role == "assistant" for i in ctx.items))
        blocks = [i.text_content for i in ctx.items if "历史用户话题数据" in i.text_content]
        self.assertEqual(len(blocks), 1)
        self.assertIn(old, blocks[0])
        self.assertIn("不得重执行旧动作", blocks[0])
        self.assertEqual(http_rows(h.boundary), [])

    async def test_ask_city_without_weather_tool_no_guide_or_query(self):
        h = await self.make()
        parent = h.generate(None, content="请问你想查哪个城市？", user="先问我城市，暂时别查天气")
        await h.finish(parent)
        self.assertEqual(http_rows(h.boundary), [])
        self.assertEqual(h.children, [])
        self.assertNotIn(GUIDE, messages(h.boundary))


def main():
    global OPTIONS, LOADER, FAKES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--sdk-tests", required=True, type=Path)
    parser.add_argument("--loader", required=True, type=Path)
    parser.add_argument("--sanitizer", required=True, type=Path)
    OPTIONS, remaining = parser.parse_known_args()
    spec = importlib.util.spec_from_file_location("candidate04_sdk_loader", OPTIONS.loader)
    LOADER = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = LOADER
    spec.loader.exec_module(LOADER)
    FAKES = LOADER.load_sdk_fakes(OPTIONS.sdk_tests)
    program = unittest.main(argv=[sys.argv[0], *remaining], verbosity=2, exit=False)
    print(
        f"OFFLINE_BOUNDARY denied_network_attempts={len(NETWORK_ATTEMPTS)} "
        "model_requests=0 real_device_actions=0"
    )
    print(f"OFFLINE_NETWORK_DENIALS {NETWORK_ATTEMPTS!r}")
    raise SystemExit(0 if program.result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
