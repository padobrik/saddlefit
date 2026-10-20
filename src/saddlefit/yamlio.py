"""YAML loading that refuses duplicate keys (PyYAML silently keeps the last one)."""

from __future__ import annotations

from typing import Any

import yaml


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that raises on a mapping key that appears twice, at any depth."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    """Build a mapping, refusing unhashable and repeated keys."""
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found unhashable key {key!r}",
                key_node.start_mark,
            ) from None
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def safe_load_unique(text: str) -> Any:
    """``yaml.safe_load`` that rejects duplicate mapping keys."""
    return yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - a SafeLoader subclass
