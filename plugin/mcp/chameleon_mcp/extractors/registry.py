"""Extractor registry.

The ordered list of extractor classes bootstrap chooses from. A new language is
added by appending its extractor here (or via :func:`register`) plus mapping its
AST to the ``ParsedFile`` shape -- no edit to bootstrap's selection logic.
``select_extractor`` iterates the same ``(TypeScript, Ruby)`` precedence the
former hardcoded loop used, so existing profiles do not re-cluster.

The Tree-sitter-backed extractor is that new class: it satisfies the same
``Extractor`` protocol and is tried FIRST, with the bespoke ts_dump.mjs /
prism_dump.rb / libcst_dump.py paths intact behind it as the fallback. It
reproduces all three dumpers byte-for-byte on the committed fixture corpus
(``tests/differential_treesitter.py``), so the swap is invisible downstream --
but a fallback that costs nothing to keep is worth keeping while the in-process
path earns real-repo mileage. ``CHAMELEON_TREE_SITTER=0`` reverts to the
dumpers.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.extractors._base import Extractor
from chameleon_mcp.extractors.python import PythonExtractor
from chameleon_mcp.extractors.ruby import RubyExtractor
from chameleon_mcp.extractors.typescript import TypeScriptExtractor


def _spec_driven_extractor_classes() -> list[type[Extractor]]:
    """The spec-driven detectors, or none if their module cannot load.

    Fails open to the historical three-extractor registry: a broken spec must
    cost the languages it describes, never the three that have always worked.
    """
    try:
        from chameleon_mcp.extractors.spec_driven import extractor_classes

        return extractor_classes()
    except Exception:
        return []


# Order is precedence: the first extractor whose can_handle() matches wins.
# TypeScript before Ruby preserves the historical bootstrap order; Python is last
# so its liberal "any .py file" detection only claims repos TS/Ruby did not.
#
# The spec-driven languages sit BETWEEN Ruby and Python, and the position is
# load-bearing in both directions. After TS/Ruby, because those two carry
# equally strong markers and moving them would re-cluster existing profiles.
# Before Python, because `PythonExtractor.can_handle` claims any repo holding a
# single `.py` file -- a Go or Rust repo that ships one helper script would
# otherwise be profiled as Python, which is the silent-wrong-answer case rather
# than a missing one. That position only settles the weak half of the question;
# `_resolve_first_class_precedence` settles the other half below.
EXTRACTORS: list[type[Extractor]] = [
    TypeScriptExtractor,
    RubyExtractor,
    *_spec_driven_extractor_classes(),
    PythonExtractor,
]


def register(extractor_cls: type[Extractor]) -> None:
    """Append an extractor class to the registry (idempotent).

    Call at import time only. ``EXTRACTORS`` is read (never mutated) on the hook
    and bootstrap paths, so registering after a worker is serving select calls
    would race; there is no runtime registration path today.
    """
    if extractor_cls not in EXTRACTORS:
        EXTRACTORS.append(extractor_cls)


def select_extractor(repo_root: Path) -> Extractor | None:
    """Return the extractor that should analyze ``repo_root``.

    The in-process tree-sitter extractor is tried first and the dump-script
    extractors are the fallback, so a repo it cannot claim -- or a grammar that
    will not load -- still derives a profile through the path that has always
    worked. Selection order is the only thing that changes; the ``ParsedFile``
    contract is identical either way.
    """
    for index, ext_cls in enumerate(EXTRACTORS):
        ext = ext_cls()
        if not ext.can_handle(repo_root):
            continue
        ext = _resolve_first_class_precedence(ext, repo_root, EXTRACTORS[index + 1 :])
        # Detection and precedence stay the DUMPERS' -- language choice is a
        # marker-file question (Gemfile, tsconfig.json, pyproject.toml) that has
        # nothing to do with which parser reads the files, and downstream code
        # reads `.language` off whatever comes back. Only the parsing backend is
        # swapped, and only for a language tree-sitter reproduces exactly.
        if _treesitter_enabled():
            try:
                from chameleon_mcp.extractors.treesitter.extractor import TreeSitterExtractor

                if TreeSitterExtractor.supports(ext.language):
                    return TreeSitterExtractor(ext.language)
            except Exception:
                # A missing grammar package or an ABI mismatch must not take the
                # repo's profile down with it: keep the dump-script extractor,
                # which needs no Python-side parser at all.
                pass
        return ext
    return None


def _resolve_first_class_precedence(
    ext: Extractor, repo_root: Path, remaining: list[type[Extractor]]
) -> Extractor:
    """Hand a spec-driven match back to a first-class language that outranks it.

    The spec-driven detectors sit ahead of Python so a Go or Rust repo shipping
    one helper script is not profiled as Python on the strength of a single
    `.py` file. That is right only while Python's claim IS the weak marker-less
    one. A repo that carries a Python BUILD MANIFEST and more Python source than
    Go or Rust source -- a PyO3 crate's ``pyproject.toml`` beside its
    ``Cargo.toml``, a Django repo with a ``services/x/go.mod`` sidecar -- is a
    Python repo, and the extraction tier it would otherwise fall to has no lint
    rules, no reverse index and no framework classification.

    Only extractors LATER in the registry are considered, so nothing here can
    overturn the TypeScript/Ruby precedence that already ran, and only
    non-spec-driven ones, so the extraction tier never reshuffles internally.
    Fails open to the match already in hand.
    """
    try:
        from chameleon_mcp.extractors.spec_driven import SpecDrivenExtractor, outranked_by

        if not isinstance(ext, SpecDrivenExtractor):
            return ext
        candidates = (cls() for cls in remaining)
        later = {c.language: c for c in candidates if not isinstance(c, SpecDrivenExtractor)}
        winner = outranked_by(repo_root, ext.language, tuple(later)) if later else None
        if winner is None:
            return ext
        # The winner's own detector still has the last word, and it is asked only
        # here: a repo the spec-driven detectors already claimed must not pay
        # Python's marker-less `.py` scan on every bootstrap.
        claimed = later[winner]
        return claimed if claimed.can_handle(repo_root) else ext
    except Exception:
        return ext


def _treesitter_enabled() -> bool:
    """Default-ON, killed by ``CHAMELEON_TREE_SITTER=0``.

    Offline, no repo-code execution, no network, so it follows the repo's
    default-on-with-kill-switch convention rather than shipping opt-in. The
    evidence for defaulting it on is `tests/differential_treesitter.py
    --strict`, which compares EVERY field each dump script emits (not a curated
    subset) and reports PARITY for all three languages on the committed fixture
    corpus.

    Read at call time so a test can toggle it without a module reload.
    """
    import os

    return os.environ.get("CHAMELEON_TREE_SITTER") != "0"
