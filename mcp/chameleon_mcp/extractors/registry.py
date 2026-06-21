"""Extractor registry.

The ordered list of extractor classes bootstrap chooses from. A new language is
added by appending its extractor here (or via :func:`register`) plus mapping its
AST to the ``ParsedFile`` shape -- no edit to bootstrap's selection logic.
``select_extractor`` iterates the same ``(TypeScript, Ruby)`` precedence the
former hardcoded loop used, so existing profiles do not re-cluster.

A Tree-sitter-backed extractor is then a new class that satisfies the
``Extractor`` protocol plus a ``register()`` call; the bespoke ts_dump.mjs /
prism_dump.rb paths stay as-is alongside it.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.extractors._base import Extractor
from chameleon_mcp.extractors.ruby import RubyExtractor
from chameleon_mcp.extractors.typescript import TypeScriptExtractor

# Order is precedence: the first extractor whose can_handle() matches wins.
# TypeScript before Ruby preserves the historical bootstrap order.
EXTRACTORS: list[type[Extractor]] = [TypeScriptExtractor, RubyExtractor]


def register(extractor_cls: type[Extractor]) -> None:
    """Append an extractor class to the registry (idempotent).

    Call at import time only. ``EXTRACTORS`` is read (never mutated) on the hook
    and bootstrap paths, so registering after a worker is serving select calls
    would race; there is no runtime registration path today.
    """
    if extractor_cls not in EXTRACTORS:
        EXTRACTORS.append(extractor_cls)


def select_extractor(repo_root: Path) -> Extractor | None:
    """Return the first registered extractor that can handle ``repo_root``."""
    for ext_cls in EXTRACTORS:
        ext = ext_cls()
        if ext.can_handle(repo_root):
            return ext
    return None
