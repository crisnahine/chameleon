"""Grammar resolution: file extension -> loaded tree-sitter Language.

Grammars load lazily and stay cached for the process. Lazy matters because the
extractor runs inside the MCP server rather than a short-lived subprocess: a
Ruby-only repo should never pay to dlopen the TypeScript, TSX, and JavaScript
grammars, and an import-time load would make every server start pay for all
five.

The ABI check is not defensive boilerplate. A grammar wheel encodes a Language
ABI number that the core library must accept, and the two version ranges float
independently on PyPI: installing the current tree-sitter-python (0.25.x, ABI
15) against the core that tree-sitter-typescript pins (0.23.x, accepts 13-14)
raises ``ValueError: Incompatible Language version 15`` from the C extension at
Language() construction. That surfaces as a bare ValueError from a dependency
the operator never named, so it is caught here and re-raised as the extractor's
own unavailable-toolchain error, carrying the grammar that failed.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from chameleon_mcp.extractors._base import ExtractorUnavailableError


class TreeSitterUnavailableError(ExtractorUnavailableError):
    """A tree-sitter grammar could not be loaded.

    Raised for a missing grammar package and for an ABI mismatch between a
    grammar wheel and the core library. Subclasses ``ExtractorUnavailableError``
    so the bootstrap orchestrator degrades to a clean failed report through the
    path it already has for node/ruby being absent, rather than letting a
    dependency's ValueError escape to the MCP boundary.
    """


# Extension -> (grammar module, factory attribute). The language name is the
# key downstream consumers already know ("typescript", "ruby", "python"), and
# several extensions deliberately share one: .mjs/.cjs are JavaScript, and .tsx
# needs the separate TSX grammar because the TypeScript grammar cannot parse
# JSX syntax.
_EXTENSION_GRAMMARS: dict[str, tuple[str, str, str]] = {
    ".ts": ("tree_sitter_typescript", "language_typescript", "typescript"),
    ".mts": ("tree_sitter_typescript", "language_typescript", "typescript"),
    ".cts": ("tree_sitter_typescript", "language_typescript", "typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx", "typescript"),
    ".js": ("tree_sitter_javascript", "language", "typescript"),
    ".jsx": ("tree_sitter_javascript", "language", "typescript"),
    ".mjs": ("tree_sitter_javascript", "language", "typescript"),
    ".cjs": ("tree_sitter_javascript", "language", "typescript"),
    ".rb": ("tree_sitter_ruby", "language", "ruby"),
    ".rake": ("tree_sitter_ruby", "language", "ruby"),
    ".gemspec": ("tree_sitter_ruby", "language", "ruby"),
    ".py": ("tree_sitter_python", "language", "python"),
    # .pyi stubs were never discoverable through the libcst dumper's glob, but
    # downstream paths (signature contract-diff, phantom-symbol probes) already
    # honor them. Grammar selection is by extension here, so they cost nothing.
    ".pyi": ("tree_sitter_python", "language", "python"),
}

# The wheel each spec-driven language loads from. Kept beside the table above
# rather than inside the spec, because a spec describes a GRAMMAR's shape while
# this names the Python package that happens to ship it -- two things that
# change for different reasons. PHP's factory is `language_php`; the wheel also
# exposes a separate one for its template dialect.
_SPEC_GRAMMAR_MODULES: dict[str, tuple[str, str]] = {
    "go": ("tree_sitter_go", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "java": ("tree_sitter_java", "language"),
    "csharp": ("tree_sitter_c_sharp", "language"),
    "php": ("tree_sitter_php", "language_php"),
}


def _register_spec_extensions() -> None:
    """Fold every spec's extensions into the extension table.

    Derived rather than written twice: the spec already declares what a language
    is called and which files it claims, and a second hand-maintained copy is
    exactly how a discovery glob and a grammar lookup drift apart.

    A spec with no grammar module is a hard error rather than a skip. Skipping
    it left the language selectable everywhere else -- the extractor still built
    its tables and the registry still registered its detector -- so it would win
    a repo, resolve no extension, parse zero files, and write a profile that
    looks clean because it is empty. Raising costs the tree-sitter backend and
    degrades to the dump scripts, which is a failure an operator can see.
    """
    from chameleon_mcp.extractors.treesitter.lang.specs import ALL

    for spec in ALL:
        entry = _SPEC_GRAMMAR_MODULES.get(spec.name)
        if entry is None:
            raise TreeSitterUnavailableError(
                f"language spec {spec.name!r} names no grammar package in "
                "_SPEC_GRAMMAR_MODULES; every spec needs the wheel that parses it"
            )
        module_name, factory_attr = entry
        for ext in spec.extensions:
            _EXTENSION_GRAMMARS.setdefault(ext, (module_name, factory_attr, spec.name))


_register_spec_extensions()

_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def _load(module_name: str, factory_attr: str) -> Any:
    """Import a grammar module and build its Language, or raise unavailable.

    The import and the Language() construction fail for unrelated reasons -- a
    grammar package that was never installed versus one whose compiled ABI the
    core rejects -- and an operator fixes them differently, so the messages stay
    distinct.
    """
    from tree_sitter import Language

    try:
        module = __import__(module_name, fromlist=[factory_attr])
    except ImportError as exc:
        raise TreeSitterUnavailableError(
            f"tree-sitter grammar {module_name!r} is not installed: {exc}"
        ) from exc

    try:
        factory: Callable[[], Any] = getattr(module, factory_attr)
    except AttributeError as exc:
        raise TreeSitterUnavailableError(
            f"{module_name!r} exposes no {factory_attr!r}; the installed grammar "
            "release does not match the one this extractor targets"
        ) from exc

    try:
        return Language(factory())
    except ValueError as exc:
        # The ABI mismatch path. Name both halves: the operator has to pin a
        # version pair, and neither number appears in the raw ValueError.
        raise TreeSitterUnavailableError(
            f"tree-sitter grammar {module_name!r} is ABI-incompatible with the "
            f"installed tree-sitter core ({exc}); pin the grammar and core "
            "versions together"
        ) from exc


def language_for_path(path: Path | str) -> str | None:
    """The chameleon language name for ``path``, or None if unsupported.

    Cheap and grammar-free: callers use it to decide whether a file is worth
    reading at all, so it must not trigger a dlopen.
    """
    entry = _EXTENSION_GRAMMARS.get(Path(path).suffix.lower())
    return entry[2] if entry else None


def grammar_for_path(path: Path | str) -> Any:
    """Return the loaded tree-sitter Language for ``path``.

    Raises TreeSitterUnavailableError when the extension has no grammar or the
    grammar cannot load. Cached per (module, factory) pair, so .ts and .mts
    share one load while .tsx gets its own.
    """
    suffix = Path(path).suffix.lower()
    entry = _EXTENSION_GRAMMARS.get(suffix)
    if entry is None:
        raise TreeSitterUnavailableError(f"no tree-sitter grammar for extension {suffix!r}")

    module_name, factory_attr, _language = entry
    key = f"{module_name}.{factory_attr}"

    cached = _cache.get(key)
    if cached is not None:
        return cached

    with _cache_lock:
        # Re-check under the lock: two bootstrap threads reaching the same
        # extension would otherwise both pay the dlopen.
        cached = _cache.get(key)
        if cached is not None:
            return cached
        language = _load(module_name, factory_attr)
        _cache[key] = language
        return language


def supported_extensions(language: str | None = None) -> tuple[str, ...]:
    """Extensions this extractor can parse, optionally filtered by language."""
    if language is None:
        return tuple(sorted(_EXTENSION_GRAMMARS))
    return tuple(sorted(ext for ext, e in _EXTENSION_GRAMMARS.items() if e[2] == language))


def extensions_by_language() -> dict[str, tuple[str, ...]]:
    """Every language this extractor knows, mapped to the extensions it claims.

    The inverse of :func:`supported_extensions`, for callers that need the whole
    language set rather than one language's files -- repo detection asks "which
    languages could this tree hold" before it knows which to ask about.
    """
    by_language: dict[str, list[str]] = {}
    for ext, entry in _EXTENSION_GRAMMARS.items():
        by_language.setdefault(entry[2], []).append(ext)
    return {language: tuple(sorted(exts)) for language, exts in sorted(by_language.items())}


def probe() -> dict[str, str]:
    """Load every grammar once and report per-language status.

    Used by doctor and by the differential harness before a run: an ABI
    mismatch that only shows up on the first .tsx file of a bootstrap is a much
    worse failure than one reported upfront.
    """
    status: dict[str, str] = {}
    for module_name, factory_attr, language in sorted(set(_EXTENSION_GRAMMARS.values())):
        label = f"{language}:{factory_attr}"
        try:
            _load(module_name, factory_attr)
            status[label] = "ok"
        except TreeSitterUnavailableError as exc:
            status[label] = str(exc)
    return status
