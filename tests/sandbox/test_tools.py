"""Student tools against a real sandbox."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from saddlefit.config import SandboxConfig
from saddlefit.sandbox.container import Sandbox
from saddlefit.tools.builtin import Toolbox

pytestmark = pytest.mark.docker


@pytest.fixture
def box(docker_client, sandbox_image: str, tmp_path: Path) -> Iterator[Toolbox]:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    with Sandbox(
        docker_client, sandbox_image, tmp_path, SandboxConfig(memory="256m"), run_timeout_s=120
    ) as sandbox:
        yield Toolbox(sandbox)


def test_read_list_search(box: Toolbox) -> None:
    assert "return 1" in box.read_file("pkg/mod.py").output
    assert box.read_file("pkg/mod.py", start=2, end=2).output.strip().endswith("return 1")
    assert "mod.py" in box.list_dir("pkg").output
    assert "pkg/mod.py:2" in box.search("return", ".").output


def test_edit_write_and_run(box: Toolbox, tmp_path: Path) -> None:
    edited = box.edit_file("pkg/mod.py", "return 1", "return 'it''s; $(id)'")
    assert edited.ok, edited.output
    assert "return 'it''s; $(id)'" in (tmp_path / "pkg/mod.py").read_text(encoding="utf-8")
    missing = box.edit_file("pkg/mod.py", "not there", "x")
    assert not missing.ok
    assert "found 0 times" in missing.output
    assert box.write_file("pkg/new/deep.py", "X = 'ünï'\n").ok
    assert (tmp_path / "pkg/new/deep.py").read_text(encoding="utf-8") == "X = 'ünï'\n"
    run = box.bash("python3 -c 'from pkg.new.deep import X; print(X)'")
    assert (run.ok, run.output.strip()) == (True, "ünï")


def test_escapes_are_refused_before_reaching_the_sandbox(box: Toolbox) -> None:
    assert not box.call("read_file", {"path": "/etc/passwd"}).ok
    assert not box.call("write_file", {"path": "../outside.py", "content": "x"}).ok


def test_large_writes_survive_argument_limits(box: Toolbox, tmp_path: Path) -> None:
    content = "".join(f"line {i:06d} ü\n" for i in range(34_000))  # about 510 KB
    assert len(content.encode("utf-8")) < 512_000
    result = box.write_file("big.txt", content)
    assert result.ok, result.output
    assert (tmp_path / "big.txt").read_text(encoding="utf-8") == content


def test_missing_file_in_a_ranged_read_fails(box: Toolbox) -> None:
    assert not box.read_file("nope.py", start=1, end=3).ok


def test_search_without_matches_is_not_an_error(box: Toolbox) -> None:
    result = box.search("no-such-text-anywhere")
    assert result.ok
    assert result.output.strip() == "no matches"
