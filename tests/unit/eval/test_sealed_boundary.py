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


def _static_str(node: ast.AST) -> str | None:
    """The value of a string expression built only from literals, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_str(node.left), _static_str(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        parts = [
            _static_str(v.value if isinstance(v, ast.FormattedValue) else v) for v in node.values
        ]
        return "".join(p for p in parts if p is not None) if None not in parts else None
    return None


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
        # The two branches below are deliberately broad: outside eval/, ANY attribute named
        # `sealed` and ANY call with a "sealed" argument fails test 11, even a harmless
        # `config.sealed`. For a security boundary a false positive (rename the attribute)
        # is the safe failure mode; a false negative would leak sealed data.
        elif isinstance(node, ast.Attribute):
            # e.g. ev.sealed.SealedStore after `from .. import eval as ev`
            hit = node.attr == "sealed"
        elif isinstance(node, ast.Call):
            # getattr(ev, "sealed") or importlib.import_module(".sealed", "saddlefit.eval")
            hit = any(_static_str(arg) in {"sealed", ".sealed"} for arg in node.args)
        elif isinstance(node, ast.Constant | ast.BinOp | ast.JoinedStr):
            # "saddlefit.eval.sealed", also built as "saddlefit.eval." + "sealed" or an f-string
            hit = ACCESSOR in (_static_str(node) or "")
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
        "student/i.py": "from .. import eval as ev\nstore = ev.sealed.SealedStore\n",
        "student/j.py": "import saddlefit.eval as ev\nmod = getattr(ev, 'sealed')\n",
        "student/k.py": (
            "import importlib\nm = importlib.import_module('.sealed', 'saddlefit.eval')\n"
        ),
        "student/l.py": (
            "import importlib\nm = importlib.import_module('saddlefit.eval.' + 'sealed')\n"
        ),
        "student/m.py": (
            "import importlib\nm = importlib.import_module(f'saddlefit.{\"eval\"}.sealed')\n"
        ),
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
        "student/i.py",
        "student/j.py",
        "student/k.py",
        "student/l.py",
        "student/m.py",
    }
