"""The generator role: one call writes one task for a task type (A11, B13).

The generator sees a repository summary, a sample of its source files, a few training tasks
as examples and the constraints; it answers through the ``submit_task`` tool with the task
text, the hidden tests, a reference patch and a visible check. It never sees sealed data:
nothing here reads the sealed store, and the examples are public task texts only.

Task types are pluggable: each is a name and the instructions the generator gets for it.
Whether a task is kept is decided later, in a fresh container and then by calibration.
"""

from __future__ import annotations

import posixpath
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import PurePosixPath
from string import Template
from typing import Any, Final

from saddlefit.eval.tamper import matches_any
from saddlefit.providers.base import GenParams, Message, ModelClient, ProviderError, ToolSpec, Usage
from saddlefit.textdiff import file_diff

TOOL_NAME: Final = "submit_task"
MAX_EXAMPLES: Final = 3
MAX_COVERED: Final = 60  # titles, the newest kept when there are more
# a hidden-test line this long showing up in the task text gives the test away
DISCLOSED_LINE_CHARS: Final = 25
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
    description="Submit exactly one task with its hidden tests and reference solution.",
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
            "reference_edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old": {"type": "string"},
                        "new": {"type": "string"},
                    },
                    "required": ["path", "old", "new"],
                    "additionalProperties": False,
                },
            },
            "visible_check": {"type": "string"},
        },
        "required": ["task_md", "hidden_tests", "reference_edits", "visible_check"],
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
    # titles of tasks in the suite and of candidates already tried: not to be repeated
    covered: Sequence[str] = ()
    # functions kept tasks already change (``path::Name``), from the repeat index, those in
    # the files shown first; and files a kept task changes as a whole
    covered_functions: Sequence[str] = ()
    covered_files: Sequence[str] = ()
    # candidates of this command calibration dropped: the student passed all runs, or none
    too_easy: int = 0
    too_hard: int = 0


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
    files: Sequence[RepoFile],
    rng: random.Random,
    budget_chars: int = FILE_BUDGET_CHARS,
    covered: Mapping[str, int] | None = None,
) -> list[RepoFile]:
    """A random sample of files that fits the budget, in path order; each call can differ.

    ``covered`` counts the kept tasks in each file: a file is drawn with weight
    ``1 / (1 + count)``, so the generator mostly sees code no task is about yet, and no file
    is ever left out. The draw is weighted sampling without replacement (each file's key is
    ``random() ** (1 + count)``, largest first).
    """
    counts = covered or {}
    keyed = [(rng.random() ** (1 + counts.get(f.path, 0)), f) for f in files]
    chosen: list[RepoFile] = []
    used = 0
    for _, file in sorted(keyed, key=lambda pair: pair[0], reverse=True):
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


def task_title(task_md: str) -> str:
    """A task's first non-empty line without its heading marks, as the generator is shown it."""
    for line in task_md.splitlines():
        if line.strip():
            return line.strip().lstrip("#").strip()[:160]
    return ""


def render_user(request: GeneratorRequest) -> str:
    """The files to change, example tasks, and the titles the new task must not repeat."""
    parts = ["# Files you may change"]
    for file in request.files:
        parts.append(f"## {file.path}\n\n````\n{file.content.rstrip()}\n````")
    if request.examples:
        parts.append("# Example tasks from this repository")
        parts += [example.strip() for example in request.examples[:MAX_EXAMPLES]]
    if request.covered_files:
        parts.append(
            "# Files tasks change as a whole\n\nDo not write a task that changes these files."
            "\n\n" + "\n".join(f"- `{path}`" for path in request.covered_files[:MAX_COVERED])
        )
    if request.covered_functions:
        parts.append(
            "# Functions tasks already change\n\nWrite the task about a function that is not "
            "in this list.\n\n"
            + "\n".join(f"- `{name}`" for name in request.covered_functions[:MAX_COVERED])
        )
    if request.covered:
        parts.append(
            "# Already covered\n\nThese tasks exist or were tried already. Do not write "
            "another task about the same bug, feature or function as any of them.\n\n"
            + "\n".join(f"- {title}" for title in request.covered[-MAX_COVERED:])
        )
    if request.too_easy or request.too_hard:
        parts.append(_difficulty(request.too_easy, request.too_hard))
    parts.append(f"Call `{TOOL_NAME}` exactly once.")
    return "\n\n".join(parts) + "\n"


def _difficulty(too_easy: int, too_hard: int) -> str:
    """How hard to aim, from the candidates this command dropped at calibration."""
    if too_easy > too_hard:
        aim = "Write a harder task than those: a less obvious bug or a larger change."
    elif too_hard > too_easy:
        aim = "Write an easier task than those: a smaller change in one function."
    else:
        aim = "Aim between the two."
    return (
        "# How hard earlier candidates were\n\n"
        f"Of the candidates tried so far, the student solved {too_easy} in every run (too "
        f"easy) and {too_hard} in none (too hard); neither was kept. A task is kept when the "
        f"student solves it in some runs but not all. {aim}"
    )


def _disclosures(task_md: str, tests: list[Any]) -> list[str]:
    """The hidden tests' names or code in the text the student reads."""
    found = []
    for entry in tests:
        if not isinstance(entry, dict):
            continue
        path, content = entry.get("path"), entry.get("content")
        if isinstance(path, str) and PurePosixPath(path).name in task_md:
            found.append(f"task_md names the hidden test {path}")
        if isinstance(content, str) and any(
            len(line.strip()) >= DISCLOSED_LINE_CHARS and line.strip() in task_md
            for line in content.splitlines()
        ):
            found.append(f"task_md quotes the hidden test {path}")
    return found


def reference_patch(edits: Sequence[Any], request: GeneratorRequest) -> str:
    """The patch the generator's edits make, built here so its context always matches.

    Each edit replaces ``old``, which must occur exactly once in a file shown to the
    generator, with ``new``; an empty ``old`` creates a file that does not exist yet.
    """
    shown = {f.path: f.content for f in request.files}
    after: dict[str, str] = {}
    created: set[str] = set()
    for edit in edits:
        if not isinstance(edit, dict) or not all(
            isinstance(edit.get(k), str) for k in ("path", "old", "new")
        ):
            raise ValueError("each reference edit needs path, old and new strings")
        path, old, new = edit["path"], edit["old"], edit["new"]
        if matches_any(path, request.protected):
            raise ValueError(f"reference_edits touch protected paths: {path}")
        if not old:
            if path in shown or path in request.existing_paths or path in after:
                raise ValueError(f"reference edit with empty old must create a new file: {path}")
            rel = PurePosixPath(path)
            unsafe = "\\" in path or rel.is_absolute() or ".." in rel.parts or not rel.parts
            if unsafe or posixpath.normpath(path) != path:
                raise ValueError(f"reference edit path must be relative and normal: {path!r}")
            after[path] = new
            created.add(path)
            continue
        current = after.get(path, shown.get(path))
        if current is None:
            raise ValueError(f"reference edit for a file you were not shown: {path}")
        count = current.count(old)
        if count != 1:
            raise ValueError(f"reference edit old text occurs {count} times in {path}, not once")
        after[path] = current.replace(old, new, 1)
    parts = [
        file_diff(p, None if p in created else shown[p], text) for p, text in sorted(after.items())
    ]
    patch = "".join(parts)
    if not patch:
        raise ValueError("reference_edits change nothing")
    return patch


def _test_path_problem(path: str, request: GeneratorRequest) -> str | None:
    rel = PurePosixPath(path)
    if "\\" in path or rel.is_absolute() or ".." in rel.parts or not rel.parts:
        return f"hidden test path must be relative and stay inside: {path!r}"
    # compared in normal form, so tests/./x.py cannot stand in for an existing tests/x.py
    if posixpath.normpath(path) != path:
        return f"hidden test path must be in normal form: {path!r}"
    if not matches_any(path, request.protected):
        return f"hidden test {path} matches no protected pattern"
    if path in request.existing_paths:
        return f"hidden test {path} already exists in the repository"
    return None


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
        elif problem := _test_path_problem(path, request):
            problems.append(problem)
    edits = arguments.get("reference_edits")
    if not isinstance(edits, list) or not edits:
        problems.append("reference_edits must list at least one edit")
    else:
        try:
            reference_patch(edits, request)
        except ValueError as exc:
            problems.append(str(exc))
    if isinstance(task_md, str) and isinstance(tests, list):
        problems += _disclosures(task_md, tests)
    check = arguments.get("visible_check")
    if not isinstance(check, str) or not check.strip():
        problems.append("visible_check must be a shell command")
    return problems


def generate(
    client: ModelClient, request: GeneratorRequest, max_tokens: int | None = None
) -> GeneratorResult:
    """One generator call: the request in, exactly one checked task out."""
    shown = sum(len(f.content) for f in request.files)
    if shown > FILE_BUDGET_CHARS:
        raise ValueError(f"the request shows {shown} characters of files, over {FILE_BUDGET_CHARS}")
    messages = [
        Message(role="system", content=render_system(request)),
        Message(role="user", content=render_user(request)),
    ]
    # low effort: the generator's thinking counts against max_tokens, and a reply cut off
    # there is paid for and useless
    reply = client.chat(messages, [SUBMIT_TOOL], GenParams(max_tokens=max_tokens, reasoning="low"))
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
        reference_patch=reference_patch(arguments["reference_edits"], request),
        visible_check=arguments["visible_check"].strip(),
    )
    served = getattr(reply.raw, "model", None) or client.info().model
    return GeneratorResult(task=task, usage=reply.usage, model=served)
