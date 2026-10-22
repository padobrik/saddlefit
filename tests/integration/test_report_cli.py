"""``saddlefit report``: progress since the baseline, from the run records (B15)."""

import json
from pathlib import Path

import pytest
from tests.fixtures.toyrepo.build import build
from typer.testing import CliRunner

from saddlefit.cli import app
from saddlefit.project import Project

pytestmark = pytest.mark.integration
runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    repo = build(tmp_path / "repo")
    project = Project(repo)
    project.init()
    monkeypatch.chdir(repo)
    return project


def fake_baseline(project: Project, commit: str) -> None:
    project.reports.mkdir(parents=True, exist_ok=True)
    report = {
        "kind": "baseline",
        "model": {"id": "qwen3:8b", "digest": "sha256:m"},
        "harness_commit": commit,
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
    assert report["model"] == {"id": "qwen3:8b", "digest": "sha256:m"}
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
