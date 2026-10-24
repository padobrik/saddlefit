"""``loop.yaml`` migrations (C7)."""

from __future__ import annotations

from typing import Final

from saddlefit.storage.migrations import Record, register

LOOP_KIND: Final = "loop"


@register(LOOP_KIND, 1)
def _v1_to_v2(record: Record) -> Record:
    """v2 drops ``reasoning.language`` and ``reasoning.latent_depth``, which no run used."""
    reasoning = record.get("reasoning")
    if isinstance(reasoning, dict):
        reasoning = {k: v for k, v in reasoning.items() if k not in {"language", "latent_depth"}}
        record = {**record, "reasoning": reasoning}
    return {**record, "schema_version": 2}
