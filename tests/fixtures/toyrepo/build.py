"""Build the toy repository: a tiny Python package with a bug-fix git history.

The first commit adds the package ``toykit`` with eight known bugs and one passing test.
Each later commit fixes exactly one bug and adds a test that fails on its parent and
passes on the commit itself, which is the shape ``saddlefit tasks mine`` looks for.

Authors and dates are fixed, so the commit SHAs are the same on every machine.
Written for this project; contains no third-party code.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

PACKAGE = "toykit"

INITIAL: dict[str, str] = {
    "pyproject.toml": (
        "[project]\n"
        'name = "toykit"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.12"\n'
        "\n"
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
        'pythonpath = ["src"]\n'
    ),
    "README.md": "# toykit\n\nSmall helpers for dates, text and numbers.\n",
    f"src/{PACKAGE}/__init__.py": '"""Small helpers for dates, text and numbers."""\n',
    f"src/{PACKAGE}/dates.py": (
        '"""Date helpers."""\n'
        "\n"
        "from datetime import date\n"
        "\n"
        "\n"
        "def parse_iso(text: str) -> date:\n"
        '    """Parse YYYY-MM-DD."""\n'
        '    year, day, month = text.split("-")\n'
        "    return date(int(year), int(month), int(day))\n"
        "\n"
        "\n"
        "def is_leap(year: int) -> bool:\n"
        "    return year % 4 == 0\n"
        "\n"
        "\n"
        "def days_between(a: date, b: date) -> int:\n"
        '    """Absolute number of days between two dates."""\n'
        "    return (b - a).days\n"
    ),
    f"src/{PACKAGE}/text.py": (
        '"""Text helpers."""\n'
        "\n"
        "\n"
        "def slugify(text: str) -> str:\n"
        '    """Lowercase words joined by single hyphens."""\n'
        '    return text.lower().replace(" ", "-")\n'
        "\n"
        "\n"
        "def truncate(text: str, limit: int) -> str:\n"
        '    """Cut to at most ``limit`` characters, ending with an ellipsis when cut."""\n'
        "    if len(text) <= limit:\n"
        "        return text\n"
        '    return text[:limit] + "…"\n'
        "\n"
        "\n"
        "def count_words(text: str) -> int:\n"
        '    return len(text.split(" "))\n'
    ),
    f"src/{PACKAGE}/numbers.py": (
        '"""Number helpers."""\n'
        "\n"
        "\n"
        "def clamp(value: float, low: float, high: float) -> float:\n"
        "    return max(low, min(value, low))\n"
        "\n"
        "\n"
        "def mean(values: list[float]) -> float:\n"
        '    """Arithmetic mean; raises ValueError for an empty list."""\n'
        "    return sum(values) / len(values)\n"
    ),
    "tests/test_smoke.py": (
        f"import {PACKAGE}\n"
        "\n"
        "\n"
        "def test_package_imports() -> None:\n"
        f"    assert {PACKAGE}.__doc__\n"
    ),
}

# (commit message, source path, buggy line, fixed line, test file, test body)
FIXES: list[tuple[str, str, str, str, str, str]] = [
    (
        "fix: parse_iso reads month before day",
        "dates.py",
        '    year, day, month = text.split("-")\n',
        '    year, month, day = text.split("-")\n',
        "test_parse_iso.py",
        "from toykit.dates import parse_iso\n\n\n"
        "def test_month_before_day() -> None:\n"
        '    assert parse_iso("2026-10-20").isoformat() == "2026-10-20"\n',
    ),
    (
        "fix: is_leap handles century years",
        "dates.py",
        "    return year % 4 == 0\n",
        "    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)\n",
        "test_is_leap.py",
        "from toykit.dates import is_leap\n\n\n"
        "def test_century_years() -> None:\n"
        "    assert not is_leap(1900)\n"
        "    assert is_leap(2000)\n"
        "    assert is_leap(2024)\n",
    ),
    (
        "fix: days_between is absolute",
        "dates.py",
        "    return (b - a).days\n",
        "    return abs((b - a).days)\n",
        "test_days_between.py",
        "from datetime import date\n\nfrom toykit.dates import days_between\n\n\n"
        "def test_order_does_not_matter() -> None:\n"
        "    assert days_between(date(2026, 1, 10), date(2026, 1, 1)) == 9\n",
    ),
    (
        "fix: slugify collapses repeated spaces",
        "text.py",
        '    return text.lower().replace(" ", "-")\n',
        '    return "-".join(text.lower().split())\n',
        "test_slugify.py",
        "from toykit.text import slugify\n\n\n"
        "def test_repeated_spaces() -> None:\n"
        '    assert slugify("  Hello   World ") == "hello-world"\n',
    ),
    (
        "fix: truncate keeps the ellipsis within the limit",
        "text.py",
        '    return text[:limit] + "…"\n',
        '    return text[: limit - 1] + "…"\n',
        "test_truncate.py",
        "from toykit.text import truncate\n\n\n"
        "def test_result_fits_the_limit() -> None:\n"
        '    assert truncate("abcdef", 4) == "abc…"\n'
        '    assert truncate("abc", 4) == "abc"\n',
    ),
    (
        "fix: count_words ignores extra whitespace",
        "text.py",
        '    return len(text.split(" "))\n',
        "    return len(text.split())\n",
        "test_count_words.py",
        "from toykit.text import count_words\n\n\n"
        "def test_extra_whitespace() -> None:\n"
        '    assert count_words(" one  two\\nthree ") == 3\n'
        '    assert count_words("") == 0\n',
    ),
    (
        "fix: clamp respects the upper bound",
        "numbers.py",
        "    return max(low, min(value, low))\n",
        "    return max(low, min(value, high))\n",
        "test_clamp.py",
        "from toykit.numbers import clamp\n\n\n"
        "def test_clamp() -> None:\n"
        "    assert clamp(5, 0, 10) == 5\n"
        "    assert clamp(-1, 0, 10) == 0\n"
        "    assert clamp(11, 0, 10) == 10\n",
    ),
    (
        "fix: mean raises ValueError for an empty list",
        "numbers.py",
        "    return sum(values) / len(values)\n",
        "    if not values:\n"
        '        raise ValueError("mean of an empty list")\n'
        "    return sum(values) / len(values)\n",
        "test_mean.py",
        "import pytest\n\nfrom toykit.numbers import mean\n\n\n"
        "def test_mean() -> None:\n"
        "    assert mean([1.0, 2.0, 3.0]) == 2.0\n\n\n"
        "def test_empty() -> None:\n"
        "    with pytest.raises(ValueError):\n"
        "        mean([])\n",
    ),
]

_AUTHOR = "Toy Maintainer"
_EMAIL = "toy@example.invalid"
_EPOCH = 1_767_225_600  # 2026-01-01T00:00:00Z


def _git(repo: Path, *args: str, when: int) -> None:
    stamp = f"{when} +0000"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(repo),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": _AUTHOR,
        "GIT_AUTHOR_EMAIL": _EMAIL,
        "GIT_COMMITTER_NAME": _AUTHOR,
        "GIT_COMMITTER_EMAIL": _EMAIL,
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
    }
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def build(dest: Path) -> Path:
    """Create the toy repository in ``dest`` (which must not exist) and return it."""
    dest.mkdir(parents=True)
    _git(dest, "init", "--quiet", "--initial-branch=main", when=_EPOCH)
    for rel, content in INITIAL.items():
        _write(dest, rel, content)
    _git(dest, "add", "--all", when=_EPOCH)
    _git(dest, "commit", "--quiet", "--message", "feat: add toykit helpers", when=_EPOCH)

    for step, (message, module, buggy, fixed, test_name, test_body) in enumerate(FIXES, 1):
        source = dest / "src" / PACKAGE / module
        text = source.read_text(encoding="utf-8")
        if text.count(buggy) != 1:
            raise RuntimeError(f"fix {step}: buggy line not found exactly once in {module}")
        _write(dest, f"src/{PACKAGE}/{module}", text.replace(buggy, fixed))
        _write(dest, f"tests/{test_name}", test_body)
        when = _EPOCH + step * 86_400
        _git(dest, "add", "--all", when=when)
        _git(dest, "commit", "--quiet", "--message", message, when=when)
    return dest
