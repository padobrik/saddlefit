import pytest

from saddlefit.compact import ModelFacts, classify, fetch_ollama_facts, parse_ollama_show

# the shape of Ollama's /api/show for qwen3:8b, trimmed to the keys doctor reads
QWEN3_8B = {
    "model_info": {
        "general.architecture": "qwen3",
        "general.parameter_count": 8_190_735_360,
        "qwen3.attention.head_count_kv": 8,
        "qwen3.attention.key_length": 128,
        "qwen3.block_count": 36,
        "qwen3.context_length": 40960,
    }
}
QWEN3_30B_A3B = {
    "model_info": {
        "general.architecture": "qwen3moe",
        "general.parameter_count": 30_532_122_624,
        "qwen3moe.expert_count": 128,
        "qwen3moe.expert_used_count": 8,
        "qwen3moe.attention.head_count_kv": 4,
        "qwen3moe.attention.key_length": 128,
        "qwen3moe.block_count": 48,
        "qwen3moe.context_length": 262144,
    }
}
DENSE_70B = {"model_info": {"general.architecture": "llama", "general.parameter_count": 70 * 10**9}}


def test_dense_8b_is_tier_m() -> None:
    facts = parse_ollama_show(QWEN3_8B)
    assert facts is not None
    assert not facts.moe
    assert facts.active_params == facts.total_params
    assert facts.max_context == 40960
    assert 9.0 < facts.memory_gb() < 10.0  # 4.5 GB of weights plus a 4.8 GB 32k KV cache
    assert classify(facts, 25.0, tools_ok=True) == ("M", [])


def test_mixture_of_experts_counts_active_parameters() -> None:
    facts = parse_ollama_show(QWEN3_30B_A3B)
    assert facts is not None
    assert facts.moe
    assert 1.5e9 < facts.active_params < 2.5e9
    assert classify(facts, 40.0, tools_ok=True) == ("L", [])


def test_small_dense_is_tier_s() -> None:
    assert classify(ModelFacts(total_params=4 * 10**9), 30.0, tools_ok=True)[0] == "S"
    assert classify(ModelFacts(total_params=600 * 10**6), 90.0, tools_ok=True)[0] == "S"


def test_every_failed_condition_is_named() -> None:
    facts = parse_ollama_show(DENSE_70B)
    tier, failed = classify(facts, 5.0, tools_ok=False)
    assert tier == "not compact"
    assert [f.split(":")[0] for f in failed] == ["memory", "active size", "speed", "tool use"]


def test_unknown_size_is_not_guessed() -> None:
    assert classify(None, 30.0, tools_ok=True) == ("unknown", [])
    assert classify(None, 5.0, tools_ok=True)[0] == "not compact"


@pytest.mark.parametrize(
    "data",
    [None, {}, {"model_info": {}}, {"model_info": {"general.parameter_count": "8B"}}, []],
)
def test_metadata_without_a_parameter_count_gives_no_facts(data: object) -> None:
    assert parse_ollama_show(data) is None


def test_fetch_refuses_non_http_urls() -> None:
    assert fetch_ollama_facts("file:///etc/passwd", "m") is None
