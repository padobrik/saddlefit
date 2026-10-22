"""``export --format skill``: the fitted harness as Agent Skills for other agents (B14).

The harness at a commit becomes a directory of skills:

- ``saddlefit-<project>/SKILL.md`` carries the harness itself: the system prompt, the tool
  guidance, the finish-gate rule from ``loop.yaml`` and the lessons for the student model;
  the harness's tool scripts sit next to it in ``scripts/``;
- every harness skill, ``skills/<name>/``, is copied as it is, since it already is one.

Only allowlisted harness files are read, and the whole export is scanned for canaries
before it is kept: nothing sealed may leave the project (B10).
"""

from __future__ import annotations

import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from saddlefit.context.assembly import rank_lessons
from saddlefit.eval.leak_guard import LeakGuard
from saddlefit.harness.loop_config import load_loop_config
from saddlefit.harness.store import HarnessStore
from saddlefit.providers.base import Message

_NAME: Final = re.compile(r"[^a-z0-9]+")
MAX_NAME: Final = 64
EXPORT_SCHEMA_VERSION: Final = 1
MANIFEST: Final = "export.json"


class ExportManifest(BaseModel):
    """``export.json`` at the export's root: what was exported, from where, for whom."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    format: Literal["skill"]
    project: str
    model: str
    harness_commit: str
    created: str  # ISO 8601, UTC
    skills: tuple[str, ...]


def skill_name(project: str) -> str:
    """``saddlefit-<project>`` in lowercase letters, digits and hyphens, at most 64 long."""
    slug = _NAME.sub("-", project.lower()).strip("-") or "project"
    return f"saddlefit-{slug}"[:MAX_NAME].rstrip("-")


def _free_name(name: str, taken: set[str]) -> str:
    """``name``, or ``name-2``, ``name-3`` … when a harness skill already has it."""
    if name not in taken:
        return name
    number = 2
    while f"{name}-{number}" in taken:
        number += 1
    return f"{name}-{number}"


def _main_skill(name: str, harness: Path, project: str, model: str, commit: str) -> str:
    def read(rel: str) -> str:
        path = harness / rel
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""

    gate = load_loop_config(harness / "loop.yaml").finish_gate
    rules = []
    if gate.reread_task:
        rules.append("- Before you finish, read the task again and check every point is done.")
    if gate.run:
        rules.append(f"- Before you finish, run `{gate.run}`; finish only when it passes.")
    lessons = rank_lessons(read("EXPERIENCE.md"), model, "")
    frontmatter = yaml.safe_dump(
        {
            "name": name,
            "description": f"Working rules for {project}, fitted by Saddlefit for {model}. "
            f"Use it when solving coding tasks in {project}.",
        },
        sort_keys=False,
        allow_unicode=True,
        width=1000,
    )
    sections = [
        "---",
        frontmatter.rstrip(),
        "---",
        "",
        f"<!-- exported from harness commit {commit[:12]} -->",
        "",
        read("systemprompt.md"),
    ]
    if read("tool_guidance.md"):
        sections += ["", "## Using tools", "", read("tool_guidance.md")]
    if rules:
        sections += ["", "## Finishing", "", *rules]
    if lessons:
        sections += ["", "## Lessons", "", *lessons]
    if any((harness / "tools").glob("*.*")):
        sections += ["", "## Scripts", "", "Helper scripts for this project are in `scripts/`."]
    return "\n".join(sections).rstrip() + "\n"


def export_skill(
    store: HarnessStore,
    ref: str,
    dest: Path,
    *,
    project: str,
    model: str,
    guard: LeakGuard,
) -> list[Path]:
    """Write the skills under ``dest``; return their directories. ``dest`` must not exist."""
    if dest.exists():
        raise FileExistsError(f"{dest} already exists; choose another --out")
    commit = store.head(ref)
    with tempfile.TemporaryDirectory(prefix="saddlefit-export-") as tmp:
        harness = Path(tmp) / "harness"
        out = Path(tmp) / "out"
        store.export(commit, harness)
        harness_skills = {p.parent.name for p in (harness / "skills").glob("*/SKILL.md")}
        name = _free_name(skill_name(project), harness_skills)
        main = out / name
        main.mkdir(parents=True)
        (main / "SKILL.md").write_text(
            _main_skill(name, harness, project, model, commit), encoding="utf-8"
        )
        scripts = [p for p in sorted((harness / "tools").glob("*")) if p.is_file() and p.suffix]
        if scripts:
            (main / "scripts").mkdir()
            for script in scripts:
                shutil.copy2(script, main / "scripts" / script.name)
        for skill in sorted((harness / "skills").glob("*/SKILL.md")):
            shutil.copytree(skill.parent, out / skill.parent.name)
        manifest = ExportManifest(
            schema_version=EXPORT_SCHEMA_VERSION,
            format="skill",
            project=project,
            model=model,
            harness_commit=commit,
            created=datetime.now(UTC).isoformat(timespec="seconds"),
            skills=tuple(sorted(p.name for p in out.iterdir() if p.is_dir())),
        )
        (out / MANIFEST).write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        files = sorted(p for p in out.rglob("*") if p.is_file())
        # nothing sealed may leave the project; LeakDetected aborts with exit code 2
        guard.check(
            [
                Message(role="user", content=p.read_text(encoding="utf-8", errors="replace"))
                for p in files
            ]
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(out, dest)
    return sorted(p for p in dest.iterdir() if p.is_dir())
