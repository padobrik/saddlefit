"""Required test 8: synthetic traces map to the expected primary failure tags."""

import pytest

from saddlefit.eval.taxonomy import PRIORITY, RunSignals, classify

FINISHED_OK = {"finished": True, "ran_visible_check": True}

CASES = [
    # (name, signals, expected primary, expected tags)
    ("solved", RunSignals(check_passed=True, **FINISHED_OK), "solved", ("solved",)),
    ("check_fail", RunSignals(check_passed=False, **FINISHED_OK), "check_fail", ("check_fail",)),
    (
        "env_error",
        RunSignals(check_passed=False, env_error=True),
        "env_error",
        ("env_error", "check_fail"),
    ),
    (
        "env_error even if check passed",
        RunSignals(check_passed=True, env_error=True),
        "env_error",
        ("env_error",),
    ),
    (
        "tampered beats a passing check",
        RunSignals(check_passed=True, tampered=True, **FINISHED_OK),
        "tampered",
        ("tampered",),
    ),
    (
        "step limit",
        RunSignals(check_passed=False, hit_step_limit=True),
        "timeout",
        ("timeout", "check_fail"),
    ),
    (
        "time limit",
        RunSignals(check_passed=False, hit_time_limit=True),
        "timeout",
        ("timeout", "check_fail"),
    ),
    (
        "95% of context",
        RunSignals(check_passed=False, max_prompt_tokens=9500, context_length=10000),
        "context_overflow",
        ("context_overflow", "check_fail"),
    ),
    (
        "94% is fine",
        RunSignals(check_passed=False, max_prompt_tokens=9400, context_length=10000, **FINISHED_OK),
        "check_fail",
        ("check_fail",),
    ),
    (
        "context-length error",
        RunSignals(check_passed=False, context_length_error=True),
        "context_overflow",
        ("context_overflow", "check_fail"),
    ),
    (
        "3 malformed calls",
        RunSignals(check_passed=False, malformed_tool_calls=3),
        "malformed_tool_call",
        ("malformed_tool_call", "check_fail"),
    ),
    (
        "2 malformed calls",
        RunSignals(check_passed=False, malformed_tool_calls=2, **FINISHED_OK),
        "check_fail",
        ("check_fail",),
    ),
    (
        "loop",
        RunSignals(check_passed=False, stopped_by_loop_detection=True),
        "loop",
        ("loop", "check_fail"),
    ),
    (
        "finished without the check",
        RunSignals(check_passed=False, finished=True),
        "gave_up",
        ("gave_up", "check_fail"),
    ),
    (
        "finished with empty diff",
        RunSignals(check_passed=False, diff_empty=True, **FINISHED_OK),
        "gave_up",
        ("gave_up", "check_fail"),
    ),
    (
        "3 failing tools in a row",
        RunSignals(check_passed=False, max_failing_tool_streak=3, **FINISHED_OK),
        "tool_error",
        ("tool_error", "check_fail"),
    ),
    (
        "priority: tampered over timeout and loop",
        RunSignals(
            check_passed=False, tampered=True, hit_step_limit=True, stopped_by_loop_detection=True
        ),
        "tampered",
        ("tampered", "timeout", "loop", "check_fail"),
    ),
    (
        "priority: overflow over malformed",
        RunSignals(check_passed=False, context_length_error=True, malformed_tool_calls=5),
        "context_overflow",
        ("context_overflow", "malformed_tool_call", "check_fail"),
    ),
    (
        "solved run keeps secondary signals",
        RunSignals(check_passed=True, finished=True, max_failing_tool_streak=4),
        "solved",
        ("solved", "gave_up", "tool_error"),
    ),
]


@pytest.mark.parametrize(
    ("signals", "primary", "tags"), [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_synthetic_traces(signals: RunSignals, primary: str, tags: tuple[str, ...]) -> None:
    result = classify(signals)
    assert result.primary == primary
    assert result.tags == tags


def test_priority_order_matches_spec() -> None:
    assert PRIORITY == (
        "env_error",
        "tampered",
        "timeout",
        "context_overflow",
        "malformed_tool_call",
        "loop",
        "gave_up",
        "tool_error",
        "check_fail",
    )


def test_env_errors_are_excluded_from_statistics() -> None:
    assert not classify(RunSignals(check_passed=False, env_error=True)).counts_in_statistics
    assert classify(RunSignals(check_passed=False)).counts_in_statistics
