"""Required test 12: loop.yaml values are clamped, and no proposal can touch saddlefit.yaml."""

from pathlib import Path

import pytest

from saddlefit.config import Config, EvaluationConfig, StudentConfig
from saddlefit.harness.allowlist import disallowed, is_allowed
from saddlefit.harness.loop_config import (
    ContextConfig,
    LoopConfig,
    LoopConfigError,
    clamp,
    default_loop_config,
    dump_loop_config,
    load_loop_config,
)

SPEC_EXAMPLE = """\
schema_version: 1
max_steps: 40
tool_output:
  max_chars: 4000
context:
  budget_tokens: 24000
  harness_budget_tokens: 4000
finish_gate:
  reread_task: true
  run: "pytest -q"
  max_retries: 1
loop_detection:
  identical_calls: 3
reasoning:
  language: mixed
  effort: low
  latent_depth: null
"""

CONFIG = Config(
    schema_version=1,
    project="p",
    student=StudentConfig(context_length=16000),
    evaluation=EvaluationConfig(timeout_seconds=300),
)


def test_spec_example_loads_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "loop.yaml"
    path.write_text(SPEC_EXAMPLE, encoding="utf-8")
    loop = load_loop_config(path)
    assert loop == default_loop_config()
    path.write_text(dump_loop_config(loop), encoding="utf-8")
    assert load_loop_config(path) == loop


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("schema_version: 1\nmax_budget_usd: 99\n", "max_budget_usd"),
        ("schema_version: 1\nmax_steps: 0\n", "max_steps"),
        ("schema_version: 2\n", "schema_version"),
        ("- a\n", "mapping"),
        ("schema_version: [1\n", "cannot read"),
    ],
)
def test_invalid_loop_yaml(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "loop.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(LoopConfigError, match=message):
        load_loop_config(path)


def test_values_beyond_the_limits_are_clamped() -> None:
    loop = LoopConfig(
        schema_version=1,
        max_steps=10_000,
        context=ContextConfig(budget_tokens=100_000, harness_budget_tokens=50_000),
    )
    effective, notes = clamp(loop, CONFIG)
    assert effective.max_steps == 300
    assert effective.context.budget_tokens == 16000
    assert effective.context.harness_budget_tokens == 16000
    assert notes == [
        "max_steps 10000 clamped to 300",
        "context.budget_tokens 100000 clamped to 16000",
        "context.harness_budget_tokens 50000 clamped to 16000",
    ]


def test_values_within_the_limits_are_untouched() -> None:
    effective, notes = clamp(
        default_loop_config(),
        CONFIG.model_copy(update={"student": StudentConfig(context_length=32768)}),
    )
    assert effective == default_loop_config()
    assert notes == []


@pytest.mark.parametrize(
    "path",
    [
        "systemprompt.md",
        "tool_guidance.md",
        "loop.yaml",
        "EXPERIENCE.md",
        "skills/pydantic/SKILL.md",
        "tools/find_v1.py",
    ],
)
def test_allowlisted_paths(path: str) -> None:
    assert is_allowed(path)


@pytest.mark.parametrize(
    "path",
    [
        "saddlefit.yaml",
        "../saddlefit.yaml",
        "skills/../../saddlefit.yaml",
        "/etc/passwd",
        "tasks/sealed/x/check.sh",
        "skills",
        "tools",
        "./loop.yaml",
        "",
        ".git/config",
        "loop.yaml.bak",
    ],
)
def test_everything_else_is_refused(path: str) -> None:
    assert not is_allowed(path)


def test_a_change_touching_saddlefit_yaml_is_refused() -> None:
    assert disallowed(["loop.yaml", "saddlefit.yaml", "skills/a/SKILL.md"]) == ["saddlefit.yaml"]
    assert disallowed(["loop.yaml"]) == []
