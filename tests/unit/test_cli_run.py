"""``saddlefit run`` exits 4 when the task is not solved (B14 plus NOTES)."""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from saddlefit import cli
from saddlefit.loops.fast import FastResult, Probe
from saddlefit.project import Project, WorkResult

runner = CliRunner()


@pytest.mark.parametrize(("passed", "code"), [(True, 0), (False, 4)])
def test_run_exits_by_whether_the_check_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passed: bool, code: int
) -> None:
    probe = Probe("run1", passed, (), "")

    def work(self: Project, **kwargs: Any) -> WorkResult:
        return WorkResult(
            result=FastResult(passed=passed, diff="", probes=[probe]),
            answer=tmp_path / "a.patch",
            journal=None,
        )

    monkeypatch.setattr(Project, "work", work)
    monkeypatch.setattr(Project, "effective_loop", lambda self: (object(), []))
    monkeypatch.setattr(cli, "_project", lambda *a: Project(tmp_path))
    monkeypatch.setattr(cli, "_student", lambda project: (None, None))
    monkeypatch.setattr(cli, "_docker", lambda: None)
    result = runner.invoke(cli.app, ["run", "Do it.", "--check", "true"])
    assert result.exit_code == code, result.output
    assert ("passed" if passed else "FAILED (best attempt)") in result.output
