"""The in-process tree-sitter extractor.

Satisfies the same ``Extractor`` protocol the dump-script extractors do and
returns the same ``ParsedFile`` shape, so nothing downstream changes. What goes
away is the subprocess: no NDJSON protocol, no pipe-deadlock handling, no
interpreter hunting, no npm provisioning. Crash isolation, which the process
boundary used to provide for free, is re-earned per file by the try/except in
``parse_repo`` plus the node and time bounds the walker enforces.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import xxhash

from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.extractors._base import ParsedFile, ParseResult
from chameleon_mcp.extractors.treesitter import lang as _lang_pkg  # noqa: F401
from chameleon_mcp.extractors.treesitter.grammars import (
    TreeSitterUnavailableError,
    grammar_for_path,
    language_for_path,
)
from chameleon_mcp.extractors.treesitter.lang import python as python_tables
from chameleon_mcp.extractors.treesitter.lang import ruby as ruby_tables
from chameleon_mcp.extractors.treesitter.lang import typescript as typescript_tables
from chameleon_mcp.extractors.treesitter.walker import NodeBudgetExceeded, walk
from chameleon_mcp.extractors.treesitter.walker import error_nodes as walker_error_nodes

# Language name -> tables module. Adding a language is an entry here plus its
# table module; the walker and this file stay untouched, which is the whole
# point of keeping traversal in one place.
_TABLES: dict[str, Any] = {
    "ruby": ruby_tables,
    "python": python_tables,
    "typescript": typescript_tables,
}

ENABLE_ENV = "CHAMELEON_TREE_SITTER"


def is_enabled() -> bool:
    """True unless an operator has killed the tree-sitter path.

    Ships default-ON with a kill switch per the repo's convention for offline
    features: nothing here executes repo code or touches the network, so the
    opt-in form would be wrong.
    """
    return os.environ.get(ENABLE_ENV) != "0"


class TreeSitterExtractor:
    """AST extractor backed by in-process tree-sitter grammars.

    Constructed for ONE language, which it reports as its own. Downstream reads
    ``.language`` to learn what the repo is written in -- archetype naming, the
    framework-aware layers, and the lint gates all branch on it -- so a generic
    "treesitter" here would silently strip every language-specific behavior from
    a repo that merely changed parsing backends.
    """

    def __init__(self, language: str) -> None:
        if language not in _TABLES:
            raise ValueError(f"no tree-sitter tables for language {language!r}")
        self.language = language
        self._languages = (language,)

    @staticmethod
    def supports(language: str) -> bool:
        """True when tables exist to reproduce ``language`` exactly."""
        return language in _TABLES

    def can_handle(self, repo_root: Path) -> bool:
        """True when the repo holds a file of this extractor's own language.

        Detection and precedence belong to the dump-script extractors -- the
        registry asks THEM which language a repo is, then swaps in this backend
        for it -- so this is a containment check, not a competing detector.
        """
        return any(
            next(repo_root.rglob(f"*{ext}"), None) is not None
            for ext in _extensions_for(self.language)
        )

    def parse_repo(
        self,
        repo_root: Path,
        glob: str = "**/*",
        limit: int | None = None,
        paths: list[Path] | None = None,
    ) -> ParseResult:
        """Parse supported files under ``repo_root``, or exactly ``paths``.

        ``paths`` is not optional in practice: the bootstrap orchestrator has
        already applied discovery, exclusion, and workspace scoping by the time
        it calls, and passes the resulting candidate list. Globbing here instead
        would silently re-include everything discovery ruled out.
        """
        result = ParseResult()
        count = 0
        candidates = sorted(repo_root.glob(glob)) if paths is None else paths
        for path in candidates:
            if not path.is_file():
                continue
            if language_for_path(path) not in self._languages:
                continue
            if limit is not None and count >= limit:
                break
            count += 1

            parsed, skip_reason = parse_file(path)
            if parsed is not None:
                result.files.append(parsed)
            else:
                result.skipped.append((path, skip_reason or "parse_error"))
        return result


def _extras(tables: Any, walked: Any) -> dict[str, Any]:
    """The extras payload, omitting keys the language has no concept of.

    `class_body_calls` is Ruby-exclusive (receiverless DSL macros); the other
    dumpers omit the key entirely rather than emitting an empty list, and
    absent-vs-empty is a real distinction downstream.
    """
    extras: dict[str, Any] = {
        "function_scopes": walked.function_scopes,
        "callable_signatures": walked.callable_signatures,
        "class_shapes": walked.class_shapes,
        "call_sites": walked.call_sites,
        "call_sites_total": walked.call_sites_seen,
        "call_sites_truncated": walked.call_sites_truncated,
    }
    if getattr(tables, "call_site_carries_nesting", False):
        extras["class_body_calls"] = walked.class_body_calls
    if getattr(tables, "class_property_types", None) is not None:
        # Declared property types feed the dependency-injection grade, which is
        # a TypeScript concept; the other dumpers omit the key rather than
        # emitting an empty list.
        extras["class_property_types"] = walked.class_property_types
    return extras


def _file_extras(tables: Any, root: Any, src: bytes, path: Path, walked: Any) -> dict[str, Any]:
    """Whole-file extras a per-node walk cannot produce.

    The export set and the import-binding rows are properties of the FILE, not
    of any one node: `export *` opens the set from a single statement, and
    `named_export_names` is a union over every top-level binding. They are
    computed from the root's direct children rather than during the walk, which
    keeps them out of the hot loop and out of the frame bookkeeping.
    """
    hook = getattr(tables, "file_extras", None)
    if hook is None:
        return {}
    return hook(root, _text_of(src), path, walked)


def _text_of(src: bytes) -> Any:
    """A node -> source-text reader bound to one file's bytes."""

    def text(node: Any) -> str:
        return src[node.start_byte : node.end_byte].decode("utf-8", "replace")

    return text


def _diagnostics_count(root: Any, src: bytes, tables: Any) -> int:
    """1 when the file has real syntax errors, 0 when it parses clean.

    A language may declare `known_parse_defect` for constructs its grammar
    rejects but the reference parser accepts. Those are suppressed, because the
    field means "is this file broken" and answering yes on valid modern React or
    NestJS code is simply wrong -- and it is wrong at scale, since the flagged
    constructs are ordinary idioms rather than exotica.

    Suppression is all-or-nothing per file ON PURPOSE: a file is clean only when
    EVERY error node is a recognized defect. One unrecognized error and the file
    is reported damaged, so a genuine syntax error sitting next to a known
    defect can never be masked by it.
    """
    if not root.has_error:
        return 0
    defect = getattr(tables, "known_parse_defect", None)
    if defect is None:
        return 1
    errors = walker_error_nodes(root, threshold_int("TS_MAX_AST_NODES"))
    if not errors:
        # `has_error` with nothing locatable under the budget: trust the flag.
        return 1
    return 0 if all(defect(node, src) for node in errors) else 1


def _extensions_for(language: str) -> tuple[str, ...]:
    from chameleon_mcp.extractors.treesitter.grammars import supported_extensions

    return supported_extensions(language)


def parse_file(path: Path) -> tuple[ParsedFile | None, str | None]:
    """Parse one file into a ParsedFile, or return (None, skip_reason).

    Returns rather than raises for the expected skips -- oversized, symlinked,
    unreadable, node-capped -- because they are per-file outcomes the caller
    records alongside successes, exactly as the dump scripts' skip vocabulary
    did.
    """
    language = language_for_path(path)
    if language is None:
        return None, "unsupported_language"
    tables = _TABLES.get(language)
    if tables is None:
        return None, "unsupported_language"

    try:
        stat = path.lstat()
    except OSError as exc:
        return None, f"read_error: {exc}"
    if stat.st_mode & 0o170000 == 0o120000:
        return None, "symlink_refused"
    if stat.st_size > threshold_int("TS_MAX_FILE_BYTES"):
        return None, "file_too_large"

    try:
        src = path.read_bytes()
    except OSError as exc:
        return None, f"read_error: {exc}"

    try:
        language_obj = grammar_for_path(path)
    except TreeSitterUnavailableError:
        raise

    from tree_sitter import Parser

    parser = Parser(language_obj)
    # Cores through 0.25 bound a parse with `timeout_micros`; 0.26 removed the
    # attribute outright, having moved the bound into a parse-options progress
    # callback the Python binding does not expose. Setting it unconditionally
    # is therefore an AttributeError on a current core, and the bound is simply
    # unavailable there -- so what actually holds the line is the size cap
    # above. That is not a downgrade in practice: tree-sitter is built to
    # reparse on every keystroke, and the slowest file across the four
    # measured repos (4,749 files) parsed in 20.6ms at 415KB, three orders of
    # magnitude inside the 5s bound. The setting is still honored where it
    # exists rather than dropped, so pinning back down loses nothing.
    if hasattr(parser, "timeout_micros"):
        parser.timeout_micros = threshold_int("TS_PARSE_TIMEOUT_MICROS")

    try:
        tree = parser.parse(src)
    except ValueError as exc:
        # A core that HAS the bound raises rather than returning a tree when it
        # hits it; that is a skip, not a crash.
        return None, f"parse_timeout: {exc}"

    root = tree.root_node
    try:
        walked = walk(
            root,
            src,
            tables,
            max_nodes=threshold_int("TS_MAX_AST_NODES"),
            max_call_sites=threshold_int("TS_MAX_CALL_SITES"),
            max_callable_signatures=threshold_int("TS_MAX_CALLABLE_SIGNATURES"),
        )
    except NodeBudgetExceeded:
        return None, "ast_node_ceiling_exceeded"

    kinds = tuple(k for k in walked.top_level_node_kinds if k)
    # Both export fields are the language's own predicate over the top-level
    # statements, not arithmetic on `kinds`. Ruby counts definitions while a
    # top-level require adds a statement but no export, so deriving either one
    # from len(kinds) disagrees with the dumper on any file that has both.
    # Counted from the MAPPED kinds, not the raw rule names: Python's
    # `decorated_definition` hides whether it wraps a function or a class, and
    # the mapping has already resolved that.
    export_facts = getattr(tables, "export_facts", None)
    if export_facts is not None:
        # TypeScript has REAL exports: the facts come off the `export` modifiers
        # and the bindings inside an export clause, none of which survives into
        # a flat kind list.
        default_export_kind, named_export_count = export_facts(root, _text_of(src))
    else:
        default_export_kind, named_export_count = tables.export_fields(list(kinds))
    return (
        ParsedFile(
            path=path,
            content_first_200_bytes=src[:200].decode("utf-8", "replace"),
            top_level_node_kinds=kinds,
            default_export_kind=default_export_kind,
            named_export_count=named_export_count,
            import_specifiers=tuple(walked.import_specifiers),
            has_jsx=walked.has_jsx,
            # tree-sitter is error-recovering, so a damaged file yields a tree
            # with ERROR nodes rather than an exception. Prism and ts_dump both
            # report a graded count; libcst could not, which is why Python's
            # diagnostics column was a documented partial.
            parse_diagnostics_count=_diagnostics_count(root, src, tables),
            sha_hint=xxhash.xxh64(src).hexdigest(),
            extras={**_extras(tables, walked), **_file_extras(tables, root, src, path, walked)},
        ),
        None,
    )
