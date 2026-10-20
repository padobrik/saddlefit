import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Network stays blocked for every layer except live tests, which talk to a model runner.
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(pytest.mark.enable_socket)
