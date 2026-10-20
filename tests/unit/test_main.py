import runpy
import subprocess
import sys

import pytest

from saddlefit import __version__


def test_module_entry_point() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "saddlefit", "--version"], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == f"saddlefit {__version__}"


def test_module_entry_point_in_process(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["saddlefit", "--version"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("saddlefit", run_name="__main__")
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"saddlefit {__version__}"
