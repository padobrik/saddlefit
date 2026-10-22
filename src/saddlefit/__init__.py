"""Saddlefit fits an agent's harness to one compact model, on your own tasks."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("saddlefit")
except PackageNotFoundError:  # pragma: no cover - only when running from an unbuilt tree
    __version__ = "0.2.0"

__all__ = ["__version__"]
