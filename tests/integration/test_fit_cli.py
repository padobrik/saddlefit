"""The ``fit`` command's checks before any container starts."""

from pathlib import Path

import pytest
from tests.fixtures.toyrepo.build import build
from typer.testing import CliRunner

from saddlefit import cli
from saddlefit.cli import app, round_line
from saddlefit.loops.middle import RoundReport
from saddlefit.project import Project
from saddlefit.providers.base import ModelInfo

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = build(tmp_path / "repo")
    Project(repo).init()
    monkeypatch.chdir(repo)
    # no model runner and no Docker in these tests
    monkeypatch.setattr(
        cli, "_student", lambda p: (ModelInfo(provider="fake", model="m"), lambda: None)
    )
    monkeypatch.setattr(cli, "_docker", object)
    return repo


def test_fit_needs_the_anthropic_tutor(project: Path) -> None:
    config = project / ".saddlefit" / "saddlefit.yaml"
    text = config.read_text()
    tutor = "tutor:\n  provider: anthropic\n"
    assert text.count(tutor) == 1
    config.write_text(text.replace(tutor, "tutor:\n  provider: fake\n"))
    result = runner.invoke(app, ["fit"])
    assert result.exit_code == 1
    assert "anthropic tutor provider" in str(result.exception)


def test_fit_warns_when_prices_are_unset_and_needs_training_tasks(project: Path) -> None:
    result = runner.invoke(app, ["fit"])
    assert "tutor prices are 0.0" in result.output
    assert result.exit_code == 1
    assert "no training tasks" in str(result.exception)


def test_fit_says_nothing_about_prices_once_they_are_set(project: Path) -> None:
    config = project / ".saddlefit" / "saddlefit.yaml"
    config.write_text(config.read_text().replace("input: 0.0", "input: 4.0"))
    result = runner.invoke(app, ["fit"])
    assert "tutor prices" not in result.output


def test_round_line() -> None:
    report = RoundReport(batch=("a", "b", "c"), train_before=1, refused=1, lessons=1)
    report.tutor_calls, report.tutor_usd = 4, 0.1234
    assert round_line(2, report) == (
        "round 2: batch solved 1/3; 0 candidates tried, 1 refused; nothing accepted; "
        "1 lessons; tutor 4 calls, $0.12"
    )
