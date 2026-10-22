"""``saddlefit export --format skill``: the fitted harness as Agent Skills (B14)."""

import json
from pathlib import Path

import pytest
import yaml
from tests.fixtures.toyrepo.build import build
from typer.testing import CliRunner

from saddlefit.cli import app
from saddlefit.eval.leak_guard import LeakDetected, LeakGuard
from saddlefit.export.skill import export_skill, skill_name
from saddlefit.project import Project

pytestmark = pytest.mark.integration
runner = CliRunner()


def new_file(path: str, lines: list[str]) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


SKILL = ["---", "name: dates", "description: Parsing ISO dates here", "---", "- Use fromisoformat."]
LESSONS = (
    "diff --git a/EXPERIENCE.md b/EXPERIENCE.md\n--- a/EXPERIENCE.md\n+++ b/EXPERIENCE.md\n"
    "@@ -1 +1,3 @@\n # Lessons\n"
    "+- (model: qwen3:8b) Run the visible tests before finishing.\n"
    "+- (model: llama3:8b) A lesson for another model.\n"
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    repo = build(tmp_path / "repo")
    project = Project(repo)
    project.init()
    monkeypatch.chdir(repo)
    patch = (
        new_file("skills/dates/SKILL.md", SKILL)
        + new_file("tools/find_old.py", ["print('ok')"])
        + LESSONS
    )
    project.harness.propose("p1", patch, "fitted")
    project.harness.accept("p1")
    return project


def test_export_writes_the_harness_as_skills(project: Project, tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = runner.invoke(app, ["--json", "export", "--format", "skill", "--out", str(out)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [Path(p).name for p in data["skills"]] == ["dates", "saddlefit-repo"]

    main = (out / "saddlefit-repo" / "SKILL.md").read_text(encoding="utf-8")
    meta = frontmatter(main)
    assert meta["name"] == "saddlefit-repo"
    manifest = json.loads((out / "export.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["skills"] == ["dates", "saddlefit-repo"]
    assert manifest["harness_commit"] == project.harness.head()
    assert "fitted by Saddlefit for qwen3:8b" in main
    assert "You are a careful software engineer." in main  # the system prompt
    assert "run `pytest -q`; finish only when it passes" in main  # loop.yaml's finish gate
    assert "Run the visible tests before finishing." in main
    assert "another model" not in main  # lessons for other models stay out
    assert (out / "saddlefit-repo" / "scripts" / "find_old.py").read_text() == "print('ok')\n"
    assert (out / "dates" / "SKILL.md").read_text(encoding="utf-8") == "\n".join(SKILL) + "\n"
    exported = {p.name for p in out.rglob("*") if p.is_file()}
    assert {"harness.json", "loop.yaml", "EXPERIENCE.md"}.isdisjoint(exported)


def test_export_of_another_ref(project: Project, tmp_path: Path) -> None:
    first = project.harness.head("main~1")
    out = tmp_path / "old"
    result = runner.invoke(app, ["export", "--out", str(out), "--ref", first])
    assert result.exit_code == 0, result.output
    # before the skill was added
    assert sorted(p.name for p in out.iterdir()) == ["export.json", "saddlefit-repo"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--format", "agents-md"], "not available yet"),
        (["--ref", "no-such-ref"], "cannot export"),
    ],
)
def test_export_user_errors(project: Project, args: list[str], message: str) -> None:
    result = runner.invoke(app, ["export", *args])
    assert result.exit_code == 1
    assert message in str(result.exception)


def test_export_never_overwrites(project: Project, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    result = runner.invoke(app, ["export", "--out", str(out)])
    assert result.exit_code == 1
    assert "already exists" in str(result.exception)


def test_a_canary_stops_the_export(
    project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canary = "saddlefit-canary-0d15ea5e"
    monkeypatch.setattr("saddlefit.project.guard_for_suite", lambda *a: LeakGuard({canary: "t1"}))
    patch = new_file("skills/leak/SKILL.md", ["---", "name: leak", "description: x", "---", canary])
    project.harness.propose("p2", patch, "leaky")
    project.harness.accept("p2")
    out = tmp_path / "out"
    result = runner.invoke(app, ["export", "--out", str(out)])
    assert isinstance(result.exception, LeakDetected)
    assert result.exception.exit_code == 2  # what `saddlefit` exits with
    assert not out.exists()


def test_default_export_location(project: Project) -> None:
    assert runner.invoke(app, ["export"]).exit_code == 0
    assert runner.invoke(app, ["export"]).exit_code == 0  # in the same second
    folders = sorted((project.state / "exports").iterdir())
    assert len(folders) == 2
    for folder in folders:
        assert folder.name.startswith("skill-")
        assert (folder / "saddlefit-repo" / "SKILL.md").exists()


def frontmatter(text: str) -> dict[str, object]:
    head = text.split("---\n")[1]
    loaded = yaml.safe_load(head)
    assert isinstance(loaded, dict)
    return loaded


def test_a_project_name_with_yaml_syntax_stays_valid(project: Project, tmp_path: Path) -> None:
    out = tmp_path / "out"
    export_skill(
        project.harness,
        "main",
        out,
        project="team: service #1",
        model="qwen3:8b",
        guard=LeakGuard({}),
    )
    (main,) = [p for p in out.iterdir() if p.name.startswith("saddlefit-")]
    meta = frontmatter((main / "SKILL.md").read_text(encoding="utf-8"))
    assert meta["name"] == "saddlefit-team-service-1"
    assert "team: service #1" in str(meta["description"])


def test_a_harness_skill_with_the_generated_name_is_kept(project: Project, tmp_path: Path) -> None:
    clash = new_file(
        "skills/saddlefit-repo/SKILL.md", ["---", "name: saddlefit-repo", "description: x", "---"]
    )
    project.harness.propose("p3", clash, "clash")
    project.harness.accept("p3")
    out = tmp_path / "out"
    assert runner.invoke(app, ["export", "--out", str(out)]).exit_code == 0
    assert sorted(p.name for p in out.iterdir() if p.is_dir()) == [
        "dates",
        "saddlefit-repo",
        "saddlefit-repo-2",
    ]
    assert frontmatter((out / "saddlefit-repo-2" / "SKILL.md").read_text())["name"] == (
        "saddlefit-repo-2"
    )


@pytest.mark.parametrize(
    ("project_name", "name"),
    [
        ("repo", "saddlefit-repo"),
        ("My_Service.v2", "saddlefit-my-service-v2"),
        ("---", "saddlefit-project"),
        ("x" * 80, ("saddlefit-" + "x" * 80)[:64]),
    ],
)
def test_skill_names(project_name: str, name: str) -> None:
    assert skill_name(project_name) == name
