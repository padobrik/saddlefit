import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import docker
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
    docker_checks,
    measure_speed,
    probe_tools,
    render,
    run_doctor,
    served_context_check,
)
from saddlefit.providers.base import (
    GenParams,
    Message,
    ModelReply,
    ProviderError,
    ToolCall,
    ToolSpec,
    Usage,
)
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
    config.write_text(dump_config(Config(schema_version=2, project="p")), encoding="utf-8")
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


def test_a_runner_that_returns_garbage_is_an_environment_failure() -> None:
    def broken(
        messages: Sequence[Message], tools: Sequence[ToolSpec] | None, params: GenParams
    ) -> ModelReply:
        raise IndexError("no choices in the response")

    report = run(FakeClient(broken))
    assert not report.environment_ok
    assert report.checks[-1].name == "runner"
    assert "no choices" in report.checks[-1].detail


def test_a_probe_that_raises_counts_as_a_failed_probe() -> None:
    calls = {"n": 0}

    def flaky(
        messages: Sequence[Message], tools: Sequence[ToolSpec] | None, params: GenParams
    ) -> ModelReply:
        calls["n"] += 1
        if calls["n"] % 2:
            raise KeyError("tool_calls")
        city = messages[-1].content.split(" in ")[1].split(" right")[0]
        return ModelReply(
            tool_calls=(ToolCall(id="c", name="get_weather", arguments={"city": city}),)
        )

    assert probe_tools(FakeClient(flaky), "native") == 5


class _Docker:
    """A stub of docker-py: ping works; version or the image lookup can fail."""

    def __init__(self, fail: str) -> None:
        self.fail = fail
        self.images = self
        self.closed = False

    def ping(self) -> bool:
        return True

    def version(self) -> dict[str, str]:
        if self.fail == "version":
            raise docker.errors.APIError("version endpoint broke")
        return {"Version": "28"}

    def get(self, image: str) -> object:
        if self.fail == "image":
            raise docker.errors.APIError("image store broke")
        return object()

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("fail", ["version", "image"])
def test_docker_api_errors_become_failed_checks(monkeypatch: pytest.MonkeyPatch, fail: str) -> None:
    stub = _Docker(fail)
    monkeypatch.setattr(docker, "from_env", lambda: stub)
    checks = docker_checks("img")
    assert any(c.ok is False and "broke" in c.detail for c in checks)
    assert stub.closed


def test_a_failed_record_write_shows_the_report_and_exits_3(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / ".saddlefit" / "saddlefit.yaml"
    config.parent.mkdir()
    config.write_text(dump_config(Config(schema_version=2, project="p")), encoding="utf-8")
    monkeypatch.setattr(cli, "doctor_for", lambda student, sandbox: canned("M"))
    real_write = Path.write_text

    def read_only(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == "doctor.json":
            raise PermissionError("read-only")
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", read_only)
    result = CliRunner().invoke(app, ["doctor", "--config", str(config)])
    assert "tier: M (compact)" in result.output
    assert isinstance(result.exception, cli.EnvironmentFailure)
    assert result.exception.exit_code == ExitCode.ENVIRONMENT_ERROR


@pytest.mark.parametrize(
    ("served", "ok", "environment_ok"),
    [(32768, True, True), (4096, False, False), (None, None, True)],
)
def test_the_served_context_window_must_hold_the_configured_one(
    served: int | None, ok: bool | None, environment_ok: bool
) -> None:
    report = run_doctor(
        weather_client(9, 0),
        docker_status=lambda: [Check("docker", True, "Docker 28")],
        facts=lambda: parse_ollama_show(QWEN3_8B),
        context_length=32768,
        now=lambda: NOW,
        served_context=lambda: served,
    )
    check = next(c for c in report.checks if c.name == "context window")
    assert check.ok is ok
    assert report.environment_ok is environment_ok


def test_a_model_that_does_not_load_fails_the_context_check() -> None:
    def fail() -> int | None:
        raise ProviderError("model not found")

    check = served_context_check(fail, 32768)
    assert check.ok is False
    assert "did not load" in check.detail
