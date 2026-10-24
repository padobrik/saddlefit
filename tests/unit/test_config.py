from pathlib import Path

import pytest
import yaml

from saddlefit import __version__
from saddlefit.config import Config, ConfigError, dump_config, load_config

SPEC_EXAMPLE = """\
schema_version: 2
project: my-service
student:
  provider: openai-compatible
  base_url: http://localhost:11434/v1
  model: qwen3:8b
  tool_mode: auto
  temperature: 0.2
  seed: 7
  context_length: 32768
  release_date: null
tutor:
  provider: anthropic
  model: claude-opus-5-5
  max_calls_per_round: 20
  max_usd_per_round: 5.0
  prices_usd_per_mtok: {input: 0.0, output: 0.0}
generator:
  provider: anthropic
  model: claude-fable-5-1
evaluation:
  runs_per_task: 3
  solved_threshold: 2
  timeout_seconds: 900
  protected_paths: ["tests/**", "**/test_*.py", "**/*_test.py"]
splits:
  train: 0.5
  held_out: 0.25
  control: 0.25
sandbox:
  image: ghcr.io/OWNER/saddlefit-sandbox:0.1.0
  cpus: 2
  memory: 4g
  pids_limit: 512
fast_loop:
  max_variants: 3
  runs_per_variant: 2
  time_budget_minutes: 10
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "saddlefit.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_spec_example_loads(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, SPEC_EXAMPLE))
    assert config.project == "my-service"
    assert config.evaluation.solved_threshold == 2
    assert config.splits.held_out == 0.25


def test_minimal_config_uses_defaults(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "schema_version: 2\nproject: p\n"))
    assert config == Config(schema_version=3, project="p")


def test_round_trip(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, SPEC_EXAMPLE))
    assert load_config(write(tmp_path, dump_config(config))) == config


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("schema_version: 2\nproject: p\nbogus: 1\n", "bogus: unknown key"),
        ("schema_version: 2\nproject: p\nstudent:\n  modle: x\n", "student.modle: unknown key"),
        ("schema_version: 0\nproject: p\n", "schema_version 0 is not supported"),
        ("project: p\n", "missing or non-integer schema_version"),
        (
            "schema_version: 2\nproject: p\nsplits: {train: 0.5, held_out: 0.5, control: 0.5}\n",
            "sum to 1.0",
        ),
        (
            "schema_version: 2\nproject: p\nevaluation: {runs_per_task: 3, solved_threshold: 4}\n",
            "cannot exceed",
        ),
        ("schema_version: 2\nproject: p\nsandbox: {memory: lots}\n", "sandbox.memory"),
        ("schema_version: 3\nproject: p\ngenerator: {max_calls: 0}\n", "generator.max_calls"),
        ("schema_version: 3\nproject: p\ngenerator: {max_usd: -1}\n", "generator.max_usd"),
        (
            "schema_version: 3\nproject: p\ngenerator: {prices_usd_per_mtok: {output: -2}}\n",
            "generator.prices_usd_per_mtok.output",
        ),
        ("- a\n- b\n", "must be a mapping"),
        ("project: [unclosed\n", "not valid YAML"),
    ],
)
def test_invalid_configs_have_clear_errors(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, text))
    assert message in str(exc.value)


def test_missing_file_points_to_init(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="saddlefit init"):
        load_config(tmp_path / "absent.yaml")


def test_dump_is_safe_yaml() -> None:
    assert yaml.safe_load(dump_config(Config(schema_version=3, project="p")))["project"] == "p"


@pytest.mark.parametrize(
    "text",
    [
        "schema_version: 2\nproject: p\ntutor: {max_usd_per_round: .inf}\n",
        "schema_version: 2\nproject: p\nstudent: {temperature: .nan}\n",
        "schema_version: 2\nproject: p\nfast_loop: {time_budget_minutes: .inf}\n",
        "schema_version: 3\nproject: p\ngenerator: {max_usd: .inf}\n",
        "schema_version: 3\nproject: p\ngenerator: {prices_usd_per_mtok: {input: .nan}}\n",
    ],
)
def test_non_finite_limits_are_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError, match="finite"):
        load_config(write(tmp_path, text))


def test_unquoted_release_date(tmp_path: Path) -> None:
    config = load_config(
        write(tmp_path, "schema_version: 2\nproject: p\nstudent: {release_date: 2026-10-20}\n")
    )
    assert str(config.student.release_date) == "2026-10-20"
    assert "release_date: '2026-10-20'" in dump_config(config)
    assert load_config(write(tmp_path, dump_config(config))) == config


def test_non_utf8_file_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "saddlefit.yaml"
    path.write_bytes(b"project: \xff\xfe\n")
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(path)


def test_unreadable_path_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path)


def test_the_default_sandbox_image_is_the_one_released_with_this_version() -> None:
    config = Config(schema_version=3, project="p")
    assert config.sandbox.image == f"ghcr.io/padobrik/saddlefit-sandbox:{__version__}"
    schema = Path(__file__).parents[2] / "schemas" / "saddlefit-config.schema.json"
    assert "saddlefit-sandbox:" not in schema.read_text(encoding="utf-8")  # no pin to go stale


def test_a_written_config_keeps_its_image_across_releases(tmp_path: Path) -> None:
    # init writes the image out, so a project keeps its image when the default moves on
    written = yaml.safe_load(dump_config(Config(schema_version=3, project="p")))
    assert written["sandbox"]["image"] == f"ghcr.io/padobrik/saddlefit-sandbox:{__version__}"
    old = (
        "schema_version: 2\nproject: p\n"
        "sandbox:\n  image: ghcr.io/padobrik/saddlefit-sandbox:0.1.0\n"
    )
    assert load_config(write(tmp_path, old)).sandbox.image.endswith(":0.1.0")


def test_a_v2_config_is_read_as_v3_with_the_default_generator_budget(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, SPEC_EXAMPLE.replace("input: 0.0", "input: 4.0")))
    assert config.schema_version == 3
    assert config.tutor.prices_usd_per_mtok.input == 4.0
    assert (config.generator.max_calls, config.generator.max_usd) == (10, 5.0)
    assert config.generator.prices_usd_per_mtok.output == 0.0
    assert yaml.safe_load(dump_config(config))["schema_version"] == 3


def test_a_v1_config_is_no_longer_read(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="schema_version 1 is not supported"):
        load_config(write(tmp_path, "schema_version: 1\nproject: p\n"))


def test_generator_budget_round_trips(tmp_path: Path) -> None:
    text = (
        "schema_version: 3\nproject: p\ngenerator:\n  max_calls: 4\n  max_usd: 2.5\n"
        "  prices_usd_per_mtok: {input: 5, output: 25}\n"
    )
    config = load_config(write(tmp_path, text))
    assert config.generator.max_calls == 4
    assert config.generator.prices_usd_per_mtok.output == 25
    assert load_config(write(tmp_path, dump_config(config))) == config


def test_cache_prices_round_trip(tmp_path: Path) -> None:
    text = (
        "schema_version: 2\nproject: p\ntutor:\n  prices_usd_per_mtok:\n"
        "    {input: 4, output: 20, cache_write_5m: 5, cache_write_1h: 8, cache_read: 0.2}\n"
    )
    config = load_config(write(tmp_path, text))
    assert config.tutor.prices_usd_per_mtok.cache_read == 0.2
    assert load_config(write(tmp_path, dump_config(config))) == config
