"""Turn identity and committed SDK output, independent of semantic classification."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from livekit.agents import StopResponse, llm


def fingerprint(message: Any) -> str:
    return hashlib.sha256(message.text_content.encode()).hexdigest()


def paired_items(items: list) -> list:
    """Discard orphan tool calls/results after history truncation."""
    calls = {item.call_id for item in items if isinstance(item, llm.FunctionCall)}
    results = {item.call_id for item in items if isinstance(item, llm.FunctionCallOutput)}
    paired = calls & results
    return [
        item
        for item in items
        if not isinstance(item, (llm.FunctionCall, llm.FunctionCallOutput))
        or item.call_id in paired
    ]


@dataclass
class Turn:
    epoch: int
    message: Any
    fingerprint: str
    decision: Any
    speech: Any = None
    items: list = field(default_factory=list)
    output_speeches: list = field(default_factory=list)
    interacted: bool = False

    def valid(self, epoch: int) -> bool:
        return (
            self.epoch == epoch
            and self.fingerprint == fingerprint(self.message)
            and self.speech is not None
            and not self.speech.interrupted
            and not self.speech.done()
        )

    def collect(self) -> None:
        """SDK chat_items contain committed output, not speculative transcription text."""
        if self.speech is None:
            return
        candidates = list(self.speech.chat_items)
        for child in self.output_speeches:
            candidates.extend(child.chat_items)
        unique = {
            item.id: item
            for item in candidates
            if not isinstance(item, llm.ChatMessage) or item.role == "assistant"
        }
        self.items = paired_items(sorted(unique.values(), key=lambda item: item.created_at)[-12:])
        self.interacted = self.interacted or bool(self.items)

    def require(self, epoch: int) -> None:
        if not self.valid(epoch):
            raise StopResponse()


def user_topics(
    turns: dict, epoch: int, current_id: str | None = None, *, uncommitted: bool = False
) -> list[dict]:
    """Historical addressed user data, never proof of assistant output or execution."""
    result = []
    for turn in turns.values():
        if (
            turn.epoch != epoch
            or turn.message.id == current_id
            or turn.fingerprint != fingerprint(turn.message)
            or not turn.decision.addressed
        ):
            continue
        if uncommitted:
            turn.collect()
            if turn.interacted:
                continue
        result.append({"role": "user", "text": turn.message.text_content[:500]})
    return result[-6:]


def confirmed(turns: dict, epoch: int) -> list[dict]:
    result = []
    for turn in turns.values():
        if turn.epoch != epoch or turn.fingerprint != fingerprint(turn.message):
            continue
        turn.collect()
        if not turn.interacted:
            continue
        result.append({"role": "user", "text": turn.message.text_content[:500]})
        result.extend(
            {"role": "assistant", "text": item.text_content[:500]}
            for item in turn.items
            if isinstance(item, llm.ChatMessage) and item.text_content
        )
    return result[-6:]
