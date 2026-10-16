"""Revocable fixed-text say output, separate from action authorization."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from livekit.agents import StopResponse


@dataclass
class OutputLink:
    child: Any
    source: Any
    epoch: int
    text: str
    purpose: str
    deadline: float
    frames: int = 0
    synthesized: bool = False
    played: bool = False
    source_callback: Any = None


class OutputLinks:
    def __init__(self, gate: Any) -> None:
        self.gate = gate
        self.links: dict[str, OutputLink] = {}

    def register(self, child: Any, source: Any, text: str, purpose: str) -> OutputLink:
        self.gate.require(source)
        if child is source or child.id in self.links or purpose not in {"command", "weather"}:
            raise StopResponse()
        if not text or (purpose == "weather" and text != "我帮你查一下天气"):
            raise StopResponse()
        link = OutputLink(child, source, self.gate.epoch, text, purpose, time.monotonic() + 12)
        self.links[child.id] = link
        link.source_callback = lambda _: self.remove(child, interrupt=True)
        source.add_done_callback(link.source_callback)
        return link

    def require(self, child: Any) -> OutputLink:
        link = self.links.get(child.id) if child is not None else None
        if (
            link is None
            or link.child is not child
            or link.epoch != self.gate.epoch
            or child.interrupted
            or time.monotonic() >= link.deadline
        ):
            raise StopResponse()
        self.gate.require(link.source)
        return link

    def validate_text(self, child: Any, chunk: str, offset: int) -> None:
        link = self.require(child)
        if not link.text.startswith(chunk, offset):
            child.interrupt(force=True)
            raise StopResponse()

    async def text_stream(self, child: Any, stream: Any):
        offset = 0
        async for chunk in stream:
            self.validate_text(child, str(chunk), offset)
            offset += len(chunk)
            yield chunk
        if offset != len(self.require(child).text):
            raise StopResponse()

    async def audio_stream(self, child: Any, stream: Any):
        link = self.require(child)
        async for frame in stream:
            self.require(child)
            link.frames += 1
            yield frame
        self.require(child)
        link.synthesized = link.frames > 0

    def remove(self, child: Any, *, interrupt: bool = False) -> None:
        link = self.links.pop(child.id, None)
        if link is not None:
            link.source.remove_done_callback(link.source_callback)
        if interrupt and not child.done():
            child.interrupt(force=True)

    def reset(self) -> None:
        for link in list(self.links.values()):
            self.remove(link.child, interrupt=True)


class _LinkedSay:
    def __init__(self, session: Any, context: Any, gate: Any, options: dict) -> None:
        self.session, self.context, self.gate = session, context, gate
        self.options = options
        self.source = context.speech_handle
        self.audio = session.output.audio
        self.child = None
        self.link = None

    def require_source(self) -> None:
        if self.gate is not None:
            self.gate.require(self.source)
        elif self.source.interrupted:
            raise StopResponse()

    def finished(self, event: Any) -> None:
        from livekit.agents.voice.agent_activity import _SpeechHandleContextVar

        if (
            self.link is not None
            and self.link.frames
            and _SpeechHandleContextVar.get(None) is self.child
        ):
            self.link.played = not event.interrupted and event.playback_position > 0

    async def watch(self) -> None:
        while not self.child.done():
            self.require_source()
            if self.gate is not None:
                self.gate.outputs.require(self.child)
            await asyncio.sleep(0.02)

    async def wait_child(self) -> None:
        watcher = asyncio.create_task(self.watch())
        waiter = asyncio.create_task(self.child.wait_for_playout())
        try:
            done, _ = await asyncio.wait({watcher, waiter}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            await waiter
        finally:
            watcher.cancel()
            waiter.cancel()
            await asyncio.gather(watcher, waiter, return_exceptions=True)

    async def play(self, text: str) -> Any:
        self.require_source()
        await self.context.wait_for_playout()
        self.require_source()
        if self.audio is None:
            raise StopResponse()
        self.audio.on("playback_finished", self.finished)
        history = self.options.get("history", False)
        self.child = self.session.say(text, add_to_chat_ctx=history, allow_interruptions=True)
        if self.gate is not None:
            self.link = self.gate.outputs.register(
                self.child, self.source, text, self.options["purpose"]
            )
            self.link.deadline = time.monotonic() + self.options.get("timeout_s", 12.0)
        await self.wait_child()
        self.require_source()
        if self.child.interrupted:
            raise StopResponse()
        if self.gate is not None:
            link = self.gate.outputs.require(self.child)
            if not (link.frames and link.synthesized and link.played):
                raise StopResponse()
            if history:
                self.gate.record_output(self.source, self.child)
        return self.child

    def cleanup(self, *, interrupt: bool = False) -> None:
        if self.audio is not None:
            self.audio.off("playback_finished", self.finished)
        if self.child is not None:
            if interrupt:
                self.child.interrupt(force=True)
            if self.gate is not None:
                self.gate.outputs.remove(self.child)


async def say_linked(session: Any, context: Any, gate: Any, text: str, **options: Any) -> Any:
    """Wait for successful audio playback, not handle.done; always revoke on exit."""
    operation = _LinkedSay(session, context, gate, options)
    try:
        return await asyncio.wait_for(operation.play(text), options.get("timeout_s", 12.0))
    except asyncio.CancelledError:
        operation.cleanup(interrupt=True)
        raise
    except Exception as exc:
        operation.cleanup(interrupt=True)
        raise StopResponse() from exc
    finally:
        operation.cleanup()
