"""``saddlefit run`` end to end: a failed task rescued by a playbook variant, journaled (A6)."""

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from tests.fixtures.toyrepo.build import build

from saddlefit.config import dump_config
from saddlefit.harness.store import MAIN
from saddlefit.project import Project
from saddlefit.providers.base import ModelReply, ToolCall
from saddlefit.providers.fake import FakeClient
from saddlefit.storage.journal import read_entries
from saddlefit.tasks.mining import _git_env

pytestmark = pytest.mark.docker

FINISH = ToolCall(id="f", name="finish", arguments={"summary": "done"})
WRITE = ToolCall(id="w", name="write_file", arguments={"path": "DONE.txt", "content": "ok\n"})


def student(messages: Any, tools: Any, params: Any) -> ModelReply:
    """Gets it right only when it is shown the repository's files: the repo-map variant."""
    if any(m.role == "tool" for m in messages):
        return ModelReply(tool_calls=(FINISH,))
    shown_files = "## Files in the repository" in messages[1].content
    return ModelReply(tool_calls=(WRITE,) if shown_files else (FINISH,))


def test_run_rescues_a_task_with_a_playbook_variant_on_probation(
    tmp_path: Path, docker_client, sandbox_image: str
) -> None:
    repo = build(tmp_path / "repo")
    project = Project(repo)
    project.init(model="fake-student")
    config = project.config()
    project.config_path.write_text(
        dump_config(
            config.model_copy(
                update={
                    "sandbox": config.sandbox.model_copy(
                        update={"image": sandbox_image, "memory": "512m"}
                    ),
                    "evaluation": config.evaluation.model_copy(update={"timeout_seconds": 120}),
                }
            )
        ),
        encoding="utf-8",
    )
    main_before = project.harness.head(MAIN)
    work = project.work(
        task_text="Create DONE.txt.",
        check="test -f DONE.txt",
        docker_client=docker_client,
        model_client=lambda: FakeClient(student),
        model_info=FakeClient(student).info(),
    )
    assert work.result.passed
    assert [e.id for e in work.result.helped] == ["repo-map"]
    assert "DONE.txt" in work.answer.read_text(encoding="utf-8")
    (entry,) = read_entries(project.state)
    assert (entry.status, entry.passed, entry.proposal.source) == ("probation", True, "fast_loop")
    assert "repo_map: true" in entry.proposal.patch
    assert len(entry.probe_run_ids) == len(work.result.probes)
    # probation: the harness store's main never moved, and the user's tree is untouched
    assert project.harness.head(MAIN) == main_before
    assert not (repo / "DONE.txt").exists()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    assert "DONE.txt" not in status.stdout


def test_the_final_check_gets_its_own_time(
    tmp_path: Path, docker_client, sandbox_image: str
) -> None:
    """A student that uses most of the run must not starve the check that judges it."""
    repo = build(tmp_path / "repo")
    project = Project(repo)
    project.init(model="fake-student")
    config = project.config()
    project.config_path.write_text(
        dump_config(
            config.model_copy(
                update={
                    "sandbox": config.sandbox.model_copy(
                        update={"image": sandbox_image, "memory": "512m"}
                    ),
                    "evaluation": config.evaluation.model_copy(update={"timeout_seconds": 12}),
                }
            )
        ),
        encoding="utf-8",
    )

    def slow_student(messages: Any, tools: Any, params: Any) -> ModelReply:
        time.sleep(9)  # most of the twelve seconds
        return ModelReply(tool_calls=(FINISH,))

    work = project.work(
        task_text="Nothing to do.",
        check="sleep 5",  # longer than what the student leaves of the run
        docker_client=docker_client,
        model_client=lambda: FakeClient(slow_student),
        model_info=FakeClient(slow_student).info(),
    )
    assert work.result.probes[0].passed
