"""``tasks generate`` end to end: generated, accepted in Docker, calibrated, kept (B13)."""

from pathlib import Path
from typing import Any

import pytest
from tests.fixtures.toyrepo.build import build
from tests.sandbox.test_acceptance import HIDDEN, PATCH, SOURCE

from saddlefit.config import dump_config
from saddlefit.loops.generate import CALIBRATION_RUNS
from saddlefit.project import Project
from saddlefit.providers.base import ModelReply, ToolCall, Usage
from saddlefit.providers.fake import FakeClient
from saddlefit.storage.runs import find_runs
from saddlefit.tasks.generator import SUBMIT_TOOL

pytestmark = pytest.mark.docker

ANSWER = ModelReply(
    tool_calls=(
        ToolCall(
            id="g1",
            name=SUBMIT_TOOL.name,
            arguments={
                "task_md": "# Add shout\n\n`toykit.shout.shout(text)` upper-cases it and adds !.",
                "hidden_tests": [{"path": p, "content": c} for p, c in HIDDEN.items()],
                "reference_patch": PATCH,
                "visible_check": "true",
            },
        ),
    ),
    usage=Usage(prompt_tokens=2_000, completion_tokens=500),
)
WRITE = ToolCall(
    id="s1",
    name="write_file",
    arguments={"path": SOURCE, "content": "def shout(text):\n    return text.upper() + '!'\n"},
)
FINISH = ToolCall(id="s2", name="finish", arguments={"summary": "done"})


def test_a_task_the_student_sometimes_solves_is_kept(
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
    runs = {"n": 0}

    def student(messages: Any, tools: Any, params: Any) -> ModelReply:
        if any(m.role == "tool" for m in messages):
            return ModelReply(tool_calls=(FINISH,))
        runs["n"] += 1
        # solves the second and the fourth of five runs: 40%, inside 20-80%
        return ModelReply(tool_calls=(WRITE if runs["n"] in (2, 4) else FINISH,))

    report = project.generate_tasks(
        kind="feature",
        count=1,
        docker_client=docker_client,
        generator_client=FakeClient([ANSWER]),
        model_client=lambda: FakeClient(student),
    )
    assert report.calls == 1
    assert [a.passes for a in report.attempts] == [2]
    assert len(report.kept) == 1
    task_id = report.kept[0]
    meta = next(m for m in project.tasks() if m.id == task_id)
    assert (meta.source, meta.type) == ("generated", "feature")
    assert meta.split in {"train", "held_out"}
    assert (project.suite.sealed_dir / task_id / "reference.patch").exists()
    assert project.task_text(task_id).startswith("# Add shout")
    assert len(find_runs(project.state / "calibration")) == CALIBRATION_RUNS
