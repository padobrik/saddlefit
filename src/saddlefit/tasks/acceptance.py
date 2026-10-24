"""Fail-without, pass-with acceptance of a generated task (A11, B13).

The generator's hidden tests and reference patch go straight to the sealed store. The
evaluator then judges two diffs from the base snapshot in fresh containers: none, which
must fail, and the reference patch, which must pass. Only then is the task worth
calibrating against the student.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from saddlefit.eval.evaluator import Evaluator, Verdict
from saddlefit.storage.runs import new_run_id
from saddlefit.tasks.generator import GeneratedTask
from saddlefit.tasks.sealing import SealedWriter
from saddlefit.tasks.suite import PYTEST

# pytest's exit codes for failed tests (1) and an error while collecting them (2), such as
# importing a module the task asks for; anything else means the tests never really ran
FAILED_TESTS = frozenset({1, 2})


@dataclass(frozen=True)
class Acceptance:
    accepted: bool
    reason: str


def check_script(test_paths: list[str]) -> str:
    """``check.sh`` for hidden tests: the same pytest call mined tasks use."""
    if not test_paths:
        # pytest without file arguments would collect the repository's own tests instead
        raise ValueError("a generated task needs at least one hidden test")
    tests = " ".join(shlex.quote(path) for path in sorted(test_paths))
    return f"#!/usr/bin/env bash\nset -u\ncd /work\n{PYTEST} {tests}\n"


def seal_generated(sealed_root: Path, task_id: str, task: GeneratedTask) -> None:
    """Write the task's check, hidden tests and reference patch to the sealed store."""
    SealedWriter(sealed_root).write_task(
        task_id,
        check_sh=check_script(list(task.hidden_tests)),
        hidden_tests=dict(task.hidden_tests),
        reference_patch=task.reference_patch,
        replace=True,
    )


def _broken(verdict: Verdict) -> str | None:
    if verdict.env_error:
        return "the environment failed"
    if verdict.timed_out:
        return "the hidden check timed out"
    return None


def _without_reference(verdict: Verdict) -> str | None:
    if problem := _broken(verdict):
        return f"without the reference patch, {problem}"
    if verdict.passed:
        return "the hidden tests pass without the reference patch"
    if verdict.exit_code not in FAILED_TESTS:
        return f"without the reference patch, pytest exited {verdict.exit_code}, not a test failure"
    return None


def _with_reference(verdict: Verdict) -> str | None:
    if verdict.tampered:
        return "the reference patch touches a protected path"
    if not verdict.diff_applied:
        return "the reference patch does not apply to the base snapshot"
    if problem := _broken(verdict):
        return f"with the reference patch, {problem}"
    if not verdict.passed:
        return "the hidden tests fail with the reference patch"
    return None


def accept(
    evaluator: Evaluator, task_id: str, base_snapshot: Path, task: GeneratedTask
) -> Acceptance:
    """Whether the sealed task fails on the base snapshot and passes with its reference."""
    without = evaluator.evaluate(task_id, base_snapshot, "", new_run_id())
    problem = _without_reference(without)
    if problem is None:
        reference = evaluator.evaluate(task_id, base_snapshot, task.reference_patch, new_run_id())
        problem = _with_reference(reference)
    if problem is not None:
        return Acceptance(False, problem)
    return Acceptance(True, "fails without the reference patch and passes with it")
