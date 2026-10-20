"""Context assembly and budgets (B7).

Order: system prompt, tool guidance for enabled tools only, selected skills, relevant
EXPERIENCE.md lessons, then the task text. When the harness share is over budget, lessons
go first, then lower-ranked skills, then tool-guidance details. The task is never cut.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

Tier = Literal["S", "M", "L"]
# Harness share of the context by compact-model tier (B7); configurable through loop.yaml.
TIER_HARNESS_BUDGET: dict[Tier, int] = {"S": 2500, "M": 4000, "L": 6000}
DEFAULT_MAX_SKILLS = 3

TokenCounter = Callable[[str], int]
_WORD = re.compile(r"[a-z0-9_]{2,}")  # keeps short terms such as "ui" and "go"
_STOP_WORDS = frozenset(
    [
        "an",
        "as",
        "at",
        "be",
        "by",
        "do",
        "if",
        "in",
        "is",
        "it",
        "no",
        "of",
        "on",
        "or",
        "so",
        "to",
        "up",
        "we",
        "and",
        "are",
        "but",
        "can",
        "for",
        "has",
        "its",
        "not",
        "the",
        "was",
        "use",
        "with",
        "this",
        "that",
        "from",
        "into",
    ]
)
# The student's tools (saddlefit.tools.builtin.TOOL_NAMES); other "##" headings stay in the
# preamble.
KNOWN_TOOLS = frozenset(
    {"read_file", "list_dir", "search", "edit_file", "write_file", "bash", "finish"}
)
_TOOL_SECTION = re.compile(rf"^## +({'|'.join(sorted(KNOWN_TOOLS))})\s*$", re.MULTILINE)
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_MODEL_LABEL = re.compile(r"\((?:model|for):\s*([^)]+)\)")


def estimate_tokens(text: str) -> int:
    """Conservative estimate when the runner exposes no tokenizer: characters / 3."""
    return math.ceil(len(text) / 3)


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower())) - _STOP_WORDS


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str

    @property
    def text(self) -> str:
        return f"## Skill: {self.name}\n{self.body.strip()}\n"


@dataclass(frozen=True)
class Harness:
    """The harness files the assembler reads; see ``HarnessStore.export``."""

    system_prompt: str
    tool_guidance: str
    skills: tuple[Skill, ...]
    experience: str

    @classmethod
    def load(cls, root: Path) -> Harness:
        def read(name: str) -> str:
            path = root / name
            return path.read_text(encoding="utf-8") if path.is_file() else ""

        skills = []
        for skill_file in sorted((root / "skills").glob("*/SKILL.md")):
            skills.append(_parse_skill(skill_file.parent.name, skill_file.read_text("utf-8")))
        return cls(
            read("systemprompt.md"), read("tool_guidance.md"), tuple(skills), read("EXPERIENCE.md")
        )


def _parse_skill(dirname: str, text: str) -> Skill:
    match = _FRONTMATTER.match(text)
    if match is None:
        return Skill(name=dirname, description="", body=text)
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return Skill(
        name=str(meta.get("name") or dirname),
        description=str(meta.get("description") or ""),
        body=match.group(2),
    )


def guidance_for(tool_guidance: str, enabled: Iterable[str]) -> tuple[str, list[str]]:
    """The preamble plus ``## <tool>`` sections for enabled tools, and the section details.

    Returns the preamble and, separately, one text per enabled tool section, so details can
    be dropped under budget pressure while the preamble stays.
    """
    enabled_set = set(enabled)
    parts = _TOOL_SECTION.split(tool_guidance)
    preamble, sections = parts[0], []
    for name, body in zip(parts[1::2], parts[2::2], strict=True):
        if name in enabled_set:
            sections.append(f"## {name}\n{body.strip()}\n")
    return preamble.strip(), sections


def rank_skills(skills: Iterable[Skill], task_type: str, task_text: str) -> list[Skill]:
    """Skills relevant to the task: type match first, then word overlap; zero scores dropped."""
    task_words = _words(task_text)
    scored = []
    for skill in skills:
        about = f"{skill.name} {skill.description}".lower()
        score = 10 * (task_type.lower() in about) + len(_words(about) & task_words)
        if score > 0:
            scored.append((score, skill.name, skill))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [skill for _, _, skill in scored]


def rank_lessons(experience: str, model: str, task_text: str) -> list[str]:
    """Lessons for this model (or unlabelled), most related to the task first."""
    task_words = _words(task_text)
    lessons = []
    for line in experience.splitlines():
        if not line.startswith("- "):
            continue
        label = _MODEL_LABEL.search(line)
        if label and label.group(1).strip() != model:
            continue
        lessons.append((len(_words(line) & task_words), line))
    lessons.sort(key=lambda item: -item[0])
    return [line for _, line in lessons]


@dataclass
class AssembledContext:
    system: str
    task: str
    harness_tokens: int
    over_budget: bool = False
    skills: list[str] = field(default_factory=list)
    lessons: int = 0
    dropped: list[str] = field(default_factory=list)


def assemble(
    harness: Harness,
    *,
    task_text: str,
    task_type: str,
    model: str,
    enabled_tools: Iterable[str],
    harness_budget_tokens: int,
    max_skills: int = DEFAULT_MAX_SKILLS,
    count: TokenCounter = estimate_tokens,
) -> AssembledContext:
    """Build the system message within the harness budget; the task text is never cut."""
    preamble, tool_sections = guidance_for(harness.tool_guidance, enabled_tools)
    skills = rank_skills(harness.skills, task_type, task_text)[:max_skills]
    lessons = rank_lessons(harness.experience, model, task_text)
    dropped: list[str] = []

    def render() -> str:
        blocks = [harness.system_prompt.strip(), preamble, *tool_sections]
        blocks += [s.text for s in skills]
        if lessons:
            blocks.append("## Lessons\n" + "\n".join(lessons))
        return "\n\n".join(b for b in blocks if b).strip() + "\n"

    system = render()
    while count(system) > harness_budget_tokens:
        if lessons:
            dropped.append(f"lesson: {lessons.pop()}")
        elif skills:
            dropped.append(f"skill: {skills.pop().name}")
        elif tool_sections:
            dropped.append(f"tool guidance: {tool_sections.pop().splitlines()[0][3:]}")
        else:
            break
        system = render()
    tokens = count(system)
    return AssembledContext(
        system=system,
        task=task_text,
        harness_tokens=tokens,
        over_budget=tokens > harness_budget_tokens,
        skills=[s.name for s in skills],
        lessons=len(lessons),
        dropped=dropped,
    )
