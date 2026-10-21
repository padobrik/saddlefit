from pathlib import Path
from typing import Any

import pytest

from saddlefit.sandbox.container import SandboxError
from saddlefit.sandbox.environment import (
    dockerfile,
    ensure_environment,
    find_dependencies,
    fingerprint,
    image_tag,
)


def write(repo: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    return repo


@pytest.mark.parametrize(
    ("files", "source", "found"),
    [
        ({"pyproject.toml": "", "uv.lock": ""}, "uv.lock", ("pyproject.toml", "uv.lock")),
        (
            {"pyproject.toml": "", "requirements.txt": "", "requirements/dev.txt": ""},
            "requirements",
            ("requirements.txt", "requirements/dev.txt"),
        ),
        ({"pyproject.toml": ""}, "pyproject", ("pyproject.toml",)),
    ],
)
def test_the_lock_file_wins_then_requirements_then_pyproject(
    tmp_path: Path, files: dict[str, str], source: str, found: tuple[str, ...]
) -> None:
    deps = find_dependencies(write(tmp_path, files))
    assert deps is not None
    assert (deps.source, deps.files) == (source, found)


def test_a_repository_without_dependencies_needs_no_image(tmp_path: Path) -> None:
    write(tmp_path, {"src/a.py": "x = 1\n"})
    assert find_dependencies(tmp_path) is None

    class NoDocker:
        images = None  # any use would fail

    assert ensure_environment(NoDocker(), "base", tmp_path, "p") is None


def test_the_fingerprint_follows_contents_and_base_image(tmp_path: Path) -> None:
    write(tmp_path, {"requirements.txt": "pytest==9.1.1\n"})
    deps = find_dependencies(tmp_path)
    assert deps is not None
    first = fingerprint(tmp_path, deps, "sha256:base1")
    assert fingerprint(tmp_path, deps, "sha256:base1") == first
    assert fingerprint(tmp_path, deps, "sha256:base2") != first
    write(tmp_path, {"requirements.txt": "pytest==9.1.2\n"})
    assert fingerprint(tmp_path, deps, "sha256:base1") != first


def test_tags_are_valid_docker_names() -> None:
    assert image_tag("My Project!", "ab" * 32) == f"saddlefit-env-my-project:{'ab' * 8}"
    assert image_tag("", "cd" * 32).startswith("saddlefit-env-project:")


@pytest.mark.parametrize(
    ("source", "files", "needle"),
    [
        ("uv.lock", ("pyproject.toml", "uv.lock"), "uv export --frozen --all-groups"),
        (
            "requirements",
            ("requirements.txt",),
            "pip install --no-cache-dir -r /tmp/deps/requirements.txt",
        ),
        ("pyproject", ("pyproject.toml",), "uv pip compile --all-extras"),
    ],
)
def test_the_dockerfile_installs_as_root_and_ends_as_the_student(
    source: str, files: tuple[str, ...], needle: str
) -> None:
    from saddlefit.sandbox.environment import Dependencies  # noqa: PLC0415

    text = dockerfile("saddlefit-sandbox:test", Dependencies(source, files))  # type: ignore[arg-type]
    assert text.splitlines()[1] == "FROM saddlefit-sandbox:test"
    assert needle in text
    assert text.rstrip().endswith("USER student")


class FakeImages:
    def __init__(self, existing: set[str], fail_build: bool = False) -> None:
        self.existing = existing
        self.fail_build = fail_build
        self.builds: list[dict[str, Any]] = []

    def get(self, name: str) -> Any:
        if name not in self.existing:
            raise LookupError(name)
        return type("Image", (), {"id": f"sha256:{name}"})()

    def build(self, **kwargs: Any) -> None:
        context = Path(kwargs["path"])
        self.builds.append(
            {
                **kwargs,
                "files": sorted(
                    p.relative_to(context).as_posix() for p in context.rglob("*") if p.is_file()
                ),
            }
        )
        if self.fail_build:
            raise RuntimeError("pip install failed")
        self.existing.add(kwargs["tag"])


class FakeDocker:
    def __init__(self, images: FakeImages) -> None:
        self.images = images


def test_an_image_is_built_once_per_set_of_dependencies(tmp_path: Path) -> None:
    write(tmp_path, {"requirements.txt": "pytest\n", "src/a.py": "x = 1\n"})
    images = FakeImages({"base"})
    first = ensure_environment(FakeDocker(images), "base", tmp_path, "toy")
    assert first is not None
    assert first.image.startswith("saddlefit-env-toy:")
    assert len(images.builds) == 1
    assert images.builds[0]["files"] == ["Dockerfile", "deps/requirements.txt"]  # never the code
    again = ensure_environment(FakeDocker(images), "base", tmp_path, "toy")
    assert again is not None
    assert again.image == first.image
    assert len(images.builds) == 1  # reused, not rebuilt
    write(tmp_path, {"requirements.txt": "pytest\nhypothesis\n"})
    changed = ensure_environment(FakeDocker(images), "base", tmp_path, "toy")
    assert changed is not None
    assert changed.image != first.image
    assert len(images.builds) == 2


def test_build_and_base_failures_are_environment_errors(tmp_path: Path) -> None:
    write(tmp_path, {"requirements.txt": "pytest\n"})
    with pytest.raises(SandboxError, match="not available"):
        ensure_environment(FakeDocker(FakeImages(set())), "base", tmp_path, "toy")
    with pytest.raises(SandboxError, match="pip install failed"):
        ensure_environment(
            FakeDocker(FakeImages({"base"}, fail_build=True)), "base", tmp_path, "toy"
        )
