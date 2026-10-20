"""Required test 4: splits are deterministic, and adding tasks never moves existing ones."""

from collections import Counter

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from saddlefit.config import SplitsConfig
from saddlefit.tasks import splits as splits_module
from saddlefit.tasks.models import LockedTask, Source, SplitFractions, SuiteLock
from saddlefit.tasks.splits import (
    SplitError,
    add_task,
    assign_split,
    check_fractions,
    new_lock,
    split_point,
)

SALT = bytes.fromhex("11" * 32)
FRACTIONS = SplitFractions(train=0.5, held_out=0.25, control=0.25)
HASH = "sha256:" + "0" * 64


@pytest.fixture(autouse=True)
def fixed_salt(monkeypatch: pytest.MonkeyPatch) -> None:
    """``new_lock`` draws a random salt; pin it so every assignment here is reproducible."""
    monkeypatch.setattr(splits_module.secrets, "token_hex", lambda n: SALT.hex())


task_ids = st.from_regex(r"\A[a-z0-9][a-z0-9._-]{0,39}\Z")


def test_known_vectors_are_stable() -> None:
    # Pinned against openssl: HMAC-SHA256(0x11 * 32, id) starts with 25140be915921863.
    # A change here would silently reshuffle every existing suite.
    assert split_point(SALT, "fix-date-parser-0042") == 0x25140BE915921863 / 2**64
    assert assign_split(SALT, "fix-date-parser-0042", FRACTIONS, "history") == "train"


@given(task_ids)
def test_assignment_is_deterministic(task_id: str) -> None:
    first = assign_split(SALT, task_id, FRACTIONS, "history")
    assert all(assign_split(SALT, task_id, FRACTIONS, "history") == first for _ in range(3))


@settings(max_examples=50)
@given(st.lists(task_ids, unique=True, min_size=1, max_size=30), st.lists(task_ids, max_size=30))
def test_adding_tasks_never_moves_existing_ones(first: list[str], more: list[str]) -> None:
    lock = new_lock(SplitsConfig())
    for tid in first:
        lock = add_task(lock, tid, "history", HASH)
    before = {t.id: t.split for t in lock.tasks}
    for tid in more:
        lock = add_task(lock, tid, "history", HASH)
    after = {t.id: t.split for t in lock.tasks}
    assert all(after[tid] == split for tid, split in before.items())


def test_salt_changes_the_assignment() -> None:
    ids = [f"task-{i}" for i in range(200)]
    other = bytes.fromhex("22" * 32)
    moved = sum(
        assign_split(SALT, t, FRACTIONS, "history") != assign_split(other, t, FRACTIONS, "history")
        for t in ids
    )
    assert moved > 50


def test_distribution_follows_the_fractions() -> None:
    counts = Counter(assign_split(SALT, f"t{i}", FRACTIONS, "history") for i in range(4000))
    assert counts["train"] / 4000 == pytest.approx(0.5, abs=0.03)
    assert counts["held_out"] / 4000 == pytest.approx(0.25, abs=0.03)
    assert counts["control"] / 4000 == pytest.approx(0.25, abs=0.03)


@pytest.mark.parametrize("source", ["generated", "mutation"])
def test_generated_and_mutated_tasks_never_enter_control(source: Source) -> None:
    splits = {assign_split(SALT, f"t{i}", FRACTIONS, source) for i in range(500)}
    assert splits == {"train", "held_out"}


def test_control_only_fractions_reject_generated_tasks() -> None:
    only_control = SplitFractions(train=0, held_out=0, control=1)
    with pytest.raises(SplitError, match="no train or held-out"):
        assign_split(SALT, "t", only_control, "generated")


def test_existing_task_keeps_split_and_updates_hash() -> None:
    lock = add_task(new_lock(SplitsConfig()), "a", "history", HASH)
    new_hash = "sha256:" + "f" * 64
    updated = add_task(lock, "a", "history", new_hash)
    assert updated.tasks[0].split == lock.tasks[0].split
    assert updated.tasks[0].content_hash == new_hash
    assert updated.suite_hash() != lock.suite_hash()
    with pytest.raises(SplitError, match="recorded as history"):
        add_task(lock, "a", "generated", HASH)


def test_tampered_salt_is_detected() -> None:
    lock = new_lock(SplitsConfig())
    forged = lock.model_copy(update={"salt": "22" * 32})
    with pytest.raises(SplitError, match="digest"):
        add_task(forged, "a", "history", HASH)


def test_fractions_are_fixed_at_init() -> None:
    lock = new_lock(SplitsConfig())
    check_fractions(lock, SplitsConfig())
    with pytest.raises(SplitError, match="never change"):
        check_fractions(lock, SplitsConfig(train=0.6, held_out=0.2, control=0.2))


def test_lock_round_trips_and_hash_ignores_order() -> None:
    lock = new_lock(SplitsConfig())
    for tid in ["b", "a", "c"]:
        lock = add_task(lock, tid, "user", HASH)
    assert [t.id for t in lock.tasks] == ["a", "b", "c"]
    again = SuiteLock.model_validate_json(lock.model_dump_json())
    assert again == lock
    reordered = lock.model_copy(update={"tasks": tuple(reversed(lock.tasks))})
    assert reordered.suite_hash() == lock.suite_hash()
    assert len(lock.salt) == 64


def test_lock_rejects_control_for_generated_or_mutated_tasks() -> None:
    for source in ("generated", "mutation"):
        with pytest.raises(ValidationError, match="never control"):
            LockedTask(id="t", split="control", source=source, content_hash=HASH)
    LockedTask(id="t", split="control", source="history", content_hash=HASH)


def test_persisted_fractions_must_sum_to_one() -> None:
    with pytest.raises(ValidationError, match=r"sum to 1\.0"):
        SplitFractions(train=0.5, held_out=0.5, control=0.5)


def test_lock_rejects_duplicate_task_ids() -> None:
    lock = add_task(new_lock(SplitsConfig()), "a", "history", HASH)
    data = lock.model_dump()
    data["tasks"] = [data["tasks"][0], {**data["tasks"][0], "content_hash": "sha256:" + "f" * 64}]
    with pytest.raises(ValidationError, match="duplicate task id"):
        SuiteLock.model_validate(data)


def test_replacement_hash_is_validated() -> None:
    lock = add_task(new_lock(SplitsConfig()), "a", "history", HASH)
    with pytest.raises(ValidationError):
        add_task(lock, "a", "history", "not-a-hash")
