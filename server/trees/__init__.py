"""Perspective-tree builders.

Every builder takes (db, lang, path: list[str]) and returns either:
  {'children': [...]}  — for intermediate nodes
  {'chunks':   [...]}  — for leaf nodes
plus a 'node' dict describing the current position in the tree.
"""
from __future__ import annotations

from typing import Callable, Protocol

from . import scripture, source, kind, term, methodology, pericope, aquifer


class TreeBuilder(Protocol):
    def root(self, db, *, lang: str) -> dict: ...
    def descend(self, db, path: list[str], *, lang: str) -> dict: ...


BUILDERS: dict[str, TreeBuilder] = {
    "scripture":   scripture,    # type: ignore[dict-item]
    "source":      source,       # type: ignore[dict-item]
    "kind":        kind,         # type: ignore[dict-item]
    "term":        term,         # type: ignore[dict-item]
    "methodology": methodology,  # type: ignore[dict-item]
    "pericope":    pericope,     # type: ignore[dict-item]
    "aquifer":     aquifer,      # type: ignore[dict-item]
}
