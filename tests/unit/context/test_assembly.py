from pathlib import Path

from saddlefit.context.assembly import (
    TIER_HARNESS_BUDGET,
    Harness,
    Skill,
    assemble,
    estimate_tokens,
    guidance_for,
    rank_lessons,
    rank_skills,
)

GUIDANCE = """Prefer small edits.

## read_file
Read before you edit.

## bash
Run the tests before finishing.

## search
Use search to find definitions.
"""
TASK = "Fix the date parser: parse_iso swaps month and day."
PYDANTIC = Skill("pydantic-v2", "Migrating Pydantic v1 models to v2", "- .dict() -> .model_dump()")
DATES = Skill("dates", "bugfix help for date parsing", "- check month/day order")
OTHER = Skill("css", "styling web pages", "- nothing relevant")


def harness(**overrides: object) -> Harness:
    base: dict[str, object] = {
        "system_prompt": "You are careful.",
        "tool_guidance": GUIDANCE,
        "skills": (PYDANTIC, DATES, OTHER),
        "experience": "# Lessons\n"
        "- (model: qwen3:8b) run the tests before finish; date bugs hide in order\n"
        "- (model: llama3:8b) unrelated lesson for another model\n"
        "- unlabelled lesson about parse helpers\n",
    }
    base.update(overrides)
    return Harness(**base)  # type: ignore[arg-type]


def build(budget: int = 10_000, **kwargs: object):  # type: ignore[no-untyped-def]
    params: dict[str, object] = {
        "task_text": TASK,
        "task_type": "bugfix",
        "model": "qwen3:8b",
        "enabled_tools": ["read_file", "bash"],
        "harness_budget_tokens": budget,
    }
    params.update(kwargs)
    return assemble(harness(), **params)  # type: ignore[arg-type]


def test_order_and_selection() -> None:
    ctx = build()
    s = ctx.system
    order = [
        s.index("You are careful."),
        s.index("Prefer small edits."),
        s.index("## read_file"),
        s.index("## bash"),
        s.index("## Skill: dates"),
        s.index("## Lessons"),
    ]
    assert order == sorted(order)
    assert "## search" not in s  # tool not enabled
    assert ctx.skills == ["dates"]  # only relevant skills; css and pydantic do not match
    assert "llama3" not in s  # lesson for another model
    assert ctx.lessons == 2
    assert ctx.task == TASK
    assert ctx.harness_tokens == estimate_tokens(s)
    assert not ctx.over_budget


def test_drop_order_lessons_then_skills_then_tool_details() -> None:
    full = build()
    no_lessons = build(budget=full.harness_tokens - 1)
    assert no_lessons.dropped
    assert all(d.startswith("lesson:") for d in no_lessons.dropped)
    tiny = build(budget=estimate_tokens("You are careful.\n\nPrefer small edits.\n"))
    kinds = [d.split(":")[0] for d in tiny.dropped]
    assert kinds == ["lesson", "lesson", "skill", "tool guidance", "tool guidance"]
    assert "Prefer small edits." in tiny.system  # the preamble stays
    assert tiny.task == TASK  # never cut
    assert not tiny.over_budget


def test_over_budget_is_reported_when_nothing_is_left_to_drop() -> None:
    ctx = build(budget=1)
    assert ctx.over_budget
    assert "You are careful." in ctx.system


def test_max_skills_and_ranking() -> None:
    skills = (
        Skill("a", "bugfix tips", "x"),
        Skill("b", "bugfix and date parser tips", "x"),
        Skill("c", "date parser", "x"),
        Skill("d", "bugfix", "x"),
    )
    ranked = rank_skills(skills, "bugfix", TASK)
    assert [s.name for s in ranked][:2] == ["b", "a"]
    ctx = assemble(
        harness(skills=skills),
        task_text=TASK,
        task_type="bugfix",
        model="m",
        enabled_tools=[],
        harness_budget_tokens=10_000,
        max_skills=2,
    )
    assert ctx.skills == ["b", "a"]


def test_guidance_sections() -> None:
    preamble, sections = guidance_for(GUIDANCE, ["search"])
    assert preamble == "Prefer small edits."
    assert sections == ["## search\nUse search to find definitions.\n"]


def test_lessons_rank_by_overlap() -> None:
    lessons = rank_lessons(
        "- a lesson about css\n- (for: m) date parser order matters\n", "m", TASK
    )
    assert lessons[0].startswith("- (for: m) date parser")


def test_load_from_a_harness_directory(tmp_path: Path) -> None:
    (tmp_path / "skills" / "dates").mkdir(parents=True)
    (tmp_path / "skills" / "dates" / "SKILL.md").write_text(
        "---\nname: date-fixes\ndescription: bugfix help\n---\n- check order\n", encoding="utf-8"
    )
    (tmp_path / "skills" / "plain").mkdir()
    (tmp_path / "skills" / "plain" / "SKILL.md").write_text("no frontmatter\n", encoding="utf-8")
    (tmp_path / "skills" / "broken").mkdir()
    (tmp_path / "skills" / "broken" / "SKILL.md").write_text(
        "---\n: [\n---\nbody\n", encoding="utf-8"
    )
    (tmp_path / "systemprompt.md").write_text("Be careful.\n", encoding="utf-8")
    loaded = Harness.load(tmp_path)
    assert loaded.system_prompt == "Be careful.\n"
    assert loaded.tool_guidance == ""
    assert [s.name for s in loaded.skills] == ["broken", "date-fixes", "plain"]
    assert loaded.skills[1].description == "bugfix help"


def test_tier_budgets_match_spec() -> None:
    assert TIER_HARNESS_BUDGET == {"S": 2500, "M": 4000, "L": 6000}
    assert estimate_tokens("abcd") == 2


def test_short_terms_count_for_skill_selection() -> None:
    skills = (Skill("go-tips", "Go modules", "x"), Skill("ui", "UI layout rules", "y"))
    ranked = rank_skills(skills, "change", "Fix the UI layout of the settings page")
    assert [s.name for s in ranked] == ["ui"]
    assert [s.name for s in rank_skills(skills, "change", "Bump Go to 1.23")] == ["go-tips"]
    # short stop words alone never select a skill
    assert rank_skills((Skill("of", "an it to", "z"),), "change", "it is an issue of to") == []


def test_only_real_tool_names_are_tool_sections() -> None:
    guidance = "Intro.\n\n## Safety\nNever delete user data.\n\n## bash\nRun tests.\n"
    preamble, sections = guidance_for(guidance, [])
    assert "## Safety" in preamble
    assert "Never delete user data." in preamble
    assert sections == []
    _, enabled = guidance_for(guidance, ["bash"])
    assert enabled == ["## bash\nRun tests.\n"]


def test_general_sections_after_a_tool_section_survive() -> None:
    guidance = "Intro.\n\n## bash\nRun tests.\n\n## Safety\nNever delete user data.\n"
    preamble, sections = guidance_for(guidance, [])
    assert "## Safety\nNever delete user data." in preamble
    assert "Run tests." not in preamble
    assert sections == []
    _, enabled = guidance_for(guidance, ["bash"])
    assert enabled == ["## bash\nRun tests.\n"]


def test_harness_scripts_are_listed_for_the_student() -> None:
    ctx = build(scripts=["run_one_test.sh", "lint.py"])
    assert "## Harness scripts" in ctx.system
    assert "- /harness/tools/lint.py\n- /harness/tools/run_one_test.sh" in ctx.system
    assert "Harness scripts" not in build().system
