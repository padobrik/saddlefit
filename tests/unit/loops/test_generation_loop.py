"""``tasks generate`` keeps only accepted, calibrated tasks and stays within budget (B13)."""

import random
from typing import Any

import pytest

from saddlefit.config import GeneratorConfig, TokenPrices
from saddlefit.loops.generate import (
    CalibrationError,
    Difficulty,
    Generation,
    task_id_for,
    worst_case_usd,
)
from saddlefit.providers.base import AccountError, ProviderError, Usage
from saddlefit.tasks.acceptance import Acceptance
from saddlefit.tasks.generator import (
    TASK_KINDS,
    GeneratedTask,
    GeneratorRequest,
    GeneratorResult,
)

USAGE = Usage(prompt_tokens=10_000, completion_tokens=2_000)
PRICES = TokenPrices(input=5.0, output=25.0)  # one call: 0.05 + 0.05 = 0.10 USD


def generated(n: int) -> GeneratedTask:
    return GeneratedTask(
        task_md=f"# Task number {n}\n",
        hidden_tests={"tests/test_gen.py": "def test(): ..."},
        reference_patch="diff --git a/x b/x\n",
        visible_check="true",
    )


class World:
    """Fakes for every side effect, with what each saw."""

    def __init__(self, passes: list[int], accepted: list[bool] | None = None) -> None:
        self.passes = list(passes)
        self.accepted = list(accepted or [True] * len(passes))
        self.calls = 0
        self.sealed: list[str] = []
        self.kept: list[str] = []
        self.discarded: list[str] = []
        self.existing: set[str] = set()

    def call(self, request: GeneratorRequest) -> GeneratorResult:
        self.calls += 1
        return GeneratorResult(task=generated(self.calls), usage=USAGE, model="gen")

    def accept(self, task_id: str, task: GeneratedTask) -> Acceptance:
        ok = self.accepted.pop(0)
        return Acceptance(ok, "fine" if ok else "the hidden tests pass without the reference")

    def generation(self, budget: GeneratorConfig | None = None, **changes: Any) -> Generation:
        fields: dict[str, Any] = {
            "kind": "feature",
            "budget": budget or GeneratorConfig(prices_usd_per_mtok=PRICES),
            "request": lambda rng, difficulty: GeneratorRequest(
                TASK_KINDS["feature"], "summary", [], [], ("tests/**",), frozenset()
            ),
            "call": self.call,
            "seal": lambda task_id, task: self.sealed.append(task_id),
            "accept": self.accept,
            "calibrate": lambda task_id, task: self.passes.pop(0),
            "exists": lambda task_id: task_id in self.existing,
            "duplicate_of": lambda task_id, task: None,
            "keep": lambda task_id, task: self.kept.append(task_id),
            "discard": self.discarded.append,
            "prompt_chars": lambda request: 4_000,
            "would_train": lambda task_id: True,
        }
        return Generation(**{**fields, **changes})


def test_tasks_the_student_sometimes_solves_are_kept() -> None:
    world = World(passes=[0, 3, 5, 1])
    report = world.generation().run(count=2)
    assert world.calls == 4
    assert len(report.kept) == 2
    assert world.kept == report.kept
    assert len(world.discarded) == 2  # never solved, and always solved
    assert [a.passes for a in report.attempts] == [0, 3, 5, 1]
    assert report.usd == pytest.approx(0.40)
    assert report.stopped is None


def test_a_task_that_fails_acceptance_is_discarded_before_calibration() -> None:
    world = World(passes=[2], accepted=[False, True])
    report = world.generation().run(count=1)
    assert report.attempts[0].reason == "the hidden tests pass without the reference"
    assert world.discarded == [report.attempts[0].task_id]
    assert world.kept == [report.attempts[1].task_id]


def test_the_call_budget_stops_the_command() -> None:
    world = World(passes=[0, 0, 0])
    budget = GeneratorConfig(max_calls=2, prices_usd_per_mtok=PRICES)
    report = world.generation(budget).run(count=1)
    assert world.calls == 2
    assert report.stopped == "generator.max_calls (2) reached"


def test_the_dollar_budget_stops_before_a_call_that_could_pass_it() -> None:
    world = World(passes=[0, 0, 0])
    # each call costs 0.10; its worst case is about 0.41 (1,000 prompt tokens, a full reply),
    # so after two calls 0.20 + 0.41 could pass 0.6
    budget = GeneratorConfig(max_usd=0.6, prices_usd_per_mtok=PRICES)
    report = world.generation(budget).run(count=1)
    assert world.calls == 2
    assert report.usd == pytest.approx(0.20)
    assert report.stopped == "the next call could pass generator.max_usd (0.6)"


def test_a_refused_answer_still_costs_money() -> None:
    world = World(passes=[1])

    def refuse(request: GeneratorRequest) -> GeneratorResult:
        world.calls += 1
        if world.calls == 1:
            raise ProviderError("the task is unusable: no title", USAGE)
        return world.call(request)

    report = world.generation(call=refuse).run(count=1)
    assert report.attempts[0].reason == "generator: the task is unusable: no title"
    assert report.attempts[0].task_id is None
    assert report.usd == pytest.approx(0.20)


def test_a_crash_during_calibration_leaves_nothing_behind() -> None:
    world = World(passes=[], accepted=[True])

    def crash(task_id: str, task: GeneratedTask) -> int:
        raise RuntimeError("docker went away")

    with pytest.raises(RuntimeError):
        world.generation(calibrate=crash).run(count=1)
    assert world.discarded == world.sealed


def test_task_ids_are_readable_and_unique_per_text() -> None:
    first = task_id_for("bugfix", generated(1))
    assert first.startswith("gen-bugfix-task-number-1-")
    assert first != task_id_for("bugfix", generated(2))


def test_worst_case_counts_a_full_reply() -> None:
    budget = GeneratorConfig(prices_usd_per_mtok=PRICES)
    assert worst_case_usd(4_000, budget) == pytest.approx((1_000 * 5 + 16_000 * 25) / 1e6)


def test_the_same_seed_shows_the_same_files() -> None:
    seen: list[list[float]] = []

    def request(rng: random.Random, difficulty: Difficulty) -> GeneratorRequest:
        seen[-1].append(rng.random())
        return GeneratorRequest(TASK_KINDS["feature"], "s", [], [], ("tests/**",), frozenset())

    for _ in range(2):
        seen.append([])
        World(passes=[0, 2]).generation(request=request).run(count=1)
    assert seen[0] == seen[1]
    assert len(seen[0]) == 2


def test_a_task_already_in_the_suite_is_left_alone() -> None:
    world = World(passes=[2])
    world.existing.add(task_id_for("feature", generated(1)))
    report = world.generation().run(count=1)
    assert report.attempts[0].reason == "the same task is already in the suite"
    assert world.sealed == [report.attempts[1].task_id]
    assert world.discarded == []


def test_a_calibration_without_five_real_runs_is_discarded() -> None:
    world = World(passes=[2])

    def flaky(task_id: str, task: GeneratedTask) -> int:
        raise CalibrationError("only 3 of 5 calibration runs counted")

    report = world.generation(
        budget=GeneratorConfig(max_calls=1, prices_usd_per_mtok=PRICES), calibrate=flaky
    ).run(count=1)
    assert report.attempts[0].reason == "only 3 of 5 calibration runs counted"
    assert world.discarded == world.sealed


def test_a_refused_account_stops_the_command_and_keeps_what_it_has() -> None:
    world = World(passes=[3])
    calls = 0

    def call(request: GeneratorRequest) -> GeneratorResult:
        nonlocal calls
        calls += 1
        if calls == 2:  # the credit runs out after the first task
            raise AccountError("Anthropic API error 400: Your credit balance is too low")
        return world.call(request)

    report = world.generation(call=call).run(count=5)
    assert calls == 2  # no third call: every later one would fail the same way
    assert report.kept == world.kept
    assert len(report.kept) == 1
    assert report.account_refused
    assert report.stopped is not None
    assert "credit balance" in report.stopped


def test_a_refused_account_still_counts_what_was_billed() -> None:
    world = World(passes=[])

    def call(request: GeneratorRequest) -> GeneratorResult:
        raise AccountError("Anthropic API error 402: payment required", USAGE)

    report = world.generation(call=call).run(count=1)
    assert report.account_refused
    assert report.usd == pytest.approx(0.10)


def test_a_repeat_is_dropped_before_sealing_and_judging() -> None:
    world = World(passes=[3])
    accepted: list[str] = []
    report = world.generation(
        duplicate_of=lambda task_id, task: (
            "repeats gen-feature-old: both change x.py::f" if "number 1" in task.task_md else None
        ),
        accept=lambda task_id, task: accepted.append(task_id) or Acceptance(True, "fine"),
    ).run(count=1)
    first, second = report.attempts
    assert (first.kept, first.reason) == (False, "repeats gen-feature-old: both change x.py::f")
    assert world.sealed == [second.task_id]  # the repeat was never sealed
    assert accepted == [second.task_id]  # nor judged
    assert report.kept == [second.task_id]


def test_each_request_hears_how_hard_the_dropped_candidates_were() -> None:
    told: list[Difficulty] = []

    def request(rng: random.Random, difficulty: Difficulty) -> GeneratorRequest:
        told.append(difficulty)
        return GeneratorRequest(TASK_KINDS["feature"], "s", [], [], ("tests/**",), frozenset())

    world = World(passes=[5, 0, 2, 5, 3], accepted=[True, True, True, False, True, True])
    world.generation(request=request).run(count=2)
    # kept tasks (2 and 3 of 5) and the one that failed acceptance are never counted
    assert told == [
        Difficulty(0, 0),
        Difficulty(1, 0),
        Difficulty(1, 1),
        Difficulty(1, 1),
        Difficulty(1, 1),
        Difficulty(2, 1),
    ]


def test_a_candidate_that_maps_onto_held_out_is_never_counted() -> None:
    told: list[Difficulty] = []

    def request(rng: random.Random, difficulty: Difficulty) -> GeneratorRequest:
        told.append(difficulty)
        return GeneratorRequest(TASK_KINDS["feature"], "s", [], [], ("tests/**",), frozenset())

    world = World(passes=[5, 0, 5, 2])
    # the second and third candidates' ids map onto held-out: their runs stay out of the prompt
    held_out = {task_id_for("feature", generated(n)) for n in (2, 3)}
    world.generation(request=request, would_train=lambda t: t not in held_out).run(count=1)
    assert told == [Difficulty(0, 0), Difficulty(1, 0), Difficulty(1, 0), Difficulty(1, 0)]
