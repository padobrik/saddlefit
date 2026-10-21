"""The baseline report: training and held-out aggregates only, never control (B15).

Held-out tasks are reported as counts, not per-task outcomes; per-task detail is given
for training tasks only.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saddlefit.loops.baseline import BaselineResult

Z95 = 1.959963984540054


REPORT_SCHEMA_VERSION = 1


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """The Wilson score interval for a binomial proportion (95% by default)."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class ReportContext:
    """What every report states (B15): model, harness and suite identity."""

    model: str
    model_digest: str | None
    harness_commit: str
    suite_hash: str
    saddlefit_version: str


def build(result: BaselineResult, ctx: ReportContext) -> dict[str, Any]:
    splits: dict[str, dict[str, Any]] = {}
    for split, (count, n) in result.by_split().items():
        low, high = wilson(count, n)
        splits[split] = {"solved": count, "tasks": n, "wilson95": [round(low, 3), round(high, 3)]}
    solved = result.solved()
    train_tasks = sorted({r.task_id for r in result.runs if r.split == "train"})
    tokens = [r.harness_tokens for r in result.runs]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "excluded_runs": result.excluded_runs(),
        "inconclusive_tasks": len(result.inconclusive()),
        "kind": "baseline",
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": {"id": ctx.model, "digest": ctx.model_digest},
        "harness_commit": ctx.harness_commit,
        "suite_hash": ctx.suite_hash,
        "saddlefit_version": ctx.saddlefit_version,
        "runs_per_task": result.runs_per_task,
        "solved_threshold": result.solved_threshold,
        "splits": splits,
        # decided tasks only; inconclusive ones are counted separately
        "train_tasks": {task: solved[task] for task in train_tasks if task in solved},
        "failure_profile": result.failure_profile(),
        "mean_harness_tokens": round(sum(tokens) / len(tokens), 1) if tokens else 0,
        "control_evaluations": 0,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"<!-- saddlefit baseline report, schema_version: {report['schema_version']} -->",
        "# Baseline report",
        "",
        f"- Model: `{report['model']['id']}` ({report['model']['digest'] or 'no digest'})",
        f"- Harness commit: `{report['harness_commit'][:12]}`",
        f"- Task suite: `{report['suite_hash']}`",
        f"- Runs per task: {report['runs_per_task']}, solved at "
        f"{report['solved_threshold']}+ passing runs",
        f"- Control evaluations so far: {report['control_evaluations']}",
        f"- Runs excluded for environment errors: {report.get('excluded_runs', 0)}; tasks "
        f"left inconclusive: {report.get('inconclusive_tasks', 0)}",
        "",
        "| Split | Solved | Tasks | Rate | Wilson 95% |",
        "| --- | --- | --- | --- | --- |",
    ]
    for split, row in report["splits"].items():
        rate = row["solved"] / row["tasks"] if row["tasks"] else 0.0
        low, high = row["wilson95"]
        lines.append(
            f"| {split} | {row['solved']} | {row['tasks']} | {rate:.0%} | {low:.0%} to {high:.0%} |"
        )
    lines += ["", "## Failure profile", "", "| Primary tag | Runs |", "| --- | --- |"]
    lines += [f"| {tag} | {count} |" for tag, count in report["failure_profile"].items()]
    lines += ["", f"Mean harness tokens per run: {report['mean_harness_tokens']}", ""]
    return "\n".join(lines)


def write(report: dict[str, Any], reports_dir: Path, report_id: str) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    md = reports_dir / f"{report_id}.md"
    js = reports_dir / f"{report_id}.json"
    md.write_text(render_markdown(report), encoding="utf-8")
    js.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return md, js
