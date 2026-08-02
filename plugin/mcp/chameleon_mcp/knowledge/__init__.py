"""Compiled-in software-engineering vocabulary, and what it can detect.

Two things live here, and the split matters:

``loader``  the taxonomy itself -- 541 named concepts across meta-concepts,
            language constructs, 25 language profiles, patterns, principles,
            smells, refactorings, and 64 framework profiles. Pure data. It
            never asserts anything about a particular repo.

``detect``  what that data can measure. Framework detection scored from each
            profile's own signals, replacing a hand-written branch per
            framework.

Importing this package loads nothing; the taxonomy is read lazily on first use
and cached, so a session that never asks a taxonomy question pays nothing.
"""

from __future__ import annotations

from chameleon_mcp.knowledge.detect import primary_framework, score_frameworks
from chameleon_mcp.knowledge.loader import (
    KINDS,
    concept,
    concepts,
    framework_profile,
    frameworks_for_language,
    language_profile,
    load_taxonomy,
    search,
)

__all__ = [
    "KINDS",
    "concept",
    "concepts",
    "framework_profile",
    "frameworks_for_language",
    "language_profile",
    "load_taxonomy",
    "primary_framework",
    "score_frameworks",
    "search",
]
