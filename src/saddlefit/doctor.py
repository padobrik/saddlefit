"""``saddlefit doctor``: environment, model speed and tool calling, compact-model tier (A3, B5).

Speed is measured on a fixed prompt, and tool use with ten tool-call probes, which also decide
``auto`` tool mode: ``native`` when at least nine succeed, otherwise ``json``. The tier itself
comes from ``saddlefit.compact``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from saddlefit.compact import (
    PROBES,
    PROBES_NEEDED,
    SPEED_LIMIT_TPS,
    ModelFacts,
    Tier,
    classify,
    fetch_ollama_facts,
)
from saddlefit.config import SandboxConfig, StudentConfig
from saddlefit.providers.base import (
    GenParams,
    Message,
    ModelClient,
    ModelInfo,
    ProviderError,
    ToolMode,
    ToolSpec,
)
from saddlefit.providers.openai_compat import OpenAICompatibleClient

DOCTOR_SCHEMA_VERSION: Final = 1

SPEED_PROMPT: Final = "Count from 1 to 60, separated by spaces. Reply with the numbers only."
PROBE_TOOL: Final = ToolSpec(
    name="get_weather",
    description="Current weather for one city.",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    },
)
PROBE_CITIES: Final = (
    "Paris",
    "Tokyo",
    "Lima",
    "Oslo",
    "Cairo",
    "Quito",
    "Perth",
    "Dakar",
    "Hanoi",
    "Turin",
)


class DoctorRecord(BaseModel):
    """``.saddlefit/doctor.json``: the probe result, stored with the model digest (B5)."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal[1]
    model: str
    digest: str | None
    tool_mode: Literal["native", "json"]
    native_probes: int
    json_probes: int | None
    tokens_per_second: float
    tier: Tier
    checked_at: datetime


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None  # None: could not be determined
    detail: str


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    tier: Tier = "unknown"
    failed: list[str] = field(default_factory=list)
    record: DoctorRecord | None = None
    environment_ok: bool = True

    @property
    def compact(self) -> bool:
        return self.tier in {"S", "M", "L"}

    def as_json(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "compact": self.compact,
            "failed": self.failed,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
            "record": None if self.record is None else self.record.model_dump(mode="json"),
        }


def measure_speed(client: ModelClient) -> float:
    """Completion tokens per second on a fixed prompt, after an unmeasured warm-up call.

    The first request to a runner can include loading the model, which says nothing about
    how fast it generates.
    """
    client.chat([Message(role="user", content="Say OK.")], None, GenParams(max_tokens=8))
    reply = client.chat(
        [Message(role="user", content=SPEED_PROMPT)],
        None,
        GenParams(temperature=0.0, seed=7, max_tokens=256),
    )
    if reply.latency_s <= 0:
        return 0.0
    return reply.usage.completion_tokens / reply.latency_s


def probe_tools(client: ModelClient, mode: ToolMode) -> int:
    """How many of ten simple tool calls come back right in ``mode``."""
    successes = 0
    for city in PROBE_CITIES:
        prompt = f"What is the weather in {city} right now? Use the get_weather tool."
        try:
            reply = client.chat(
                [Message(role="user", content=prompt)],
                [PROBE_TOOL],
                GenParams(temperature=0.0, seed=7, max_tokens=1024, tool_mode=mode),
            )
        except ProviderError:
            continue
        calls = reply.tool_calls
        if (
            len(calls) == 1
            and calls[0].name == PROBE_TOOL.name
            and city.lower() in str(calls[0].arguments.get("city", "")).lower()
        ):
            successes += 1
    return successes


def run_doctor(
    client: ModelClient,
    *,
    docker_status: Callable[[], list[Check]],
    facts: Callable[[], ModelFacts | None],
    context_length: int,
    now: Callable[[], datetime],
) -> DoctorReport:
    """Every check B5 lists, in order; stops after the runner if it cannot be reached."""
    report = DoctorReport()
    docker_checks = docker_status()
    report.checks.extend(docker_checks)
    report.environment_ok = all(c.ok is not False for c in docker_checks)
    try:
        info: ModelInfo = client.info()
        tps = measure_speed(client)
    except ProviderError as exc:
        report.checks.append(Check("runner", False, f"unreachable: {exc}"))
        report.environment_ok = False
        return report
    runner = f"{info.runner or 'unknown'} {info.runner_version or ''}".strip()
    report.checks.append(Check("runner", True, runner))
    report.checks.append(Check("model", True, f"{info.model} digest {info.digest or 'unknown'}"))
    report.checks.append(Check("speed", tps >= SPEED_LIMIT_TPS, f"{tps:.1f} tokens/s"))

    model_facts = facts()
    if model_facts is None:
        report.checks.append(Check("size", None, "the runner exposes no parameter count"))
    else:
        size = f"{model_facts.total_params / 1e9:.1f}B parameters"
        if model_facts.moe:
            size += f", about {model_facts.active_params / 1e9:.1f}B active"
        report.checks.append(Check("size", True, f"{size}, about {model_facts.memory_gb():.1f} GB"))
        if model_facts.max_context is not None:
            report.checks.append(
                Check(
                    "context",
                    model_facts.max_context >= context_length,
                    f"model {model_facts.max_context}, configured {context_length}",
                )
            )

    native = probe_tools(client, "native")
    json_probes = None if native >= PROBES_NEEDED else probe_tools(client, "json")
    mode: Literal["native", "json"] = "native" if native >= PROBES_NEEDED else "json"
    tools_ok = native >= PROBES_NEEDED or (json_probes or 0) >= PROBES_NEEDED
    probes = f"native {native}/{PROBES}"
    if json_probes is not None:
        probes += f", json {json_probes}/{PROBES}"
    report.checks.append(Check("tool calls", tools_ok, f"{probes}; auto mode uses {mode}"))

    report.tier, report.failed = classify(model_facts, tps, tools_ok)
    report.record = DoctorRecord(
        schema_version=DOCTOR_SCHEMA_VERSION,
        model=info.model,
        digest=info.digest,
        tool_mode=mode,
        native_probes=native,
        json_probes=json_probes,
        tokens_per_second=round(tps, 2),
        tier=report.tier,
        checked_at=now(),
    )
    return report


def docker_checks(image: str) -> list[Check]:
    """Docker is reachable and the sandbox image is present."""
    import docker  # noqa: PLC0415 - the SDK is only needed when doctor runs

    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:
        return [Check("docker", False, f"unreachable: {exc}")]
    try:
        version = client.version().get("Version", "unknown")
        try:
            client.images.get(image)
            image_check = Check("sandbox image", True, image)
        except docker.errors.ImageNotFound:
            image_check = Check("sandbox image", False, f"{image} not found; pull or build it")
        return [Check("docker", True, f"Docker {version}"), image_check]
    finally:
        client.close()


def doctor_for(student: StudentConfig, sandbox: SandboxConfig) -> DoctorReport:
    """Run every check against the configured runner, model and sandbox image."""
    client = OpenAICompatibleClient(
        base_url=student.base_url,
        model=student.model,
        context_length=student.context_length,
        timeout_s=300,
    )
    return run_doctor(
        client,
        docker_status=lambda: docker_checks(sandbox.image),
        facts=lambda: fetch_ollama_facts(student.base_url, student.model),
        context_length=student.context_length,
        now=lambda: datetime.now(UTC),
    )


def render(report: DoctorReport) -> str:
    """The human-readable report: one line per check, then the tier."""
    marks = {True: "ok  ", False: "FAIL", None: "?   "}
    lines = [f"{marks[c.ok]} {c.name}: {c.detail}" for c in report.checks]
    if report.compact:
        lines.append(f"tier: {report.tier} (compact)")
    elif report.tier == "not compact":
        lines.append("tier: not compact")
        lines.extend(f"  failed {condition}" for condition in report.failed)
    else:
        lines.append("tier: unknown (the runner does not report the model's size)")
    return "\n".join(lines)
