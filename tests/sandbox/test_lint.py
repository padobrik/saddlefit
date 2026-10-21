"""Tool scripts are linted by ruff and shellcheck inside the sandbox image."""

import pytest

from saddlefit.config import SandboxConfig
from saddlefit.harness.lint import SandboxLinter

pytestmark = pytest.mark.docker


@pytest.fixture
def linter(docker_client, sandbox_image: str) -> SandboxLinter:
    return SandboxLinter(docker_client, sandbox_image, SandboxConfig(memory="256m"))


def test_clean_scripts_pass(linter: SandboxLinter) -> None:
    scripts = {
        "tools/count.py": b"import sys\n\nprint(len(sys.argv))\n",
        "tools/run.sh": b'#!/bin/sh\nset -eu\necho "$1"\n',
    }
    assert linter.lint(scripts) == []


def test_ruff_problems_are_reported(linter: SandboxLinter) -> None:
    problems = linter.lint({"tools/bad.py": b"print(undefined_name)\n"})
    assert len(problems) == 1
    assert "F821" in problems[0]
    assert "tools/bad.py" in problems[0]


def test_shellcheck_problems_are_reported(linter: SandboxLinter) -> None:
    problems = linter.lint({"tools/bad.sh": b"#!/bin/sh\nrm -rf $1/x\n"})
    assert len(problems) == 1
    assert "SC2086" in problems[0]
