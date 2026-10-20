import json
from pathlib import Path

import pytest
from tests.fixtures.toyrepo.build import build
from typer.testing import CliRunner

from saddlefit.cli import app
from saddlefit.project import Project

pytestmark = pytest.mark.integration
runner = CliRunner()


def invoke(repo: Path, *args: str, monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_init_mine_list_show(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = build(tmp_path / "repo")
    init = invoke(repo, "init", "--model", "qwen3:8b", monkeypatch=monkeypatch)
    assert init["model"] == "qwen3:8b"  # type: ignore[index]
    assert (repo / ".saddlefit" / "saddlefit.yaml").exists()
    assert ".saddlefit/" in (repo / ".gitignore").read_text(encoding="utf-8")
    assert (repo / ".saddlefit" / "tasks" / "suite.lock").exists()

    mined = invoke(repo, "tasks", "mine", "--trust-local", monkeypatch=monkeypatch)
    assert mined["viable"] == 8  # type: ignore[index]
    rows = invoke(repo, "tasks", "list", monkeypatch=monkeypatch)
    assert len(rows) == 8  # type: ignore[arg-type]
    assert {r["split"] for r in rows} <= {"train", "held_out", "control"}  # type: ignore[union-attr]

    task_id = rows[0]["id"]  # type: ignore[index]
    shown = invoke(repo, "tasks", "show", task_id, monkeypatch=monkeypatch)
    assert shown["meta"]["id"] == task_id  # type: ignore[index]
    assert "canary" not in json.dumps(shown).lower()  # the sealed part is never shown


def test_user_errors_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = build(tmp_path / "repo")
    monkeypatch.chdir(repo)
    from saddlefit.cli import main  # noqa: PLC0415 - main maps errors onto exit codes

    for args in (["tasks", "list"], ["report"]):
        monkeypatch.setattr("sys.argv", ["saddlefit", *args])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
    Project(repo).init()
    monkeypatch.setattr("sys.argv", ["saddlefit", "init"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    not_git = tmp_path / "plain"
    not_git.mkdir()
    with pytest.raises(Exception, match="not a git repository"):
        Project(not_git).init()
