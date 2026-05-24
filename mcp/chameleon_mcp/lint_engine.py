r"""Lint engine — compare a file's AST-shape dimensions against an archetype's
canonical `ast_query` and emit violations, AND scan content for secrets.

Phase 4.1 (v0.3): regex-heuristic extraction. The cluster signature function
in `signatures.py` operates on a real ParsedFile produced by the long-lived
ts_dump.mjs / prism_dump.rb subprocesses. Round-tripping through the
subprocess for every lint_file call would dominate latency (cold-start cost
of ~200ms per Node spawn, plus the `npm install` first-run trip), so for v0.3
the lint engine derives the same dimensions from the raw `content` string via
language-specific regex heuristics.

v0.4 (4.8) adds a `secret-detected-in-content` rule wired to the bootstrap
`detect-secrets` integration. The rule fires regardless of `ast_query` —
even files without an archetype get scanned — and emits a violation per
detected secret, capped at 50 per file to avoid the engine blowing up on
a key dump.

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


@dataclass(frozen=True)
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

_RUBY_SUPERCLASS_RE = re.compile(r"^class\s+\w[\w:]*\s*<\s*([\w:]+)", re.MULTILINE)
_RUBY_INCLUDE_RE = re.compile(r"^\s+include\s+([\w:]+)", re.MULTILINE)
_RUBY_DSL_CALLS = frozenset({
    "validates", "validate", "belongs_to", "has_many", "has_one",
    "has_and_belongs_to_many", "before_action", "after_action",
    "around_action", "before_validation", "after_commit", "scope",
    "enum", "delegate", "attr_accessor", "attr_reader",
})
_RUBY_DSL_RE = re.compile(
    r"^\s+(" + "|".join(_RUBY_DSL_CALLS) + r")\b", re.MULTILINE
)


def _extract_ruby(content: str) -> DimensionSnapshot:
    """Best-effort Ruby dimension extraction.

    Detects top-level class/module/def at column 0, plus enriched
    dimensions for archetype differentiation:
    - Superclass (ClassNode:ApplicationRecord vs ClassNode:ApplicationController)
    - Include calls (IncludeCall:Sidekiq::Job)
    - DSL calls (DslCall:validates, DslCall:belongs_to)
    """
    stripped = _strip_ruby_strings_and_comments(content)

    top_level: list[str] = []
    top_level_class_or_module: list[str] = []
    named_export_count = 0

    superclass: str | None = None
    sc_match = _RUBY_SUPERCLASS_RE.search(stripped)
    if sc_match:
        superclass = sc_match.group(1)

    _NESTED_CLASS_RE = re.compile(
        r"^\s{2,}class\s+(\w[\w:]*)\s*(?:<\s*([\w:\[\]]+))?", re.MULTILINE
    )

    for line in stripped.splitlines():
        if not line or line.startswith(" ") or line.startswith("\t"):
            continue
        for pat, kind in _RUBY_TOP_LEVEL_RULES:
            if pat.match(line):
                if kind == "ClassNode" and superclass:
                    top_level.append(f"ClassNode:{superclass}")
                    top_level_class_or_module.append("ClassNode")
                else:
                    top_level.append(kind)
                    if kind in ("ClassNode", "ModuleNode"):
                        top_level_class_or_module.append(kind)
                if kind in ("ClassNode", "ModuleNode", "DefNode"):
                    named_export_count += 1
                break

    # Detect classes nested inside modules (2-space indent).
    # module Api; class FooController < ApplicationController; end; end
    # The column-0 scan sees ModuleNode but misses the class inside.
    has_module = any(
        k == "ModuleNode" or k.startswith("ModuleNode:") for k in top_level
    )
    if has_module:
        for m in _NESTED_CLASS_RE.finditer(stripped):
            nested_sc = m.group(2)
            if nested_sc:
                top_level.append(f"ClassNode:{nested_sc}")
            else:
                top_level.append("ClassNode")
            top_level_class_or_module.append("ClassNode")

    for m in _RUBY_INCLUDE_RE.finditer(stripped):
        top_level.append(f"IncludeCall:{m.group(1)}")

    seen_dsl: set[str] = set()
    for m in _RUBY_DSL_RE.finditer(stripped):
        dsl_name = m.group(1)
        if dsl_name not in seen_dsl:
            seen_dsl.add(dsl_name)
            top_level.append(f"DslCall:{dsl_name}")

    default_export_kind = (
        top_level_class_or_module[0]
        if len(top_level_class_or_module) == 1
        else None
    )

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


_TS_CODE_KINDS = frozenset({
    "FunctionDeclaration", "FirstStatement", "ExportAssignment",
})

_DSL_CATEGORY: dict[str, str] = {}
for _d in ("validates", "validate", "belongs_to", "has_many", "has_one",
           "has_and_belongs_to_many", "scope", "enum",
           "before_validation", "after_commit"):
    _DSL_CATEGORY[_d] = "DslCall:ActiveRecord"
for _d in ("before_action", "after_action", "around_action"):
    _DSL_CATEGORY[_d] = "DslCall:ActionController"
for _d in ("delegate", "attr_accessor", "attr_reader"):
    _DSL_CATEGORY[_d] = "DslCall:Ruby"


def _normalize_kind(kind: str) -> str:
    """Normalize a node kind for fuzzy matching.

    ClassNode:ApplicationRecord -> ClassNode (strips superclass).
    DslCall:validates -> DslCall:ActiveRecord (DSL category, so models
    with different ActiveRecord DSLs still match, but controllers with
    ActionController DSLs don't).
    IncludeCall:* -> IncludeCall (any include matches any include).
    TS FunctionDeclaration/FirstStatement/ExportAssignment -> CodeDeclaration.
    """
    if kind.startswith("DslCall:"):
        dsl_name = kind.split(":", 1)[1]
        return _DSL_CATEGORY.get(dsl_name, "DslCall")
    if kind.startswith("IncludeCall:"):
        return "IncludeCall"
    if kind.startswith(("ClassNode:", "ModuleNode:")):
        return kind.split(":")[0]
    if kind in _TS_CODE_KINDS:
        return "CodeDeclaration"
    return kind


def _top_level_kinds_match(file_kinds: list[str], expected: list[str]) -> bool:
    """Similarity-based comparison with fuzzy matching for enriched kinds.

    At least half the expected kinds must have a match in the file.
    Matching uses normalized kinds: ClassNode:ApplicationRecord and
    ClassNode:ApplicationController both normalize to ClassNode, so
    any class matches any class regardless of superclass. Same for
    DslCall:* and IncludeCall:* - any DSL call matches any DSL call.
    """
    if not expected:
        return True

    normalized_file = {_normalize_kind(k) for k in file_kinds}
    file_set = set(file_kinds)
    expected_deduped = set(expected)

    matched = 0
    for kind in expected_deduped:
        if kind in file_set or _normalize_kind(kind) in normalized_file:
            matched += 1

    if matched < len(expected_deduped) * 0.5:
        return False

    # DSL conflict check: archetype expects DslCall:ActiveRecord but file
    # has DslCall:ActionController -> wrong framework. Only triggers when
    # BOTH sides have DSL calls from different categories.
    expected_dsl = {
        _normalize_kind(k) for k in expected if k.startswith("DslCall:")
    }
    file_dsl = {
        _normalize_kind(k) for k in file_kinds if k.startswith("DslCall:")
    }
    if expected_dsl and file_dsl and not (expected_dsl & file_dsl):
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


# -----------------------------------------------------------------------------
# Public API: scan_secrets (v0.4 — 4.8)
# -----------------------------------------------------------------------------


# Hard cap on the number of secret violations a single lint_file call
# returns. A real key dump or a giant accidental commit could trip dozens
# of patterns per line — we want the model to see "this file has secrets"
# without exhausting the response token budget.
MAX_SECRETS_PER_FILE = 50

# v0.5.2 (Bug — GitHub PAT bypassed by string-concat): the fallback regex in
# `profile/secret_scanner._FALLBACK_PATTERNS` and detect-secrets's per-line
# scanners require the secret prefix (e.g., `ghp_`) and the rest of the token
# to live in the *same* string literal. A trivially-obfuscated payload like
# `"ghp_" + "abcdef…"` (TS/JS) or `"ghp_" + "abcdef…"` (Python) defeats both.
#
# We fold consecutive same-quote-style string literals joined by `+` into a
# single literal *before* invoking the underlying scanners. Two safety rails:
#
# 1. We only fold literal-to-literal concat. `"ghp_" + foo()` is out of scope
#    (we'd need real dataflow to know what `foo()` returns) and stays
#    un-folded so detect-secrets sees the original text.
# 2. We bound the number of substitutions per call. A pathological generated
#    file with thousands of `+`-joined string fragments could otherwise burn
#    CPU before the existing 100KB content cap took effect.
_MAX_CONCAT_FOLDS_PER_FILE = 1000

# Quote-style-aware joined-string folder. Matches `"a" + "b"` with arbitrary
# whitespace (including newlines) around the `+`, regardless of escapes in
# the literal bodies. Three patterns — one per quote style — so we don't
# accidentally fold across mismatched delimiters (`"a" + 'b'` is left alone:
# the source language might treat that as an error and silently joining
# could produce a literal that doesn't exist in any source).
#
# We do NOT attempt to handle JS template literals (backticks) here: those
# can contain `${…}` interpolations whose contents are not known statically,
# so folding two backtick strings would produce a literal that does not
# represent the runtime value. Most real-world obfuscation attempts use
# plain quotes anyway.
_CONCAT_DQ = re.compile(
    r'"((?:\\.|[^"\\])*)"\s*\+\s*"((?:\\.|[^"\\])*)"',
    re.DOTALL,
)
_CONCAT_SQ = re.compile(
    r"'((?:\\.|[^'\\])*)'\s*\+\s*'((?:\\.|[^'\\])*)'",
    re.DOTALL,
)


def _fold_string_concat(
    content: str, *, max_folds: int = _MAX_CONCAT_FOLDS_PER_FILE
) -> str:
    """Iteratively collapse `"a" + "b"` / `'a' + 'b'` into single literals.

    Runs multiple passes because folding can create new folding opportunities
    (`"a" + "b" + "c"` → `"ab" + "c"` → `"abc"`). We bound *total*
    substitutions across passes at `max_folds`; once we're at the cap we
    stop returning whatever we've already produced. This keeps a pathologically
    long concat chain (auto-generated code, fuzzer input, etc.) from
    dominating lint_file latency. The 100KB content cap upstream gives a
    secondary defense.

    Mixed-quote pairs (`"a" + 'b'`) are left alone — see module-level comment
    above. The returned text is a strict structural subset of `content`'s
    information: every fold replaces a substring of length N with a substring
    of length ≤ N, so downstream regex line numbers may shift but no new
    text is introduced.

    Pure function — no I/O. Safe to call on hostile input; the regex engine
    runs at most `max_folds × 2` total substitutions before bailing.
    """
    if "+" not in content:
        return content

    remaining = max_folds
    out = content
    while remaining > 0:
        before = out

        def _join_dq(m: re.Match) -> str:
            return '"' + m.group(1) + m.group(2) + '"'

        def _join_sq(m: re.Match) -> str:
            return "'" + m.group(1) + m.group(2) + "'"

        # subn returns (new_string, n) so we can decrement the budget.
        out, n_dq = _CONCAT_DQ.subn(_join_dq, out, count=remaining)
        remaining -= n_dq
        if remaining <= 0:
            break
        out, n_sq = _CONCAT_SQ.subn(_join_sq, out, count=remaining)
        remaining -= n_sq
        # Idempotent fixpoint: if neither regex fired, we're done.
        if out == before:
            break
    return out


def scan_secrets(content: str, *, max_results: int = MAX_SECRETS_PER_FILE) -> list[Violation]:
    """Return one Violation per detected secret in `content`.

    Wires the bootstrap-time `detect-secrets` integration (see
    `profile/secret_scanner.scan_for_secrets`) into the edit-time lint path
    so files that introduce hardcoded credentials are flagged before they
    reach the model's output. Severity is `error` (this is a real security
    issue, not a style mismatch); the rule fires regardless of whether the
    file has an archetype, so even out-of-tree edits are covered.

    v0.5.2 (forem dogfood bug — "GitHub PAT bypassed by string-concat"):
    a preprocessing pass folds `"prefix" + "rest"` patterns before invoking
    the underlying scanners so that trivially-obfuscated tokens like
    `"ghp_" + "abc…"` reach detect-secrets as `"ghp_abc…"`. Same applies to
    Python concat (`"a" + "b"`) since the operator is identical. We then run
    BOTH the original content (to keep line numbers truthful for already-
    visible secrets) AND the folded content through the scanner, de-duped
    by (type, position) on the original-text scan and (type, "[concat]") for
    fold-only hits.

    Caps the result at `max_results` to avoid blowing up on a dump-style
    file. When the cap is hit we still report the cap so the caller can
    surface "and 17 more". Pure function — no I/O.
    """
    if not content:
        return []
    from chameleon_mcp.profile.secret_scanner import scan_for_secrets

    hits = scan_for_secrets(content)

    # Fold concat-obfuscated literals and re-scan. New hits (those that
    # didn't appear in the unfolded content) are appended with a clear
    # location marker so the operator knows we caught a deobfuscated form.
    folded = _fold_string_concat(content)
    if folded != content:
        # Build a quick dedup set keyed by (type, secret-shaped substring) on
        # the original. We can't trust (type, line_number) alone because the
        # fold changes the byte offsets within a line — but if the underlying
        # secret VALUE is already flagged in the original, we don't need to
        # re-flag it. detect-secrets redacts the value, so as a second-best
        # we dedup on (type, line_number) for line-bearing hits.
        seen_types_lines = {
            (h.get("type"), h.get("line_number"))
            for h in hits
            if h.get("line_number") is not None
        }
        seen_types = {h.get("type") for h in hits}
        for fh in scan_for_secrets(folded):
            key_line = (fh.get("type"), fh.get("line_number"))
            if fh.get("line_number") is not None and key_line in seen_types_lines:
                continue
            if fh.get("type") in seen_types and fh.get("line_number") is None:
                # Position-keyed hits collide if the same type already showed
                # up in the original; the folded text's positions are not
                # comparable. Drop to avoid double-reporting.
                continue
            # Tag the hit so the reported message makes the deobfuscation
            # explicit; the user shouldn't have to guess why a `ghp_…`
            # warning fired on a line that doesn't visually contain one.
            fh = dict(fh)
            fh["concat_folded"] = True
            hits.append(fh)

    if not hits:
        return []

    violations: list[Violation] = []
    capped = hits[:max_results]
    for hit in capped:
        location: str
        if "line_number" in hit and hit.get("line_number") is not None:
            location = f"line {hit['line_number']}"
        elif "position" in hit and hit.get("position") is not None:
            location = f"position {hit['position']}"
        else:
            location = "unknown location"
        kind = str(hit.get("type") or "unknown")
        # v0.5.2: surface deobfuscation-via-fold so the operator sees why a
        # ghp_… flag fired on a line whose visible text is two short literals.
        fold_suffix = " [after string-concat fold]" if hit.get("concat_folded") else ""
        violations.append(
            Violation(
                rule="secret-detected-in-content",
                expected="<no secret>",
                actual=f"{kind} at {location}{fold_suffix}",
                severity="error",
                message=(
                    f"detect-secrets flagged a {kind} at {location}{fold_suffix}. "
                    "Never commit credentials — rotate the secret and move it "
                    "to an environment variable or a secret manager."
                ),
            )
        )

    if len(hits) > len(capped):
        remaining = len(hits) - len(capped)
        violations.append(
            Violation(
                rule="secret-detected-in-content",
                expected="<no secrets beyond the cap>",
                actual=f"+{remaining} more (capped at {max_results})",
                severity="error",
                message=(
                    f"file contains {len(hits)} potential secrets; reporting "
                    f"the first {max_results}. Treat this file as compromised "
                    "and rotate every credential it touched."
                ),
            )
        )

    return violations
