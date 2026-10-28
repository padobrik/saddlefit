"""``process_alive`` probes a process without signalling it, on every platform."""

import os
import subprocess
import sys

from saddlefit.storage.progress import process_alive


def test_this_process_is_alive() -> None:
    # on Windows os.kill(pid, 0) would send Ctrl+C to this very process group
    assert process_alive(os.getpid())


def test_a_process_that_exited_is_not() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not process_alive(child.pid)
