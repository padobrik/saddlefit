import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import docker
import pytest
from tests.fixtures.toyrepo.build import build
from tests.integration.host_runner import HostRunner
from tests.integration.test_mining import add_fix
from typer.testing import CliRunner

from saddlefit import cli
from saddlefit.cli import app
from saddlefit.errors import UserError
from saddlefit.harness.store import HarnessStore, HarnessStoreError
from saddlefit.project import Project, remove_tree

pytestmark = pytest.mark.integration
runner = CliRunner()


def git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


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
    listed = invoke(repo, "tasks", "list", monkeypatch=monkeypatch)
    for row in rows:  # type: ignore[union-attr]
        token = (repo / ".saddlefit/tasks/sealed" / row["id"] / "canary.txt").read_text().strip()
        assert token.startswith("SADDLEFIT-CANARY-")
        assert token not in json.dumps(shown)  # the sealed part is never shown
        assert token not in json.dumps(listed)


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


class _DeadDocker:
    """docker-py stand-in whose daemon does not answer; no real socket is opened."""

    def __init__(self) -> None:
        self.closed = False

    def ping(self) -> bool:
        raise docker.errors.DockerException("daemon not running")

    def close(self) -> None:
        self.closed = True


def test_mining_without_docker_is_an_environment_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = build(tmp_path / "repo")
    Project(repo).init()
    monkeypatch.chdir(repo)
    dead = _DeadDocker()
    monkeypatch.setattr(docker, "from_env", lambda: dead)
    assert run_main(monkeypatch, "tasks", "mine") == 3
    assert dead.closed


def test_a_failed_init_can_be_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = build(tmp_path / "repo")
    project = Project(repo)

    def broken_harness(self: object) -> str:
        raise HarnessStoreError("git is not available")

    monkeypatch.setattr(HarnessStore, "init", broken_harness)
    with pytest.raises(HarnessStoreError):
        project.init()
    assert not project.config_path.exists()
    monkeypatch.undo()
    shutil.rmtree(project.harness.path, ignore_errors=True)
    assert project.init()["project"] == "repo"


def test_a_lost_suite_lock_is_an_error_not_a_new_salt(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    project = Project(repo)
    project.init()
    project.mine(HostRunner(python=sys.executable), limit=2)
    (project.suite.root / "suite.lock").unlink()
    with pytest.raises(UserError, match=r"suite\.lock"):
        project.mine(HostRunner(python=sys.executable), limit=2)


def test_test_paths_are_quoted_in_the_checks(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    add_fix(repo, "tests/test $(touch pwned) x.py")
    project = Project(repo)
    project.init()
    _, metas = project.mine(HostRunner(python=sys.executable), limit=1)
    assert metas
    quoted = "'tests/test $(touch pwned) x.py'"
    visible = metas[0].visible_check or ""
    # the new file is not in the parent, so the visible check runs its directory instead
    assert "$(touch pwned)" not in visible or quoted in visible
    check_sh = next((project.suite.sealed_dir / metas[0].id).glob("check.sh")).read_text()
    assert quoted in check_sh


@pytest.mark.parametrize(("release", "flagged"), [("2099-01-01", True), ("2000-01-01", False)])
def test_contamination_follows_the_configured_release_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release: str, flagged: bool
) -> None:
    repo = build(tmp_path / "repo")
    project = Project(repo)
    project.init()
    _, metas = project.mine(HostRunner(python=sys.executable), limit=2)
    assert metas
    assert all(m.committed is not None for m in metas)
    rows = invoke(repo, "tasks", "list", monkeypatch=monkeypatch)
    assert not any(r["contaminated"] for r in rows)  # type: ignore[union-attr,index]
    # the release date is set after mining, and the listing follows it without re-mining
    config = project.config_path.read_text(encoding="utf-8")
    project.config_path.write_text(
        config.replace("release_date: null", f"release_date: {release}"), encoding="utf-8"
    )
    rows = invoke(repo, "tasks", "list", monkeypatch=monkeypatch)
    assert {r["contaminated"] for r in rows} == {flagged}  # type: ignore[union-attr,index]


def test_a_corrupt_lock_or_meta_is_a_user_error(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    project = Project(repo)
    project.init()
    _, metas = project.mine(HostRunner(python=sys.executable), limit=1)
    meta = project.suite.public_dir / metas[0].id / "meta.yaml"
    meta.write_text("schema_version: [", encoding="utf-8")
    with pytest.raises(UserError, match=r"meta\.yaml"):
        project.tasks()
    project.suite.lock.write_text("{not json", encoding="utf-8")
    with pytest.raises(UserError, match=r"suite\.lock"):
        project.tasks()


def test_the_lock_is_replaced_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = build(tmp_path / "repo")
    project = Project(repo)
    project.init()
    before = project.suite.lock.read_text(encoding="utf-8")

    real_write = Path.write_text

    def dies_halfway_through_the_lock(self: Path, *args: object, **kwargs: object) -> int:
        if "suite.lock" in self.name:
            self.open("w").close()  # the write had truncated the file when the disk filled up
            raise OSError("disk full")
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", dies_halfway_through_the_lock)
    with pytest.raises(OSError, match="disk full"):
        project.mine(HostRunner(python=sys.executable), limit=1)
    monkeypatch.undo()
    assert project.suite.lock.read_text(encoding="utf-8") == before


def test_init_recovers_from_a_half_built_harness_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = build(tmp_path / "repo")
    project = Project(repo)
    real_git = HarnessStore._git

    def fails_on_commit(self: HarnessStore, *args: str, **kwargs: object) -> str:
        if args[0] == "commit":
            raise HarnessStoreError("git commit failed: boom")
        return real_git(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(HarnessStore, "_git", fails_on_commit)
    with pytest.raises(HarnessStoreError):
        project.init()
    assert (project.harness.path / ".git").exists()  # left half-built
    monkeypatch.undo()
    assert project.init()["harness_commit"]  # retried without any manual cleanup


def test_init_reuses_a_finished_harness_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = build(tmp_path / "repo")
    project = Project(repo)
    real_write = Path.write_text

    def config_write_fails(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == "saddlefit.yaml":
            raise OSError("read-only")
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", config_write_fails)
    with pytest.raises(OSError, match="read-only"):
        project.init()
    first = project.harness.head()
    monkeypatch.undo()
    assert project.init()["harness_commit"] == first


def test_the_visible_check_skips_a_directory_without_tests(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    add_fix(repo, "src/toykit/test_extra.py")  # src/toykit exists in the parent, but has no tests
    project = Project(repo)
    project.init()
    _, metas = project.mine(HostRunner(python=sys.executable), limit=1)
    assert metas
    check = metas[0].visible_check or ""
    assert "src/toykit" not in check
    if os.name == "nt":
        return  # the check is a command for the Linux sandbox (":" in PYTHONPATH, bash)
    # what it does run collects tests on the parent
    subprocess.run(["git", "checkout", "-q", metas[0].repo.base_commit], cwd=repo, check=True)
    command = check.replace("python", shlex.quote(sys.executable), 1)
    result = subprocess.run(["bash", "-c", command], cwd=repo, check=False)
    assert result.returncode == 0


def test_removing_a_store_handles_read_only_git_objects(tmp_path: Path) -> None:
    # git writes its object files read-only; Windows refuses to delete those as they are
    store = tmp_path / "harness"
    (store / ".git" / "objects" / "ab").mkdir(parents=True)
    obj = store / ".git" / "objects" / "ab" / "cdef"
    obj.write_text("x", encoding="utf-8")
    obj.chmod(stat.S_IREAD)
    remove_tree(store)
    assert not store.exists()


def test_init_keeps_a_store_it_cannot_read(tmp_path: Path) -> None:
    repo = build(tmp_path / "repo")
    project = Project(repo)
    project.harness.init()  # a finished store, but its branch is not where init expects it
    subprocess.run(
        ["git", "branch", "-m", "main", "elsewhere"],
        cwd=project.harness.path,
        env=git_env(project.harness.path),
        check=True,
    )
    with pytest.raises(HarnessStoreError):
        project.init()
    assert project.harness.path.exists()
    assert not project.harness.is_empty()


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs privileges on Windows")
def test_remove_tree_reraises_other_errors_and_leaves_link_targets_alone(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("x", encoding="utf-8")
    mode = target.stat().st_mode
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError, match="link"):  # the message differs across Python versions
        remove_tree(link)
    assert (target / "keep.txt").exists()
    assert target.stat().st_mode == mode
