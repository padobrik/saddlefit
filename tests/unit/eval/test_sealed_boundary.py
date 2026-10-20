"""Required test 11: only ``saddlefit.eval`` imports the sealed-store accessor."""

import ast
from pathlib import Path

import saddlefit

PACKAGE_ROOT = Path(saddlefit.__file__).parent
ACCESSOR = "saddlefit.eval.sealed"


def _names_accessor(module: str) -> bool:
    return module == ACCESSOR or module.startswith(ACCESSOR + ".")


def _absolute(node: ast.ImportFrom, package: str) -> str:
    """Resolve ``from ..x import y`` inside ``package`` to an absolute module name."""
    if node.level == 0:
        return node.module or ""
    parts = package.split(".")
    base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
    return ".".join([*base, node.module] if node.module else base)


def imports_accessor(tree: ast.AST, package: str = "saddlefit") -> list[int]:
    """Line numbers of imports of the accessor, including relative and dynamic imports."""
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hit = any(_names_accessor(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node, package)
            hit = _names_accessor(module) or (
                module == "saddlefit.eval" and any(a.name == "sealed" for a in node.names)
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            hit = ACCESSOR in node.value  # e.g. importlib.import_module("saddlefit.eval.sealed")
        else:
            hit = False
        if hit:
            lines.append(node.lineno)
    return lines


def _package_of(rel: str) -> str:
    parts = ["saddlefit", *rel.split("/")[:-1]]
    return ".".join(parts)


def offenders(root: Path) -> dict[str, list[int]]:
    found = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("eval/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # module docstrings may mention the accessor by name
        body = getattr(tree, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body[0].value.value = ""
        lines = imports_accessor(tree, _package_of(rel))
        if lines:
            found[rel] = lines
    return found


def test_only_eval_imports_the_sealed_accessor() -> None:
    assert offenders(PACKAGE_ROOT) == {}


def test_eval_package_does_not_reexport_the_accessor() -> None:
    init = (PACKAGE_ROOT / "eval" / "__init__.py").read_text(encoding="utf-8")
    assert imports_accessor(ast.parse(init), "saddlefit.eval") == []
    assert imports_accessor(ast.parse("from . import sealed\n"), "saddlefit.eval") == [1]


def test_detector_catches_every_import_form(tmp_path: Path) -> None:
    samples = {
        "a.py": "import saddlefit.eval.sealed\n",
        "b.py": "from saddlefit.eval.sealed import SealedStore\n",
        "c.py": "from saddlefit.eval import sealed\n",
        "d.py": "import importlib\nm = importlib.import_module('saddlefit.eval.sealed')\n",
        "eval/ok.py": "from saddlefit.eval.sealed import SealedStore\n",
        "e.py": '"""Mentions saddlefit.eval.sealed in a docstring only."""\n',
        "student/f.py": "from ..eval.sealed import SealedStore\n",
        "student/g.py": "from ..eval import sealed\n",
        "student/h.py": "from .. import eval as ev\nfrom ..eval import taxonomy\n",
    }
    for name, text in samples.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    assert set(offenders(tmp_path)) == {
        "a.py",
        "b.py",
        "c.py",
        "d.py",
        "student/f.py",
        "student/g.py",
    }
