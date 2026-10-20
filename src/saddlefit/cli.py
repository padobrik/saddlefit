"""Command-line interface."""

from __future__ import annotations

import json
from datetime import date
from enum import IntEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from saddlefit import __version__
from saddlefit.config import ConfigError, SandboxConfig, StudentConfig, load_config
from saddlefit.doctor import doctor_for, render
from saddlefit.errors import EnvironmentFailure, SaddlefitError, UserError
from saddlefit.loops.baseline import RunOutcome
from saddlefit.project import Project
from saddlefit.providers.base import ModelClient
from saddlefit.providers.openai_compat import OpenAICompatibleClient
from saddlefit.reports.baseline import render_markdown
from saddlefit.tasks.mining import Runner, SandboxRunner


class ExitCode(IntEnum):
    """Process exit codes (B14)."""

    OK = 0
    USER_ERROR = 1
    INTEGRITY_VIOLATION = 2
    ENVIRONMENT_ERROR = 3


app = typer.Typer(
    name="saddlefit",
    help="Fit an agent's harness to one compact model, on your own tasks.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable JSON output.")
    ] = False,
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    """Saddlefit command group."""
    ctx.obj = {"json": json_output}
    if version:
        if json_output:
            typer.echo(json.dumps({"version": __version__}))
        else:
            typer.echo(f"saddlefit {__version__}")
        raise typer.Exit(ExitCode.OK)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


DEFAULT_CONFIG = Path(".saddlefit") / "saddlefit.yaml"

tasks_app = typer.Typer(help="Mine, list and show tasks.", no_args_is_help=True)
app.add_typer(tasks_app, name="tasks")


def _emit(ctx: typer.Context, data: object, text: str) -> None:
    """Print ``data`` as JSON under ``--json``, else the human-readable ``text``."""
    root_ctx = ctx.find_root()
    if root_ctx.obj and root_ctx.obj.get("json"):
        typer.echo(json.dumps(data, default=str))
    else:
        typer.echo(text)


def _project(repo: Path | None = None) -> Project:
    return Project((repo or Path.cwd()).resolve())


def _docker() -> Any:
    import docker  # noqa: PLC0415 - only the commands that run containers need the SDK

    try:
        client = docker.from_env()
    except docker.errors.DockerException as exc:
        raise EnvironmentFailure(f"Docker is not available: {exc}") from exc
    try:
        client.ping()
    except docker.errors.DockerException as exc:
        client.close()
        raise EnvironmentFailure(f"Docker is not available: {exc}") from exc
    return client


def mining_runner(project: Project) -> Runner:
    """Candidates' tests run in the sandbox, never on the host (B11)."""
    config = project.config()
    return SandboxRunner(_docker(), config.sandbox.image, config.sandbox)


@app.command()
def init(
    ctx: typer.Context,
    model: Annotated[str | None, typer.Option(help="Student model, e.g. qwen3:8b.")] = None,
    repo: Annotated[Path | None, typer.Option(help="Repository root (default: here).")] = None,
) -> None:
    """Create .saddlefit/: configuration, harness store and the split salt."""
    result = _project(repo).init(model)
    _emit(ctx, result, f"initialised {result['state']} for {result['model']}")


@tasks_app.command("mine")
def tasks_mine(
    ctx: typer.Context,
    limit: Annotated[int | None, typer.Option(help="Scan at most N commits.")] = None,
    since: Annotated[str | None, typer.Option(help="Only commits since YYYY-MM-DD.")] = None,
) -> None:
    """Turn git history into tasks: tests that fail before a commit and pass after."""
    try:
        since_date = date.fromisoformat(since) if since else None
    except ValueError as exc:
        raise UserError(f"--since must be YYYY-MM-DD: {since}") from exc
    project = _project()
    report, metas = project.mine(mining_runner(project), limit=limit, since=since_date)
    data = {**report.summary(), "tasks": [m.id for m in metas]}
    _emit(
        ctx,
        data,
        f"scanned {report.scanned} commits, {len(metas)} tasks; rejected: {dict(report.rejected)}",
    )


@tasks_app.command("list")
def tasks_list(ctx: typer.Context) -> None:
    """List tasks with their type and split."""
    metas = _project().tasks()
    rows = [{"id": m.id, "type": m.type, "split": m.split} for m in metas]
    _emit(ctx, rows, "\n".join(f"{m.split:8} {m.type:8} {m.id}" for m in metas) or "no tasks")


@tasks_app.command("show")
def tasks_show(ctx: typer.Context, task_id: str) -> None:
    """Show a task's public text and metadata (never its sealed part)."""
    project = _project()
    text = project.task_text(task_id)
    meta = next(m for m in project.tasks() if m.id == task_id)
    _emit(ctx, {"meta": meta.model_dump(mode="json"), "task": text}, text)


@app.command()
def doctor(
    ctx: typer.Context,
    config: Annotated[
        Path, typer.Option(help="Project config; without one, the defaults are checked.")
    ] = DEFAULT_CONFIG,
) -> None:
    """Check Docker, the runner, speed, tool calling and the compact-model tier (A3)."""
    try:
        loaded = load_config(config) if config.exists() else None
    except ConfigError as exc:
        raise UserError(str(exc)) from exc
    student = loaded.student if loaded else StudentConfig()
    sandbox = loaded.sandbox if loaded else SandboxConfig()
    report = doctor_for(student, sandbox)
    if loaded is not None and report.record is not None:
        # B5: the probe result is stored with the model digest, for tool_mode "auto"
        (config.parent / "doctor.json").write_text(
            report.record.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    if ctx.obj["json"]:
        typer.echo(json.dumps(report.as_json(), indent=2))
    else:
        typer.echo(render(report))
    if not report.environment_ok:
        raise EnvironmentFailure("Docker or the model runner is not usable; see the checks above")
    if not report.compact:
        raise typer.Exit(ExitCode.USER_ERROR)


@app.command()
def baseline(
    ctx: typer.Context,
    limit: Annotated[int | None, typer.Option(help="Run at most N tasks.")] = None,
) -> None:
    """Measure the student on training and held-out tasks; control is never run."""
    project = _project()
    config = project.config()

    def model_client() -> ModelClient:
        return OpenAICompatibleClient(
            base_url=config.student.base_url,
            model=config.student.model,
            context_length=config.student.context_length,
        )

    def progress(run: RunOutcome) -> None:
        if not (ctx.find_root().obj or {}).get("json"):
            typer.echo(f"{run.split:8} {run.classification.primary:20} {run.task_id}")

    report, markdown = project.baseline(
        docker_client=_docker(), model_client=model_client, limit=limit, on_run=progress
    )
    _emit(ctx, report, markdown.read_text(encoding="utf-8"))


@app.command()
def report(ctx: typer.Context) -> None:
    """Show the latest baseline report (training and held-out aggregates)."""
    data = _project().latest_report()
    _emit(ctx, data, render_markdown(data))


def main() -> None:
    """Console-script entry point; maps Saddlefit errors onto exit codes 1, 2 and 3."""
    try:
        app()
    except SaddlefitError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise SystemExit(exc.exit_code) from exc
