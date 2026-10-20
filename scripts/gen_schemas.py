"""Generate JSON Schemas for persisted models into schemas/.

Usage: gen_schemas.py [--check]. With --check, exit 1 if schemas/ is out of date.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from saddlefit.schema_registry import SCHEMA_MODELS

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
SUFFIX = ".schema.json"


def render() -> dict[str, str]:
    return {
        f"{name}{SUFFIX}": json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        for name, model in sorted(SCHEMA_MODELS.items())
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    expected = render()
    existing = {p.name: p.read_text(encoding="utf-8") for p in SCHEMA_DIR.glob(f"*{SUFFIX}")}
    stale = sorted(set(existing) - set(expected))
    changed = sorted(n for n, text in expected.items() if existing.get(n) != text)

    if args.check:
        for name in changed:
            print(f"out of date: schemas/{name}", file=sys.stderr)
        for name in stale:
            print(f"stale: schemas/{name}", file=sys.stderr)
        if changed or stale:
            print("run: uv run python scripts/gen_schemas.py", file=sys.stderr)
            return 1
        print(f"schemas up to date ({len(expected)} files)")
        return 0

    for name in stale:
        (SCHEMA_DIR / name).unlink()
    for name in changed:
        (SCHEMA_DIR / name).write_text(expected[name], encoding="utf-8")
    print(f"wrote {len(changed)}, removed {len(stale)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
