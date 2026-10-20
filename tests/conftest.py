import pytest
from hypothesis import settings


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Network stays blocked for every layer except live tests, which talk to a model runner.
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(pytest.mark.enable_socket)


# Property tests draw the same examples on every run, so CI results are reproducible.
settings.register_profile("deterministic", derandomize=True, deadline=None)
settings.load_profile("deterministic")
