"""Command-line interface."""

from __future__ import annotations

import json
from enum import IntEnum
from typing import Annotated

import typer

from saddlefit import __version__


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


def main() -> None:
    """Console-script entry point."""
    app()
