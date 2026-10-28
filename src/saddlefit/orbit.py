"""The framed status line ``fit`` shows on a terminal, with a ball running around it (#113).

The animation runs on a timer, so it says nothing about progress by itself; the stall rule
does: when no run has finished for longer than the run timeout, the ball stops, the frame
turns amber and the line says for how long. Colours follow rich's colour system, so
``NO_COLOR`` and terminals without truecolor are handled there.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from rich.console import Console, ConsoleOptions, RenderResult
from rich.style import Style
from rich.text import Text

BALL: Final = "#4493f8"
GREY: Final = "#6e7781"
AMBER: Final = "#d29922"
LAP_SECONDS: Final = 5.0
TRAIL: Final = 6


def perimeter(width: int) -> list[tuple[int, int]]:
    """Clockwise (row, column) cells of a three-row frame, from the top-left corner."""
    top = [(0, c) for c in range(width)]
    bottom = [(2, c) for c in reversed(range(width))]
    return [*top, (1, width - 1), *bottom, (1, 0)]


def _blend(a: str, b: str, share: float) -> str:
    """``a`` mixed toward ``b`` by ``share`` (0 keeps ``a``)."""
    pa = [int(a[i : i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * share):02x}" for x, y in zip(pa, pb, strict=True))


def frame(
    status: str, width: int, ball: int | None, stalled: bool = False, suffix: str = ""
) -> list[Text]:
    """The three rows; ``ball`` is the ball's index on the perimeter, ``None`` for none.

    ``suffix`` is never cut: the status gives way to it.
    """
    inner = width - 4
    room = max(inner - len(suffix), 0)
    shown = status if len(status) <= room else status[: max(room - 1, 0)] + "…"
    shown = (shown + suffix)[:inner]
    rows = [
        list("╭" + "─" * (width - 2) + "╮"),
        list("│ " + shown.ljust(inner) + " │"),
        list("╰" + "─" * (width - 2) + "╯"),
    ]
    base = AMBER if stalled else GREY
    styles: dict[tuple[int, int], str] = {}
    cells = perimeter(width)
    if ball is not None:
        for back in range(TRAIL, -1, -1):
            row, col = cells[(ball - back) % len(cells)]
            styles[(row, col)] = _blend(BALL, base, back / TRAIL)
        row, col = cells[ball % len(cells)]
        rows[row][col] = "●"
    texts = []
    for r, chars in enumerate(rows):
        text = Text()
        run, run_colour = "", None
        for c, char in enumerate(chars):
            on_frame = r != 1 or c in {0, width - 1}
            colour = styles.get((r, c), base if on_frame else None)
            if colour != run_colour and run:
                text.append(run, Style(color=run_colour) if run_colour else Style())
                run = ""
            run, run_colour = run + char, colour
        text.append(run, Style(color=run_colour) if run_colour else Style())
        texts.append(text)
    return texts


@dataclass
class OrbitStatus:
    """A rich renderable: the current status, framed, with the ball where time puts it."""

    stall_after_s: float
    width: int = 0  # the longest status so far plus the frame; it never shrinks
    status: str = "starting"
    clock: Callable[[], float] = time.monotonic
    last_progress: float = field(default=0.0)
    stopped_at: int | None = None

    def __post_init__(self) -> None:
        self.last_progress = self.clock()

    def update(self, status: str, *, progressed: bool) -> None:
        self.status = status
        self.width = max(self.width, len(status) + 4)
        if progressed:
            self.last_progress = self.clock()
            self.stopped_at = None

    def stalled_for(self) -> float:
        return self.clock() - self.last_progress

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = max(min(self.width, options.max_width), 10)
        cells = len(perimeter(width))
        waited = self.stalled_for()
        stalled = waited > self.stall_after_s
        suffix = ""
        if stalled:
            suffix = f" · no progress {int(waited // 60)}m"
            if self.stopped_at is None:
                self.stopped_at = int(self.clock() / LAP_SECONDS * cells) % cells
            ball = self.stopped_at
        else:
            ball = int(self.clock() / LAP_SECONDS * cells) % cells
        yield from frame(self.status, width, ball, stalled, suffix)
