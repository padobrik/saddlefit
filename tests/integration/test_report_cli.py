"""``saddlefit report``: progress since the baseline, from the run records (B15)."""

import json
from pathlib import Path

import pytest
from tests.fixtures.toyrepo.build import build
from typer.testing import CliRunner

from saddlefit.cli import app
from saddlefit.project import Project
from saddlefit.tasks.suite import load_lock

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    repo = build(tmp_path / "repo")
    project = Project(repo)
    project.init()
    monkeypatch.chdir(repo)
    return project


def fake_baseline(project: Project, commit: str, **changes: object) -> None:
    project.reports.mkdir(parents=True, exist_ok=True)
    config = project.config()
    report = {
        "kind": "baseline",
        "model": {"id": config.student.model, "digest": "sha256:m"},
        "harness_commit": commit,
        "suite_hash": load_lock(project.suite, config.splits).suite_hash(),
        "runs_per_task": config.evaluation.runs_per_task,
        "solved_threshold": config.evaluation.solved_threshold,
        **changes,
    }
    (project.reports / "baseline-20261022T000000Z.json").write_text(json.dumps(report))


def test_report_needs_a_baseline(project: Project) -> None:
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 1
    assert "run `saddlefit baseline`" in str(result.exception)


def test_report_writes_a_progress_report(project: Project) -> None:
    fake_baseline(project, project.harness.head())
    result = runner.invoke(app, ["--json", "report"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["kind"] == "progress"
    assert report["model"] == {"id": project.config().student.model, "digest": "sha256:m"}
    assert set(report["splits"]) == {"train", "held_out"}  # never control
    assert report["control_evaluations"] == 0
    assert report["tutor"] == {"calls": 0, "usd": 0.0}
    written = sorted(p.name for p in project.reports.glob("progress-*"))
    assert [Path(n).suffix for n in written] == [".json", ".md"]
    markdown = runner.invoke(app, ["report"]).output
    assert "# Progress report" in markdown
    assert "| train |" in markdown


def test_report_with_a_baseline_from_a_lost_harness(project: Project) -> None:
    fake_baseline(project, "0123456789abcdef")
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 1
    assert "not in the harness store" in str(result.exception)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"runs_per_task": 5}, "evaluation settings changed"),
        ({"solved_threshold": 1}, "evaluation settings changed"),
        ({"suite_hash": "sha256:older-suite"}, "task suite changed"),
        ({"model": {"id": "llama3:8b", "digest": None}}, "student model changed"),
    ],
)
def test_report_refuses_a_baseline_that_is_not_comparable(
    project: Project, changes: dict[str, object], message: str
) -> None:
    fake_baseline(project, project.harness.head(), **changes)
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 1
    assert message in str(result.exception)
