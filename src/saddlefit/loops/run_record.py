"""Run signals for the taxonomy and the summary line of a run record."""

from __future__ import annotations

from typing import TYPE_CHECKING

from saddlefit import __version__
from saddlefit.config import Config
from saddlefit.eval.evaluator import Verdict
from saddlefit.eval.taxonomy import Classification, RunSignals
from saddlefit.providers.base import ModelInfo
from saddlefit.storage.runs import RUN_SCHEMA_VERSION, CheckResult, DiffStats, ModelRef, RunSummary
from saddlefit.student.loop import StudentResult
from saddlefit.tasks.models import TaskMeta

if TYPE_CHECKING:
    from saddlefit.loops.baseline import BaselineRunner


def run_signals(
    result: StudentResult | None, verdict: Verdict, stats: DiffStats, config: Config
) -> RunSignals:
    if result is None:
        return RunSignals(check_passed=False, env_error=True)
    return RunSignals(
        check_passed=verdict.passed,
        # the model runner failing is an environment failure: excluded and rerun (B9)
        env_error=verdict.env_error or result.stop_reason == "provider_error",
        tampered=bool(verdict.tampered),
        hit_step_limit=result.stop_reason == "step_limit",
        hit_time_limit=result.stop_reason == "time_limit",
        max_prompt_tokens=result.max_prompt_tokens,
        context_length=config.student.context_length,
        context_length_error=result.context_length_error,
        malformed_tool_calls=result.malformed_tool_calls,
        stopped_by_loop_detection=result.stopped_by_loop_detection,
        finished=result.finished,
        ran_visible_check=result.ran_visible_check,
        diff_empty=stats.files == 0,
        max_failing_tool_streak=result.max_failing_tool_streak,
    )


def run_summary(
    *,
    run_id: str,
    meta: TaskMeta,
    attempt: int,
    seed: int,
    info: ModelInfo,
    runner: BaselineRunner,
    result: StudentResult | None,
    verdict: Verdict,
    classification: Classification,
    stats: DiffStats,
    harness_tokens: int,
    wall: float,
) -> RunSummary:
    check = (
        CheckResult(exit_code=verdict.exit_code, duration_s=verdict.duration_s)
        if verdict.exit_code is not None
        else None
    )
    return RunSummary(
        schema_version=RUN_SCHEMA_VERSION,
        type="summary",
        run_id=run_id,
        task_id=meta.id,
        split=meta.split,
        attempt=attempt + 1,
        model=ModelRef(
            id=info.model, digest=info.digest, runner=info.runner, tool_mode=runner.tool_mode
        ),
        harness_commit=runner.harness_commit,
        saddlefit_version=__version__,
        suite_hash=runner.suite_hash,
        sandbox_image=runner.image,
        seed=seed,
        steps=result.steps if result else 0,
        prompt_tokens=result.prompt_tokens if result else 0,
        completion_tokens=result.completion_tokens if result else 0,
        harness_tokens=harness_tokens,
        wall_seconds=round(wall, 3),
        outcome="pass" if verdict.passed else "fail",
        primary_tag=classification.primary,
        tags=classification.tags,
        check=check,
        diff_stats=stats,
    )
