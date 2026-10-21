from datetime import UTC, datetime

from saddlefit.tasks.mining import Candidate
from saddlefit.tasks.suite import task_text_for

PR_BODY = """## Summary

Adds the `saddlefit.yaml` configuration model and loader (spec B4).

- `load_config` reports every problem with its dotted path.
- [x] not a checkbox inside a list item of the summary

## Linked issue

Closes #5

## Checklist

- [x] Tests added or updated; `uv run pytest` passes locally

## Evaluation-integrity impact

None.
"""


def candidate(subject: str, body: str) -> Candidate:
    return Candidate(
        sha="a" * 40,
        parent="b" * 40,
        committed=datetime(2026, 10, 20, tzinfo=UTC),
        subject=subject,
        body=body,
        test_files=("tests/unit/test_config.py",),
        source_files=("src/saddlefit/config.py",),
    )


def test_a_pr_description_becomes_an_instruction() -> None:
    text = task_text_for(
        candidate("feat(config): add saddlefit.yaml model and loader (#55)", PR_BODY)
    )
    assert text.startswith("# add saddlefit.yaml model and loader\n")
    assert "not in the repository yet" in text
    assert "`load_config` reports every problem" in text
    for leftover in (
        "Linked issue",
        "Closes #5",
        "Checklist",
        "Tests added",
        "Evaluation-integrity",
    ):
        assert leftover not in text
    assert "- [x]" not in text


def test_a_plain_commit_keeps_its_message() -> None:
    text = task_text_for(
        candidate(
            "Fix month and day order in parse_iso",
            "Dates were read as MM-DD.\n\nSigned-off-by: a <a@b>",
        )
    )
    assert text.startswith("# Fix month and day order in parse_iso\n")
    assert "Dates were read as MM-DD." in text
    assert "Signed-off-by" not in text


def test_an_empty_body_still_makes_a_task() -> None:
    text = task_text_for(candidate("fix: handle empty input", ""))
    assert text.startswith("# handle empty input\n")
    assert "not in the repository yet" in text


def test_an_identifier_in_the_subject_stays_exact() -> None:
    text = task_text_for(candidate("fix: parse_iso reads month before day", ""))
    assert text.startswith("# parse_iso reads month before day\n")
