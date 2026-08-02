"""Load the compiled-in software-engineering taxonomy.

Chameleon's thesis is that a repo's conventions are DERIVED, never hand-written.
This module does not weaken that: nothing here says what a repo does. It carries
the vocabulary the derivation is described IN -- what a "repository pattern" is,
which files Rails calls a concern, that Go names its files lowercase -- so a
finding can be stated in the reader's own words and a framework can be
recognized without a hand-written arm per framework.

The distinction that keeps the thesis intact: a taxonomy entry never asserts
anything about THIS repo. It supplies a name, a definition, and a detection
hint; whether the repo matches is still measured.

The data is 541 entries across five sections (A meta-concepts, B language
constructs and 25 language profiles, C patterns/principles/smells/refactorings,
D 64 framework profiles, E tooling). It ships inside the package rather than
being fetched, so it is available offline and pinned to the plugin version.

Not `safe_open`: that helper exists to keep REPO file reads inside the repo
boundary. This file is plugin-internal data next to the module that reads it,
so the boundary check has nothing to check and the path can never come from a
caller.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

_TAXONOMY_PATH: Final[Path] = Path(__file__).resolve().parent / "taxonomy.json"

DISABLE_ENV: Final[str] = "CHAMELEON_TAXONOMY"

# Every `kind` the taxonomy declares. Kept here so a caller can validate a
# filter argument without loading the file, and so a kind added upstream that
# nothing reads is visible in a diff.
KINDS: Final[tuple[str, ...]] = (
    "meta-concept",
    "construct",
    "language-profile",
    "framework-profile",
    "design-pattern",
    "architecture-pattern",
    "principle",
    "code-smell",
    "refactoring",
    "testing-pattern",
    "concurrency-pattern",
    "distributed-pattern",
    "tooling",
    "schema",
)


def enabled() -> bool:
    """Whether the taxonomy layer is on.

    Default-on with a kill switch: this is an offline read of a file the plugin
    ships, so it executes no repo code and touches no network.
    """
    return os.environ.get(DISABLE_ENV) != "0"


@lru_cache(maxsize=1)
def load_taxonomy() -> dict[str, Any]:
    """The whole taxonomy document, or an empty one.

    Fails open. A malformed or missing file costs the taxonomy layer and
    nothing else -- every caller treats an empty document as "no vocabulary
    available", which is the pre-taxonomy behavior.
    """
    if not enabled():
        return {"meta": {}, "concepts": []}
    try:
        raw = json.loads(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"meta": {}, "concepts": []}
    if not isinstance(raw, dict):
        return {"meta": {}, "concepts": []}
    concepts = raw.get("concepts")
    if not isinstance(concepts, list):
        return {"meta": raw.get("meta") or {}, "concepts": []}
    # Drop anything without the two fields every consumer indexes on, rather
    # than letting a half-entry reach a caller that assumes both.
    clean = [
        c
        for c in concepts
        if isinstance(c, dict) and isinstance(c.get("term"), str) and isinstance(c.get("kind"), str)
    ]
    return {"meta": raw.get("meta") or {}, "concepts": clean}


@lru_cache(maxsize=1)
def _by_term() -> dict[str, dict[str, Any]]:
    """Case-folded term -> concept, with aliases resolving to the same entry.

    A term wins over an alias: `Proxy` is a design pattern in its own right and
    also an alias of something else, and the named entry is what a caller asking
    for "Proxy" means.
    """
    index: dict[str, dict[str, Any]] = {}
    for c in load_taxonomy()["concepts"]:
        for alias in c.get("aliases") or ():
            if isinstance(alias, str):
                index.setdefault(alias.casefold(), c)
    for c in load_taxonomy()["concepts"]:
        index[c["term"].casefold()] = c
    return index


@lru_cache(maxsize=1)
def _by_kind() -> dict[str, tuple[dict[str, Any], ...]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for c in load_taxonomy()["concepts"]:
        grouped.setdefault(c["kind"], []).append(c)
    return {k: tuple(v) for k, v in grouped.items()}


def concepts(kind: str | None = None) -> tuple[dict[str, Any], ...]:
    """Every concept, or every concept of one kind."""
    if kind is None:
        return tuple(load_taxonomy()["concepts"])
    return _by_kind().get(kind, ())


def concept(term: str) -> dict[str, Any] | None:
    """One concept by term or alias, case-insensitively."""
    if not isinstance(term, str):
        return None
    return _by_term().get(term.casefold())


def language_profile(language: str) -> dict[str, Any] | None:
    """The profile for a language id (`python`, `go`, `typescript`, ...)."""
    hit = concept(language)
    return hit if hit is not None and hit.get("kind") == "language-profile" else None


def framework_profile(framework: str) -> dict[str, Any] | None:
    """The profile for a framework id (`rails`, `nextjs`, `spring-boot`, ...)."""
    hit = concept(framework)
    return hit if hit is not None and hit.get("kind") == "framework-profile" else None


def frameworks_for_language(language: str) -> tuple[dict[str, Any], ...]:
    """Every framework profile that declares this language.

    A profile's `language` field is a list, because most frameworks span
    JavaScript and TypeScript.
    """
    if not isinstance(language, str):
        return ()
    want = language.casefold()
    return tuple(
        f
        for f in concepts("framework-profile")
        if any(
            isinstance(lang, str) and lang.casefold() == want for lang in f.get("language") or ()
        )
    )


def search(query: str, limit: int = 10) -> tuple[dict[str, Any], ...]:
    """Concepts whose term, alias or definition mentions `query`.

    Ranked by where the match landed -- an exact term beats a term substring,
    which beats a definition mention -- so `search("repository")` leads with the
    Repository pattern rather than the several profiles that mention repositories
    in passing.
    """
    if not isinstance(query, str) or not query.strip():
        return ()
    q = query.casefold().strip()
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for c in load_taxonomy()["concepts"]:
        term = c["term"].casefold()
        aliases = [a.casefold() for a in c.get("aliases") or () if isinstance(a, str)]
        definition = str(c.get("definition") or "").casefold()
        if term == q or q in aliases:
            rank = 0
        elif q in term:
            rank = 1
        elif any(q in a for a in aliases):
            rank = 2
        elif q in definition:
            rank = 3
        else:
            continue
        scored.append((rank, c["term"], c))
    scored.sort(key=lambda row: (row[0], row[1]))
    return tuple(c for _, _, c in scored[: max(0, limit)])
