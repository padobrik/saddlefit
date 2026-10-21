from pathlib import Path

from saddlefit.loops.workspace import WorkspaceTracker


def test_bytes_that_are_not_utf8_and_crlf_survive_the_diff(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "win.txt").write_bytes(b"one\r\ntwo\r\n")
    tracker = WorkspaceTracker(work, tmp_path / "git")
    (work / "win.txt").write_bytes(b"one\r\nthree\r\n")
    (work / "latin.txt").write_bytes(b"caf\xe9\n")
    before = tracker.digest()
    diff = tracker.diff()
    assert "+three\r\n" in diff  # CRLF kept, so git apply on the base snapshot still matches
    assert "caf\udce9" in diff  # the bad byte rides along, for the evaluator to refuse
    (work / "latin.txt").write_bytes(b"caf\xe8\n")
    assert tracker.digest() != before
