import json
import sys
from pathlib import Path

import pytest
from tests.fixtures.toyrepo.build import build
from tests.integration.host_runner import HostRunner
from typer.testing import CliRunner

from saddlefit import cli
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
    assert (repo / ".saddlefit" / "harness" / "harness.json").exists()

    # the toy repository is Saddlefit's own; its tests may run on the host here
    monkeypatch.setattr(cli, "mining_runner", lambda project: HostRunner(python=sys.executable))
    mined = invoke(repo, "tasks", "mine", monkeypatch=monkeypatch)
    assert mined["viable"] == 8  # type: ignore[index]
    rows = invoke(repo, "tasks", "list", monkeypatch=monkeypatch)
    assert len(rows) == 8  # type: ignore[arg-type]
    assert {r["split"] for r in rows} <= {"train", "held_out", "control"}  # type: ignore[union-attr]

    task_id = rows[0]["id"]  # type: ignore[index]
    shown = invoke(repo, "tasks", "show", task_id, monkeypatch=monkeypatch)
    assert shown["meta"]["id"] == task_id  # type: ignore[index]
    assert "canary" not in json.dumps(shown).lower()  # the sealed part is never shown


def run_main(monkeypatch: pytest.MonkeyPatch, *args: str) -> object:
    monkeypatch.setattr("sys.argv", ["saddlefit", *args])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value.code


def test_user_errors_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = build(tmp_path / "repo")
    monkeypatch.chdir(repo)
    assert run_main(monkeypatch, "tasks", "list") == 1  # not initialised
    Project(repo).init()
    assert run_main(monkeypatch, "init") == 1  # already initialised
    assert run_main(monkeypatch, "tasks", "show", "no-such-task") == 1
    assert run_main(monkeypatch, "tasks", "mine", "--since", "yesterday") == 1
    (repo / ".saddlefit" / "saddlefit.yaml").write_text("schema_version: 1\nbogus: 1\n")
    assert run_main(monkeypatch, "tasks", "list") == 1  # an invalid config is a user error
    not_git = tmp_path / "plain"
    not_git.mkdir()
    monkeypatch.chdir(not_git)
    assert run_main(monkeypatch, "init") == 1


def test_mining_without_docker_is_an_environment_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = build(tmp_path / "repo")
    Project(repo).init()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    assert run_main(monkeypatch, "tasks", "mine") == 3
