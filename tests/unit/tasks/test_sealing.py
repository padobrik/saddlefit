import os
import stat
from pathlib import Path

import pytest

from saddlefit.eval.sealed import SealedStore, SealedStoreError
from saddlefit.tasks import sealing
from saddlefit.tasks.sealing import CANARY_PREFIX, SealedWriter, SealingError

RUN_ID = "01JB2Y8Q4V9N1K3D7M5R6T0W2X"


def seal(root: Path, task_id: str = "fix-0001", **kwargs: object) -> SealedStore:
    defaults: dict[str, object] = {
        "check_sh": "#!/usr/bin/env bash\npytest -q tests/test_dates.py\n",
        "hidden_tests": {
            "tests/test_dates.py": "def test_x():\n    assert True\n",
            "tests/data.json": "{}",
        },
        "target_commit": "3f2a9c1",
    }
    SealedWriter(root).write_task(task_id, **{**defaults, **kwargs})  # type: ignore[arg-type]
    return SealedStore(root)


def test_canary_is_embedded_everywhere(tmp_path: Path) -> None:
    store = seal(tmp_path)
    canary = store.canary("fix-0001")
    assert canary.startswith(CANARY_PREFIX)
    assert len(canary) == len(CANARY_PREFIX) + 32
    check = store.check_script("fix-0001").read_text(encoding="utf-8")
    assert check.startswith("#!/usr/bin/env bash\n# saddlefit canary ")
    assert canary in check
    hidden = store.hidden_tests_dir("fix-0001")
    assert canary in (hidden / "tests/test_dates.py").read_text(encoding="utf-8")
    assert (hidden / "tests/data.json").read_text(encoding="utf-8") == "{}"
    assert store.hidden_test_paths("fix-0001") == ["tests/data.json", "tests/test_dates.py"]


def test_canaries_are_unique_and_listed(tmp_path: Path) -> None:
    store = seal(tmp_path, "a")
    seal(tmp_path, "b")
    assert len(store.all_canaries()) == 2
    assert SealedStore(tmp_path / "absent").all_canaries() == frozenset()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not exist on Windows")
def test_files_are_private(tmp_path: Path) -> None:
    store = seal(tmp_path)
    assert stat.S_IMODE((tmp_path / "fix-0001").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "fix-0001" / "canary.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE(store.check_script("fix-0001").stat().st_mode) == 0o700


def test_history_and_generated_variants(tmp_path: Path) -> None:
    store = seal(tmp_path, "h")
    assert store.target_commit("h") == "3f2a9c1"
    assert store.reference_patch("h") is None
    seal(tmp_path, "g", target_commit=None, reference_patch="diff --git a/x b/x\n")
    assert store.target_commit("g") is None
    assert store.reference_patch("g") == "diff --git a/x b/x\n"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"hidden_tests": {"../escape.py": ""}}, "stay inside"),
        ({"hidden_tests": {"/abs.py": ""}}, "stay inside"),
        ({"reference_patch": "x"}, "exactly one"),
        ({"target_commit": None}, "exactly one"),
    ],
)
def test_writer_rejects_unsafe_input(
    tmp_path: Path, kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(SealingError, match=message):
        seal(tmp_path, **kwargs)
    assert not (tmp_path / "fix-0001").exists()


def test_writer_rejects_bad_ids_and_overwrites(tmp_path: Path) -> None:
    with pytest.raises(SealingError, match="invalid task id"):
        seal(tmp_path, "../x")
    store = seal(tmp_path)
    first = store.canary("fix-0001")
    with pytest.raises(SealingError, match="already exists"):
        seal(tmp_path)
    seal(tmp_path, replace=True)
    assert store.canary("fix-0001") != first


def test_writer_has_no_read_operations() -> None:
    public = {name for name in dir(SealedWriter) if not name.startswith("_")}
    assert public == {"write_task"}


def test_reader_guards_paths_and_ids(tmp_path: Path) -> None:
    store = seal(tmp_path)
    for bad in ("../fix-0001", "fix-0001/..", "missing", "..", ".", ""):
        with pytest.raises(SealedStoreError):
            store.check_script(bad)
    path = store.record_check_output("fix-0001", RUN_ID, "FAILED secret details")
    assert path.parent == tmp_path / "fix-0001" / "check_output"
    with pytest.raises(SealedStoreError, match="invalid run id"):
        store.record_check_output("fix-0001", "../../leak", "x")
    (tmp_path / "fix-0001" / "canary.txt").write_text("nope\n", encoding="utf-8")
    with pytest.raises(SealedStoreError, match="malformed canary"):
        store.canary("fix-0001")


@pytest.mark.parametrize(
    "hidden",
    [
        {"tests": "x", "tests/x.py": "y"},
        {"tests/a.py": "x", "tests/./a.py": "y"},
    ],
    ids=["file-prefix-of-dir", "same-normalized-path"],
)
def test_colliding_hidden_paths_are_rejected(tmp_path: Path, hidden: dict[str, str]) -> None:
    with pytest.raises(SealingError, match="collide"):
        seal(tmp_path, hidden_tests=hidden)
    assert not (tmp_path / "fix-0001").exists()


def test_failed_replace_keeps_the_old_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = seal(tmp_path)
    old_canary = store.canary("fix-0001")
    real_write = sealing._write

    def failing_write(path: Path, content: str, *, executable: bool = False) -> None:
        if path.name == "test_dates.py":
            raise OSError("disk full")
        real_write(path, content, executable=executable)

    monkeypatch.setattr(sealing, "_write", failing_write)
    with pytest.raises(OSError, match="disk full"):
        seal(tmp_path, replace=True)
    assert store.canary("fix-0001") == old_canary
    assert store.hidden_test_paths("fix-0001") == ["tests/data.json", "tests/test_dates.py"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fix-0001"]


def test_leftover_staging_directories_are_ignored(tmp_path: Path) -> None:
    store = seal(tmp_path)
    leftover = tmp_path / ".fix-0002.abc123"
    leftover.mkdir()
    (leftover / "canary.txt").write_text(CANARY_PREFIX + "0" * 32 + "\n", encoding="utf-8")
    assert store.all_canaries() == {store.canary("fix-0001")}


def test_backslash_hidden_paths_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(SealingError, match="POSIX separators"):
        seal(tmp_path, hidden_tests={"..\\..\\escape.py": "x"})
    assert not (tmp_path / "fix-0001").exists()


def test_interrupted_replace_keeps_the_old_task_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = seal(tmp_path)
    old_canary = store.canary("fix-0001")
    real_rename = Path.rename

    def dies_while_installing(self: Path, target: str | os.PathLike[str]) -> Path:
        if Path(target).name == "fix-0001" and self.name.startswith(".fix-0001."):
            raise KeyboardInterrupt  # the process stops between retiring and installing
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", dies_while_installing)
    with pytest.raises(KeyboardInterrupt):
        seal(tmp_path, replace=True)
    monkeypatch.undo()
    assert store.canary("fix-0001") == old_canary
    assert store.all_canaries() == {old_canary}
    seal(tmp_path, replace=True)
    assert store.canary("fix-0001") != old_canary
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fix-0001"]


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs privileges on Windows")
def test_reader_refuses_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "sealed"
    store = seal(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "fix-0002").symlink_to(root / "fix-0001")
    with pytest.raises(SealedStoreError, match="no sealed data"):
        store.check_script("fix-0002")
    out_dir = root / "fix-0001" / "check_output"
    out_dir.symlink_to(outside)
    with pytest.raises(SealedStoreError, match="symlink"):
        store.record_check_output("fix-0001", RUN_ID, "secret")
    out_dir.unlink()
    out_dir.mkdir()
    (out_dir / f"{RUN_ID}.txt").symlink_to(outside / "leak.txt")
    with pytest.raises(SealedStoreError, match="symlink"):
        store.record_check_output("fix-0001", RUN_ID, "secret")
    assert list(outside.iterdir()) == []
