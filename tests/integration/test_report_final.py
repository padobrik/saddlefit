"""``saddlefit report --final``: control once, on a frozen harness, logged and counted (B10)."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.fixtures.toyrepo.build import build
from tests.integration.test_report_cli import fake_baseline
from typer.testing import CliRunner

from saddlefit import cli
from saddlefit.cli import app
from saddlefit.errors import UserError
from saddlefit.project import Project
from saddlefit.providers.base import ModelInfo
from saddlefit.reports.progress import render_markdown
from saddlefit.storage.control_log import control_evaluations, load_control_log
from saddlefit.storage.fit_state import FitState, save_fit_state

pytestmark = pytest.mark.integration
runner = CliRunner()

LIVE = ModelInfo(provider="fake", model="qwen3:8b", digest="sha256:m")
CONTROL = [SimpleNamespace(id=f"c{i}", split="control", type="bugfix") for i in range(6)]
TRAIN = [SimpleNamespace(id="t0", split="train", type="bugfix")]
LESSON = (
    "diff --git a/EXPERIENCE.md b/EXPERIENCE.md\n--- a/EXPERIENCE.md\n+++ b/EXPERIENCE.md\n"
    "@@ -1 +1 @@\n-# Lessons\n+# Lessons learned\n"
)


class FakeControl:
    """Control outcomes per harness commit: the baseline solves 1 of 6, a changed one 6."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self.runs: list[str] = []

    def outcomes(self, ref: str, tasks: Any) -> tuple[dict[str, bool | None], float]:
        commit = self.project.harness.head(ref)
        self.runs.append(commit)
        changed = "learned" in self.project.harness.read("EXPERIENCE.md", commit)
        return {t.id: changed or t.id == "c0" for t in tasks}, 2000.0


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    repo = build(tmp_path / "repo")
    project = Project(repo)
    project.init()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(Project, "tasks", lambda self: CONTROL + TRAIN)
    model = ModelInfo(provider="fake", model=project.config().student.model, digest="sha256:m")
    monkeypatch.setattr(cli, "_student", lambda p: (model, lambda: None))
    monkeypatch.setattr(cli, "_docker", object)
    fake_baseline(project, project.harness.head())
    return project


def use_fake_control(project: Project, monkeypatch: pytest.MonkeyPatch) -> FakeControl:
    control = FakeControl(project)
    monkeypatch.setattr(Project, "_control_runner", lambda self, *args: control)
    return control


def test_final_freezes_the_harness_and_logs_one_control_evaluation(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = use_fake_control(project, monkeypatch)
    result = runner.invoke(app, ["--json", "report", "--final"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["kind"] == "final"
    tag = report["frozen_tag"]
    assert tag.startswith("fit-")
    assert tag.endswith("-1")
    assert project.harness.head(tag) == project.harness.head()
    # the harness did not change since the baseline: control ran once
    assert len(control.runs) == 1
    summary = report["control"]
    assert (summary["before"]["solved"], summary["after"]["solved"]) == (1, 1)
    assert summary["delegation"] == {"bugfix": "cloud"}
    assert report["control_evaluations"] == 1
    (entry,) = load_control_log(project.state).evaluations
    assert (entry.tag, entry.tasks) == (tag, 6)
    assert not (project.state / "fit.lock").exists()
    # every later report prints the count
    plain = json.loads(runner.invoke(app, ["--json", "report"]).output)
    assert plain["control_evaluations"] == 1
    assert "control" not in plain


def test_final_compares_the_baseline_harness_with_the_frozen_one(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.harness.propose("p1", LESSON, "a lesson")
    project.harness.accept("p1")
    control = use_fake_control(project, monkeypatch)
    report = json.loads(runner.invoke(app, ["--json", "report", "--final"]).output)
    baseline = report["baseline_harness_commit"]
    assert control.runs == [project.harness.head(baseline), project.harness.head()]
    summary = report["control"]
    assert (summary["before"]["solved"], summary["after"]["solved"]) == (1, 6)
    assert summary["delegation"] == {"bugfix": "local"}
    assert summary["context_efficiency"] == pytest.approx(0.5)
    second = json.loads(runner.invoke(app, ["--json", "report", "--final"]).output)
    assert second["frozen_tag"].endswith("-2")
    assert control_evaluations(project.state) == 2


def test_final_waits_for_a_running_fit(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    control = use_fake_control(project, monkeypatch)
    (project.state / "fit.lock").write_text("1\n")
    result = runner.invoke(app, ["report", "--final"])
    assert result.exit_code == 1
    assert "another fit is running" in str(result.exception)
    assert control.runs == []
    assert control_evaluations(project.state) == 0


def test_final_needs_control_tasks(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_control(project, monkeypatch)
    monkeypatch.setattr(Project, "tasks", lambda self: TRAIN)
    result = runner.invoke(app, ["report", "--final"])
    assert result.exit_code == 1
    assert "no control tasks" in str(result.exception)


def test_final_refuses_an_incomparable_baseline_before_running_control(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = use_fake_control(project, monkeypatch)
    fake_baseline(project, project.harness.head(), runs_per_task=5)  # replaces the baseline
    result = runner.invoke(app, ["report", "--final"])
    assert result.exit_code == 1
    assert "evaluation settings changed" in str(result.exception)
    assert control.runs == []


def test_a_failed_control_run_leaves_no_tag_and_no_log(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(FakeControl):
        def outcomes(self, ref: str, tasks: Any) -> tuple[dict[str, bool | None], float]:
            raise RuntimeError("sandbox died")

    monkeypatch.setattr(Project, "_control_runner", lambda self, *args: Broken(project))
    with pytest.raises(RuntimeError):
        project.final_report(docker_client=None, model_client=lambda: None, model_info=LIVE)  # type: ignore[arg-type]
    assert (
        subprocess.run(
            ["git", "tag", "--list", "fit-*"],
            cwd=project.harness.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )
    assert control_evaluations(project.state) == 0
    assert not (project.state / "fit.lock").exists()


def test_the_final_report_reads_the_fit_state_under_the_lock(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = FakeControl(project)

    def runner_while_a_fit_finishes(self: Project, *args: Any) -> FakeControl:
        # building the environment image takes minutes; a fit finishes meanwhile
        save_fit_state(self.state, FitState(schema_version=1, rounds_done=7, tutor_calls=9))
        return control

    monkeypatch.setattr(Project, "_control_runner", runner_while_a_fit_finishes)
    report = json.loads(runner.invoke(app, ["--json", "report", "--final"]).output)
    assert report["fit_rounds"] == 7
    assert report["tutor"]["calls"] == 9


def test_zero_context_efficiency_is_shown_as_zero() -> None:
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "final",
        "model": {"id": "m", "digest": None},
        "baseline_harness_commit": "a" * 12,
        "harness_commit": "b" * 12,
        "suite_hash": "sha256:x",
        "runs_per_task": 3,
        "solved_threshold": 2,
        "fit_rounds": 1,
        "tutor": {"calls": 0, "usd": 0.0},
        "control_evaluations": 1,
        "splits": {},
        "failure_profile": {},
        "frozen_tag": "fit-2026-10-22-1",
        "control": {
            "tasks": 6,
            "unmeasured": 0,
            "before": {"solved": 0, "wilson95": [0.0, 0.39]},
            "after": {"solved": 0, "wilson95": [0.0, 0.39]},
            "difference95": [0.0, 0.0],
            "delegation": {"bugfix": "cloud"},
            "context_efficiency": 0.0,
        },
    }
    text = render_markdown(report)
    assert "Context efficiency: 0.0 control successes per 1,000 harness tokens" in text
    assert "n/a" not in text


@pytest.mark.parametrize(
    "live",
    [
        ModelInfo(provider="fake", model="llama3:8b", digest="sha256:m"),
        ModelInfo(provider="fake", model="qwen3:8b", digest="sha256:new-weights"),
    ],
)
def test_final_refuses_a_live_model_other_than_the_baseline(
    project: Project, monkeypatch: pytest.MonkeyPatch, live: ModelInfo
) -> None:
    control = use_fake_control(project, monkeypatch)
    with pytest.raises(UserError, match="not the baseline's"):
        project.final_report(docker_client=None, model_client=lambda: None, model_info=live)  # type: ignore[arg-type]
    assert control.runs == []


def test_final_refuses_a_suite_that_changed_while_preparing(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = FakeControl(project)

    def runner_while_tasks_are_mined(self: Project, *args: Any) -> FakeControl:
        monkeypatch.setattr(Project, "tasks", lambda self: CONTROL[:3] + TRAIN)
        return control

    monkeypatch.setattr(Project, "_control_runner", runner_while_tasks_are_mined)
    with pytest.raises(UserError, match="changed while"):
        project.final_report(docker_client=None, model_client=lambda: None, model_info=LIVE)  # type: ignore[arg-type]
    assert control.runs == []
    assert control_evaluations(project.state) == 0


@pytest.mark.parametrize("change", ["settings", "baseline digest"])
def test_final_refuses_a_config_or_baseline_that_changed_while_preparing(
    project: Project, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    control = FakeControl(project)

    def runner_while_things_change(self: Project, *args: Any) -> FakeControl:
        if change == "settings":  # new settings and a new baseline that matches them
            config = self.config_path
            config.write_text(config.read_text().replace("runs_per_task: 3", "runs_per_task: 5"))
            fake_baseline(self, self.harness.head(), runs_per_task=5)
        else:
            fake_baseline(self, self.harness.head(), model={"id": "qwen3:8b", "digest": "sha256:z"})
        return control

    monkeypatch.setattr(Project, "_control_runner", runner_while_things_change)
    with pytest.raises(UserError, match="changed while"):
        project.final_report(docker_client=None, model_client=lambda: None, model_info=LIVE)  # type: ignore[arg-type]
    assert control.runs == []
