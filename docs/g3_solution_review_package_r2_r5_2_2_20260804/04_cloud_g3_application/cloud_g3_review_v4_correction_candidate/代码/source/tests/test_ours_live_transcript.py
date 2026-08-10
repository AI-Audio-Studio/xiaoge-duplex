from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

from live_transcript import LiveTranscript, LiveTranscriptConfig  # noqa: E402


def test_reset_turn_clears_committed_text_even_when_not_open() -> None:
    events: list[dict] = []
    live = LiveTranscript(events.append, LiveTranscriptConfig(enabled=True))

    live.feed_commit("turn-a")
    live.reset_turn("g3_protocol_cmd")
    live.feed_full("turn-b")

    partials = [event["text"] for event in events if event.get("type") == "user_partial"]
    assert partials == ["turn-a", "turn-b"]
