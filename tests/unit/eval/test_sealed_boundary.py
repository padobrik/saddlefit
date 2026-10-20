"""Required test 11: only ``saddlefit.eval`` imports the sealed-store accessor."""

import ast
from pathlib import Path

import saddlefit

PACKAGE_ROOT = Path(saddlefit.__file__).parent
ACCESSOR = "saddlefit.eval.sealed"


def _names_accessor(module: str) -> bool:
    return module == ACCESSOR or module.startswith(ACCESSOR + ".")


def imports_accessor(tree: ast.AST) -> list[int]:
    """Line numbers of imports of the accessor, including dynamic imports by name."""
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hit = any(_names_accessor(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
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
        lines = imports_accessor(tree)
        if lines:
            found[rel] = lines
    return found


def test_only_eval_imports_the_sealed_accessor() -> None:
    assert offenders(PACKAGE_ROOT) == {}


def test_eval_package_does_not_reexport_the_accessor() -> None:
    init = (PACKAGE_ROOT / "eval" / "__init__.py").read_text(encoding="utf-8")
    assert imports_accessor(ast.parse(init)) == []


def test_detector_catches_every_import_form(tmp_path: Path) -> None:
    samples = {
        "a.py": "import saddlefit.eval.sealed\n",
        "b.py": "from saddlefit.eval.sealed import SealedStore\n",
        "c.py": "from saddlefit.eval import sealed\n",
        "d.py": "import importlib\nm = importlib.import_module('saddlefit.eval.sealed')\n",
        "eval/ok.py": "from saddlefit.eval.sealed import SealedStore\n",
        "e.py": '"""Mentions saddlefit.eval.sealed in a docstring only."""\n',
    }
    for name, text in samples.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    assert set(offenders(tmp_path)) == {"a.py", "b.py", "c.py", "d.py"}
