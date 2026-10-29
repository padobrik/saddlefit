"""The ``fit`` command's checks before any container starts."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.fixtures.toyrepo.build import build
from typer.testing import CliRunner

from saddlefit import cli
from saddlefit import project as project_module
from saddlefit.cli import app, round_data, round_line
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
    assert "tutor prices not set (input, output, cache_write_1h, cache_read)" in result.output
    assert result.exit_code == 1
    assert "no training tasks" in str(result.exception)


def set_prices(project: Path, **prices: float) -> None:
    config = project / ".saddlefit" / "saddlefit.yaml"
    text = config.read_text()
    for name, value in prices.items():
        text = text.replace(f"    {name}: 0.0", f"    {name}: {value}", 1)
    config.write_text(text)


def test_fit_names_each_missing_price(project: Path) -> None:
    set_prices(project, input=4, cache_write_1h=8, cache_read=0.2)
    result = runner.invoke(app, ["fit"])
    assert "tutor prices not set (output)" in result.output


def test_fit_says_nothing_about_prices_once_they_are_set(project: Path) -> None:
    set_prices(project, input=4, output=20, cache_write_1h=8, cache_read=0.2)
    result = runner.invoke(app, ["fit"])
    assert "tutor prices" not in result.output


def test_round_line() -> None:
    report = RoundReport(batch=("a", "b", "c"), train_before=1, refused=1, lessons=1)
    report.tutor_calls, report.tutor_usd = 4, 0.1234
    assert round_line(2, report) == (
        "round 2: batch solved 1/3; 0 candidates tried, 1 refused; nothing accepted; "
        "1 lessons; tutor 4 calls, $0.12"
    )


def test_round_line_escapes_terminal_controls_in_reasons() -> None:
    report = RoundReport(batch=("a",), train_before=0, refused=1)
    osc52 = "\x1b]52;c;ZWNobyBoaQ==\x07"  # would set the clipboard
    report.refusals = [f"proposal: SC2086 {osc52}\nnext line\u202e"]
    shown = round_line(1, report).split("\n", 1)[1]
    assert shown == "  refused proposal: SC2086 \\x1b]52;c;ZWNobyBoaQ==\\x07\\x0anext line\\u202e"


def test_round_data_keeps_the_refusal_reasons() -> None:
    report = RoundReport(batch=("a",), train_before=0, refused=2)
    report.refusals = ["tutor: the call failed", "proposal: a disallowed file"]
    data = round_data(report)
    assert (data["refused"], data["refusals"]) == (2, report.refusals)


def test_fit_needs_held_out_tasks(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    train_only = [SimpleNamespace(id=f"t{i}", split="train") for i in range(3)]
    monkeypatch.setattr(Project, "tasks", lambda self: train_only)
    result = runner.invoke(app, ["fit"])
    assert result.exit_code == 1
    assert "no held-out tasks" in str(result.exception)


class ClosingPractice:
    """Records the closing evaluation, and whether the fit lock was held during it."""

    def __init__(self, state: Path, order: list[str] | None = None) -> None:
        self.state = state
        self.order = order if order is not None else []
        self.progress: Any = None
        self.evaluated: list[tuple[str, bool, str]] = []

    def evaluate(self, ref: str) -> int:
        stage = self.progress.record.stage if self.progress.record else ""
        self.order.append("evaluate")
        self.evaluated.append((ref, (self.state / "fit.lock").exists(), stage))
        return 3


def test_fit_ends_with_a_closing_evaluation_of_the_fitted_harness(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = Project(project)
    order: list[str] = []
    practice = ClosingPractice(proj.state, order)
    monkeypatch.setattr(Project, "_practice", lambda self, **kw: (practice, []))
    monkeypatch.setattr(project_module, "SandboxLinter", lambda *a, **k: None)

    def rounds(*args: Any, **kwargs: Any) -> list[RoundReport]:
        order.append("rounds")
        return []

    monkeypatch.setattr(project_module, "run_rounds", rounds)
    proj.fit(
        docker_client=object(),
        model_client=lambda: None,  # type: ignore[arg-type,return-value]
        tutor_client=object(),  # type: ignore[arg-type]
        model_info=ModelInfo(provider="fake", model="m"),
        rounds=1,
    )
    assert order == ["rounds", "evaluate"]
    # after the rounds, on main, under the fit lock, shown as its own stage
    assert practice.evaluated == [("main", True, "closing")]


def test_a_failed_round_skips_the_closing_evaluation(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = Project(project)
    practice = ClosingPractice(proj.state)
    monkeypatch.setattr(Project, "_practice", lambda self, **kw: (practice, []))
    monkeypatch.setattr(project_module, "SandboxLinter", lambda *a, **k: None)

    def broken(*args: Any, **kwargs: Any) -> list[RoundReport]:
        raise RuntimeError("the round failed")

    monkeypatch.setattr(project_module, "run_rounds", broken)
    with pytest.raises(RuntimeError):
        proj.fit(
            docker_client=object(),
            model_client=lambda: None,  # type: ignore[arg-type,return-value]
            tutor_client=object(),  # type: ignore[arg-type]
            model_info=ModelInfo(provider="fake", model="m"),
            rounds=1,
        )
    assert practice.evaluated == []  # the report then says "not measured", as it should
