import json
import os
import urllib.request

import pytest

pytestmark = pytest.mark.live

BASE_URL = os.environ.get("SADDLEFIT_LIVE_BASE_URL", "http://localhost:11434/v1")
MODEL = os.environ.get("SADDLEFIT_LIVE_MODEL", "qwen3:0.6b")


def test_runner_serves_the_model() -> None:
    with urllib.request.urlopen(f"{BASE_URL}/models", timeout=30) as resp:  # noqa: S310
        models = {m["id"] for m in json.load(resp)["data"]}
    assert MODEL in models
