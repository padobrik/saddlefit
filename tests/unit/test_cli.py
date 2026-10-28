import json

from typer.testing import CliRunner

from saddlefit import __version__
from saddlefit.cli import ExitCode, app

runner = CliRunner()


def test_version_human() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == ExitCode.OK
    assert result.output.strip() == f"saddlefit {__version__}"


def test_version_json() -> None:
    result = runner.invoke(app, ["--json", "--version"])
    assert result.exit_code == ExitCode.OK
    assert json.loads(result.output) == {"version": __version__}


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output


def test_exit_codes_match_spec() -> None:
    assert [int(c) for c in ExitCode] == [0, 1, 2, 3, 4]
