from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "voice_agents"))

from live_transcript import LiveTranscript, LiveTranscriptConfig  # noqa: E402


def test_interim_and_commits_share_one_turn_identity() -> None:
    events: list[dict] = []
    live = LiveTranscript(events.append, LiveTranscriptConfig(enabled=True))

    live.feed_full("first")
    live.feed_commit("first")
    live.feed_full(" second")
    live.feed_commit(" second")

    partials = [event for event in events if event.get("type") == "user_partial"]
    ids = {event.get("utterance_id") for event in partials}
    assert len(ids) == 1
    assert next(iter(ids))
    assert all("final" not in event for event in partials)


def test_authoritative_finish_reuses_identity_and_next_turn_differs() -> None:
    events: list[dict] = []
    live = LiveTranscript(events.append, LiveTranscriptConfig(enabled=True))

    live.feed_full("turn-a")
    first_id = [event for event in events if event.get("type") == "user_partial"][-1][
        "utterance_id"
    ]
    assert live.finish_turn("conversation_item_added", "item-a") == first_id

    live.feed_full("turn-b")
    second_id = [event for event in events if event.get("type") == "user_partial"][-1][
        "utterance_id"
    ]
    assert second_id != first_id


def test_authoritative_finish_without_partial_uses_fallback_identity() -> None:
    live = LiveTranscript(lambda _event: None, LiveTranscriptConfig(enabled=True))

    assert live.finish_turn("conversation_item_added", "item-final-only") == "item-final-only"
    assert live.finish_turn("conversation_item_added", "item-next") == "item-next"


def test_reset_turn_clears_committed_text_even_when_not_open() -> None:
    events: list[dict] = []
    live = LiveTranscript(events.append, LiveTranscriptConfig(enabled=True))

    live.feed_commit("turn-a")
    live.reset_turn("g3_protocol_cmd")
    live.feed_full("turn-b")

    partials = [event["text"] for event in events if event.get("type") == "user_partial"]
    assert partials == ["turn-a", "turn-b"]


def test_live_partial_paths_strip_leading_punctuation() -> None:
    events: list[dict] = []
    live = LiveTranscript(events.append, LiveTranscriptConfig(enabled=True))

    live.feed_online("，online", False)
    live.reset_turn()
    live.feed_full("。 full")
    live.reset_turn()
    live.feed_commit("！？commit")

    partials = [event["text"] for event in events if event.get("type") == "user_partial"]
    assert partials == ["online", "full", "commit"]


def test_live_partial_does_not_emit_punctuation_only_text() -> None:
    events: list[dict] = []
    live = LiveTranscript(events.append, LiveTranscriptConfig(enabled=True))

    live.feed_full("，！？ ")
    live.feed_commit("。")

    assert not [event for event in events if event.get("type") == "user_partial"]
