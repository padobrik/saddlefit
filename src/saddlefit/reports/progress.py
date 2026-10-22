"""The progress report: the baseline harness against the current one (B15).

Training and held-out aggregates only: success before and after with Wilson intervals, a
paired bootstrap interval for the difference, success per task type, the failure profile
of the current harness and the tutor's cost. Control numbers, context efficiency and the
delegation map appear only in ``report --final``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

PROGRESS_SCHEMA_VERSION: Final = 1


@dataclass(frozen=True)
class ProgressContext:
    model: str
    model_digest: str | None
    baseline_harness_commit: str
    harness_commit: str
    suite_hash: str
    saddlefit_version: str
    runs_per_task: int
    solved_threshold: int
    fit_rounds: int
    tutor_calls: int
    tutor_usd: float
    control_evaluations: int


def build(progress: dict[str, Any], ctx: ProgressContext) -> dict[str, Any]:
    return {
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "kind": "progress",
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": {"id": ctx.model, "digest": ctx.model_digest},
        "baseline_harness_commit": ctx.baseline_harness_commit,
        "harness_commit": ctx.harness_commit,
        "suite_hash": ctx.suite_hash,
        "saddlefit_version": ctx.saddlefit_version,
        "runs_per_task": ctx.runs_per_task,
        "solved_threshold": ctx.solved_threshold,
        "fit_rounds": ctx.fit_rounds,
        "splits": progress["splits"],
        "failure_profile": progress["failure_profile"],
        "tutor": {"calls": ctx.tutor_calls, "usd": round(ctx.tutor_usd, 4)},
        "control_evaluations": ctx.control_evaluations,
    }


def _rate(row: dict[str, Any], n: int) -> str:
    low, high = row["wilson95"]
    rate = row["solved"] / n if n else 0.0
    return f"{row['solved']}/{n} ({rate:.0%}, {low:.0%} to {high:.0%})"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"<!-- saddlefit {report['kind']} report, schema_version: {report['schema_version']} -->",
        "# Final report" if report["kind"] == "final" else "# Progress report",
        "",
        f"- Model: `{report['model']['id']}` ({report['model']['digest'] or 'no digest'})",
        f"- Harness: `{report['baseline_harness_commit'][:12]}` (baseline) → "
        f"`{report['harness_commit'][:12]}` (now), after {report['fit_rounds']} fit rounds",
        f"- Task suite: `{report['suite_hash']}`",
        f"- Runs per task: {report['runs_per_task']}, solved at "
        f"{report['solved_threshold']}+ passing runs",
        f"- Tutor: {report['tutor']['calls']} calls, ${report['tutor']['usd']:.2f}",
        f"- Control evaluations so far: {report['control_evaluations']}",
        "",
        "Tasks decided under both harnesses; Wilson 95% intervals; the difference is a paired "
        "bootstrap 95% interval.",
        "",
        "| Split | Tasks | Before | After | Difference | Not measured |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for split, row in report["splits"].items():
        n = row["tasks"]
        low, high = row["difference95"]
        lines.append(
            f"| {split} | {n} | {_rate(row['before'], n)} | {_rate(row['after'], n)} | "
            f"{low:+.0%} to {high:+.0%} | {row['unmeasured']} |"
        )
    lines += ["", "## Per task type", "", "| Split | Type | Tasks | Before | After |"]
    lines.append("| --- | --- | --- | --- | --- |")
    for split, row in report["splits"].items():
        for task_type, counts in row["by_type"].items():
            lines.append(
                f"| {split} | {task_type} | {counts['tasks']} | {counts['before']} | "
                f"{counts['after']} |"
            )
    lines += ["", "## Failure profile, current harness", "", "| Primary tag | Runs |"]
    lines.append("| --- | --- |")
    lines += [f"| {tag} | {count} |" for tag, count in report["failure_profile"].items()]
    if "control" in report:
        lines += _control_lines(report)
    lines.append("")
    return "\n".join(lines)


def _control_lines(report: dict[str, Any]) -> list[str]:
    """Only in ``report --final``: control, context efficiency and the delegation map."""
    control = report["control"]
    n = control["tasks"]
    low, high = control["difference95"]
    efficiency = control["context_efficiency"]
    lines = [
        "",
        f"## Control, frozen harness `{report['frozen_tag']}`",
        "",
        f"- Tasks decided under both harnesses: {n}; not measured: {control['unmeasured']}",
        f"- Before: {_rate(control['before'], n)}",
        f"- After: {_rate(control['after'], n)}",
        f"- Difference (paired bootstrap 95%): {low:+.0%} to {high:+.0%}",
        "- Context efficiency: "
        + (
            "n/a"
            if efficiency is None
            else f"{efficiency} control successes per 1,000 harness tokens"
        ),
        "",
        "| Task type | Delegation |",
        "| --- | --- |",
    ]
    lines += [f"| {t} | {verdict} |" for t, verdict in control["delegation"].items()]
    return lines


def write(report: dict[str, Any], reports_dir: Path, report_id: str) -> Path:
    """Write ``<id>.md`` and ``<id>.json``; return the Markdown path."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    md = reports_dir / f"{report_id}.md"
    md.write_text(render_markdown(report), encoding="utf-8")
    (reports_dir / f"{report_id}.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return md
