"""Commented-out-code detection — bootstrap / pr-review only.

A reviewer comment that recurs across teams is "drop the commented-out block."
Chameleon blanks comments before every other scan, so it has no view of what a
comment contains. This module restores that view at bootstrap by capturing each
comment span and handing it to the real parser: a span flagged here parsed as a
complete statement or import with zero parse errors, which is the high-precision
signal that it is dead code, not prose.

The parse round-trip is far too slow for the per-edit hot path, so this runs
only at bootstrap (against the canonical witness pool) and pr-review. The result
is advisory only — surfaced as a NIT, never a block.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from chameleon_mcp.extractors._base import Extractor
from chameleon_mcp.lint_engine import extract_comment_spans

# Node kinds that mark a span as a complete statement or import rather than
# stray expression fragments. TS aliases a VariableStatement to "FirstStatement"
# in SyntaxKind, so both names are accepted. Prose never reaches this gate (it
# parses with diagnostics > 0), but restricting to declaration/import kinds keeps
# an idiomatic example expression in a comment from reading as dead code.
_TS_CODE_KINDS = frozenset(
    {
        "ImportDeclaration",
        "ExportDeclaration",
        "ExportAssignment",
        "VariableStatement",
        "FirstStatement",
        "FunctionDeclaration",
        "ClassDeclaration",
        "InterfaceDeclaration",
        "TypeAliasDeclaration",
        "EnumDeclaration",
    }
)
# Ruby structural kinds that always read as commented-out code. A bare CallNode
# is NOT here: Prism parses prose like "just a normal comment" into CallNodes
# with zero diagnostics, so accepting CallNode wholesale floods the advisory
# with false positives. A commented-out `require`/`require_relative`/`autoload`
# is still caught — it surfaces in import_specifiers — handled separately.
_RUBY_CODE_KINDS = frozenset(
    {
        "ClassNode",
        "ModuleNode",
        "DefNode",
    }
)
# Python structural kinds that always read as commented-out code. The kinds are
# the dumper's top-level node names: libcst emits FunctionDef for both sync and
# async defs (async is an attribute, not a separate node), so a commented-out
# `async def` is caught by FunctionDef. AsyncFunctionDef is kept defensively for
# the stdlib-ast fallback's node names; that path currently never reaches the
# kinds check (it carries a non-zero diagnostics count, which _span_is_code
# rejects first), so the entry is inert today but harmless. A commented-out
# import surfaces in import_specifiers and is caught by the same fall-through
# Ruby uses for its require-family calls. Bare expression/assignment kinds are
# NOT here: prose parses into them with zero diagnostics and would flood the
# advisory with false positives.
_PY_CODE_KINDS = frozenset(
    {
        "ClassDef",
        "FunctionDef",
        "AsyncFunctionDef",
    }
)

# A span must reach this many characters to be worth a parse round-trip; below
# it the false-positive cost (a one-word comment that happens to parse)
# outweighs the value. Kept conservative: the parser gate is the real precision
# lever, this only trims the candidate set.
_MIN_SPAN_CHARS = 3


def _ext_for(language: str) -> str | None:
    if language == "typescript":
        return ".ts"
    if language == "ruby":
        return ".rb"
    if language == "python":
        return ".py"
    return None


def _span_is_code(parsed_file, language: str) -> bool:
    """True if a parsed span reads as a complete statement/import, not prose.

    Requires zero parse diagnostics, then a language-specific structural check.
    TS: any top-level declaration/import kind. Ruby: a class/module/def, OR a
    require-family call (which the parser records in import_specifiers) — a bare
    CallNode from prose is rejected because Prism parses prose without errors.
    Python: a class/def, OR an import (recorded in import_specifiers) — a bare
    expression/assignment is rejected because prose parses without diagnostics.
    """
    if parsed_file.parse_diagnostics_count != 0:
        return False
    kinds = parsed_file.top_level_node_kinds
    if language == "typescript":
        return any(kind in _TS_CODE_KINDS for kind in kinds)
    if language == "python":
        if any(kind in _PY_CODE_KINDS for kind in kinds):
            return True
        return bool(getattr(parsed_file, "import_specifiers", ()))
    if any(kind in _RUBY_CODE_KINDS for kind in kinds):
        return True
    return bool(getattr(parsed_file, "import_specifiers", ()))


def detect_commented_out_code(
    contents: list[str],
    *,
    language: str,
    extractor: Extractor,
    max_spans: int = 200,
) -> int:
    """Count comment spans across ``contents`` that parse as real code.

    Each file's comment spans are captured, stripped of their markers, and
    written to a temp file the real parser re-reads. A span counts only when it
    parses with zero diagnostics AND yields at least one statement/import node.
    ``max_spans`` bounds the parse work so a comment-heavy corpus cannot blow up
    bootstrap.

    Fails open: any extraction or parse error yields the count gathered so far
    (never raises). Returns 0 for an unsupported language.
    """
    ext = _ext_for(language)
    if ext is None:
        return 0

    spans: list[str] = []
    for content in contents:
        try:
            for span in extract_comment_spans(content, language=language):
                if len(span.strip()) >= _MIN_SPAN_CHARS:
                    spans.append(span)
                    if len(spans) >= max_spans:
                        break
        except Exception:
            continue
        if len(spans) >= max_spans:
            break
    if not spans:
        return 0

    return _parse_and_count(spans, language=language, ext=ext, extractor=extractor)


def detect_commented_out_code_by_group(
    contents_by_group: dict[str, list[str]],
    *,
    language: str,
    extractor: Extractor,
    max_spans_per_group: int = 200,
) -> dict[str, int]:
    """Per-group commented-out-code counts in a single parse batch.

    Captures each group's spans, parses them all in one extractor invocation,
    and attributes each flagged span back to its group. One subprocess for the
    whole corpus instead of one per group keeps bootstrap cheap. Fails open: any
    error yields ``{}`` (groups simply carry no advisory). Returns ``{}`` for an
    unsupported language or when no group has a code-shaped span.
    """
    ext = _ext_for(language)
    if ext is None:
        return {}

    # Flat list of (group, span) so attribution survives the single parse batch.
    indexed: list[tuple[str, str]] = []
    for group, contents in contents_by_group.items():
        gathered = 0
        for content in contents:
            if gathered >= max_spans_per_group:
                break
            try:
                group_spans = extract_comment_spans(content, language=language)
            except Exception:
                continue
            for span in group_spans:
                if len(span.strip()) < _MIN_SPAN_CHARS:
                    continue
                indexed.append((group, span))
                gathered += 1
                if gathered >= max_spans_per_group:
                    break
    if not indexed:
        return {}

    counts: dict[str, int] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="chameleon-cot-") as tmp:
            tmp_dir = Path(tmp)
            path_to_group: dict[str, str] = {}
            span_paths: list[Path] = []
            for i, (group, span) in enumerate(indexed):
                p = tmp_dir / f"span_{i}{ext}"
                try:
                    p.write_text(span, encoding="utf-8")
                except OSError:
                    continue
                path_to_group[str(p.resolve())] = group
                span_paths.append(p)
            if not span_paths:
                return {}
            result = extractor.parse_repo(tmp_dir, paths=span_paths)
            for pf in result.files:
                if not _span_is_code(pf, language):
                    continue
                group = path_to_group.get(str(Path(pf.path).resolve()))
                if group is not None:
                    counts[group] = counts.get(group, 0) + 1
    except Exception:
        return {}
    return counts


def _parse_and_count(
    spans: list[str],
    *,
    language: str,
    ext: str,
    extractor: Extractor,
) -> int:
    flagged = 0
    try:
        with tempfile.TemporaryDirectory(prefix="chameleon-cot-") as tmp:
            tmp_dir = Path(tmp)
            span_paths: list[Path] = []
            for i, span in enumerate(spans):
                p = tmp_dir / f"span_{i}{ext}"
                try:
                    p.write_text(span, encoding="utf-8")
                except OSError:
                    continue
                span_paths.append(p)
            if not span_paths:
                return 0
            result = extractor.parse_repo(tmp_dir, paths=span_paths)
            for pf in result.files:
                if _span_is_code(pf, language):
                    flagged += 1
    except Exception:
        # A subprocess/parse failure must not abort bootstrap; report what we
        # have. The witness is still committed without the advisory.
        return flagged
    return flagged
