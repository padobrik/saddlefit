from pathlib import Path

import pytest
from pydantic import ValidationError

from saddlefit.schema_registry import SCHEMA_MODELS
from saddlefit.storage.fit_state import FitState, load_fit_state, save_fit_state


def test_no_state_means_no_rounds(tmp_path: Path) -> None:
    assert load_fit_state(tmp_path).rounds_done == 0


def test_the_round_count_survives(tmp_path: Path) -> None:
    save_fit_state(tmp_path, FitState(schema_version=1, rounds_done=4))
    assert load_fit_state(tmp_path).rounds_done == 4
    assert [p.name for p in tmp_path.iterdir()] == ["fit.json"]  # no temporary file left


@pytest.mark.parametrize(
    "raw", ['{"schema_version": 1, "rounds_done": -1}', '{"schema_version": 1}']
)
def test_a_bad_state_is_refused(tmp_path: Path, raw: str) -> None:
    (tmp_path / "fit.json").write_text(raw)
    with pytest.raises(ValidationError):
        load_fit_state(tmp_path)


def test_the_fit_state_schema_is_generated() -> None:
    assert SCHEMA_MODELS["fit-state"] is FitState
