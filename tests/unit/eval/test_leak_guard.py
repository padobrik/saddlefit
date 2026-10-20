"""Required test 2: a canary in any prompt aborts the run with exit code 2."""

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from saddlefit import cli
from saddlefit.cli import ExitCode
from saddlefit.errors import IntegrityViolation
from saddlefit.eval.leak_guard import GuardedClient, LeakDetected, LeakGuard, guard_for_suite
from saddlefit.eval.sealed import SealedStore
from saddlefit.providers.base import GenParams, Message, ModelInfo, ModelReply, ToolCall, ToolSpec
from saddlefit.tasks.sealing import SealedWriter

CANARY = "SADDLEFIT-CANARY-" + "ab" * 16
OTHER = "SADDLEFIT-CANARY-" + "cd" * 16
CANARIES = {CANARY: "fix-0001", OTHER: "fix-0002"}
CLEAN = [
    Message(role="system", content="You are a coding agent."),
    Message(role="user", content="Fix it."),
]


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def info(self) -> ModelInfo:
        return ModelInfo(provider="recording", model="m")

    def chat(
        self, messages: list[Message], tools: list[ToolSpec] | None, params: GenParams
    ) -> ModelReply:
        self.calls.append(list(messages))
        return ModelReply(text="ok")


def guarded(tmp_path: Path) -> tuple[GuardedClient, RecordingClient, Path]:
    inner = RecordingClient()
    log = tmp_path / "incidents.jsonl"
    return GuardedClient(inner, LeakGuard(CANARIES, incident_log=log)), inner, log


@pytest.mark.parametrize(
    ("messages", "tools", "where"),
    [
        (
            [Message(role="user", content=f"see # saddlefit canary {CANARY}")],
            None,
            "messages[0].content",
        ),
        (
            [*CLEAN, Message(role="tool", content=f"cat check.sh\n{CANARY}", tool_call_id="c1")],
            None,
            "messages[2].content",
        ),
        (
            [
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(id="c", name="write_file", arguments={"content": CANARY}),
                    ),
                )
            ],
            None,
            "messages[0].tool_calls[0].arguments.content",
        ),
        (CLEAN, [ToolSpec(name="t", description=f"leaky {CANARY}")], "tools[0].description"),
    ],
    ids=["user", "tool-output", "tool-call-args", "tool-description"],
)
def test_canary_anywhere_in_a_prompt_aborts(
    tmp_path: Path, messages: list[Message], tools: list[ToolSpec] | None, where: str
) -> None:
    client, inner, log = guarded(tmp_path)
    with pytest.raises(LeakDetected) as exc:
        client.chat(messages, tools, GenParams())
    assert exc.value.exit_code == ExitCode.INTEGRITY_VIOLATION == 2
    assert isinstance(exc.value, IntegrityViolation)
    assert exc.value.task_ids == ("fix-0001",)
    assert where in exc.value.locations
    assert inner.calls == []  # the prompt never reached the model
    incident = json.loads(log.read_text(encoding="utf-8"))
    assert incident["schema_version"] == 1
    assert incident["type"] == "canary_leak"
    assert incident["tasks"] == ["fix-0001"]
    assert CANARY not in log.read_text(encoding="utf-8")


def test_clean_prompts_pass_through(tmp_path: Path) -> None:
    client, inner, log = guarded(tmp_path)
    assert client.chat(CLEAN, None, GenParams()).text == "ok"
    assert len(inner.calls) == 1
    assert client.info().provider == "recording"
    assert not log.exists()


def test_several_canaries_are_all_reported(tmp_path: Path) -> None:
    client, _, _ = guarded(tmp_path)
    with pytest.raises(LeakDetected) as exc:
        client.chat([Message(role="user", content=f"{OTHER} {CANARY}")], None, GenParams())
    assert exc.value.task_ids == ("fix-0001", "fix-0002")


def test_guard_without_incident_log_still_aborts() -> None:
    with pytest.raises(LeakDetected):
        LeakGuard(CANARIES).check([Message(role="user", content=CANARY)])


@pytest.fixture
def leaking_command() -> Iterator[None]:
    app = cli.app

    @app.command("leak-test")
    def leak_test() -> None:
        guard = LeakGuard(CANARIES)
        GuardedClient(RecordingClient(), guard).chat(
            [Message(role="user", content=CANARY)], None, GenParams()
        )

    yield
    app.registered_commands = [c for c in app.registered_commands if c.name != "leak-test"]


@pytest.mark.usefixtures("leaking_command")
def test_cli_exits_with_code_2_on_a_leak(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["saddlefit", "leak-test"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "sealed data leaked" in err
    assert CANARY not in err


def test_cli_passes_other_exits_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["saddlefit", "--version"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0


def test_unwritable_incident_log_still_raises_the_integrity_error(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    guard = LeakGuard(CANARIES, incident_log=blocker / "incidents.jsonl")
    with pytest.raises(LeakDetected) as exc:
        guard.check([Message(role="user", content=CANARY)])
    assert exc.value.exit_code == 2
    assert "incident log could not be written" in str(exc.value)


def test_canaries_nested_in_arguments_and_schemas_are_found() -> None:
    call = ToolCall(id="c", name="write_file", arguments={"files": [{"body": f"x {CANARY}"}]})
    schema_tool = ToolSpec(
        name="t",
        description="d",
        parameters={"type": "object", "properties": {"mode": {"enum": ["a", OTHER]}}},
    )
    with pytest.raises(LeakDetected) as exc:
        LeakGuard(CANARIES).check([Message(role="assistant", tool_calls=(call,))], [schema_tool])
    assert exc.value.task_ids == ("fix-0001", "fix-0002")
    assert exc.value.locations == (
        "messages[0].tool_calls[0].arguments.files[0].body",
        "tools[0].parameters.properties.mode.enum[1]",
    )


def test_each_location_is_reported_once() -> None:
    text = f"{CANARY} and again {CANARY} and {OTHER}"
    with pytest.raises(LeakDetected) as exc:
        LeakGuard(CANARIES).check([Message(role="user", content=text)])
    assert exc.value.locations == ("messages[0].content",)


def test_locations_never_contain_a_canary(tmp_path: Path) -> None:
    call = ToolCall(id="c", name="write_file", arguments={CANARY: {"nested": f"again {CANARY}"}})
    log = tmp_path / "incidents.jsonl"
    with pytest.raises(LeakDetected) as exc:
        LeakGuard(CANARIES, incident_log=log).check([Message(role="assistant", tool_calls=(call,))])
    assert CANARY not in str(exc.value)
    assert CANARY not in " ".join(exc.value.locations)
    assert CANARY not in log.read_text(encoding="utf-8")
    assert exc.value.locations == (
        "messages[0].tool_calls[0].arguments.<key>",
        "messages[0].tool_calls[0].arguments.<redacted>.nested",
    )


def test_tool_call_ids_are_scanned() -> None:
    message = Message(role="tool", content="ok", tool_call_id=f"id-{CANARY}")
    with pytest.raises(LeakDetected) as exc:
        LeakGuard(CANARIES).check([message])
    assert exc.value.locations == ("messages[0].tool_call_id",)


def test_a_suite_guard_knows_every_sealed_canary(tmp_path: Path) -> None:
    sealed = tmp_path / "sealed"
    writer = SealedWriter(sealed)
    for task_id in ("fix-0001", "fix-0002"):
        writer.write_task(task_id, check_sh="true\n", hidden_tests={}, target_commit="abc1234")
    (sealed / ".fix-0003.leftover").mkdir()  # a staging leftover is skipped, not fatal
    store = SealedStore(sealed)
    guard = guard_for_suite(sealed, tmp_path / "incidents.jsonl")
    for task_id in ("fix-0001", "fix-0002"):
        leaked = [Message(role="user", content=f"hint: {store.canary(task_id)}")]
        with pytest.raises(LeakDetected) as exc:
            guard.check(leaked, None)
        assert exc.value.task_ids == (task_id,)
    guard.check(CLEAN, None)
    assert guard_for_suite(tmp_path / "missing").check(CLEAN, None) is None
