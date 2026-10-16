"""Evidence/denominator regression tests without model or network access."""

import pytest

from eval_10097.grading import (
    NATURALNESS_DIMENSIONS,
    grade_naturalness,
    grade_tool_calls,
    latency_summary,
    parameters_match,
    parse_milliseconds,
    summarize_outcomes,
    summarize_tool_grades,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("12.345ms", 12.345),
        (".125 s", 125.0),
        ("17.25", 17.25),
        (0, 0.0),
        (None, None),
        (True, None),
        ("N/A", None),
        ("NaN", None),
        (float("inf"), None),
        ("-1ms", None),
        ("123 seconds later", None),
    ],
)
def test_decimal_latency(value, expected):
    assert parse_milliseconds(value) == expected


def test_latency_missing_not_zero():
    summary = latency_summary(["10.5ms", "20.5ms", None, "N/A"])
    assert summary["n"] == 2
    assert summary["missing"] == 2
    assert summary["p50_ms"] == 15.5
    assert summary["p90_ms"] == 19.5
    assert summary["small_sample"] is True
    assert latency_summary([None])["p50_ms"] is None


@pytest.mark.parametrize(
    "name,actual,expected,match",
    [
        ("play_music", {}, {"music_id": "random"}, True),
        ("play_music", {"music_id": ""}, {"music_id": "random"}, True),
        ("play_music", {"music_id": "Random"}, {"music_id": "random"}, False),
        ("adjust_volume", {"command": "up", "value": 30}, {"command": "up"}, True),
        ("adjust_volume", {"command": "set"}, {"command": "set", "value": 50}, True),
        (
            "adjust_volume",
            {"command": "set", "value": "30"},
            {"command": "set", "value": 30},
            False,
        ),
        ("adjust_volume", {"command": "set", "value": True}, {"command": "set", "value": 1}, False),
        ("check_battery", {"battery_type": "cap"}, {"battery_type": "cap", "gauge_id": None}, True),
        ("check_battery", {"battery_type": "cap", "gauge_id": 2}, {"battery_type": "cap"}, False),
    ],
)
def test_equivalences(name, actual, expected, match):
    assert parameters_match(name, actual, expected) is match


def test_declared_alternatives_only():
    assert parameters_match(
        "query_knowledge", {"query": "能力"}, {"query": "会什么"}, [{"query": "能力"}]
    )
    assert not parameters_match("query_knowledge", {"query": "能力范围"}, {"query": "能力"})


def expectation(name="take_photo", arguments=None):
    return {"tools": [{"name": name, "arguments": arguments or {}}]}


def actual(name="take_photo", arguments=None, status="ok"):
    return {
        "status": status,
        "calls": [{"name": name, "arguments": arguments or {}, "status": "recorded"}],
    }


def test_selection_parameter_joint_denominators_and_failure_retention():
    grades = [
        grade_tool_calls(expectation(), actual()),
        grade_tool_calls(
            expectation("adjust_volume", {"command": "set", "value": 30}),
            actual("adjust_volume", {"command": "set", "value": 60}),
        ),
        grade_tool_calls(expectation(), actual("stop_music")),
        grade_tool_calls({"tools": []}, {"status": "ok", "calls": []}),
        grade_tool_calls({"tools": []}, actual()),
        grade_tool_calls({"tools": []}, {"status": "timeout", "calls": []}),
        grade_tool_calls(expectation(), {"status": "blocked", "calls": []}),
    ]
    summary = summarize_tool_grades(grades)
    assert summary["total"] == 7
    assert summary["selection"] == {"correct": 3, "total": 6, "rate": 0.5}
    assert summary["parameters_given_correct_tool"] == {"correct": 1, "total": 2, "rate": 0.5}
    assert summary["joint"]["correct"] == 2
    assert summary["false_trigger"] == {"correct": 1, "total": 2, "rate": 0.5}
    assert summary["unobserved_negative"] == 1
    assert summary["statuses"]["timeout"] == 1
    assert summary["execution_success"] is None


def test_parallel_multiset_duplicates_and_rejected_calls():
    expected = {
        "tools": [
            {"name": "take_photo", "arguments": {}},
            {"name": "adjust_volume", "arguments": {"command": "up"}},
        ]
    }
    calls = actual("adjust_volume", {"command": "up"})["calls"] + actual()["calls"]
    assert grade_tool_calls(expected, {"status": "ok", "calls": calls})["joint_correct"]
    assert not grade_tool_calls(expectation(), {"status": "ok", "calls": calls})[
        "selection_correct"
    ]
    duplicate = actual()["calls"] * 2
    assert not grade_tool_calls(expectation(), {"status": "ok", "calls": duplicate})[
        "joint_correct"
    ]
    rejected = {
        "status": "ok",
        "calls": [{"name": "take_photo", "arguments": {}, "status": "rejected"}],
    }
    assert not grade_tool_calls(expectation(), rejected)["joint_correct"]


def test_failed_no_call_not_success_and_unknown_false_trigger():
    grade = grade_tool_calls({"tools": []}, {"status": "error", "calls": []})
    assert not grade["selection_correct"]
    assert grade["false_trigger"] is None
    assert grade_tool_calls({"tools": []}, actual("unknown", status="error"))["false_trigger"]


def test_human_review_not_fabricated():
    assert grade_naturalness(None) == {"status": "unreviewed", "scores": None, "mean": None}
    with pytest.raises(ValueError, match="human"):
        grade_naturalness(
            {
                "scores": dict.fromkeys(NATURALNESS_DIMENSIONS, 5),
                "quote": "你好",
                "reason": "流畅",
                "reviewer_type": "ai",
            }
        )


def test_outcomes_keep_timeout_errors_blocked_and_unreviewed():
    results = [
        {"status": "ok", "task_success": True},
        {"status": "error"},
        {"status": "timeout"},
        {"status": "blocked"},
        {"status": "ok"},
    ]
    summary = summarize_outcomes(results)
    assert summary["results"] == results
    assert summary["task_success"] == {"correct": 1, "total": 4, "rate": 0.25}
    assert summary["timeout_rate"] == 0.25
    assert summary["unreviewed"] == 1
