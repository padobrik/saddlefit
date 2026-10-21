"""Command-line interface."""

from __future__ import annotations

import json
from enum import IntEnum
from pathlib import Path
from typing import Annotated

import typer

from saddlefit import __version__
from saddlefit.config import ConfigError, SandboxConfig, StudentConfig, load_config
from saddlefit.doctor import doctor_for, render
from saddlefit.errors import EnvironmentFailure, SaddlefitError, UserError


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
    # the report comes first, so a failed write below never hides the checks
    if ctx.obj["json"]:
        typer.echo(json.dumps(report.as_json(), indent=2))
    else:
        typer.echo(render(report))
    if loaded is not None and report.record is not None:
        # B5: the probe result is stored with the model digest, for tool_mode "auto"
        record = config.parent / "doctor.json"
        try:
            record.write_text(report.record.model_dump_json(indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise EnvironmentFailure(f"cannot write {record}: {exc}") from exc
    if not report.environment_ok:
        raise EnvironmentFailure("Docker or the model runner is not usable; see the checks above")
    if not report.compact:
        raise typer.Exit(ExitCode.USER_ERROR)


def main() -> None:
    """Console-script entry point; maps Saddlefit errors onto exit codes 1, 2 and 3."""
    try:
        app()
    except SaddlefitError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise SystemExit(exc.exit_code) from exc
