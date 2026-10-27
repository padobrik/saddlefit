"""``tasks mutate`` end to end: a bug injected, written up, accepted, calibrated, kept (A11)."""

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.fixtures.toyrepo.build import build
from tests.integration.host_runner import HostRunner

from saddlefit.config import dump_config
from saddlefit.project import Project
from saddlefit.providers.base import ModelReply, ToolCall, Usage
from saddlefit.providers.fake import FakeClient
from saddlefit.tasks.mining import _git_env
from saddlefit.tasks.mutation import ISSUE_TOOL, MUTATION_REF

pytestmark = pytest.mark.docker


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=_git_env()
    ).stdout.strip()


def test_an_injected_bug_the_student_sometimes_fixes_is_kept(
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
    head = git(repo, "rev-parse", "HEAD")
    originals = {
        p: git(repo, "show", f"{head}:{p}") + "\n"
        for p in git(repo, "ls-files", "src").splitlines()
        if p.endswith(".py")
    }
    issue = ModelReply(
        tool_calls=(
            ToolCall(id="i", name=ISSUE_TOOL, arguments={"task_md": "# A helper misbehaves\n"}),
        ),
        usage=Usage(prompt_tokens=800, completion_tokens=60),
    )
    runs = {"n": 0}

    def student(messages: Any, tools: Any, params: Any) -> ModelReply:
        if any(m.role == "tool" for m in messages):
            return ModelReply(
                tool_calls=(ToolCall(id="f", name="finish", arguments={"summary": "x"}),)
            )
        runs["n"] += 1
        if runs["n"] not in (2, 4):  # fixes two of five calibration runs
            return ModelReply(
                tool_calls=(ToolCall(id="f", name="finish", arguments={"summary": "x"}),)
            )
        # writing every source back as it was at HEAD undoes whichever bug went in
        writes = tuple(
            ToolCall(id=f"w{i}", name="write_file", arguments={"path": p, "content": c})
            for i, (p, c) in enumerate(sorted(originals.items()))
        )
        return ModelReply(tool_calls=writes)

    report = project.mutate_tasks(
        count=1,
        docker_client=docker_client,
        generator_client=FakeClient(lambda m, t, p: issue),
        model_client=lambda: FakeClient(student),
        model_info=FakeClient(student).info(),
        runner=HostRunner(python=sys.executable),
    )
    assert report.kept, [a.reason for a in report.attempts]
    assert report.attempts[-1].passes == 2
    task_id = report.kept[0]
    assert task_id.startswith("mut-bugfix-")
    meta = next(m for m in project.tasks() if m.id == task_id)
    assert (meta.source, meta.type) == ("mutation", "bugfix")
    assert meta.repo.base_commit == git(repo, "rev-parse", f"{MUTATION_REF}/{task_id}")
    assert git(repo, "rev-parse", "HEAD") == head  # the user's branch never moved
    assert (project.suite.sealed_dir / task_id / "reference.patch").exists()
