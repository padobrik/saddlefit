"""The framed status line with the orbiting ball (#113)."""

from rich.console import Console

from saddlefit.orbit import AMBER, BALL, GREY, LAP_SECONDS, OrbitStatus, frame, perimeter


def plain(rows: list) -> list[str]:  # type: ignore[type-arg]
    return [row.plain for row in rows]


def test_the_perimeter_runs_clockwise_from_the_top_left() -> None:
    cells = perimeter(5)
    assert cells[:5] == [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4)]
    assert cells[5] == (1, 4)  # down the right side
    assert cells[6:11] == [(2, 4), (2, 3), (2, 2), (2, 1), (2, 0)]
    assert cells[11] == (1, 0)
    assert len(cells) == 12


def test_the_ball_sits_on_the_frame_and_its_trail_fades_to_grey() -> None:
    rows = frame("round 1/2", 20, ball=8)
    assert plain(rows)[0] == "╭───────●──────────╮"
    assert plain(rows)[1] == "│ round 1/2        │"
    colours = [rows[0].get_style_at_offset(Console(), i).color for i in range(20)]
    names = [c.name if c else None for c in colours]
    assert names[8] == BALL
    assert names[2] == GREY
    assert names[7] not in {BALL, GREY}  # the trail fades from blue to grey


def test_a_long_status_is_cut_to_fit() -> None:
    rows = frame("x" * 50, 20, ball=None)
    assert plain(rows)[1] == "│ " + "x" * 15 + "… │"


def test_a_stall_stops_the_ball_turns_the_frame_amber_and_says_so() -> None:
    now = [100.0]
    orbit = OrbitStatus(stall_after_s=900, clock=lambda: now[0])
    orbit.update("round 1/2 · train 0/6 · $0.00", progressed=True)
    console = Console(width=80, force_terminal=True, color_system="truecolor")

    def render() -> str:
        with console.capture() as capture:
            console.print(orbit)
        return capture.get()

    moving = render()
    now[0] += LAP_SECONDS / 4
    assert render() != moving  # the ball moved
    now[0] += 15 * 60
    stalled = render()
    assert "no progress 15m" in stalled
    assert AMBER == "#d29922"
    assert "210;153;34" in stalled  # the frame turns amber
    now[0] += LAP_SECONDS / 4
    assert render() == stalled  # stopped
    orbit.update("round 1/2 · train 1/6 · $0.00", progressed=True)
    assert "no progress" not in render()


def test_the_width_follows_the_longest_status_and_never_shrinks() -> None:
    orbit = OrbitStatus(stall_after_s=900, clock=lambda: 0.0)
    orbit.update("a much longer status line here", progressed=True)
    orbit.update("short", progressed=False)
    assert orbit.width == len("a much longer status line here") + 4
