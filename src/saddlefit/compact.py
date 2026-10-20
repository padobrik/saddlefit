"""What counts as a compact model (A3): size facts from the runner and the tier they give.

Memory and active size come from the model's metadata (Ollama's ``/api/show``); a runner
without that metadata leaves the tier unknown rather than guessed.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import urlsplit, urlunsplit

MEMORY_LIMIT_GB: Final = 24.0
ACTIVE_LIMIT: Final = 10e9
SPEED_LIMIT_TPS: Final = 20.0
TIER_CONTEXT: Final = 32768
PROBES: Final = 10
PROBES_NEEDED: Final = 9
SMALL_DENSE_LIMIT: Final = 4e9  # tier S is dense 1-4B; anything larger and dense is tier M

Tier = Literal["S", "M", "L", "not compact", "unknown"]


@dataclass(frozen=True)
class ModelFacts:
    """Model size facts from the runner, enough to estimate memory and active size."""

    total_params: int
    expert_count: int = 0
    experts_used: int = 0
    block_count: int | None = None
    kv_heads: int | None = None
    key_length: int | None = None
    max_context: int | None = None

    @property
    def moe(self) -> bool:
        return self.expert_count > 1 and self.experts_used > 0

    @property
    def active_params(self) -> int:
        """All parameters for a dense model; the routed share of them for a mixture of experts.

        The share overstates the attention and embedding parameters every token uses only a
        little, so it is a rough estimate in the model's favour.
        """
        if not self.moe:
            return self.total_params
        return round(self.total_params * self.experts_used / self.expert_count)

    def memory_gb(self, context: int = TIER_CONTEXT) -> float:
        """4-bit weights (plus 10% for scales and embeddings) and an fp16 KV cache."""
        weights = self.total_params * 0.5 * 1.1
        kv = 0
        if self.block_count and self.kv_heads and self.key_length:
            kv = 2 * self.block_count * self.kv_heads * self.key_length * context * 2
        return (weights + kv) / 1e9


def _int(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def parse_ollama_show(data: Any) -> ModelFacts | None:
    """Model facts from an Ollama ``/api/show`` response; ``None`` without a parameter count."""
    info = data.get("model_info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return None
    total = _int(info.get("general.parameter_count"))
    if total is None:
        return None
    arch = info.get("general.architecture")
    prefix = f"{arch}." if isinstance(arch, str) else ""
    return ModelFacts(
        total_params=total,
        expert_count=_int(info.get(f"{prefix}expert_count")) or 0,
        experts_used=_int(info.get(f"{prefix}expert_used_count")) or 0,
        block_count=_int(info.get(f"{prefix}block_count")),
        kv_heads=_int(info.get(f"{prefix}attention.head_count_kv")),
        key_length=_int(info.get(f"{prefix}attention.key_length")),
        max_context=_int(info.get(f"{prefix}context_length")),
    )


def fetch_ollama_facts(base_url: str, model: str, timeout_s: float = 10) -> ModelFacts | None:
    """Ask the runner's native Ollama API; ``None`` when it is not Ollama or says nothing."""
    parts = urlsplit(base_url)
    root = urlunsplit(
        (parts.scheme, parts.netloc, parts.path.rstrip("/").removesuffix("/v1"), "", "")
    )
    if parts.scheme not in {"http", "https"}:
        return None
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        f"{root}/api/show",
        data=json.dumps({"model": model}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            return parse_ollama_show(json.loads(response.read()))
    except (OSError, ValueError):
        return None


def classify(
    facts: ModelFacts | None, tokens_per_second: float, tools_ok: bool
) -> tuple[Tier, list[str]]:
    """The A3 tier, or "not compact" with every condition that failed."""
    failed = []
    if facts is not None and facts.memory_gb() > MEMORY_LIMIT_GB:
        failed.append(f"memory: about {facts.memory_gb():.1f} GB at 4-bit with 32k context")
    if facts is not None and facts.active_params > ACTIVE_LIMIT:
        failed.append(f"active size: about {facts.active_params / 1e9:.1f}B parameters per token")
    if tokens_per_second < SPEED_LIMIT_TPS:
        failed.append(f"speed: {tokens_per_second:.1f} tokens/s")
    if not tools_ok:
        failed.append(f"tool use: fewer than {PROBES_NEEDED} of {PROBES} probes succeeded")
    if failed:
        return "not compact", failed
    if facts is None:
        return "unknown", []
    if facts.moe:
        return "L", []
    return ("S" if facts.total_params <= SMALL_DENSE_LIMIT else "M"), []
