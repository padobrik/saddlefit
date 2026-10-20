import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.test_compact import DENSE_70B, QWEN3_8B
from typer.testing import CliRunner

from saddlefit import cli
from saddlefit.cli import ExitCode, app
from saddlefit.compact import ModelFacts, parse_ollama_show
from saddlefit.config import Config, dump_config
from saddlefit.doctor import (
    Check,
    DoctorReport,
    measure_speed,
    probe_tools,
    render,
    run_doctor,
)
from saddlefit.providers.base import GenParams, Message, ModelReply, ToolCall, ToolSpec, Usage
from saddlefit.providers.fake import FakeClient

NOW = datetime(2026, 10, 21, tzinfo=UTC)


def weather_client(native_ok: int, json_ok: int) -> FakeClient:
    """Answers the speed prompt, then gets ``native_ok``/``json_ok`` probes right."""
    counts = {"native": 0, "json": 0}

    def script(
        messages: Sequence[Message], tools: Sequence[ToolSpec] | None, params: GenParams
    ) -> ModelReply:
        if tools is None:
            return ModelReply(text="1 2 3", usage=Usage(completion_tokens=100), latency_s=2.0)
        city = messages[-1].content.split(" in ")[1].split(" right")[0]
        counts[params.tool_mode] += 1
        good = counts[params.tool_mode] <= (native_ok if params.tool_mode == "native" else json_ok)
        args = {"city": city if good else "nowhere"}
        return ModelReply(tool_calls=(ToolCall(id="c", name="get_weather", arguments=args),))

    return FakeClient(script)


def test_probes_count_only_correct_calls() -> None:
    assert probe_tools(weather_client(7, 0), "native") == 7


def run(client: FakeClient, facts: ModelFacts | None = None) -> DoctorReport:
    return run_doctor(
        client,
        docker_status=lambda: [Check("docker", True, "Docker 28")],
        facts=lambda: facts,
        context_length=32768,
        now=lambda: NOW,
    )


def test_native_tool_calling_when_nine_probes_succeed() -> None:
    report = run(weather_client(9, 0), parse_ollama_show(QWEN3_8B))
    assert report.record is not None
    assert (report.record.tool_mode, report.record.native_probes) == ("native", 9)
    assert report.record.json_probes is None
    assert report.record.tokens_per_second == 50.0
    assert report.tier == "M"
    assert report.environment_ok


def test_json_mode_when_native_probes_fall_short() -> None:
    report = run(weather_client(8, 10), parse_ollama_show(QWEN3_8B))
    assert report.record is not None
    assert (report.record.tool_mode, report.record.json_probes) == ("json", 10)
    assert report.tier == "M"


def test_an_unreachable_runner_is_an_environment_failure() -> None:
    report = run(FakeClient([]))
    assert not report.environment_ok
    assert report.record is None
    assert report.checks[-1].name == "runner"
    assert report.checks[-1].ok is False


def test_render_names_the_failed_conditions() -> None:
    text = render(run(weather_client(0, 0), parse_ollama_show(DENSE_70B)))
    assert "tier: not compact" in text
    assert "failed tool use" in text


def canned(tier: str, *, environment_ok: bool = True) -> DoctorReport:
    report = run(weather_client(10, 0), parse_ollama_show(QWEN3_8B))
    report.tier = tier  # type: ignore[assignment]
    report.environment_ok = environment_ok
    return report


@pytest.mark.parametrize(
    ("tier", "environment_ok", "code"),
    [
        ("M", True, ExitCode.OK),
        ("not compact", True, ExitCode.USER_ERROR),
        ("M", False, ExitCode.ENVIRONMENT_ERROR),
    ],
)
def test_cli_exit_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tier: str, environment_ok: bool, code: int
) -> None:
    monkeypatch.setattr(
        cli, "doctor_for", lambda student, sandbox: canned(tier, environment_ok=environment_ok)
    )
    result = CliRunner().invoke(app, ["doctor", "--config", str(tmp_path / "none.yaml")])
    exit_code = result.exit_code
    if isinstance(result.exception, cli.SaddlefitError):
        exit_code = result.exception.exit_code
    assert exit_code == code


def test_cli_stores_the_record_next_to_the_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / ".saddlefit" / "saddlefit.yaml"
    config.parent.mkdir()
    config.write_text(dump_config(Config(schema_version=1, project="p")), encoding="utf-8")
    monkeypatch.setattr(cli, "doctor_for", lambda student, sandbox: canned("M"))
    result = CliRunner().invoke(app, ["--json", "doctor", "--config", str(config)])
    assert result.exit_code == ExitCode.OK
    assert json.loads(result.output)["tier"] == "M"
    stored = json.loads((config.parent / "doctor.json").read_text(encoding="utf-8"))
    assert stored["schema_version"] == 1
    assert stored["tool_mode"] == "native"
    assert stored["digest"] == "sha256:fake"


def test_cli_without_a_config_stores_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "doctor_for", lambda student, sandbox: canned("M"))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == ExitCode.OK
    assert "tier: M (compact)" in result.output
    assert list(tmp_path.iterdir()) == []


def test_speed_is_measured_after_a_warm_up_call() -> None:
    latencies = iter([30.0, 2.0])  # the first call pays for loading the model

    def script(
        messages: Sequence[Message], tools: Sequence[ToolSpec] | None, params: GenParams
    ) -> ModelReply:
        return ModelReply(text="1 2", usage=Usage(completion_tokens=100), latency_s=next(latencies))

    client = FakeClient(script)
    assert measure_speed(client) == 50.0
    assert len(client.calls) == 2
