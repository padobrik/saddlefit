from types import SimpleNamespace
from typing import Any

import pytest

from saddlefit import cli
from saddlefit.config import Config
from saddlefit.errors import EnvironmentFailure
from saddlefit.providers.base import ModelInfo, ProviderError


class Probe:
    """Stands in for the student client ``_student`` builds."""

    served: Any = 32768

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def info(self) -> ModelInfo:
        return ModelInfo(provider="openai-compatible", model="qwen3:8b", runner="ollama")

    def served_context_length(self) -> int | None:
        if isinstance(self.served, Exception):
            raise self.served
        return self.served


def project() -> Any:
    return SimpleNamespace(
        config=lambda: Config(schema_version=3, project="p"),
        student_timeout=lambda digest: 60.0,
    )


@pytest.mark.parametrize("served", [32768, None])
def test_a_window_that_holds_the_context_is_used(
    monkeypatch: pytest.MonkeyPatch, served: int | None
) -> None:
    monkeypatch.setattr(cli, "OpenAICompatibleClient", Probe)
    monkeypatch.setattr(Probe, "served", served)
    info, make = cli._student(project())
    assert info.model == "qwen3:8b"
    assert make().kwargs["context_length"] == 32768


def test_a_smaller_served_window_is_an_environment_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "OpenAICompatibleClient", Probe)
    monkeypatch.setattr(Probe, "served", 4096)
    with pytest.raises(EnvironmentFailure, match="4096-token context window"):
        cli._student(project())


def test_a_model_that_does_not_load_is_an_environment_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "OpenAICompatibleClient", Probe)
    monkeypatch.setattr(Probe, "served", ProviderError("model not found"))
    with pytest.raises(EnvironmentFailure, match="could not be loaded"):
        cli._student(project())
