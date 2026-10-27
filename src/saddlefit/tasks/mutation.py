"""Injected bugs: one small change to working code that breaks its existing tests (A11).

The approach of SWE-smith. A mutation flips one operator or literal in a Python source
file, found with ``tokenize`` so the rest of the file keeps its exact text. If the tests
that mention the module passed before and fail after, the generator writes the bug up as an
issue, and the mutated tree becomes the task's base: a commit object under
``refs/saddlefit/mutations/`` in the repository, never on a branch or in the working tree.
The reference patch, which undoes the mutation, goes to the sealed store with the tests.
"""

from __future__ import annotations

import io
import os
import random
import re
import subprocess
import tempfile
import tokenize
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from saddlefit.providers.base import GenParams, Message, ModelClient, ProviderError, ToolSpec
from saddlefit.tasks.generator import GeneratedTask, GeneratorResult
from saddlefit.tasks.mining import _git_bytes, _git_env

MUTATION_REF: Final = "refs/saddlefit/mutations"
CONTEXT_LINES: Final = 12
ISSUE_TOOL: Final = "write_issue"
# mutation attempts per generator call the budget allows: most attempts never reach the model
MUTATION_TRIES_PER_CALL: Final = 5

# token text -> its replacement; each is a plausible slip, not a syntax error
SWAPS: Final[dict[str, str]] = {
    "<": "<=",
    "<=": "<",
    ">": ">=",
    ">=": ">",
    "==": "!=",
    "!=": "==",
    "+": "-",
    "-": "+",
    "and": "or",
    "or": "and",
    "True": "False",
    "False": "True",
    "is": "is not",
}
_INT = re.compile(r"^[0-9]+$")


@dataclass(frozen=True)
class Site:
    """One place a mutation can go: a token, by line and column, and what replaces it."""

    line: int  # 1-based
    col: int
    old: str
    new: str


@dataclass(frozen=True)
class Mutation:
    path: str
    original: str
    mutated: str
    site: Site


def _lines(source: str) -> list[str]:
    """``source`` split as ``tokenize`` reads it: at ``\n`` only, line endings kept."""
    return re.findall(r"[^\n]*\n|[^\n]+$", source)


def sites(source: str) -> list[Site]:
    """Every mutable token in ``source``; none when it does not tokenize."""
    found = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return []
    for tok in tokens:
        line, col = tok.start
        if tok.type in {tokenize.OP, tokenize.NAME} and tok.string in SWAPS:
            found.append(Site(line, col, tok.string, SWAPS[tok.string]))
        elif tok.type == tokenize.NUMBER and _INT.match(tok.string):
            found.append(Site(line, col, tok.string, str(int(tok.string) + 1)))
    return found


def apply(source: str, site: Site) -> str:
    """``source`` with the one token at ``site`` replaced; every other byte kept."""
    lines = _lines(source)
    text = lines[site.line - 1]
    if text[site.col : site.col + len(site.old)] != site.old:
        raise ValueError(f"no {site.old!r} at line {site.line}, column {site.col}")
    lines[site.line - 1] = text[: site.col] + site.new + text[site.col + len(site.old) :]
    return "".join(lines)


def compiles(source: str, path: str) -> bool:
    try:
        compile(source, path, "exec")
    except SyntaxError:
        return False
    return True


def pick(path: str, source: str, rng: random.Random) -> Mutation | None:
    """A random site in ``source`` whose mutation still compiles."""
    candidates = sites(source)
    rng.shuffle(candidates)
    for site in candidates:
        try:
            mutated = apply(source, site)
        except (ValueError, IndexError):
            continue
        if mutated != source and compiles(mutated, path):
            return Mutation(path, source, mutated, site)
    return None


def related_tests(path: str, test_files: dict[str, str]) -> list[str]:
    """Test files that import the module ``path`` defines, by its dotted name or its stem."""
    pure = PurePosixPath(path)
    stem = pure.stem
    if stem == "__init__":
        stem = pure.parent.name
    dotted = ".".join(pure.with_suffix("").parts[1 if pure.parts[0] == "src" else 0 :])
    pattern = re.compile(
        rf"^\s*(from|import)\s[^\n]*\b({re.escape(stem)}|{re.escape(dotted)})\b", re.MULTILINE
    )
    return sorted(p for p, text in test_files.items() if pattern.search(text))


def excerpt(source: str, line: int) -> str:
    """The lines around ``line``, numbered, for the issue writer."""
    lines = [text.rstrip("\r\n") for text in _lines(source)]
    first = max(line - CONTEXT_LINES, 1)
    last = min(line + CONTEXT_LINES, len(lines))
    return "\n".join(f"{n:5d}  {lines[n - 1]}" for n in range(first, last + 1))


def commit_mutation(repo: Path, base: str, mutation: Mutation, task_id: str) -> str:
    """A commit with the mutated file on top of ``base``, kept alive by a saddlefit ref.

    Built in a temporary index, so the user's index, working tree and branches never move.
    """
    blob = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],  # noqa: S607
            cwd=repo,
            input=mutation.mutated.encode("utf-8"),
            env=_git_env(),
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    mode = _git_bytes(repo, "ls-tree", base, "--", mutation.path).decode().split()[0]
    with tempfile.TemporaryDirectory(prefix="saddlefit-index-") as tmp:
        env = {**_git_env(), "GIT_INDEX_FILE": os.path.join(tmp, "index")}
        _git_bytes(repo, "read-tree", base, env=env)
        _git_bytes(repo, "update-index", "--cacheinfo", f"{mode},{blob},{mutation.path}", env=env)
        tree = _git_bytes(repo, "write-tree", env=env).decode().strip()
    # a fixed identity: the repository may have none set, and the commit is saddlefit's own
    identity = {
        "GIT_AUTHOR_NAME": "saddlefit",
        "GIT_AUTHOR_EMAIL": "saddlefit@localhost",
        "GIT_COMMITTER_NAME": "saddlefit",
        "GIT_COMMITTER_EMAIL": "saddlefit@localhost",
    }
    message = f"saddlefit mutation {task_id}"
    out = _git_bytes(repo, "commit-tree", tree, "-p", base, "-m", message, env=_git_env(**identity))
    commit = out.decode().strip()
    _git_bytes(repo, "update-ref", f"{MUTATION_REF}/{task_id}", commit)
    return commit


def drop_mutation(repo: Path, task_id: str) -> None:
    """Remove the ref of a mutation that is not kept; git collects the commit later."""
    _git_bytes(repo, "update-ref", "-d", f"{MUTATION_REF}/{task_id}")


def reference_patch(mutation: Mutation) -> str:
    """The diff that undoes the mutation, as ``git diff`` writes it."""
    with tempfile.TemporaryDirectory(prefix="saddlefit-diff-") as tmp:
        a, b = Path(tmp) / "a", Path(tmp) / "b"
        # bytes, so no line ending is translated on the way
        a.write_bytes(mutation.mutated.encode("utf-8"))
        b.write_bytes(mutation.original.encode("utf-8"))
        done = subprocess.run(
            [  # noqa: S607
                "git",
                "-c",
                "diff.noprefix=false",
                "-c",
                "diff.mnemonicPrefix=false",
                "diff",
                "--no-index",
                "--no-color",
                "--no-ext-diff",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "a",
                "b",
            ],
            cwd=tmp,
            env=_git_env(GIT_CONFIG_NOSYSTEM="1"),
            capture_output=True,
            check=False,
        )
    if done.returncode != 1:  # 1 means the files differ; anything else is an error
        raise RuntimeError(f"git diff exited {done.returncode}: {done.stderr.decode()[:300]}")
    out = done.stdout.decode("utf-8")
    body = out.split("\n", 1)[1]  # drop git's own header line, which names a and b
    body = re.sub(r"^--- a/a$", f"--- a/{mutation.path}", body, count=1, flags=re.MULTILINE)
    body = re.sub(r"^\+\+\+ b/b$", f"+++ b/{mutation.path}", body, count=1, flags=re.MULTILINE)
    return f"diff --git a/{mutation.path} b/{mutation.path}\n{body}"


ISSUE_SPEC: Final = ToolSpec(
    name=ISSUE_TOOL,
    description="Submit the bug report the student will read.",
    parameters={
        "type": "object",
        "properties": {"task_md": {"type": "string"}},
        "required": ["task_md"],
        "additionalProperties": False,
    },
)

ISSUE_PROMPT: Final = """\
You write bug reports for Saddlefit. A small change was made to working code in this
repository, and the tests below now fail. Write the issue a user would file: a `# ` title,
then what goes wrong and when, in a few sentences, naming the module or function. Describe the
behaviour, not the fix: do not quote the original line, do not say which operator or number
is wrong, and do not mention the tests. Call `write_issue` once."""


def issue_messages(mutation: Mutation, tests: Sequence[str]) -> list[Message]:
    user = (
        f"File: {mutation.path}\n\nThe code as it is now (with the bug):\n\n"
        f"{excerpt(mutation.mutated, mutation.site.line)}\n\n"
        f"The code before the change, for you only:\n\n"
        f"{excerpt(mutation.original, mutation.site.line)}\n\n"
        f"Tests that now fail: {', '.join(tests)}"
    )
    return [Message(role="system", content=ISSUE_PROMPT), Message(role="user", content=user)]


def write_issue(
    client: ModelClient,
    mutation: Mutation,
    tests: Sequence[str],
    hidden: dict[str, str],
    *,
    visible_check: str,
) -> GeneratorResult:
    """The generator's bug report for ``mutation``, as a task the acceptance step can judge."""
    reply = client.chat(issue_messages(mutation, tests), [ISSUE_SPEC], GenParams(max_tokens=2048))
    calls = [c for c in reply.tool_calls if c.name == ISSUE_TOOL]
    task_md = calls[0].arguments.get("task_md") if len(calls) == 1 else None
    if not isinstance(task_md, str) or not task_md.lstrip().startswith("# "):
        raise ProviderError("the issue is unusable: no task_md with a '# ' title", reply.usage)
    fixed_line = _lines(mutation.original)[mutation.site.line - 1].strip()
    if len(fixed_line) >= 8 and fixed_line in task_md:
        raise ProviderError("the issue quotes the original line", reply.usage)
    task = GeneratedTask(
        task_md=task_md.strip() + "\n",
        hidden_tests=hidden,
        reference_patch=reference_patch(mutation),
        visible_check=visible_check,
    )
    served = getattr(reply.raw, "model", None) or client.info().model
    return GeneratorResult(task=task, usage=reply.usage, model=served)
