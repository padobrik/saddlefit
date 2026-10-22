"""``saddlefit prune``: ablation on demand, with no tutor and no API key (B12, B14)."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.fixtures.toyrepo.build import build
from tests.integration.test_ablation_fakes import NOISE_ID, accept_by_noise
from tests.integration.test_fit_fakes import TRAIN, FakePractice, retries
from typer.testing import CliRunner

from saddlefit import cli
from saddlefit.cli import ablation_line, app
from saddlefit.eval.decisions import HeldOutComparison
from saddlefit.loops.ablation import AblationResult
from saddlefit.project import Project
from saddlefit.providers.base import ModelInfo
from saddlefit.storage.fit_state import FitLocked, FitState, load_fit_state, save_fit_state
from saddlefit.storage.library import read_records

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    repo = build(tmp_path / "repo")
    project = Project(repo)
    project.init()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        cli, "_student", lambda p: (ModelInfo(provider="fake", model="student"), lambda: None)
    )
    monkeypatch.setattr(cli, "_docker", object)
    return project


def fake_practice(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake student from the fit tests, in place of Docker."""

    def practice(self: Project, **kwargs: Any) -> tuple[FakePractice, list[str]]:
        return FakePractice(self.harness), list(TRAIN)

    monkeypatch.setattr(Project, "_practice", practice)


def test_prune_drops_the_useless_change_without_a_tutor(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_practice(project, monkeypatch)
    accept_by_noise(project.harness, project.state)
    save_fit_state(
        project.state,
        FitState(
            schema_version=1, rounds_done=4, accepted_since_ablation=2, student_model="student"
        ),
    )
    # no ANTHROPIC_API_KEY and a tutor fit would refuse: prune never calls a tutor
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = project.config_path
    config.write_text(
        config.read_text().replace("tutor:\n  provider: anthropic\n", "tutor:\n  provider: fake\n")
    )
    assert "provider: fake" in config.read_text()
    result = runner.invoke(app, ["--json", "prune"])
    assert result.exit_code == 0, result.output
    (entry,) = json.loads(result.output)
    assert entry["proposal_id"] == NOISE_ID
    assert entry["pruned"] is True
    assert "very careful" not in project.harness.read("systemprompt.md")
    assert retries(project.harness, "main") == 1
    assert [r.status for r in read_records(project.state)] == ["accepted", "pruned"]
    state = load_fit_state(project.state)
    assert (state.rounds_done, state.accepted_since_ablation) == (4, 0)
    assert not (project.state / "fit.lock").exists()


def test_prune_with_nothing_accepted(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    # no mined tasks, no model runner and no Docker: none of them is needed
    def unavailable(*args: Any) -> None:
        raise AssertionError("prune with nothing to test must not set up the student")

    monkeypatch.setattr(cli, "_student", unavailable)
    monkeypatch.setattr(cli, "_docker", unavailable)
    result = runner.invoke(app, ["prune"])
    assert result.exit_code == 0, result.output
    assert "no accepted changes to test" in result.output


def test_prune_waits_for_a_running_fit(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_practice(project, monkeypatch)
    accept_by_noise(project.harness, project.state)
    (project.state / "fit.lock").write_text("123\n")
    result = runner.invoke(app, ["prune"])
    assert result.exit_code == 1
    assert "another fit is running" in str(result.exception)


def test_prune_needs_held_out_tasks(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    train_only = [SimpleNamespace(id=f"t{i}", split="train") for i in range(3)]
    monkeypatch.setattr(Project, "tasks", lambda self: train_only)
    accept_by_noise(project.harness, project.state)
    result = runner.invoke(app, ["prune"])
    assert result.exit_code == 1
    assert "no held-out tasks" in str(result.exception)


def test_project_prune_with_nothing_accepted_needs_no_tasks(project: Project) -> None:
    assert project.tasks() == []
    assert project.prune(docker_client=None, model_client=lambda: None, model_info=None) == []  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("result", "line"),
    [
        (
            AblationResult(
                "p1",
                True,
                HeldOutComparison(wins=1, losses=0, tasks=6),
                HeldOutComparison(wins=0, losses=0, tasks=6),
            ),
            "pruned p1 (without it: training net +1, held-out net +0)",
        ),
        (
            AblationResult(
                "p2",
                False,
                HeldOutComparison(wins=0, losses=5, tasks=6),
                HeldOutComparison(wins=0, losses=2, tasks=6),
            ),
            "kept p2 (without it: training net -5, held-out net -2)",
        ),
        (AblationResult("p3", False, skipped="revert conflicts"), "skipped p3: revert conflicts"),
    ],
)
def test_ablation_line(result: AblationResult, line: str) -> None:
    assert ablation_line(result) == line


def test_prune_with_nothing_accepted_still_respects_a_running_fit(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project.state / "fit.lock").write_text("123\n")
    result = runner.invoke(app, ["prune"])
    assert result.exit_code == 1
    assert "another fit is running" in str(result.exception)
    with pytest.raises(FitLocked):
        project.prune(docker_client=None, model_client=lambda: None, model_info=None)  # type: ignore[arg-type]


def test_prune_in_an_uninitialised_project_points_to_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = build(tmp_path / "plain")
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["prune"])
    assert result.exit_code == 1
    assert "saddlefit init" in str(result.exception)
