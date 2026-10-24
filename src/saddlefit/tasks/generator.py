"""The generator role: one call writes one task for a task type (A11, B13).

The generator sees a repository summary, a sample of its source files, a few training tasks
as examples and the constraints; it answers through the ``submit_task`` tool with the task
text, the hidden tests, a reference patch and a visible check. It never sees sealed data:
nothing here reads the sealed store, and the examples are public task texts only.

Task types are pluggable: each is a name and the instructions the generator gets for it.
Whether a task is kept is decided later, in a fresh container and then by calibration.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import PurePosixPath
from string import Template
from typing import Any, Final

from saddlefit.eval.tamper import diff_paths, matches_any
from saddlefit.providers.base import GenParams, Message, ModelClient, ProviderError, ToolSpec, Usage

TOOL_NAME: Final = "submit_task"
MAX_EXAMPLES: Final = 3
FILE_BUDGET_CHARS: Final = 40_000


@dataclass(frozen=True)
class TaskKind:
    """A type of task: the name stored in ``meta.yaml`` and what the generator is told."""

    name: str
    instructions: str


TASK_KINDS: Final[Mapping[str, TaskKind]] = {
    "feature": TaskKind(
        "feature",
        "Add a small piece of behaviour the repository does not have yet: a new option, a "
        "new function, or a case an existing function does not handle. It must fit the code "
        "around it.",
    ),
    "bugfix": TaskKind(
        "bugfix",
        "Pick working code and describe a bug a user could plausibly hit there, such as a "
        "wrong edge case or a wrong error. The reference patch fixes the code you were shown, "
        "so first imagine the bug, then write tests that would catch it and a patch that makes "
        "them pass: the hidden tests must fail on the code as it is now.",
    ),
}

SUBMIT_TOOL: Final = ToolSpec(
    name=TOOL_NAME,
    description="Submit exactly one task with its hidden tests and reference patch.",
    parameters={
        "type": "object",
        "properties": {
            "task_md": {"type": "string"},
            "hidden_tests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
            "reference_patch": {"type": "string"},
            "visible_check": {"type": "string"},
        },
        "required": ["task_md", "hidden_tests", "reference_patch", "visible_check"],
        "additionalProperties": False,
    },
)


@dataclass(frozen=True)
class RepoFile:
    path: str
    content: str


@dataclass(frozen=True)
class GeneratorRequest:
    kind: TaskKind
    repo_summary: str
    files: Sequence[RepoFile]
    examples: Sequence[str]
    protected: Sequence[str]
    existing_paths: frozenset[str]


@dataclass(frozen=True)
class GeneratedTask:
    task_md: str
    hidden_tests: Mapping[str, str]
    reference_patch: str
    visible_check: str


@dataclass(frozen=True)
class GeneratorResult:
    task: GeneratedTask
    usage: Usage
    model: str


class GeneratorError(ProviderError):
    """The generator answered without a usable task; ``usage`` holds what the call cost."""


def select_files(
    files: Sequence[RepoFile], rng: random.Random, budget_chars: int = FILE_BUDGET_CHARS
) -> list[RepoFile]:
    """A random sample of files that fits the budget, in path order; each call can differ."""
    chosen: list[RepoFile] = []
    used = 0
    for file in rng.sample(list(files), len(files)):
        if used + len(file.content) <= budget_chars:
            chosen.append(file)
            used += len(file.content)
    return sorted(chosen, key=lambda f: f.path)


def _prompt() -> Template:
    text = (resources.files("saddlefit.tasks") / "prompts" / "generate.md").read_text("utf-8")
    return Template(text)


def render_system(request: GeneratorRequest) -> str:
    return _prompt().substitute(
        protected=", ".join(f"`{p}`" for p in request.protected),
        kind=request.kind.name,
        kind_instructions=request.kind.instructions,
        repo_summary=request.repo_summary.strip(),
    )


def render_user(request: GeneratorRequest) -> str:
    parts = ["# Files you may change"]
    for file in request.files:
        parts.append(f"## {file.path}\n\n````\n{file.content.rstrip()}\n````")
    if request.examples:
        parts.append("# Example tasks from this repository")
        parts += [example.strip() for example in request.examples[:MAX_EXAMPLES]]
    parts.append(f"Call `{TOOL_NAME}` exactly once.")
    return "\n\n".join(parts) + "\n"


def _problems(arguments: Mapping[str, Any], request: GeneratorRequest) -> list[str]:
    problems: list[str] = []
    task_md = arguments.get("task_md")
    if not isinstance(task_md, str) or not task_md.lstrip().startswith("# "):
        problems.append("task_md must be Markdown starting with a '# ' title")
    tests = arguments.get("hidden_tests")
    if not isinstance(tests, list) or not tests:
        return [*problems, "hidden_tests must list at least one file"]
    for entry in tests:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str) or not isinstance(entry.get("content"), str):
            problems.append("each hidden test needs a path and a content string")
            continue
        rel = PurePosixPath(path)
        if "\\" in path or rel.is_absolute() or ".." in rel.parts or not rel.parts:
            problems.append(f"hidden test path must be relative and stay inside: {path!r}")
        elif not matches_any(path, request.protected):
            problems.append(f"hidden test {path} matches no protected pattern")
        elif path in request.existing_paths:
            problems.append(f"hidden test {path} already exists in the repository")
    patch = arguments.get("reference_patch")
    if not isinstance(patch, str) or not diff_paths(patch):
        problems.append("reference_patch must be a unified diff that changes at least one file")
    else:
        touched = sorted(p for p in diff_paths(patch) if matches_any(p, request.protected))
        if touched:
            problems.append(f"reference_patch touches protected paths: {touched}")
    check = arguments.get("visible_check")
    if not isinstance(check, str) or not check.strip():
        problems.append("visible_check must be a shell command")
    return problems


def generate(
    client: ModelClient, request: GeneratorRequest, max_tokens: int | None = None
) -> GeneratorResult:
    """One generator call: the request in, exactly one checked task out."""
    messages = [
        Message(role="system", content=render_system(request)),
        Message(role="user", content=render_user(request)),
    ]
    reply = client.chat(messages, [SUBMIT_TOOL], GenParams(max_tokens=max_tokens))
    names = [c.name for c in reply.tool_calls]
    if names != [TOOL_NAME] or reply.malformed:
        attempted = names + ["<malformed>"] * len(reply.malformed)
        raise GeneratorError(f"expected exactly one {TOOL_NAME} call, got {attempted}", reply.usage)
    arguments = reply.tool_calls[0].arguments
    problems = _problems(arguments, request)
    if problems:
        raise GeneratorError("the task is unusable: " + "; ".join(problems), reply.usage)
    task = GeneratedTask(
        task_md=arguments["task_md"].strip() + "\n",
        hidden_tests={t["path"]: t["content"] for t in arguments["hidden_tests"]},
        reference_patch=arguments["reference_patch"],
        visible_check=arguments["visible_check"].strip(),
    )
    served = getattr(reply.raw, "model", None) or client.info().model
    return GeneratorResult(task=task, usage=reply.usage, model=served)
