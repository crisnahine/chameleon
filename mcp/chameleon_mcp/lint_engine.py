"""Lint engine — compare a file's AST-shape dimensions against an archetype's
canonical `ast_query` and emit violations.

Phase 4.1 (v0.3): regex-heuristic extraction. The cluster signature function
in `signatures.py` operates on a real ParsedFile produced by the long-lived
ts_dump.mjs / prism_dump.rb subprocesses. Round-tripping through the
subprocess for every lint_file call would dominate latency (cold-start cost
of ~200ms per Node spawn, plus the `npm install` first-run trip), so for v0.3
the lint engine derives the same dimensions from the raw `content` string via
language-specific regex heuristics.

Trade-offs of the heuristic approach:

  Pros
  - Zero subprocess fork; lint_file stays sub-millisecond on small files.
  - No Node/Ruby runtime dependency at lint time.
  - Survives partial / malformed files (real parsers may refuse them).

  Cons
  - Misses constructs the regex doesn't anticipate (e.g., `export {default}
    from "./x"` is structurally a default re-export but our regex won't tag
    it).
  - JSX detection via `</(\w+)` substring is approximate — a string literal
    containing `</div>` would false-positive. We mitigate by stripping
    obvious string/comment regions before the JSX scan.

  Future
  - Phase 4.2 will swap in a long-lived ts_dump.mjs / prism_dump.rb service
    that can lint single buffers without re-spawning the subprocess; the
    engine's interface (DimensionSnapshot → list[Violation]) stays the same.

Public surface:

    extract_dimensions(content, *, language) -> DimensionSnapshot
    lint(snapshot, ast_query) -> list[Violation]

Both are pure functions (no I/O, no globals), which keeps the unit tests
fast and deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from chameleon_mcp.signatures import bucket_named_export_count, content_signal_match_for

Severity = Literal["info", "warning", "error"]

# Languages the engine knows how to extract dimensions for. Extensions that
# don't match a supported language fall through to a "language_unsupported"
# diagnostic rather than producing false-positive violations.
_TS_EXTENSIONS: frozenset[str] = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
_RUBY_EXTENSIONS: frozenset[str] = frozenset({".rb"})


@dataclass(frozen=True, slots=True)
class Violation:
    """A single discrepancy between a file's shape and the archetype's ast_query.

    Frozen so callers can deduplicate via set membership; the `expected` /
    `actual` fields are stringified at construction (lists → repr) to keep
    the dataclass hashable.
    """

    rule: str
    expected: str
    actual: str
    severity: Severity
    message: str

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "expected": self.expected,
            "actual": self.actual,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass
class DimensionSnapshot:
    """The five dimensions the lint engine compares against ast_query.

    Mirrors `derive_ast_query`'s output keys 1:1 so the comparison loop is
    straightforward. `unparseable_regions` is a passthrough for the envelope
    (currently always empty in the heuristic implementation; reserved for
    a future real-parser implementation).
    """

    top_level_node_kinds: list[str] = field(default_factory=list)
    default_export_kind: str | None = None
    named_export_count: int = 0
    jsx_present: bool = False
    content_signal: str | None = None
    unparseable_regions: list[dict] = field(default_factory=list)

    @property
    def named_export_count_bucket(self) -> str:
        return bucket_named_export_count(self.named_export_count)


# -----------------------------------------------------------------------------
# Language detection
# -----------------------------------------------------------------------------


def detect_language(file_path: str | None) -> str | None:
    """Map a file extension to a supported lint language, or None.

    None means "do not run heuristics" — the engine returns no violations
    rather than guessing.
    """
    if not file_path:
        return None
    lower = file_path.lower()
    for ext in _TS_EXTENSIONS:
        if lower.endswith(ext):
            return "typescript"
    for ext in _RUBY_EXTENSIONS:
        if lower.endswith(ext):
            return "ruby"
    return None


# -----------------------------------------------------------------------------
# String / comment stripping (TypeScript)
# -----------------------------------------------------------------------------


_TS_LINE_COMMENT = re.compile(r"//[^\n]*")
_TS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# Match plain quoted strings (best-effort; we don't try to be perfect about
# escapes since we're just stripping for JSX detection).
_TS_STRING = re.compile(
    r"""(?<!\\)(?:
        "(?:\\.|[^"\\])*" |
        '(?:\\.|[^'\\])*' |
        `(?:\\.|[^`\\])*`
    )""",
    re.VERBOSE | re.DOTALL,
)


def _strip_ts_strings_and_comments(content: str) -> str:
    """Best-effort strip of strings/comments to reduce JSX false positives.

    We replace each match with a same-length run of spaces so positions
    elsewhere remain meaningful (regex flag offsets / future line numbering).
    """
    def _spaces(m: re.Match) -> str:
        return " " * len(m.group(0))

    out = _TS_BLOCK_COMMENT.sub(_spaces, content)
    out = _TS_LINE_COMMENT.sub(_spaces, out)
    out = _TS_STRING.sub(_spaces, out)
    return out


# -----------------------------------------------------------------------------
# TypeScript dimension extraction
# -----------------------------------------------------------------------------


# `export default ...` patterns. Order matters: try class / function before
# falling back to ExportAssignment-like default (object literal / identifier).
_TS_DEFAULT_FUNCTION = re.compile(r"^\s*export\s+default\s+(?:async\s+)?function\b", re.MULTILINE)
_TS_DEFAULT_CLASS = re.compile(r"^\s*export\s+default\s+class\b", re.MULTILINE)
_TS_DEFAULT_ARROW = re.compile(
    r"^\s*export\s+default\s*\(?\s*(?:async\s*)?\(.*?\)\s*=>", re.MULTILINE | re.DOTALL
)
_TS_DEFAULT_OBJECT = re.compile(r"^\s*export\s+default\s*\{", re.MULTILINE)
_TS_DEFAULT_ARRAY = re.compile(r"^\s*export\s+default\s*\[", re.MULTILINE)
_TS_DEFAULT_IDENT = re.compile(r"^\s*export\s+default\s+\w", re.MULTILINE)

# Named exports: top-level lines beginning with `export <kind>`.
_TS_NAMED_EXPORTS = [
    re.compile(r"^\s*export\s+(?:const|let|var)\s+(\w+)\s*[=:]", re.MULTILINE),
    re.compile(r"^\s*export\s+(?:async\s+)?function\s+(\w+)", re.MULTILINE),
    re.compile(r"^\s*export\s+class\s+(\w+)", re.MULTILINE),
    re.compile(r"^\s*export\s+interface\s+(\w+)", re.MULTILINE),
    re.compile(r"^\s*export\s+type\s+(\w+)", re.MULTILINE),
    re.compile(r"^\s*export\s+enum\s+(\w+)", re.MULTILINE),
]
# `export { a, b, c };` — explicit re-export list.
_TS_EXPORT_LIST = re.compile(r"^\s*export\s*\{\s*([^}]*)\s*\}\s*;?\s*$", re.MULTILINE)

# Top-level node kinds we try to detect heuristically. Each rule maps a
# regex over the (string-stripped) content to a top-level node kind name
# (matching the ts_dump.mjs SyntaxKind strings). Order matters: the first
# rule that matches wins, so put more-specific rules above more-general
# ones.
#
# Semantics note: in TS Compiler API land, `export default function Page() {}`
# is a `FunctionDeclaration` with a Default modifier (NOT an
# `ExportAssignment`). `ExportAssignment` only covers `export default
# <expression>` like `export default foo;` or `export default {}`. We
# preserve that distinction so the lint engine's top_level_node_kinds
# observations align with what ts_dump.mjs produces.
_TS_TOP_LEVEL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s*import\s", re.MULTILINE), "ImportDeclaration"),
    # `export default function ...` / `export default class ...` keep the
    # underlying declaration kind in the TS AST. Match these FIRST so the
    # more-general `^\s*export\s+default\s` rule below doesn't claim them.
    (re.compile(r"^\s*export\s+default\s+(?:async\s+)?function\b", re.MULTILINE), "FunctionDeclaration"),
    (re.compile(r"^\s*export\s+default\s+class\b", re.MULTILINE), "ClassDeclaration"),
    # `export default <expression>` (object literal, array literal, identifier,
    # arrow function) is an ExportAssignment node in the TS AST.
    (re.compile(r"^\s*export\s+default\s", re.MULTILINE), "ExportAssignment"),
    (
        re.compile(r"^\s*export\s*\{[^}]*\}\s*(?:from\s+[\"'][^\"']*[\"'])?\s*;?\s*$", re.MULTILINE),
        "ExportDeclaration",
    ),
    (re.compile(r"^\s*export\s+(?:async\s+)?function\s", re.MULTILINE), "FunctionDeclaration"),
    (re.compile(r"^\s*export\s+class\s", re.MULTILINE), "ClassDeclaration"),
    (re.compile(r"^\s*export\s+interface\s", re.MULTILINE), "InterfaceDeclaration"),
    (re.compile(r"^\s*export\s+type\s", re.MULTILINE), "TypeAliasDeclaration"),
    (re.compile(r"^\s*export\s+enum\s", re.MULTILINE), "EnumDeclaration"),
    (re.compile(r"^\s*export\s+(?:const|let|var)\s", re.MULTILINE), "FirstStatement"),
    (re.compile(r"^\s*(?:async\s+)?function\s+\w", re.MULTILINE), "FunctionDeclaration"),
    (re.compile(r"^\s*class\s+\w", re.MULTILINE), "ClassDeclaration"),
    (re.compile(r"^\s*interface\s+\w", re.MULTILINE), "InterfaceDeclaration"),
    (re.compile(r"^\s*type\s+\w+\s*=", re.MULTILINE), "TypeAliasDeclaration"),
    (re.compile(r"^\s*enum\s+\w", re.MULTILINE), "EnumDeclaration"),
    (re.compile(r"^\s*(?:const|let|var)\s+\w", re.MULTILINE), "FirstStatement"),
)

# Approximate JSX detection. Three forms count:
#   `</Name>`          — closing tag
#   `<Name ... />`     — self-closing element (HTML or component)
#   `<>` / `</>`       — fragment
# To avoid TypeScript generic false positives like `Array<string>`, we
# require self-closing forms to terminate with `/>` and demand the
# preceding character not be alphanumeric (so `x<T/>` won't match — and
# `Array<string>` has no `/>` ender so it can't match here anyway).
_JSX_CLOSING = re.compile(r"</[A-Za-z][\w.-]*\s*>")
_JSX_SELF_CLOSING = re.compile(
    r"(?<![A-Za-z0-9_])<[A-Za-z][\w.]*(?:\s[^<>]*?)?/>", re.DOTALL
)
_JSX_FRAGMENT = re.compile(r"<>|</>")


def _extract_typescript(content: str) -> DimensionSnapshot:
    """Pull DimensionSnapshot out of TS-family content via regex heuristics.

    Order of operations matters: we strip strings/comments BEFORE the JSX
    scan so a `"</div>"` literal doesn't fire jsx_present. We do NOT strip
    them before the export scan because most exports are on lines that are
    unlikely to begin inside a string literal.
    """
    stripped = _strip_ts_strings_and_comments(content)

    # default_export_kind: first matching pattern wins
    default_export_kind: str | None = None
    if _TS_DEFAULT_CLASS.search(content):
        default_export_kind = "ClassDeclaration"
    elif _TS_DEFAULT_FUNCTION.search(content):
        default_export_kind = "FunctionDeclaration"
    elif _TS_DEFAULT_ARROW.search(content):
        default_export_kind = "ArrowFunction"
    elif _TS_DEFAULT_OBJECT.search(content):
        default_export_kind = "ObjectLiteralExpression"
    elif _TS_DEFAULT_ARRAY.search(content):
        default_export_kind = "ArrayLiteralExpression"
    elif _TS_DEFAULT_IDENT.search(content):
        default_export_kind = "Identifier"

    # named_export_count: sum of unique top-level named declarations + export-list members
    named_names: set[str] = set()
    for pat in _TS_NAMED_EXPORTS:
        for m in pat.finditer(content):
            named_names.add(m.group(1))
    for m in _TS_EXPORT_LIST.finditer(content):
        body = m.group(1)
        for piece in body.split(","):
            name = piece.strip()
            if not name:
                continue
            # Strip `as Alias` if present; we only care about export count.
            head = name.split(" as ")[-1].strip()
            if head:
                named_names.add(head)
    named_export_count = len(named_names)

    # top_level_node_kinds: walk lines and tag each via the first matching rule.
    # We tag a line only once (the first regex that matches "wins").
    top_level: list[str] = []
    for line_no, line in enumerate(content.splitlines()):
        stripped_line = line.lstrip()
        if not stripped_line:
            continue
        # Match only on top-level (no indentation). This is a heuristic and
        # will miss top-level statements inside namespaces — acceptable for v0.3.
        indent = len(line) - len(stripped_line)
        if indent != 0:
            continue
        for pat, kind in _TS_TOP_LEVEL_RULES:
            if pat.match(line):
                top_level.append(kind)
                break
        del line_no  # reserved for future unparseable-region tracking

    # jsx_present: scan stripped content for any closing/self-closing/fragment tag.
    jsx_present = (
        _JSX_CLOSING.search(stripped) is not None
        or _JSX_SELF_CLOSING.search(stripped) is not None
        or _JSX_FRAGMENT.search(stripped) is not None
    )

    # content_signal: reuse the shared sig function on the first 200 bytes.
    head = content[:200]
    cs = content_signal_match_for(head)
    content_signal = cs if cs != "none" else None

    return DimensionSnapshot(
        top_level_node_kinds=top_level,
        default_export_kind=default_export_kind,
        named_export_count=named_export_count,
        jsx_present=jsx_present,
        content_signal=content_signal,
        unparseable_regions=[],
    )


# -----------------------------------------------------------------------------
# Ruby dimension extraction (v0.3: minimal — bucket of top-level nodes only)
# -----------------------------------------------------------------------------


_RUBY_LINE_COMMENT = re.compile(r"#[^\n]*")
_RUBY_BLOCK_COMMENT = re.compile(r"^=begin\b.*?^=end\b", re.DOTALL | re.MULTILINE)
_RUBY_STRING_DQ = re.compile(r'"(?:\\.|[^"\\])*"', re.DOTALL)
_RUBY_STRING_SQ = re.compile(r"'(?:\\.|[^'\\])*'", re.DOTALL)


def _strip_ruby_strings_and_comments(content: str) -> str:
    def _spaces(m: re.Match) -> str:
        return " " * len(m.group(0))

    out = _RUBY_BLOCK_COMMENT.sub(_spaces, content)
    out = _RUBY_LINE_COMMENT.sub(_spaces, out)
    out = _RUBY_STRING_DQ.sub(_spaces, out)
    out = _RUBY_STRING_SQ.sub(_spaces, out)
    return out


# Ruby top-level constructs we recognize. Class / module / def → "exports"
# in our normalized signature (mirrors prism_dump.rb's is_top_level_export?).
_RUBY_TOP_LEVEL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^class\s+\w", re.MULTILINE), "ClassNode"),
    (re.compile(r"^module\s+\w", re.MULTILINE), "ModuleNode"),
    (re.compile(r"^def\s+\w", re.MULTILINE), "DefNode"),
    (re.compile(r"^require\b", re.MULTILINE), "CallNode"),
    (re.compile(r"^require_relative\b", re.MULTILINE), "CallNode"),
)


def _extract_ruby(content: str) -> DimensionSnapshot:
    """Best-effort Ruby dimension extraction.

    Phase 4.1 covers the structural cases (class / module / def at column 0).
    Ruby has no JSX and no `export default` analogue: prism_dump.rb sets
    default_export_kind to the *kind* of the single top-level class/module
    when exactly one is present.
    """
    stripped = _strip_ruby_strings_and_comments(content)

    top_level: list[str] = []
    top_level_class_or_module: list[str] = []
    named_export_count = 0
    for line in stripped.splitlines():
        if not line or line.startswith(" ") or line.startswith("\t"):
            continue
        for pat, kind in _RUBY_TOP_LEVEL_RULES:
            if pat.match(line):
                top_level.append(kind)
                if kind in ("ClassNode", "ModuleNode"):
                    top_level_class_or_module.append(kind)
                if kind in ("ClassNode", "ModuleNode", "DefNode"):
                    named_export_count += 1
                break

    default_export_kind = (
        top_level_class_or_module[0]
        if len(top_level_class_or_module) == 1
        else None
    )

    # Ruby files have no JSX. content_signal is best-effort via the shared
    # function (catches shebangs); ruby-specific signals like `frozen_string_literal`
    # aren't part of the v0.3 cluster signature alphabet.
    head = content[:200]
    cs = content_signal_match_for(head)
    content_signal = cs if cs != "none" else None

    return DimensionSnapshot(
        top_level_node_kinds=top_level,
        default_export_kind=default_export_kind,
        named_export_count=named_export_count,
        jsx_present=False,
        content_signal=content_signal,
        unparseable_regions=[],
    )


# -----------------------------------------------------------------------------
# Public API: extract_dimensions
# -----------------------------------------------------------------------------


def extract_dimensions(
    content: str,
    *,
    language: str | None = None,
    file_path: str | None = None,
) -> DimensionSnapshot:
    """Build a DimensionSnapshot from raw source content.

    `language` overrides extension-based detection; `file_path` is consulted
    only if `language` is not provided. If neither yields a supported
    language, the returned snapshot is empty (all defaults) — the lint
    engine treats that as "no observations" and emits no violations.

    Pure function: no I/O. Safe to call on hostile / oversized input
    (callers should still cap at 100KB per the architecture's lint_file
    contract; this function won't refuse but may be slow on multi-MB
    inputs due to regex backtracking on pathological cases).
    """
    if language is None:
        language = detect_language(file_path)
    if language == "typescript":
        return _extract_typescript(content)
    if language == "ruby":
        return _extract_ruby(content)
    return DimensionSnapshot()


# -----------------------------------------------------------------------------
# Public API: lint
# -----------------------------------------------------------------------------


def _top_level_kinds_match(file_kinds: list[str], expected: list[str]) -> bool:
    """Multiset comparison: every kind expected must appear at least once
    in the file, with at least the same multiplicity.

    Sequence ordering is NOT enforced (writers reorder imports / type aliases
    freely without changing the archetype). Files are allowed to contain
    EXTRA top-level kinds beyond what the archetype lists — we only flag
    when something is missing. Rationale: the canonical's top_level_node_kinds
    is a structural lower bound, not an upper bound; adding an `import` line
    shouldn't flag a violation.
    """
    if not expected:
        return True
    from collections import Counter

    file_counts = Counter(file_kinds)
    expected_counts = Counter(expected)
    for kind, n in expected_counts.items():
        if file_counts.get(kind, 0) < n:
            return False
    return True


def lint(snapshot: DimensionSnapshot, ast_query: dict | None) -> list[Violation]:
    """Compare a snapshot against the archetype's ast_query; return violations.

    Encoding rule (from `derive_ast_query`):
    - A non-null ast_query field carries an expectation.
    - A null field means "no expectation set" — never flag.
    - `content_signal == "none"` from the cluster signature is stored as None
      in ast_query, so a null content_signal here means "any directive (or no
      directive) is acceptable".

    Severity choices:
    - `default-export-kind-mismatch`: warning. Mixing function-export and
      class-export styles within an archetype is a real inconsistency but
      not always a bug (refactors happen).
    - `top-level-node-kinds-mismatch`: warning. Missing a top-level kind
      the archetype expects often means the file is restructured.
    - `named-export-count-bucket-mismatch`: info. The bucket boundaries are
      coarse; a bucket mismatch is a soft signal.
    - `jsx-presence-mismatch`: warning when the file has JSX and the
      archetype doesn't expect it, ERROR in the reverse direction. Rationale:
      adding JSX to a non-JSX archetype (e.g., a util file) is structurally
      wrong and worth a hard flag; missing JSX in a JSX archetype could be
      a stub.
    - `content-signal-mismatch`: warning. `'use client'` etc. are
      semantically significant in modern frameworks but not always required.
    """
    if not ast_query:
        return []

    violations: list[Violation] = []

    # default_export_kind
    expected_default = ast_query.get("default_export_kind")
    actual_default = snapshot.default_export_kind
    if expected_default is not None and expected_default != actual_default:
        violations.append(
            Violation(
                rule="default-export-kind-mismatch",
                expected=str(expected_default),
                actual=str(actual_default) if actual_default is not None else "none",
                severity="warning",
                message=(
                    f"archetype expects default export of kind '{expected_default}'; "
                    f"file has '{actual_default or 'none'}'"
                ),
            )
        )

    # top_level_node_kinds (multiset comparison; see _top_level_kinds_match)
    expected_kinds = ast_query.get("top_level_node_kinds")
    if expected_kinds:
        if not _top_level_kinds_match(snapshot.top_level_node_kinds, list(expected_kinds)):
            violations.append(
                Violation(
                    rule="top-level-node-kinds-mismatch",
                    expected=repr(list(expected_kinds)),
                    actual=repr(snapshot.top_level_node_kinds),
                    severity="warning",
                    message=(
                        "file is missing one or more top-level constructs the "
                        "archetype expects (multiset comparison; extras are ok, "
                        "missing kinds are flagged)"
                    ),
                )
            )

    # named_export_count_bucket
    expected_bucket = ast_query.get("named_export_count_bucket")
    if expected_bucket is not None:
        actual_bucket = snapshot.named_export_count_bucket
        if actual_bucket != expected_bucket:
            violations.append(
                Violation(
                    rule="named-export-count-bucket-mismatch",
                    expected=str(expected_bucket),
                    actual=str(actual_bucket),
                    severity="info",
                    message=(
                        f"archetype expects named-export-count bucket "
                        f"'{expected_bucket}'; file has '{actual_bucket}'"
                    ),
                )
            )

    # jsx_present
    expected_jsx = ast_query.get("jsx_present")
    if expected_jsx is not None:
        actual_jsx = bool(snapshot.jsx_present)
        if actual_jsx and not expected_jsx:
            violations.append(
                Violation(
                    rule="jsx-presence-mismatch",
                    expected="False",
                    actual="True",
                    severity="error",
                    message=(
                        "archetype is non-JSX but file contains JSX; this is a "
                        "structural mismatch — move JSX to a component file"
                    ),
                )
            )
        elif expected_jsx and not actual_jsx:
            violations.append(
                Violation(
                    rule="jsx-presence-mismatch",
                    expected="True",
                    actual="False",
                    severity="warning",
                    message=(
                        "archetype expects JSX but file has none; if this file "
                        "is a stub, ignore — otherwise the archetype assignment "
                        "may be wrong"
                    ),
                )
            )

    # content_signal (null in ast_query → no expectation)
    expected_signal = ast_query.get("content_signal")
    if expected_signal is not None:
        actual_signal = snapshot.content_signal
        if actual_signal != expected_signal:
            violations.append(
                Violation(
                    rule="content-signal-mismatch",
                    expected=str(expected_signal),
                    actual=str(actual_signal) if actual_signal else "none",
                    severity="warning",
                    message=(
                        f"archetype expects a '{expected_signal}' directive at the "
                        f"top of the file; got '{actual_signal or 'none'}'"
                    ),
                )
            )

    return violations


def canonical_confidence(snapshot: DimensionSnapshot, ast_query: dict | None) -> float:
    """Fraction of non-null ast_query fields the file matched. 0.0–1.0.

    Counts only fields the archetype actually constrains (non-null in
    ast_query). When the archetype constrains no fields, returns 1.0
    (vacuously confident — nothing to disagree with).
    """
    if not ast_query:
        return 1.0

    checks: list[bool] = []

    if ast_query.get("default_export_kind") is not None:
        checks.append(ast_query["default_export_kind"] == snapshot.default_export_kind)

    expected_kinds = ast_query.get("top_level_node_kinds")
    if expected_kinds:
        checks.append(_top_level_kinds_match(snapshot.top_level_node_kinds, list(expected_kinds)))

    if ast_query.get("named_export_count_bucket") is not None:
        checks.append(
            ast_query["named_export_count_bucket"] == snapshot.named_export_count_bucket
        )

    if ast_query.get("jsx_present") is not None:
        checks.append(bool(ast_query["jsx_present"]) == bool(snapshot.jsx_present))

    if ast_query.get("content_signal") is not None:
        checks.append(ast_query["content_signal"] == snapshot.content_signal)

    if not checks:
        return 1.0
    return sum(1 for c in checks if c) / len(checks)
