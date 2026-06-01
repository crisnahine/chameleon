r"""Lint engine — compare a file's AST-shape dimensions against an archetype's
canonical `ast_query` and emit violations, AND scan content for secrets.

Phase 4.1: regex-heuristic extraction. The cluster signature function
in `signatures.py` operates on a real ParsedFile produced by the long-lived
ts_dump.mjs / prism_dump.rb subprocesses. Round-tripping through the
subprocess for every lint_file call would dominate latency (cold-start cost
of ~200ms per Node spawn, plus the `npm install` first-run trip), so
the lint engine derives the same dimensions from the raw `content` string via
language-specific regex heuristics.

A `secret-detected-in-content` rule is wired to the bootstrap
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


def recalibrate_ast_query(witness_snapshot: DimensionSnapshot) -> dict:
    """Build a recalibrated ast_query dict from a witness file's regex-derived dimensions.

    The stored ast_query was derived from the real AST parser (ts_dump.mjs /
    prism_dump.rb) at bootstrap, but lint() compares against regex-derived
    dimensions. The two extractors disagree on counts (ImportDeclaration,
    InterfaceDeclaration, jsx_present) causing false positives. Recalibrating
    to regex-vs-regex — deriving the query from the witness's OWN regex
    snapshot — eliminates that gap, so all five dimensions can be enforced
    instead of only two. Both the witness ast_query and the candidate snapshot
    now come from the same regex extractor, so a conforming candidate matches
    exactly. Set ``CHAMELEON_LINT_DIMENSIONS=core`` to fall back to the coarse
    two-dimension behavior (top_level_node_kinds + content_signal only).
    """
    import os

    core_only = os.environ.get("CHAMELEON_LINT_DIMENSIONS") == "core"
    return {
        "default_export_kind": None if core_only else witness_snapshot.default_export_kind,
        "jsx_present": None if core_only else witness_snapshot.jsx_present,
        "top_level_node_kinds": sorted(set(witness_snapshot.top_level_node_kinds)),
        "named_export_count_bucket": (
            None if core_only else witness_snapshot.named_export_count_bucket
        ),
        "content_signal": witness_snapshot.content_signal,
    }


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


_TS_LINE_COMMENT = re.compile(r"//[^\n]*")
_TS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
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


_TS_DEFAULT_FUNCTION = re.compile(r"^\s*export\s+default\s+(?:async\s+)?function\b", re.MULTILINE)
_TS_DEFAULT_CLASS = re.compile(r"^\s*export\s+default\s+class\b", re.MULTILINE)
_TS_DEFAULT_ARROW = re.compile(
    r"^\s*export\s+default\s*\(?\s*(?:async\s*)?\(.*?\)\s*=>", re.MULTILINE | re.DOTALL
)
_TS_DEFAULT_OBJECT = re.compile(r"^\s*export\s+default\s*\{", re.MULTILINE)
_TS_DEFAULT_ARRAY = re.compile(r"^\s*export\s+default\s*\[", re.MULTILINE)
_TS_DEFAULT_IDENT = re.compile(r"^\s*export\s+default\s+\w", re.MULTILINE)

_TS_NAMED_EXPORTS = [
    re.compile(r"^\s*export\s+(?:const|let|var)\s+(\w+)\s*[=:]", re.MULTILINE),
    re.compile(r"^\s*export\s+(?:async\s+)?function\s+(\w+)", re.MULTILINE),
    re.compile(r"^\s*export\s+class\s+(\w+)", re.MULTILINE),
    re.compile(r"^\s*export\s+interface\s+(\w+)", re.MULTILINE),
    re.compile(r"^\s*export\s+type\s+(\w+)", re.MULTILINE),
    re.compile(r"^\s*export\s+enum\s+(\w+)", re.MULTILINE),
]
_TS_EXPORT_LIST = re.compile(r"^\s*export\s*\{\s*([^}]*)\s*\}\s*;?\s*$", re.MULTILINE)

_TS_TOP_LEVEL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s*import\s", re.MULTILINE), "ImportDeclaration"),
    (
        re.compile(r"^\s*export\s+default\s+(?:async\s+)?function\b", re.MULTILINE),
        "FunctionDeclaration",
    ),
    (re.compile(r"^\s*export\s+default\s+class\b", re.MULTILINE), "ClassDeclaration"),
    (re.compile(r"^\s*export\s+default\s", re.MULTILINE), "ExportAssignment"),
    (
        re.compile(
            r"^\s*export\s*\{[^}]*\}\s*(?:from\s+[\"'][^\"']*[\"'])?\s*;?\s*$", re.MULTILINE
        ),
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

_JSX_CLOSING = re.compile(r"</[A-Za-z][\w.-]*\s*>")
_JSX_SELF_CLOSING = re.compile(r"(?<![A-Za-z0-9_])<[A-Za-z][\w.]*(?:\s[^<>]*?)?/>", re.DOTALL)
_JSX_FRAGMENT = re.compile(r"<>|</>")


def _extract_typescript(content: str) -> DimensionSnapshot:
    """Pull DimensionSnapshot out of TS-family content via regex heuristics.

    Order of operations matters: we strip strings/comments BEFORE the JSX
    scan so a `"</div>"` literal doesn't fire jsx_present. We do NOT strip
    them before the export scan because most exports are on lines that are
    unlikely to begin inside a string literal.
    """
    stripped = _strip_ts_strings_and_comments(content)

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
            head = name.split(" as ")[-1].strip()
            if head:
                named_names.add(head)
    named_export_count = len(named_names)

    top_level: list[str] = []
    for line_no, line in enumerate(content.splitlines()):
        stripped_line = line.lstrip()
        if not stripped_line:
            continue
        indent = len(line) - len(stripped_line)
        if indent != 0:
            continue
        for pat, kind in _TS_TOP_LEVEL_RULES:
            if pat.match(line):
                top_level.append(kind)
                break
        del line_no

    jsx_present = (
        _JSX_CLOSING.search(stripped) is not None
        or _JSX_SELF_CLOSING.search(stripped) is not None
        or _JSX_FRAGMENT.search(stripped) is not None
    )

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


_RUBY_TOP_LEVEL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^class\s+\w", re.MULTILINE), "ClassNode"),
    (re.compile(r"^module\s+\w", re.MULTILINE), "ModuleNode"),
    (re.compile(r"^def\s+\w", re.MULTILINE), "DefNode"),
    (re.compile(r"^require\b", re.MULTILINE), "CallNode"),
    (re.compile(r"^require_relative\b", re.MULTILINE), "CallNode"),
)

_RUBY_SUPERCLASS_RE = re.compile(r"^class\s+\w[\w:]*\s*<\s*([\w:]+)", re.MULTILINE)
_RUBY_INCLUDE_RE = re.compile(r"^\s+include\s+([\w:]+)", re.MULTILINE)
_RUBY_DSL_CALLS = frozenset(
    {
        "validates",
        "validate",
        "belongs_to",
        "has_many",
        "has_one",
        "has_and_belongs_to_many",
        "before_action",
        "after_action",
        "around_action",
        "before_validation",
        "after_commit",
        "scope",
        "enum",
        "delegate",
        "attr_accessor",
        "attr_reader",
    }
)
_RUBY_DSL_RE = re.compile(r"^\s+(" + "|".join(_RUBY_DSL_CALLS) + r")\b", re.MULTILINE)


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

    _NESTED_CLASS_RE = re.compile(
        r"^\s{2,}class\s+(\w[\w:]*)\s*(?:<\s*([\w:\[\]]+))?", re.MULTILINE
    )

    for line in stripped.splitlines():
        if not line or line.startswith(" ") or line.startswith("\t"):
            continue
        for pat, kind in _RUBY_TOP_LEVEL_RULES:
            if pat.match(line):
                if kind == "ClassNode":
                    sc_match = _RUBY_SUPERCLASS_RE.match(line)
                    if sc_match:
                        top_level.append(f"ClassNode:{sc_match.group(1)}")
                    else:
                        top_level.append("ClassNode")
                    top_level_class_or_module.append("ClassNode")
                else:
                    top_level.append(kind)
                    if kind in ("ClassNode", "ModuleNode"):
                        top_level_class_or_module.append(kind)
                if kind in ("ClassNode", "ModuleNode", "DefNode"):
                    named_export_count += 1
                break

    has_module = any(k == "ModuleNode" or k.startswith("ModuleNode:") for k in top_level)
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
        top_level_class_or_module[0] if len(top_level_class_or_module) == 1 else None
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


_TS_CODE_KINDS = frozenset(
    {
        "FunctionDeclaration",
        "FirstStatement",
        "ExportAssignment",
    }
)

_DSL_CATEGORY: dict[str, str] = {}
for _d in (
    "validates",
    "validate",
    "belongs_to",
    "has_many",
    "has_one",
    "has_and_belongs_to_many",
    "scope",
    "enum",
    "before_validation",
    "after_commit",
):
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
    if not isinstance(kind, str):
        return ""
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


def _coarse_normalize(kind: str) -> str:
    """Collapse all DslCall categories to generic DslCall for matching.

    _normalize_kind separates DslCall:validates -> DslCall:ActiveRecord and
    DslCall:attr_reader -> DslCall:Ruby — useful for the conflict check but
    too strict for presence matching. A model with attr_reader AND validates
    is still a model; "does the file have any DSL call?" is what matters for
    the threshold check. The conflict check uses the finer categories.
    """
    n = _normalize_kind(kind)
    if n.startswith("DslCall:"):
        return "DslCall"
    return n


def _top_level_kinds_match(file_kinds: list[str], expected: list[str]) -> bool:
    """Similarity-based comparison with fuzzy matching for enriched kinds.

    Matching uses coarse-normalized kinds: ClassNode:ApplicationRecord and
    ClassNode:ApplicationController both collapse to ClassNode; all DslCall
    variants collapse to DslCall; IncludeCall:* to IncludeCall. This answers
    "does the file have these structural elements?" without penalizing
    cross-category DSL presence (e.g. attr_reader + validates in the same
    model).

    BUG-031: require at least 2 matching kinds when the expected set has 2+
    unique kinds. The old 50% threshold let 1-of-2 pass, meaning a bare
    ClassNode matched any archetype that expected ClassNode + anything.
    """
    if not expected:
        return True

    coarse_expected = {_coarse_normalize(k) for k in expected}
    coarse_file = {_coarse_normalize(k) for k in file_kinds}

    matched = sum(1 for k in coarse_expected if k in coarse_file)
    n = len(coarse_expected)
    min_required = max(n * 0.5, min(2, n))
    if matched < min_required:
        return False

    _NEUTRAL_DSL = {"DslCall", "DslCall:Ruby"}
    expected_dsl = {_normalize_kind(k) for k in expected if k.startswith("DslCall:")} - _NEUTRAL_DSL
    file_dsl = {_normalize_kind(k) for k in file_kinds if k.startswith("DslCall:")} - _NEUTRAL_DSL
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
        checks.append(ast_query["named_export_count_bucket"] == snapshot.named_export_count_bucket)

    if ast_query.get("jsx_present") is not None:
        checks.append(bool(ast_query["jsx_present"]) == bool(snapshot.jsx_present))

    if ast_query.get("content_signal") is not None:
        checks.append(ast_query["content_signal"] == snapshot.content_signal)

    if not checks:
        return 1.0
    return sum(1 for c in checks if c) / len(checks)


# Surface every secret in a file (bounded by the 100KB content ceiling), not
# just the first 50; the ERROR-severity rollup still summarizes.
MAX_SECRETS_PER_FILE = 1000

_MAX_CONCAT_FOLDS_PER_FILE = 1000

_CONCAT_DQ = re.compile(
    r'"((?:\\.|[^"\\])*)"\s*\+\s*"((?:\\.|[^"\\])*)"',
    re.DOTALL,
)
_CONCAT_SQ = re.compile(
    r"'((?:\\.|[^'\\])*)'\s*\+\s*'((?:\\.|[^'\\])*)'",
    re.DOTALL,
)


def _fold_string_concat(content: str, *, max_folds: int = _MAX_CONCAT_FOLDS_PER_FILE) -> str:
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

        out, n_dq = _CONCAT_DQ.subn(_join_dq, out, count=remaining)
        remaining -= n_dq
        if remaining <= 0:
            break
        out, n_sq = _CONCAT_SQ.subn(_join_sq, out, count=remaining)
        remaining -= n_sq
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

    (Forem dogfood bug — "GitHub PAT bypassed by string-concat"):
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

    folded = _fold_string_concat(content)
    if folded != content:
        seen_types_lines = {
            (h.get("type"), h.get("line_number")) for h in hits if h.get("line_number") is not None
        }
        seen_types = {h.get("type") for h in hits}
        for fh in scan_for_secrets(folded):
            key_line = (fh.get("type"), fh.get("line_number"))
            if fh.get("line_number") is not None and key_line in seen_types_lines:
                continue
            if fh.get("type") in seen_types and fh.get("line_number") is None:
                continue
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


_CHAMELEON_IGNORE_RE = re.compile(r"//\s*chameleon-ignore\s+([\w-]+)")
_CHAMELEON_IGNORE_RUBY_RE = re.compile(r"#\s*chameleon-ignore\s+([\w-]+)")
_TS_IMPORT_FROM_RE = re.compile(r"import\s+.*?\bfrom\s+['\"]([^'\"]+)['\"]", re.MULTILINE)
_TS_INTERFACE_DECL_RE = re.compile(r"\binterface\s+([A-Z]\w*)")


def lint_conventions(
    content: str,
    conventions: dict | None,
    *,
    language: str | None = None,
) -> list[Violation]:
    """Check file content against convention rules."""
    if not conventions:
        return []

    ignored_rules: set[str] = set()
    for m in _CHAMELEON_IGNORE_RE.finditer(content):
        ignored_rules.add(m.group(1))

    # Run the NAMING + INHERITANCE violation scans against a strings/comments-
    # stripped copy so a class/interface decl inside a heredoc / template string
    # / comment (common in Rails generators + specs) doesn't trip a false
    # violation that drives the L2 STOP escalation. The strip helpers preserve
    # length so positions stay aligned. The import scan keeps RAW content — it
    # needs the `from "<module>"` literal, which the strip blanks. Ignore-
    # directive scans also stay on raw `content` (directives live in comments).
    if language == "ruby":
        scan_content = _strip_ruby_strings_and_comments(content)
    elif language == "typescript":
        scan_content = _strip_ts_strings_and_comments(content)
    else:
        scan_content = content

    violations: list[Violation] = []

    if "import-preference" not in ignored_rules:
        for competing in (conventions.get("imports") or {}).get("competing", []):
            if not isinstance(competing, dict):
                continue
            over_mod = competing.get("over")
            preferred_mod = competing.get("preferred")
            if not over_mod or not preferred_mod:
                continue
            # Match the over/preferred token on word boundaries so `useQuery`
            # doesn't match `useQueryClient` and `useCustomQuery` in a comment
            # doesn't falsely suppress the violation.
            over_re = re.compile(r"\b" + re.escape(over_mod) + r"\b")
            preferred_re = re.compile(r"\b" + re.escape(preferred_mod) + r"\b")
            if preferred_re.search(content):
                continue
            for m in _TS_IMPORT_FROM_RE.finditer(content):
                if over_re.search(m.group(0)):
                    violations.append(
                        Violation(
                            rule="import-preference-violation",
                            expected=preferred_mod,
                            actual=over_mod,
                            severity="warning",
                            message=f"IMPORT: {over_mod} imported - replace with {preferred_mod} (all usages)",
                        )
                    )
                    break

    if language == "typescript" and "naming-convention" not in ignored_rules:
        naming = conventions.get("naming") or {}
        prefix_entry = naming.get("interface_prefix")
        if prefix_entry and prefix_entry.get("consistency", 0) >= 0.60:
            expected_prefix = prefix_entry["pattern"]
            for m in _TS_INTERFACE_DECL_RE.finditer(scan_content):
                name = m.group(1)
                if not name.startswith(expected_prefix) or (len(name) > 1 and name[1].islower()):
                    violations.append(
                        Violation(
                            rule="naming-convention-violation",
                            expected=f"{expected_prefix}-prefix",
                            actual=name,
                            severity="warning",
                            message=f"NAMING: interface {name} should use {expected_prefix}-prefix ({prefix_entry['consistency']:.0%} convention)",
                        )
                    )

    if language == "ruby" and "inheritance-convention" not in ignored_rules:
        for m in _CHAMELEON_IGNORE_RUBY_RE.finditer(content):
            ignored_rules.add(m.group(1))

        if "inheritance-convention" not in ignored_rules:
            inheritance = conventions.get("inheritance") or {}
            dominant_base = inheritance.get("dominant_base")
            if dominant_base and inheritance.get("frequency", 0) >= 0.60:
                # Accept any base the repo has established for this archetype,
                # not just the single dominant one. ``[\w:]+`` for the class
                # name captures namespaced declarations fully (a bare ``\w+``
                # truncated ``Api::V1::Foo`` to ``Api`` and lost the ``< Base``,
                # mis-flagging legit controllers and driving a STOP loop).
                known_bases = set(inheritance.get("known_bases") or ())
                known_bases.add(dominant_base)
                min_class_indent = None
                for m in re.finditer(
                    r"^([ \t]*)class\s+([\w:]+)(?:\s*<\s*([\w:]+))?",
                    scan_content,
                    re.MULTILINE,
                ):
                    indent = len(m.group(1))
                    class_name = m.group(2)
                    superclass = m.group(3)
                    # Skip a class nested deeper than the outermost class: an inner
                    # class (e.g. `class Result` inside a controller) is not a
                    # top-level declaration of this archetype, so the inheritance
                    # convention does not apply. Same-indent siblings and
                    # module-nested top-level classes are still checked.
                    if min_class_indent is not None and indent > min_class_indent:
                        continue
                    min_class_indent = (
                        indent if min_class_indent is None else min(min_class_indent, indent)
                    )
                    if superclass is None or superclass not in known_bases:
                        violations.append(
                            Violation(
                                rule="inheritance-convention-violation",
                                expected=dominant_base,
                                actual=superclass or "none",
                                severity="warning",
                                message=f"INHERITANCE: class {class_name} should inherit {dominant_base} ({inheritance['frequency']:.0%} convention)",
                            )
                        )

    return violations
