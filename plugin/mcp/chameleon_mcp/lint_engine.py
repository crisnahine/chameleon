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
from pathlib import Path
from typing import Literal

from chameleon_mcp.conventions import (
    _classify_casing,
    _split_compound_suffix,
    _strip_type_params,
)
from chameleon_mcp.kind_labels import humanize_kind
from chameleon_mcp.signatures import bucket_named_export_count, content_signal_match_for

Severity = Literal["info", "warning", "error"]


def _singular(label: str) -> str:
    """Render a plural humanized kind label as "a/an <singular>" for prose.

    ``humanize_kind`` yields plurals ("classes", "modules", "imports"); a
    single-construct sentence reads as "a class", "a module". Irregulars are
    spelled out; the default strips a trailing "s" and picks the article by the
    leading vowel sound.
    """
    irregular = {
        "classes": "class",
        "type aliases": "type alias",
    }
    word = irregular.get(label) or (label[:-1] if label.endswith("s") else label)
    article = "an" if word[:1].lower() in "aeiou" else "a"
    return f"{article} {word}"


_TS_EXTENSIONS: frozenset[str] = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
_RUBY_EXTENSIONS: frozenset[str] = frozenset({".rb"})
_PY_EXTENSIONS: frozenset[str] = frozenset({".py", ".pyi"})


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
    for ext in _PY_EXTENSIONS:
        if lower.endswith(ext):
            return "python"
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
# One alternation so comments and strings consume each other's openers in
# source order. Sequential passes mis-tokenized `//` INSIDE a string (a URL
# literal) as a comment opener, which unbalanced the quote pairing across the
# newline and blanked real code below — blinding the import rules.
_TS_STRING_OR_COMMENT = re.compile(
    r"""/\*.*?\*/
        | //[^\n]*
        | (?<!\\)(?:
            "(?:\\.|[^"\\])*" |
            '(?:\\.|[^'\\])*' |
            `(?:\\.|[^`\\])*`
          )""",
    re.VERBOSE | re.DOTALL,
)


def _blank_match_to_spaces(m: re.Match) -> str:
    """Replace a regex match with a same-length run of spaces, keeping newlines.

    Shared by the string/comment strippers so blanking a multiline match (block
    comment, heredoc, template literal) leaves downstream line numbers truthful.
    """
    return re.sub(r"[^\n]", " ", m.group(0))


def _strip_ts_strings_and_comments(content: str) -> str:
    """Best-effort strip of strings/comments to reduce JSX false positives.

    We replace each match with a same-length run of spaces so positions
    elsewhere remain meaningful (regex flag offsets / future line numbering).
    Newlines inside a multiline match (block comment, template literal) are
    preserved so line numbers downstream stay truthful.
    """
    return _TS_STRING_OR_COMMENT.sub(_blank_match_to_spaces, content)


def extract_comment_spans(content: str, *, language: str) -> list[str]:
    """Return candidate commented-out-code spans with comment markers removed.

    The strings/comment strippers blank comments to spaces; this captures the
    comment text instead so the real parser can re-check it. Consecutive
    single-line comments are stitched into one span (a multi-line block of
    commented-out code is one candidate, not N one-liners). Block comments are
    returned as their own span. The leading ``//`` / ``#`` and the ``/* */``
    fences are stripped so the residue is bare source the parser can try to
    parse. Returns ``[]`` for an unsupported language.

    Intended for bootstrap / pr-review only — the parse round-trip the caller
    runs on each span is far too slow for the per-edit hot path.
    """
    if language == "typescript":
        block_re, prefix = _TS_BLOCK_COMMENT, "//"
    elif language == "ruby":
        block_re, prefix = _RUBY_BLOCK_COMMENT, "#"
    elif language == "python":
        # Python has only `#` line comments (a triple-quoted string is a string,
        # not a comment); consecutive `#` lines stitch into one commented-code
        # span the caller re-parses as Python.
        block_re, prefix = None, "#"
    else:
        return []

    spans: list[str] = []
    if block_re is not None:
        for m in block_re.finditer(content):
            inner = m.group(0)
            # Strip the fence. TS uses /* */; Ruby's block is =begin/=end lines.
            if inner.startswith("/*"):
                inner = inner[2:]
                if inner.endswith("*/"):
                    inner = inner[:-2]
                # Drop leading-star decoration common in JSDoc-style blocks.
                inner = "\n".join(re.sub(r"^\s*\*\s?", "", ln) for ln in inner.splitlines())
            else:
                # Ruby =begin/=end: drop the marker lines, keep the body.
                inner = "\n".join(
                    ln for ln in inner.splitlines() if not ln.strip().startswith(("=begin", "=end"))
                )
            spans.append(inner)

    # Stitch consecutive single-line comments. We walk the raw lines so adjacency
    # is on-disk adjacency, not regex-match order.
    run: list[str] = []
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith(prefix):
            run.append(stripped[len(prefix) :])
        elif run:
            spans.append("\n".join(run))
            run = []
    if run:
        spans.append("\n".join(run))

    # Drop spans with no word characters (rulers, dividers, empty markers): they
    # cannot parse as code and only cost a parser round-trip.
    spans = [s for s in spans if re.search(r"\w", s)]
    if language == "python":
        # Python is indentation-sensitive: stripping the `# ` marker leaves a
        # uniform leading offset (`# def f():` -> ` def f():`) that would fail to
        # parse. Remove the common indent so the span parses as the code it was.
        import textwrap

        spans = [textwrap.dedent(s) for s in spans]
    return spans


_TS_DEFAULT_FUNCTION = re.compile(
    r"^[ \t]*export\s+default\s+(?:async\s+)?function\b", re.MULTILINE
)
_TS_DEFAULT_CLASS = re.compile(r"^[ \t]*export\s+default\s+class\b", re.MULTILINE)
_TS_DEFAULT_ARROW = re.compile(
    r"^[ \t]*export\s+default\s*\(?\s*(?:async\s*)?\(.*?\)\s*=>", re.MULTILINE | re.DOTALL
)
_TS_DEFAULT_OBJECT = re.compile(r"^[ \t]*export\s+default\s*\{", re.MULTILINE)
_TS_DEFAULT_ARRAY = re.compile(r"^[ \t]*export\s+default\s*\[", re.MULTILINE)
_TS_DEFAULT_IDENT = re.compile(r"^[ \t]*export\s+default\s+\w", re.MULTILINE)

_TS_NAMED_EXPORTS = [
    re.compile(r"^[ \t]*export\s+(?:const|let|var)\s+(\w+)\s*[=:]", re.MULTILINE),
    re.compile(r"^[ \t]*export\s+(?:async\s+)?function\s+(\w+)", re.MULTILINE),
    re.compile(r"^[ \t]*export\s+class\s+(\w+)", re.MULTILINE),
    re.compile(r"^[ \t]*export\s+interface\s+(\w+)", re.MULTILINE),
    re.compile(r"^[ \t]*export\s+type\s+(\w+)", re.MULTILINE),
    re.compile(r"^[ \t]*export\s+enum\s+(\w+)", re.MULTILINE),
]
_TS_EXPORT_LIST = re.compile(r"^[ \t]*export\s*\{\s*([^}]*)\s*\}\s*;?\s*$", re.MULTILINE)

_TS_TOP_LEVEL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^[ \t]*import\s", re.MULTILINE), "ImportDeclaration"),
    (_TS_DEFAULT_FUNCTION, "FunctionDeclaration"),
    (_TS_DEFAULT_CLASS, "ClassDeclaration"),
    (re.compile(r"^[ \t]*export\s+default\s", re.MULTILINE), "ExportAssignment"),
    (
        re.compile(
            r"^[ \t]*export\s*\{[^}]*\}\s*(?:from\s+[\"'][^\"']*[\"'])?\s*;?\s*$", re.MULTILINE
        ),
        "ExportDeclaration",
    ),
    (re.compile(r"^[ \t]*export\s+(?:async\s+)?function\s", re.MULTILINE), "FunctionDeclaration"),
    (re.compile(r"^[ \t]*export\s+class\s", re.MULTILINE), "ClassDeclaration"),
    (re.compile(r"^[ \t]*export\s+interface\s", re.MULTILINE), "InterfaceDeclaration"),
    (re.compile(r"^[ \t]*export\s+type\s", re.MULTILINE), "TypeAliasDeclaration"),
    (re.compile(r"^[ \t]*export\s+enum\s", re.MULTILINE), "EnumDeclaration"),
    (re.compile(r"^[ \t]*export\s+(?:const|let|var)\s", re.MULTILINE), "FirstStatement"),
    (re.compile(r"^[ \t]*(?:async\s+)?function\s+\w", re.MULTILINE), "FunctionDeclaration"),
    (re.compile(r"^[ \t]*class\s+\w", re.MULTILINE), "ClassDeclaration"),
    (re.compile(r"^[ \t]*interface\s+\w", re.MULTILINE), "InterfaceDeclaration"),
    (re.compile(r"^[ \t]*type\s+\w+\s*=", re.MULTILINE), "TypeAliasDeclaration"),
    (re.compile(r"^[ \t]*enum\s+\w", re.MULTILINE), "EnumDeclaration"),
    (re.compile(r"^[ \t]*(?:const|let|var)\s+\w", re.MULTILINE), "FirstStatement"),
)

_JSX_CLOSING = re.compile(r"</[A-Za-z][\w.-]*\s*>")
_JSX_SELF_CLOSING = re.compile(r"(?<![A-Za-z0-9_])<[A-Za-z][\w.]*(?:\s[^<>]*?)?/>", re.DOTALL)
_JSX_FRAGMENT = re.compile(r"<>|</>")


def _extract_typescript(content: str, file_path: str | None = None) -> DimensionSnapshot:
    """Pull DimensionSnapshot out of TS-family content via regex heuristics.

    Order of operations matters: we strip strings/comments BEFORE the JSX
    scan so a `"</div>"` literal doesn't fire jsx_present. We do NOT strip
    them before the export scan because most exports are on lines that are
    unlikely to begin inside a string literal.
    """
    # A UTF-8 BOM on line 1 prefixes the first token with U+FEFF, so the
    # ``^``-anchored declaration regexes miss the first-line export/declaration
    # and skew the snapshot. The dumper bootstrap path strips it (the TS
    # compiler does); this regex runtime path did not. Drop one leading BOM so
    # line 1 parses like every other line.
    content = content[1:] if content.startswith("﻿") else content
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
    # A `.ts` / `.mts` / `.cts` file cannot legally contain JSX -- only `.tsx` / `.jsx`
    # can. So any angle brackets in a `.ts` file are generics, comparisons, or a
    # template-string SVG (`\`<svg>...</svg>\``), never JSX. The regex scan can
    # misread such a template literal as JSX and fire the block-eligible
    # jsx-presence-mismatch ERROR on conforming code (a real false positive observed
    # in real usage). Force it False for these extensions so the structural check
    # never misclassifies them; zero signal loss (JSX is a .tsx/.jsx concept).
    if file_path:
        _ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
        if _ext in ("ts", "mts", "cts"):
            jsx_present = False

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


# Python string/comment stripper. A single alternation so the leftmost token
# wins positionally -- a `#` inside a string is consumed by the string alt, a
# quote inside a comment by the comment alt. Triple-quoted forms come first so
# `"""` is not mis-read as an empty `""` plus a stray quote. The optional
# string-prefix run covers f/r/b/u (and combinations like rb, f). Blanked to
# spaces (length-preserving) so line numbers stay truthful.
_PY_STRING_OR_COMMENT = re.compile(
    r"""
      [rRbBfFuU]{0,3}\"\"\"[\s\S]*?\"\"\"     # triple double-quoted
    | [rRbBfFuU]{0,3}'''[\s\S]*?'''           # triple single-quoted
    | \#[^\n]*                                  # line comment
    | [rRbBfFuU]{0,3}"(?:\\.|[^"\\\n])*"      # double-quoted
    | [rRbBfFuU]{0,3}'(?:\\.|[^'\\\n])*'      # single-quoted
    """,
    re.VERBOSE,
)
# Known limitation: a PEP 701 (3.12+) f-string that reuses the outer quote inside
# a replacement field -- f"{eval("x")}" -- is only partially matched here, so an
# eval/exec sink in that field is under-detected. Detecting it correctly needs
# brace-aware field-preserving blanking, and getting that wrong would mis-strip
# every Python lint rule that shares this helper; the niche false negative is the
# safer trade than that blast radius.


def _strip_python_strings_and_comments(content: str) -> str:
    """Blank Python strings + comments to spaces (length-preserving).

    Used by the sink/style/convention scans so an ``eval(`` mentioned in a
    docstring or comment never fires. Newlines inside a triple-quoted string are
    preserved so downstream line numbers stay truthful.
    """
    return _PY_STRING_OR_COMMENT.sub(_blank_match_to_spaces, content)


def _blank_python_strings(content: str) -> str:
    """Blank Python string-literal bodies to spaces, leaving comments intact.

    The string-embedded-import guard and the inline-ignore directive scan both
    need comments PRESERVED (a real ``# chameleon-ignore`` and a real ``from``
    must still be found) while text inside a string constant is neutralized: a
    docstring containing ``from .ghost import x`` is not an import, and a
    ``# chameleon-ignore`` inside a help string is not author intent. Comments
    are still consumed by the scan so a ``#``-led line carrying a quote cannot
    open a phantom string across the directive below it. Length-preserving.
    """

    def repl(m: re.Match) -> str:
        s = m.group(0)
        # The comment alternative is matched (so its inner quotes can't open a
        # string) but returned verbatim; only real string literals are blanked.
        return s if s.startswith("#") else _blank_match_to_spaces(m)

    return _PY_STRING_OR_COMMENT.sub(repl, content)


# Python's exec() is the sibling of eval(): both execute an arbitrary string as
# code. Same member-call guard as eval so `obj.exec(...)` (a method) is exempt.
_PY_EXEC_CALL_RE = re.compile(r"(?<![.\w])exec\s*\(")

_RUBY_LINE_COMMENT = re.compile(r"#[^\n]*")
_RUBY_BLOCK_COMMENT = re.compile(r"^=begin\b.*?^=end\b", re.DOTALL | re.MULTILINE)
_RUBY_STRING_DQ = re.compile(r'"(?:\\.|[^"\\])*"', re.DOTALL)
_RUBY_STRING_SQ = re.compile(r"'(?:\\.|[^'\\])*'", re.DOTALL)


# A heredoc OPENER token. Delimiter must be ALL-CAPS (the overwhelming
# convention) and follow `<<` with no space, so `arr << FOO` and
# `class << self` never match; the lookbehind rejects a `<<` that shifts a
# value (`arr<<FOO`, `(x)<<FOO`, `"s"<<FOO`) — a real heredoc opener is always
# preceded by whitespace, `(`, `,`, `=`, or line start.
_RUBY_HEREDOC_OPENER = re.compile(r"(?<![\w)\]\"'])<<([~-]?)(['\"]?)([A-Z][A-Z0-9_]*)\2")


def _blank_ruby_heredocs(content: str) -> str:
    """Blank heredoc bodies in a single forward pass — length-preserving, O(n).

    A heredoc body is string content: `def fakeMethod` inside `<<~TEXT ... TEXT`
    must not feed the naming/inheritance/import scans. The first implementation
    was a lazy cross-line regex; on a file with many unterminated openers every
    match attempt rescanned to end-of-file — quadratic, multiple SECONDS at the
    100KB lint cap, on the hook hot path, over attacker-controllable content.

    One pass over lines instead: an opener queues its delimiter (FIFO — stacked
    heredocs close in order), every line inside a body is blanked until the
    front delimiter's terminator line, the terminator line itself is blanked
    (it is heredoc syntax, not code). Text after the first opener on the opener
    line is blanked too (`<<~SQL.strip` method chains are heredoc plumbing).
    An unterminated heredoc blanks to end-of-file: its body is string content
    either way, and the file is a syntax error Ruby itself would reject.
    """
    if "<<" not in content:
        return content
    lines = content.split("\n")
    pending: list[str] = []
    out: list[str] = []
    for line in lines:
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            out.append(" " * len(line))
            continue
        m = _RUBY_HEREDOC_OPENER.search(line)
        if m is None:
            out.append(line)
            continue
        for om in _RUBY_HEREDOC_OPENER.finditer(line):
            pending.append(om.group(3))
        out.append(line[: m.start()] + " " * (len(line) - m.start()))
    return "\n".join(out)


# Ruby percent-literals: %q{}, %Q[], %w(), %i<>, %r||, and the bare %(...) /
# %{...} string forms. The text inside is string/array/regex content, not code,
# so `%q{eval(}` is an inert literal and a `# chameleon-ignore` inside one is
# content, not author intent. Blank them like the quote forms so the dangerous-
# sink scan does not false-positive on the embedded text and an embedded
# directive cannot suppress a real violation. The typed forms (q/Q/w/W/i/I/r/s/x)
# accept any delimiter; the bare form is restricted to bracket pairs so a modulo
# expression (`a % b`, `a%[0]`) is not mistaken for a literal. The bracket-pair
# arms accept one level of balanced nesting (`%(eval(x))` is the string
# "eval(x)", not a literal that ends at the inner `)`); a deeper nest is rare and
# fails closed (the violation still fires, the directive deactivates), never
# open. The three inner alternatives are first-char disjoint (`\` for `\\.`, the
# open delimiter for the nested pair, everything else for the class), so the
# match is linear with no catastrophic backtracking.
_RUBY_PCT_DELIMS_ALL = (
    r"\{(?:\\.|[^\\{}]|\{[^{}]*\})*\}"
    r"|\[(?:\\.|[^\\\[\]]|\[[^\[\]]*\])*\]"
    r"|\((?:\\.|[^\\()]|\([^()]*\))*\)"
    r"|<(?:\\.|[^\\<>]|<[^<>]*>)*>"
    r"|\|(?:\\.|[^\\|])*\|"
    r"|!(?:\\.|[^\\!])*!"
    r"|/(?:\\.|[^\\/])*/"
)
_RUBY_PCT_DELIMS_BRACKET = (
    r"\{(?:\\.|[^\\{}]|\{[^{}]*\})*\}"
    r"|\[(?:\\.|[^\\\[\]]|\[[^\[\]]*\])*\]"
    r"|\((?:\\.|[^\\()]|\([^()]*\))*\)"
    r"|<(?:\\.|[^\\<>]|<[^<>]*>)*>"
)
_RUBY_PERCENT_LITERAL = re.compile(
    rf"%(?:[qQwWiIrsx](?:{_RUBY_PCT_DELIMS_ALL})|(?:{_RUBY_PCT_DELIMS_BRACKET}))",
    re.DOTALL,
)


def _blank_ruby_percent_literals(content: str) -> str:
    return _RUBY_PERCENT_LITERAL.sub(_blank_match_to_spaces, content)


# The `(?<!<<)(?<!<<~)(?<!<<-)` guard before each quote makes the string alts skip
# a QUOTED HEREDOC DELIMITER (`<<~"EOF"` / `<<-'EOF'` / `<<"EOF"`): that quote opens
# a heredoc, not a string, and must be left for the heredoc pass that runs after.
# Without it, this pass (which precedes the heredoc pass so a `<<~` inside a comment
# is blanked as a comment, not honored as a heredoc) would eat the delimiter and the
# heredoc body below it would be scanned as code.
_RUBY_STR_OR_LINE_COMMENT = re.compile(
    r'(?<!<<)(?<!<<~)(?<!<<-)"(?:\\.|[^"\\])*"'  # double-quoted string (not a heredoc delim)
    r"|(?<!<<)(?<!<<~)(?<!<<-)'(?:\\.|[^'\\])*'"  # single-quoted string (not a heredoc delim)
    r"|#[^\n]*",  # line comment
    re.DOTALL,
)


def _strip_ruby_strings_and_comments(content: str) -> str:
    # =begin/=end block comments first: unambiguous, line-anchored multi-line spans.
    out = _RUBY_BLOCK_COMMENT.sub(_blank_match_to_spaces, content)
    # Strings and line comments are resolved in ONE alternation pass so that
    # position order decides which claims a shared character: a `#` inside a
    # "..."/'...' string is consumed by the string (not read as a comment opener,
    # which would leave the quote dangling and pair it forward to a later line),
    # and a `"` (or a `<<~HEREDOC` token) inside a `# ...` comment is consumed by
    # the comment. This pass MUST precede the heredoc pass: a `<<~TOKEN` written
    # inside a comment is not a real heredoc opener, and blanking the comment here
    # first stops _blank_ruby_heredocs from honoring it and swallowing the code
    # (incl. eval/exec sinks) below it to end-of-file.
    out = _RUBY_STR_OR_LINE_COMMENT.sub(_blank_match_to_spaces, out)
    # Heredocs after strings/comments: any `<<~TOKEN` that survives is in real
    # value position. A heredoc body's own `#`/`"`/`<<` were already blanked above
    # but the body is blanked wholesale here regardless, so that is harmless.
    out = _blank_ruby_heredocs(out)
    # Percent-literals last: a `%` inside a now-blanked string can't start one.
    return _blank_ruby_percent_literals(out)


_RUBY_TOP_LEVEL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^class\s+\w", re.MULTILINE), "ClassNode"),
    (re.compile(r"^module\s+\w", re.MULTILINE), "ModuleNode"),
    (re.compile(r"^def\s+\w", re.MULTILINE), "DefNode"),
    (re.compile(r"^require\b", re.MULTILINE), "CallNode"),
    (re.compile(r"^require_relative\b", re.MULTILINE), "CallNode"),
    # A receiver call opening a block at column 0 -- `RSpec.describe … do`,
    # `Rails.application.routes.draw do`. This is the ONLY top-level node in an
    # idiomatic RSpec spec, because `rspec --init` writes `.rspec` with
    # `--require spec_helper` and the file then carries no top-level require.
    # Reporting no top-level node made canonical selection score every such spec
    # `trivial`, so the test archetype shipped witnessless and per-edit guidance
    # lost its exemplar -- measured on a paired A/B whose only difference was one
    # `require` line. Requires the block opener so an ordinary top-level
    # statement (`puts x`, `Foo.bar`) does not read as a declaration.
    (re.compile(r"^\w[\w:]*\.\w+.*\bdo\b|^\w[\w:]*\.\w+\s*\{", re.MULTILINE), "CallNode"),
)

_RUBY_SUPERCLASS_RE = re.compile(r"^class\s+\w[\w:]*\s*<\s*([\w:]+)", re.MULTILINE)
_RUBY_INCLUDE_RE = re.compile(r"^[ \t]+include\s+([\w:]+)", re.MULTILINE)
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
_RUBY_DSL_RE = re.compile(r"^[ \t]+(" + "|".join(_RUBY_DSL_CALLS) + r")\b", re.MULTILINE)

# Capture the guard method symbol of a before_action callback and any trailing
# scoping options, so the required-guard advisory can tell a blanket authz call
# from a scoped one and from a skip_before_action removal. Mirrors the capture
# the convention builder uses so what is derived is what is checked.
_RUBY_BEFORE_ACTION_LINT_RE = re.compile(
    r"^[ \t]+(skip_before_action|before_action)\s+:([A-Za-z_]\w*[!?]?)(.*)$",
    re.MULTILINE,
)
_RUBY_GUARD_SCOPE_LINT_RE = re.compile(r"\b(only|except|if|unless)\s*:")


def _extract_ruby(content: str) -> DimensionSnapshot:
    """Best-effort Ruby dimension extraction.

    Detects top-level class/module/def at column 0, plus enriched
    dimensions for archetype differentiation:
    - Superclass (ClassNode:ApplicationRecord vs ClassNode:ApplicationController)
    - Include calls (IncludeCall:Sidekiq::Job)
    - DSL calls (DslCall:validates, DslCall:belongs_to)
    """
    # Drop a single leading UTF-8 BOM so a class/module/def on line 1 is not
    # hidden behind U+FEFF from the column-0 match (Prism strips it for the
    # bootstrap path; this regex runtime path did not).
    content = content[1:] if content.startswith("﻿") else content
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


# stdlib-ast node names that differ from the libcst dump's vocabulary. The
# stored cluster signature is produced by libcst (scripts/libcst_dump.py), which
# folds async forms into their sync kind and uses "Del" / "Try" for the star
# variants; the hot-path ast extractor must agree or every async file would
# false-flag a top-level-node-kinds mismatch.
_PY_KIND_NORMALIZE = {
    "AsyncFunctionDef": "FunctionDef",
    "AsyncFor": "For",
    "AsyncWith": "With",
    "Delete": "Del",
    "TryStar": "Try",
}


def _extract_python(content: str) -> DimensionSnapshot:
    """Best-effort Python dimension extraction via stdlib ``ast``.

    Parses in-process (the hot path cannot spawn the libcst subprocess) and
    builds the same normalized shape the libcst dump stores, so an edit-time
    snapshot is directly comparable to the bootstrap cluster signature. A file
    mid-edit that does not parse yields an empty snapshot (no observations, no
    violations) rather than a crash.
    """
    import ast

    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return DimensionSnapshot()

    top_level: list[str] = []
    class_count = 0
    func_count = 0
    for node in tree.body:
        name = type(node).__name__
        top_level.append(_PY_KIND_NORMALIZE.get(name, name))
        if isinstance(node, ast.ClassDef):
            class_count += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_count += 1

    if class_count == 1 and func_count == 0:
        default_export_kind = "ClassDef"
    elif func_count == 1 and class_count == 0:
        default_export_kind = "FunctionDef"
    else:
        default_export_kind = None

    head = content[:200]
    cs = content_signal_match_for(head)
    content_signal = cs if cs != "none" else None

    return DimensionSnapshot(
        top_level_node_kinds=top_level,
        default_export_kind=default_export_kind,
        named_export_count=class_count + func_count,
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
        return _extract_typescript(content, file_path=file_path)
    if language == "ruby":
        return _extract_ruby(content)
    if language == "python":
        return _extract_python(content)
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


# A trailing CamelCase word that names an ARCHETYPE ROLE (`Controller`, `Serializer`,
# `Service`) rather than a generic base marker. When the dominant base carries such a
# role, a class extending ANOTHER class of the same role (a `*Controller` extending a
# project `*Controller` intermediate) is intra-role reuse -- the intermediate itself
# roots at the archetype's base -- not a wrong-base deviation. The generic markers are
# excluded because half the classes in a repo end in `Base`/`Error`, which would over-
# exempt (an unrelated `SomeUnrelatedBase` must still flag).
_GENERIC_BASE_WORDS: frozenset[str] = frozenset(
    {
        "Base",
        "Error",
        "Class",
        "Module",
        "Object",
        "Impl",
        "Mixin",
        "Concern",
        "Struct",
        "Exception",
    }
)
_ROLE_SUFFIX_RE = re.compile(r"[A-Z][a-z]+$")


def _base_role_suffix(base: str) -> str | None:
    """The archetype-role word a base name ends in (`BaseController` -> `Controller`,
    `serializers.ModelSerializer` -> `Serializer`), or None when the trailing word is a
    generic base marker (`Base`, `Error`) or too short to be a role."""
    tail = base.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
    m = _ROLE_SUFFIX_RE.search(tail)
    if not m:
        return None
    word = m.group(0)
    if len(word) < 3 or word in _GENERIC_BASE_WORDS:
        return None
    return word


def _superclass_shares_base_role(superclass: str, dominant_base: str) -> bool:
    """True when ``superclass`` is another class of the SAME role as the dominant base
    (a `*Controller` when the base is a `*Controller`), i.e. intra-role reuse of a
    sibling/intermediate that itself roots at the archetype's base -- not a deviation.
    Returns False when the dominant base carries no role word, so a generic-base
    archetype grants no exemption and a genuinely wrong base still flags."""
    role = _base_role_suffix(dominant_base)
    if not role:
        return False
    sup_tail = superclass.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
    if sup_tail.endswith(role):
        return True
    # A camel-bounded INTERIOR occurrence is the same role in a longer
    # compound: a DRF ModelViewSet carries the View role of a CBV-dominant
    # view module (mixed CBV+DRF files fired "should inherit ListView" on
    # every viewset). The occurrence must start and end on camel boundaries
    # so a role embedded mid-word never matches.
    i = sup_tail.find(role)
    while i != -1:
        end = i + len(role)
        if end == len(sup_tail) or sup_tail[end].isupper():
            return True
        i = sup_tail.find(role, i + 1)
    return False


def _namespace_local_base(class_name: str, known_bases: set[str], dominant: str) -> str:
    """The known base whose namespace most deeply ENCLOSES ``class_name``.

    A base is eligible only when its entire module path is a prefix of the
    class's module path — a partially-shared prefix is a sibling namespace,
    not an enclosing one (`Api::V1::BaseController` must never be suggested
    for an `Api::V2::` controller just because both start with `Api`).
    Falls back to the repo-wide dominant base when no eligible base is
    deeper; dominant wins ties so the suggestion only diverges when an
    enclosing namespace match is strictly deeper.
    """
    cls_ns = class_name.split("::")[:-1]
    if not cls_ns:
        return dominant

    def enclosing_depth(base: str) -> int | None:
        base_ns = base.split("::")[:-1]
        if len(base_ns) > len(cls_ns):
            return None
        for a, b in zip(cls_ns, base_ns, strict=False):
            if a != b:
                return None
        return len(base_ns)

    best = dominant
    best_depth = enclosing_depth(dominant)
    if best_depth is None:
        best_depth = -1
    for base in known_bases:
        depth = enclosing_depth(base)
        if depth is not None and depth > best_depth:
            best, best_depth = base, depth
    return best


# Per-file cap on how many top-level node kinds are embedded in the
# 'top-level-node-kinds-mismatch' violation's `actual` field. Mirrors the
# secret-scan and style-rule caps in this module: a file with thousands of
# top-level statements would otherwise embed a multi-KB literal repr in a
# single violation.
_TOP_LEVEL_KINDS_REPR_CAP_NAME = "TOP_LEVEL_NODE_KINDS_REPR_CAP"


def _top_level_kinds_repr_cap() -> int:
    try:
        from chameleon_mcp._thresholds import threshold_int

        return threshold_int(_TOP_LEVEL_KINDS_REPR_CAP_NAME)
    except Exception:
        return 50


def _capped_top_level_kinds_repr(kinds: list[str]) -> str:
    """Bounded repr of a file's top-level node kinds for a violation's `actual`.

    Past the cap, shows the leading slice plus a '+N more (capped at ...)'
    summary instead of a literal repr of the full list.
    """
    kinds = list(kinds)
    cap = _top_level_kinds_repr_cap()
    if len(kinds) <= cap:
        return repr(kinds)
    remaining = len(kinds) - cap
    return f"{kinds[:cap]!r} +{remaining} more (capped at {cap})"


def lint(
    snapshot: DimensionSnapshot, ast_query: dict | None, *, language: str | None = None
) -> list[Violation]:
    """Compare a snapshot against the archetype's ast_query; return violations.

    `language` (when supplied) only shapes the user-facing message wording:
    Ruby and Python have no default export, so their messages describe the
    archetype's primary construct instead of borrowing the TS export framing.

    Encoding rule (from `derive_ast_query`):
    - A non-null ast_query field carries an expectation.
    - A null field means "no expectation set" — never flag. One exception:
      for TypeScript a null `default_export_kind` alongside a non-zero
      named-export bucket IS an expectation — the archetype exports named
      bindings only — so a file that INTRODUCES a default export gets the
      symmetric `default-export-kind-mismatch` advisory.
    - `content_signal == "none"` from the cluster signature is stored as None
      in ast_query, so a null content_signal here means "any directive (or no
      directive) is acceptable".

    Severity choices:
    - `default-export-kind-mismatch`: warning. Mixing function-export and
      class-export styles within an archetype is a real inconsistency but
      not always a bug (refactors happen).
    - `top-level-node-kinds-mismatch`: info. Missing a top-level kind the
      archetype expects is a fit heuristic that is never block-eligible and
      whose message concedes the match may be wrong, so it is surfaced as an
      advisory note, not with the "Fix these." imperative.
    - `named-export-count-bucket-mismatch`: info. The bucket boundaries are
      coarse; a bucket mismatch is a soft signal.
    - `jsx-presence-mismatch`: warning when the file has JSX and the
      archetype doesn't expect it, ERROR in the reverse direction. Rationale:
      adding JSX to a non-JSX archetype (e.g., a util file) is structurally
      wrong and worth a hard flag; missing JSX in a JSX archetype could be
      a stub.
    - `content-signal-mismatch`: warning. `'use client'` etc. are
      semantically significant in modern frameworks but not always required.
      Ruby's `# frozen_string_literal: true` magic comment rides the same
      check: advisory only, never block-eligible.
    """
    if not ast_query:
        return []

    violations: list[Violation] = []

    expected_default = ast_query.get("default_export_kind")
    actual_default = snapshot.default_export_kind
    # Python/Ruby have no default export, so `default_export_kind` is a single
    # "primary construct" pick that is unreliable on mixed-construct files (a
    # module holding both functions and classes). Suppress the mismatch when the
    # expected construct is actually PRESENT among the file's top-level kinds --
    # otherwise "this file does not define one" is simply false (the file defines
    # 20 functions; the picker just led with a class).
    _py_rb_construct_present = (
        language in ("ruby", "python")
        and expected_default is not None
        and _normalize_kind(expected_default)
        in {_normalize_kind(k) for k in snapshot.top_level_node_kinds}
    )
    if (
        expected_default is not None
        and expected_default != actual_default
        and not _py_rb_construct_present
    ):
        exp_label = humanize_kind(expected_default)
        if language in ("ruby", "python"):
            # Ruby and Python have no default export; describe the construct the
            # archetype leads with instead of borrowing the TS export framing.
            if actual_default is None:
                message = (
                    f"this archetype's primary construct is {_singular(exp_label)}; "
                    "this file does not define one"
                )
            else:
                message = (
                    f"this archetype's primary construct is {_singular(exp_label)}; "
                    f"this file defines {_singular(humanize_kind(actual_default))}"
                )
        else:
            act_label = humanize_kind(actual_default) if actual_default is not None else "none"
            message = f"archetype expects a default export of {exp_label}; file has {act_label}"
        violations.append(
            Violation(
                rule="default-export-kind-mismatch",
                expected=str(expected_default),
                actual=str(actual_default) if actual_default is not None else "none",
                severity="warning",
                message=message,
            )
        )
    elif (
        expected_default is None
        and actual_default is not None
        and language == "typescript"
        and ast_query.get("named_export_count_bucket") not in (None, "0")
    ):
        # The symmetric direction of the mismatch above: the archetype exports
        # named bindings only (no witness carries a default export, and the
        # named-export bucket shows the export surface is real, not a
        # side-effect script), and this file INTRODUCES a default export. Same
        # rule id both ways -- one export-style dimension, two deviations.
        # TypeScript/JS only: for Ruby/Python a None expectation means the
        # cluster has no single primary construct, not a named-only style.
        violations.append(
            Violation(
                rule="default-export-kind-mismatch",
                expected="none",
                actual=str(actual_default),
                severity="warning",
                message=(
                    "archetype exports named bindings only (no sibling has a "
                    "default export); file adds a default export of "
                    f"{humanize_kind(actual_default)} -- export it as a named "
                    "binding like its siblings"
                ),
            )
        )

    expected_kinds = ast_query.get("top_level_node_kinds")
    if expected_kinds:
        if not _top_level_kinds_match(snapshot.top_level_node_kinds, list(expected_kinds)):
            from collections import Counter

            missing = Counter(_normalize_kind(k) for k in expected_kinds) - Counter(
                _normalize_kind(k) for k in snapshot.top_level_node_kinds
            )
            missing_labels = sorted({humanize_kind(k) for k in missing})
            missing_desc = ", ".join(missing_labels) or "(structural shape)"
            violations.append(
                Violation(
                    rule="top-level-node-kinds-mismatch",
                    expected=repr(list(expected_kinds)),
                    actual=_capped_top_level_kinds_repr(snapshot.top_level_node_kinds),
                    # Info, not warning: this is an archetype-FIT heuristic (never
                    # block-eligible) whose own message concedes the match may be
                    # wrong and tells the reader not to restructure. Surfacing it
                    # with the "Fix these." imperative and the escalation tone
                    # contradicts that hedge, so it renders as an advisory note.
                    severity="info",
                    message=(
                        f"file is missing top-level constructs the archetype "
                        f"expects: {missing_desc} (extras are ok, missing kinds "
                        "are flagged). If this file genuinely has a different "
                        "shape, the archetype match may be wrong — do not "
                        "restructure working code just to satisfy this."
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
                        f"archetype's files typically export a '{expected_bucket}' "
                        f"count of names; this file has '{actual_bucket}'. This is a "
                        "rough size signal, not a defect — a small, focused new file "
                        "(one route, one form) legitimately exports fewer. Do not add "
                        "exports just to satisfy this."
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
            if expected_signal == "frozen_string_literal":
                # Name the exact line to add: "a 'frozen_string_literal'
                # directive" alone does not tell the model the magic-comment
                # syntax the archetype's files actually open with.
                message = (
                    "this archetype's files start with `# frozen_string_literal: "
                    f"true`; got '{actual_signal or 'none'}'. Add the magic "
                    "comment as the file's first line (below the shebang, if any)."
                )
            else:
                message = (
                    f"archetype expects a '{expected_signal}' directive at the "
                    f"top of the file; got '{actual_signal or 'none'}'"
                )
            violations.append(
                Violation(
                    rule="content-signal-mismatch",
                    expected=str(expected_signal),
                    actual=str(actual_signal) if actual_signal else "none",
                    severity="warning",
                    message=message,
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

# A single "string atom": an optional language prefix (f, r, b, u, rb, fr, ...)
# followed by one string literal in ANY style — triple-double, triple-single,
# double, single, or backtick. One atom recognizer feeds every fold shape below,
# so a token split across quote STYLES, string PREFIXES, or triple/single mixes
# folds uniformly instead of needing a bespoke regex per combination (the
# whack-a-mole that let template-`${}`, f-string, adjacency, and Python-order
# `.join` splits slip through). The body is captured non-greedily.
_STR_ATOM = (
    # Optional Python string prefix (r/b/f/u and 2-char combos), guarded so it
    # cannot start mid-identifier OR right after a quote -- otherwise the prefix
    # would eat a STRING'S OWN content letters (the `b` in `'a', 'b'`) as if they
    # were a prefix and corrupt the match. A BARE quote (no prefix) has no such
    # guard, so a quote directly after another quote still matches -- zero-space
    # adjacency (`"AKIA""REST"`) folds.
    r"(?:(?<![A-Za-z0-9_'\"`])[rRbBfFuU]{1,2})?"
    # ATOMIC group (?>...): once one quote alternative matches at a position, the
    # engine never backtracks INTO it. Without this, pairing two atoms around a
    # `+` (`_UNIFIED_CONCAT`) backtracked catastrophically on a file with many
    # string literals (multi-second per pass). Atomic groups are Python 3.11+,
    # which the plugin requires.
    # Every body excludes a newline: a hardcoded secret is a single-line token
    # (`AKIA[0-9A-Z]{16}` etc. never spans a line), so a string atom never needs
    # to cross one. Without this, `[^"\\]` swallowed whole multi-line docstrings
    # from one quote to the next far away -- 100+ giant false "adjacencies" per
    # file and a multi-second fold. Newline-bounded atoms match only real
    # single-line literals, which is all secret reconstruction requires.
    r"(?>"
    r'"""(?:\\.|(?!""")[^\n])*+"""'  # triple double
    r"|'''(?:\\.|(?!''')[^\n])*+'''"  # triple single
    r'|"(?:\\.|[^"\\\n])*+"'  # double
    r"|'(?:\\.|[^'\\\n])*+'"  # single
    r"|`(?:\\.|[^`\\\n])*+`"  # backtick
    r")"
)
# Two atoms joined by `+` (cross-style, prefixed, triple/single mixed all covered
# by the atom). Also plain ADJACENCY (`"a" "b"` with only inline whitespace, no
# operator) — valid concatenation in Python and Ruby, a common no-operator split.
_UNIFIED_CONCAT = re.compile(rf"({_STR_ATOM})\s*\+\s*({_STR_ATOM})", re.DOTALL)
_ADJACENT_CONCAT = re.compile(rf"({_STR_ATOM})[ \t]*({_STR_ATOM})")
# `[...].join('')` (JS order) AND `''.join([...])` (Python order). Empty separator
# only — a non-empty one would not reconstruct a contiguous token. Elements are
# string atoms; a non-literal element leaves the array unfolded.
_ARRAY_ELEMENTS = rf"(?:{_STR_ATOM}\s*,\s*)*{_STR_ATOM}"
_ARRAY_JOIN = re.compile(
    rf"\[\s*({_ARRAY_ELEMENTS})\s*,?\s*\]\s*\.\s*join\(\s*(?:''|\"\"|``|)\s*\)",
    re.DOTALL,
)
_PY_JOIN = re.compile(
    rf"(?:''|\"\"|``)\s*\.\s*join\(\s*\[\s*({_ARRAY_ELEMENTS})\s*,?\s*\]\s*\)",
    re.DOTALL,
)
# Ruby word array joined: `%w[AKIA REST].join` / `%w(...).join` / `%w{...}` / `%w<...>`.
# Barewords (whitespace-separated, no quotes) joined with an empty separator.
_RUBY_WORDARRAY_JOIN = re.compile(
    r"%[wWiI]([\[\(\{<])([^\]\)\}>]*)[\]\)\}>]\s*\.\s*join\b(?:\(\s*(?:''|\"\")?\s*\))?",
    re.DOTALL,
)
_ATOM_RE = re.compile(_STR_ATOM, re.DOTALL)
# Empty template interpolation `${''}` / `${""}` / `${}` used purely to split a
# token inside a backtick literal (`` `AKIA${''}REST` ``); stripping it makes the
# token contiguous in the RAW content the secret scan reads. Only EMPTY / empty-
# string-literal interpolations are removed, so a real `${value}` is untouched.
_EMPTY_INTERP = re.compile(r"\$\{\s*(?:''|\"\"|``)?\s*\}")


_ESCAPE_RE = re.compile(
    r"\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|u\{[0-9A-Fa-f]{1,6}\}|U[0-9A-Fa-f]{8}|.)",
    re.DOTALL,
)


def _decode_escape(m: re.Match) -> str:
    """Resolve one backslash escape to the character it denotes.

    Numeric escapes (`\\x41`, `\\u0041`, `\\u{41}`, `\\U00000041`) DECODE to the
    real character so a secret hidden as hex/unicode escapes (`"\\x41\\x4b..."`)
    reconstructs to `AKIA...` for the scanner. Any other `\\<char>` collapses to
    that char (the pre-existing behavior). Never raises."""
    e = m.group(1)
    try:
        if e[0] == "x":
            return chr(int(e[1:], 16))
        if e[0] == "u":
            h = e[2:-1] if e[1] == "{" else e[1:]
            return chr(int(h, 16))
        if e[0] == "U":
            return chr(int(e[1:], 16))
    except (ValueError, OverflowError):
        pass
    return e


def _strip_string_escapes(body: str) -> str:
    """Resolve backslash escapes so a re-emitted literal body stays inert.

    Folding joins the raw inner text of two literals; an escape valid in its
    original quote style becomes meaningless once the body is re-wrapped. A
    numeric escape is DECODED to its character (so a hex/unicode-encoded secret
    reconstructs); any other `\\<char>` collapses to that char, and a bare
    wrapping quote is re-escaped by the caller.
    """
    return _ESCAPE_RE.sub(_decode_escape, body)


# Numeric escapes ONLY (`\x41`, `A`, `\u{41}`, `\U00000041`). Unlike the
# full _ESCAPE_RE, this never touches `\n`/`\t`/`\\` or a bare `\<char>`, so a
# global decode of the raw content leaves line breaks intact and cannot shift a
# hit's reported line number. Used to reconstruct a secret hidden as hex/unicode
# escapes in a SINGLE literal, which the concat fold never reaches.
_NUMERIC_ESCAPE_RE = re.compile(
    r"\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|u\{[0-9A-Fa-f]{1,6}\}|U[0-9A-Fa-f]{8})"
)


def _decode_numeric_escape(m: re.Match) -> str:
    """Resolve one numeric backslash escape to its character; else leave as-is."""
    e = m.group(1)
    try:
        if e[0] == "x":
            return chr(int(e[1:], 16))
        if e[0] == "u":
            h = e[2:-1] if e[1] == "{" else e[1:]
            return chr(int(h, 16))
        if e[0] == "U":
            return chr(int(e[1:], 16))
    except (ValueError, OverflowError):
        pass
    return m.group(0)


def _atom_body(atom: str) -> str:
    """Inner text of one `_STR_ATOM` match (prefix + quotes stripped, unescaped)."""
    i = 0
    while i < len(atom) and atom[i].isalpha():
        i += 1
    body = atom[i:]
    for q in ('"""', "'''"):
        if len(body) >= 6 and body.startswith(q) and body.endswith(q):
            return _strip_string_escapes(body[3:-3])
    if len(body) >= 2 and body[0] in "\"'`" and body[-1] == body[0]:
        return _strip_string_escapes(body[1:-1])
    return _strip_string_escapes(body)


def _fold_string_concat(content: str, *, max_folds: int = _MAX_CONCAT_FOLDS_PER_FILE) -> str:
    """Iteratively collapse split string literals into single literals.

    A hardcoded credential is commonly split so the raw bytes never contain the
    literal token; folding reconstructs it before the scanners see it. Covered
    shapes (any quote style, string prefix, or triple/single mix, via one atom):
    concat with `+` (cross-quote, backtick, f/r/b-prefixed, and triple-vs-single
    mixes), plain ADJACENCY (two adjacent literals with no operator -- valid
    concatenation in Python and Ruby), `[...].join('')` (JS order) and
    `''.join([...])` (Python order), Ruby `%w[...].join`, and an empty template
    interpolation used to split inside a backtick literal.

    Runs multiple passes because folding creates new opportunities
    (`"a" + "b" + "c"` → `"ab" + "c"` → `"abc"`). Total substitutions across
    passes are bounded at `max_folds` so a pathological chain can't dominate
    lint_file latency; the 100KB content cap upstream is a secondary defense.

    Pure function, no I/O. Every fold re-emits a well-formed double-quoted
    literal (escapes stripped, bare `"` re-escaped) that cannot swallow
    following text on the next pass.
    """
    # Skip only when nothing foldable is present: no concat operator/join, no
    # string quote of ANY style (backtick included -- a pure-backtick template
    # split has no "/'), and no empty `${}` interpolation to strip.
    if not any(tok in content for tok in ("+", ".join", '"', "'", "`", "${")):
        return content

    def _emit_dq(body: str) -> str:
        return '"' + _strip_string_escapes(body).replace('"', '\\"') + '"'

    def _fold_atoms(text: str) -> str:
        return "".join(_atom_body(a) for a in _ATOM_RE.findall(text) or [text])

    # One-time pre-pass: drop empty `${}` interpolations so a template-literal
    # split (`` `AKIA${''}REST` ``) is contiguous for the raw secret scan.
    content = _EMPTY_INTERP.sub("", content)

    def _join_concat(m: re.Match) -> str:
        return _emit_dq(_atom_body(m.group(1)) + _atom_body(m.group(2)))

    def _join_arraylike(m: re.Match) -> str:
        return _emit_dq(_fold_atoms(m.group(1)))

    def _join_wordarray(m: re.Match) -> str:
        return _emit_dq("".join(m.group(2).split()))

    remaining = max_folds
    # Hard pass cap: even with the per-pass substitution budget, a hostile input
    # must not spin the loop. Real split chains are only a few levels deep, so a
    # dozen passes is far more than any genuine case needs; the atomic-group
    # atom above already bounds per-regex cost.
    passes_left = 16
    out = content
    while remaining > 0 and passes_left > 0:
        passes_left -= 1
        before = out
        # Array/word-array joins first (they contain inner atoms the pairwise
        # concat folder would otherwise partially consume).
        out, n = _ARRAY_JOIN.subn(_join_arraylike, out, count=remaining)
        remaining -= n
        if remaining <= 0:
            break
        out, n = _PY_JOIN.subn(_join_arraylike, out, count=remaining)
        remaining -= n
        if remaining <= 0:
            break
        out, n = _RUBY_WORDARRAY_JOIN.subn(_join_wordarray, out, count=remaining)
        remaining -= n
        if remaining <= 0:
            break
        out, n = _UNIFIED_CONCAT.subn(_join_concat, out, count=remaining)
        remaining -= n
        if remaining <= 0:
            break
        out, n = _ADJACENT_CONCAT.subn(_join_concat, out, count=remaining)
        remaining -= n
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

    hits = _scan_with_concat_fold(content, scan_for_secrets)
    if not hits:
        return []
    return _secret_hits_to_violations(hits, max_results)


def scan_hard_secrets(content: str, *, max_results: int = MAX_SECRETS_PER_FILE) -> list[Violation]:
    """Like `scan_secrets`, restricted to the deterministic hard-block kinds.

    The latency-sensitive callers (the PreToolUse pre-write deny, the
    corrections-exhausted block gate) only act on hard-kind hits, and those
    kinds only ever originate from the regex fallback patterns — so this path
    skips detect-secrets entirely (see
    `profile.secret_scanner.scan_for_hard_secrets`) while keeping the same
    concat-fold pass, dedup keys, result cap, and violation shape. The cap
    row carries no kind, so it never hard-blocks. Pure function — no I/O.
    """
    if not content:
        return []
    from chameleon_mcp.profile.secret_scanner import scan_for_hard_secrets

    hits = _scan_with_concat_fold(content, scan_for_hard_secrets)
    if not hits:
        return []
    return _secret_hits_to_violations(hits, max_results)


# An identifier assigned its own name as a string literal — route-key maps,
# enum mirrors, redux action-type constants: `FORGET_PASSWORD: "FORGET_PASSWORD"`,
# `export const KEY = 'KEY'`, `"KEY" => "KEY"`. A real credential's value never
# equals its own key name, so these lines are never secrets no matter how
# password-like the key reads. Declaration keywords before the key are part of
# the canonical shape (`export const X = "X"`) and must not defeat the match.
_SELF_ASSIGN_RE = re.compile(
    r"""^\s*(?:(?:export|declare|const|let|var|readonly|static|public|private)\s+)*"""
    r"""['"]?(?P<key>[A-Za-z_][A-Za-z0-9_]*)['"]?\s*(?:=>|:|=)\s*['"](?P<value>[^'"]*)['"]"""
)


def _is_self_assignment_line(line: str) -> bool:
    m = _SELF_ASSIGN_RE.match(line)
    return bool(m and m.group("key") == m.group("value"))


# The keyword-STYLE detect-secrets kinds: they flag a keyword-shaped assignment
# (`password = "..."`) off the KEY name regardless of the value, so they fire on
# obvious NON-secrets -- test fixtures (`password = "test"`) and docstring examples
# (`SECRET_KEY = "your-secret-key"`) -- at block-capable error severity. Only these
# kinds are placeholder-filtered; the deterministic prefixed/structured kinds
# (aws_access_key, github_token, private_key, ...) are NEVER touched, and even a
# keyword-style hit on a genuinely high-entropy value stays (only clear
# placeholders are dropped).
_KEYWORD_DETECTOR_KINDS = frozenset({"Secret Keyword", "password_assignment"})

# The placeholder-value classifier lives in a dependency-free leaf module so the
# secret scanner can compute a per-hit verdict at scan time without a circular
# import. Re-imported here as aliases so every existing reference below is unchanged.
from chameleon_mcp.secret_placeholder import (  # noqa: E402
    line_has_colocated_real_secret,
)
from chameleon_mcp.secret_placeholder import (  # noqa: E402
    secret_value_is_placeholder as _secret_value_is_placeholder,
)

# A string value ASSIGNED to a keyword (`password = "..."` / `key: "..."`), read
# from the value position only -- never any literal on the line -- so a real key
# in `env("KEY", "AKIA...")` or `cond ? "real" : "x"` is never mistaken for the
# placeholder branch. The value length is UNBOUNDED (a `{0,200}` cap silently
# dropped a >200-char real token from the capture, letting a co-located
# placeholder suppress the line's hit); the character-class star is linear-time,
# and a long value can never equal a short placeholder anyway.
_ASSIGNED_STR_VALUE_RE = re.compile(r"""[=:]\s*(?:[rbfuRBFU]{0,2})['"]([^'"]*)['"]""")


def _keyword_hit_is_placeholder(hit: dict, lines: list[str]) -> bool:
    """True when a keyword-style hit's flagged value is a clear placeholder.

    When the scanner computed a per-hit ``value_placeholder`` verdict from the
    EXACT flagged token (the precise path), that verdict decides it -- so a
    placeholder value survives even when a co-located non-secret arg
    (`username="eric"`) sits on the same line. Absent that boolean (an older hit,
    or the `password_assignment` fallback kind with no per-value token), fall back
    to the conservative "every assigned string literal on the line is a
    placeholder" check. Either way a real-looking value is never dropped.
    """
    if str(hit.get("type")) not in _KEYWORD_DETECTOR_KINDS:
        return False
    # A concat-folded / descaped hit fired on a REASSEMBLED value the original
    # line cannot show (`secret = "te" + "st"`, `"pfx" + "<real>"`, an escaped
    # literal). Reading the original line would see only a fragment (possibly a
    # placeholder), so the value the detector actually flagged is not recoverable
    # here -- never suppress it, keeping the concat-obfuscation defense intact.
    # This short-circuit runs BEFORE the boolean so a folded token can never leak.
    if hit.get("concat_folded"):
        return False
    ln = hit.get("line_number")
    line_ok = isinstance(ln, int) and 1 <= ln <= len(lines)
    if "value_placeholder" in hit:
        if not hit["value_placeholder"]:
            return False  # the exact flagged token is real -> keep
        # The flagged token is a placeholder, but detect-secrets flags a multi-
        # assignment line only ONCE, so a real secret under a DIFFERENT key on the
        # same line would be lost if we dropped here. Keep the hit if any co-located
        # assignment carries a real credential -- a non-placeholder value under a
        # secret-named key (`token="s3cr3t"`, weak but real) or any high-entropy
        # value. A co-located username/email under a non-secret key is not a secret,
        # so the common `login(username="x", password="test")` still drops. Missing
        # the line falls back to keeping (safe).
        if not line_ok:
            return False
        return not line_has_colocated_real_secret(lines[ln - 1])
    if not line_ok:
        return False
    values = _ASSIGNED_STR_VALUE_RE.findall(lines[ln - 1])
    return bool(values) and all(_secret_value_is_placeholder(v) for v in values)


def _scan_with_concat_fold(content: str, scan_fn) -> list[dict]:
    """Run ``scan_fn`` on the content, then on its concat-folded form.

    Shared by `scan_secrets` and `scan_hard_secrets` so the fold-bypass
    coverage and the (type, line) dedup between the two passes cannot drift.
    Fold-pass hits are marked ``concat_folded`` and dropped when the original
    pass already reported the same (type, line) — or the same type, for hits
    that carry no line. Hits whose line is a key-equals-value self-assignment
    are dropped entirely (never credentials).
    """
    hits = scan_fn(content)

    seen_types_lines = {
        (h.get("type"), h.get("line_number")) for h in hits if h.get("line_number") is not None
    }
    seen_types = {h.get("type") for h in hits}

    def _merge_derived(derived: str) -> None:
        for fh in scan_fn(derived):
            key_line = (fh.get("type"), fh.get("line_number"))
            if fh.get("line_number") is not None and key_line in seen_types_lines:
                continue
            if fh.get("line_number") is None and fh.get("type") in seen_types:
                continue
            fh = dict(fh)
            fh["concat_folded"] = True
            hits.append(fh)
            if fh.get("line_number") is not None:
                seen_types_lines.add(key_line)
            seen_types.add(fh.get("type"))

    folded = _fold_string_concat(content)
    if folded != content:
        _merge_derived(folded)

    # A secret hidden as hex/unicode escapes inside a SINGLE (non-concatenated)
    # literal -- `KEY = "\x41\x4b\x49\x41..."` -- decodes to the real token only
    # when the escapes are resolved. The fold path resolves escapes but only for
    # literals it actually folds, so a lone escaped literal reached the scanner as
    # raw `\x41...` bytes and never matched. Scan a numeric-escape-decoded copy
    # too; decoding only \x/\u/\U (never \n/\t) keeps line numbers truthful.
    descaped = _NUMERIC_ESCAPE_RE.sub(_decode_numeric_escape, content)
    if descaped != content and descaped != folded:
        _merge_derived(descaped)

    if hits:
        lines = content.splitlines()

        def _on_self_assignment(h: dict) -> bool:
            ln = h.get("line_number")
            if not isinstance(ln, int) or not (1 <= ln <= len(lines)):
                return False
            return _is_self_assignment_line(lines[ln - 1])

        hits = [
            h
            for h in hits
            if not _on_self_assignment(h) and not _keyword_hit_is_placeholder(h, lines)
        ]
    return hits


def _secret_hits_to_violations(hits: list[dict], max_results: int) -> list[Violation]:
    """Render scanner hit dicts into the canonical secret Violation shape.

    Single emission point for both secret scanners, so the
    ``"<kind> at line N"`` actual format that `tag_secret_hardness` /
    `violation_line` parse cannot drift between them. Caps at ``max_results``
    with a summary row that carries no kind (and therefore never hard-blocks).
    """
    # Overlapping detectors (e.g. "Secret Keyword" + "password_assignment")
    # stack multiple findings on one line; one per line is enough to act on.
    # Deterministic hard-block kinds win the slot so dedupe can never demote
    # a blockable hit to an advisory-only kind.
    from chameleon_mcp.violation_class import _DETERMINISTIC_SECRET_KINDS

    by_line: dict[int, dict] = {}
    no_line: list[dict] = []
    for hit in hits:
        ln = hit.get("line_number")
        if not isinstance(ln, int):
            no_line.append(hit)
            continue
        prev = by_line.get(ln)
        if prev is None or (
            str(hit.get("type")) in _DETERMINISTIC_SECRET_KINDS
            and str(prev.get("type")) not in _DETERMINISTIC_SECRET_KINDS
        ):
            by_line[ln] = hit
    hits = [h for h in hits if h in no_line or by_line.get(h.get("line_number")) is h]

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


# `eval(` invoked as a function in either language. Restricted to a word-boundary
# `eval` immediately followed by `(` so member access like `obj.evaluate(` and
# identifiers ending in `eval` (`retrieval`) do not match. String/comment regions
# are blanked before this runs, so a literal mentioning "eval(" is inert.
_EVAL_CALL_RE = re.compile(r"(?<![.\w])eval\s*\(")

# Ruby dynamic-eval variants. The block forms (`instance_eval { ... }`,
# `class_eval do ... end`) are the legitimate DSL pattern; only the STRING
# argument forms execute arbitrary code. The argument shape is checked against
# the ORIGINAL content at the matched offset (the stripper blanks string
# literals but preserves length), so a literal string / heredoc argument
# fires — a plain variable argument stays unflagged to keep legitimate
# metaprogramming out of an error-severity rule, EXCEPT when the argument
# expression itself carries request input (`instance_eval(params[:x])`), which
# is the injection the exemption must not shield. `send(:eval, ...)` and the
# string form are dynamic dispatch to the same sink.
_RUBY_EVAL_VARIANT_RE = re.compile(r"(?<![:\w])(instance_eval|class_eval|module_eval)\b")
_RUBY_EVAL_STRING_ARG_RE = re.compile(r"\A\s*\(?\s*(?:\"|'|<<[~-]?[A-Z'\"])")
# Request input reaching a *_eval argument. The variable-argument exemption
# below spares legitimate metaprogramming, but an argument expression that
# syntactically carries request input (`params[...]` / `request. ...`) is the
# injection this rule exists for, so it fires — at error severity. Matched on
# the stripped scan (a mention inside a comment or a string literal is blanked
# there); horizontal whitespace only ([ \t], never \s) so the match stays on
# the call's own line, and the argument walk stops at `{`, `&`, `)` and `;` so
# a block form (`instance_eval { params[:x] }` and the `do` form), a
# block-pass (`&blk`), or a later statement on the same line never matches.
# The lookbehind keeps it word-boundary safe: `my_params[` / `request_id.` do
# not fire. The leading whitespace quantifiers are possessive (*+) so the
# `do` lookahead cannot be bypassed by backtracking into them (a single-line
# `instance_eval do params[:x] end` block must stay exempt).
_RUBY_EVAL_REQUEST_INPUT_ARG_RE = re.compile(
    r"\A[ \t]*+\(?[ \t]*+(?!do\b)[^\n{&);]*?(?<![A-Za-z0-9_])(?:params[ \t]*\[|request[ \t]*\.)"
)
_RUBY_SEND_EVAL_RE = re.compile(r"\b((?:public_)?send)\s*\(\s*(?::eval\b|[\"']eval[\"'])")
# Paren-less Kernel#eval: `eval "..."`, `eval s`, `eval %(...)`. _EVAL_CALL_RE
# only catches the `eval(` form, so the paren-less call (idiomatic Ruby) slipped
# the rule entirely. Unlike class_eval, Kernel#eval has no block form -- every
# call executes a string as code -- so any argument fires at error severity. The
# method name is matched in the stripped scan (lookbehind excludes
# `instance_eval`/`x.eval`/`:eval`); the argument shape is then read from the
# ORIGINAL content at the matched offset (the stripper blanks the string literal),
# so a value start (string, %-literal, identifier/variable, paren) fires while
# `eval = ...` / `eval ||= ...` / a bare reference does not.
# Match the method + ONE whitespace only: the stripper blanks a string-literal
# argument into spaces, so a greedy ``[ \t]+`` would run past the opening quote
# in the scan. The arg shape is then checked in the ORIGINAL content, skipping
# any further real whitespace, so both ``eval "x"`` and ``eval   s`` resolve.
_RUBY_PARENLESS_EVAL_RE = re.compile(r"(?<![.:\w])eval[ \t]")
_RUBY_PARENLESS_EVAL_ARG_RE = re.compile(r"\A[ \t]*(?:[\"'`]|%[qQ]?[({\[<|/!]|[A-Za-z_@$:(])")

# Weak message-digest constructors. Only meaningful as a security signal when a
# crypto keyword sits nearby (a stable cache key or an ETag built from MD5 is
# legitimate), so this stays advisory and is gated on `_has_security_context`.
_WEAK_HASH_RE = re.compile(r"\b(?:MD5|SHA1|SHA-1)\b", re.IGNORECASE)
# Node's crypto API takes the digest algorithm as a STRING argument
# (``crypto.createHash("md5")`` / ``createHmac("sha1")``) -- the dominant TS/JS
# weak-hash shape. That algo name lives in a string literal, which the TS stripper
# blanks before ``_WEAK_HASH_RE`` runs, so the bareword pass never sees it. Match
# it on RAW content instead; the ±200 crypto-context gate still keeps a benign
# cache-key digest quiet.
_TS_WEAK_CRYPTO_API_RE = re.compile(
    r"""create(?:Hash|Hmac)\s*\(\s*['"`]\s*(?P<algo>md5|sha-?1)\s*['"`]""",
    re.IGNORECASE,
)
# Python-specific dangerous sinks (advisory warnings, like weak-hash). Matched on
# the strings/comments-stripped scan so a mention in a docstring is inert.
_PY_INSECURE_RANDOM_RE = re.compile(
    r"\brandom\.(?:random|randint|randrange|choice|choices|sample|shuffle|uniform|getrandbits)\s*\("
)
_PY_OS_COMMAND_RE = re.compile(r"\bos\.(?:system|popen)\s*\(")
# subprocess.<fn>(... shell=True ...) — the shell-injection vector. [^)]* keeps
# the match within the one call (won't cross the closing paren of a sibling).
_PY_SUBPROCESS_SHELL_RE = re.compile(r"\bsubprocess\.\w+\s*\([^)]*\bshell\s*=\s*True")
_PY_PICKLE_RE = re.compile(r"\bpickle\.loads?\s*\(")
# yaml.load( is unsafe; yaml.safe_load( is the safe sibling and must not match.
_PY_YAML_LOAD_RE = re.compile(r"\byaml\.load\s*\(")

# Ruby dangerous sinks (advisory warnings), the mirrors of the Python set above.
# insecure-random: rand(...) / Random.rand in a crypto context -> SecureRandom.
# SecureRandom is the secure TARGET and must not match (the lookbehind on `rand(`
# rejects the `.rand` member, and `\bRandom` rejects the `SecureRandom` prefix).
_RUBY_INSECURE_RANDOM_RE = re.compile(r"(?<![.\w])rand\s*\(|\bRandom\s*\.\s*rand\b")
# command-injection requires the actual injection vector: a `#{...}` interpolation
# spliced into a shell construct (system/exec single string arg, backticks, or a
# %x{} literal). This mirrors the SQL-interpolation rule and matches the
# command-injection semantic precisely -- a static shell call (`system("ls",
# "-la")`, `system("git status")`, a literal `` `grep ...` ``) has no untrusted
# input and is the safe/idiomatic form, so it does not flag. Each runs on raw
# content (the `#{` lives inside a string the stripper blanks) and is suppressed
# when the construct itself sits inside a string / heredoc / comment.
_RUBY_SHELL_INTERP_RES = (
    # Double-quote only: Ruby single-quoted strings do not interpolate, so
    # `system '...#{x}...'` is literal bytes (flagging it is a false positive). A
    # per-delimiter class `[^"\n]*` (not `[^'"\n]*`) lets the match cross an embedded
    # single quote, so the dominant shell-wrapper idiom
    # `system "git log --grep='#{x}'"` is caught.
    ("system/exec", re.compile(r'(?<![.\w])(?:system|exec)\s*\(?\s*"[^"\n]*\#\{')),
    ("backtick command", re.compile(r"`[^`\n]*\#\{[^`\n]*`")),
    ("%x{} command", re.compile(r"%x[\[{(<][^\n]*\#\{")),
)
# insecure-deserialization: Marshal.load / YAML.load and the load_file/load_stream/
# unsafe_load variants (load_file is the dominant config-read idiom; unsafe_load is
# the explicit Psych-4 opt-in). The `safe_` prefix is excluded by the alternation and
# Marshal.dump by the verb, so the safe siblings never match.
_RUBY_MARSHAL_LOAD_RE = re.compile(r"\bMarshal\s*\.\s*load\b")
_RUBY_YAML_LOAD_RE = re.compile(r"\bYAML\s*\.\s*(?:unsafe_)?load(?:_file|_stream)?\b")

# Non-cryptographic randomness used where unpredictability matters. Same context
# gate as weak hashes: `Math.random()` for a UI jitter is fine; for a token or
# salt it is a real weakness.
_MATH_RANDOM_RE = re.compile(r"\bMath\.random\s*\(")

# Crypto-relevant keywords used to decide whether an advisory weak-hash /
# insecure-random hit is worth surfacing. Kept local to the sink scan so its
# tuning is independent of the bootstrap poisoning scanner. Deliberately omits
# "digest"/"cipher": those words are part of the construct itself (Ruby's
# `Digest::MD5`, Node's `createCipher`), so including them would defeat the
# context gate and flag every benign MD5 cache key.
# A crypto keyword must match as a whole identifier OR as a snake_case /
# camelCase COMPONENT of one (`password_salt`, `sessionToken`, `passwordHash`) --
# those compound names are the DOMINANT crypto-material naming style, and a plain
# ``\b(...)\b`` boundary silently dropped every weak-hash / insecure-random
# advisory on them (``_`` and adjacent letters are word chars, so ``\bsalt\b``
# never matches inside ``password_salt``). These identifier-segment boundaries
# accept a ``_`` separator and a lowercase->uppercase camel transition on either
# side, while still rejecting a keyword buried inside an unrelated word (``design``
# for ``sign``, ``tokenizer`` for ``token``, ``authority`` mid-word for ``auth``).
# NB: the camel-transition boundaries below are case-SENSITIVE ([a-z] vs [A-Z]),
# so the keyword alternation carries a scoped (?i:...) flag rather than compiling
# the whole pattern IGNORECASE -- a global IGNORECASE would make [a-z] and [A-Z]
# each match any letter and collapse the camel boundary to "between any two
# letters", which would re-admit the very false matches (tokenizer, design) this
# segment boundary exists to reject.
_ID_SEG_START = r"(?:(?<![A-Za-z0-9])|(?<=[a-z0-9])(?=[A-Z]))"
_ID_SEG_END = r"(?:(?![A-Za-z0-9])|(?<=[a-z0-9])(?=[A-Z]))"
_SINK_SECURITY_KEYWORDS = re.compile(
    _ID_SEG_START + r"(?i:password|passwd|pwd|secret|token|signature|auth|hmac|csrf|session|"
    r"api[_-]?key|access[_-]?token|nonce|salt|crypto|encrypt|decrypt|sign)" + _ID_SEG_END,
)

# Active Record query builders whose first string argument is emitted verbatim
# into SQL. A `#{...}` interpolation inside that string splices the interpolated
# value straight into the statement — the canonical Rails injection shape
# (`User.where("name = #{params[:q]}")`). We match the method name, then a string
# literal (single or double quoted) that contains a `#{`. Double-quoted Ruby
# strings interpolate; single-quoted ones do not, but a literal `#{` in a
# single-quoted string handed to `where` is still suspicious enough to flag.
_RUBY_SQL_METHODS = (
    r"where|having|order|group|select|joins|pluck|find_by_sql|"
    r"exists\?|reorder|from|lock|distinct\.pluck|"
    # Raw connection methods bypass the query builder entirely -- the rawest
    # injection vector. Listed after the builder methods so the alternation
    # matches the full name (e.g. select_all, not the select prefix).
    r"exec_query|execute|select_all|select_value|select_rows|select_one"
)
_RUBY_SQL_INTERP_RE = re.compile(
    rf"""\.\s*(?:{_RUBY_SQL_METHODS})\s*\(?\s*"[^"]*\#\{{[^}}]+\}}[^"]*\"""",
    re.IGNORECASE,
)
# A few query helpers are commonly called without a receiver inside a model
# scope (`scope :recent, -> { where("ts > #{cutoff}") }`), so also match the
# bare method form not preceded by a `.` member access.
_RUBY_SQL_INTERP_BARE_RE = re.compile(
    rf"""(?<![.\w])(?:{_RUBY_SQL_METHODS})\s*\(\s*"[^"]*\#\{{[^}}]+\}}[^"]*\"""",
    re.IGNORECASE,
)


def _sink_security_context(content: str, start: int, end: int, *, window: int = 200) -> bool:
    """Return True if a crypto-relevant keyword sits within ±window chars."""
    lo = max(0, start - window)
    hi = min(len(content), end + window)
    return bool(_SINK_SECURITY_KEYWORDS.search(content[lo:hi]))


def _position_to_line(content: str, position: int) -> int:
    """1-based line number for a character offset into `content`."""
    return content.count("\n", 0, position) + 1


def scan_dangerous_sinks(content: str, *, language: str | None) -> list[Violation]:
    """Return one Violation per dangerous code sink detected in `content`.

    Complements `scan_secrets` on the edit-time lint path. Where the secret scan
    flags committed credentials, this flags code shapes a security reviewer would
    stop: a dynamic `eval(...)` call, a weak hash or non-cryptographic random in
    a crypto context, and Active Record string interpolation that splices user
    input into SQL.

    Detection runs against a string/comment-stripped copy of the source so a sink
    mentioned inside a literal or a comment does not fire. The matched fragment
    and its line number come from the same stripped offsets, which line up with
    the original because the stripper preserves length.

    Rule names are distinct from `secret-detected-in-content` so the hook
    secret-rollup filters never mistake a sink for a credential.

    `eval-call` is emitted at `error` severity (it is a content fact, not a style
    mismatch); the advisory rules stay at `warning`. Whether any of these becomes
    block-eligible is decided by the calibration gate, not here. Pure function —
    no I/O, never executes the scanned code.
    """
    if not content:
        return []

    if language == "ruby":
        scan = _strip_ruby_strings_and_comments(content)
    elif language == "typescript":
        scan = _strip_ts_strings_and_comments(content)
    elif language == "python":
        scan = _strip_python_strings_and_comments(content)
    else:
        # No language means no reliable string/comment stripping; only the
        # language-agnostic `eval(` shape is safe to run, against raw content.
        scan = content

    violations: list[Violation] = []

    for m in _EVAL_CALL_RE.finditer(scan):
        line = _position_to_line(scan, m.start())
        violations.append(
            Violation(
                rule="eval-call",
                expected="<no dynamic eval>",
                actual=f"eval( at line {line}",
                severity="error",
                message=(
                    f"dynamic eval() at line {line} executes arbitrary code. "
                    "If the argument can reach user input this is remote code "
                    "execution; replace it with an explicit parser or dispatch "
                    "table."
                ),
            )
        )

    if language == "python":
        # Python's exec() executes an arbitrary string as code, exactly like
        # eval(); flag it under the same rule. Member calls (obj.exec) are
        # exempt via the same lookbehind guard.
        for m in _PY_EXEC_CALL_RE.finditer(scan):
            line = _position_to_line(scan, m.start())
            violations.append(
                Violation(
                    rule="eval-call",
                    expected="<no dynamic exec>",
                    actual=f"exec( at line {line}",
                    severity="error",
                    message=(
                        f"dynamic exec() at line {line} executes arbitrary code. "
                        "If the argument can reach user input this is remote code "
                        "execution; replace it with an explicit parser or dispatch "
                        "table."
                    ),
                )
            )

        # insecure-random: random.* in a crypto context (token/salt/nonce nearby).
        # The secrets module is the secure alternative.
        for m in _PY_INSECURE_RANDOM_RE.finditer(scan):
            if not _sink_security_context(scan, m.start(), m.end()):
                continue
            line = _position_to_line(scan, m.start())
            violations.append(
                Violation(
                    rule="insecure-random",
                    expected="<cryptographic randomness>",
                    actual=f"random.* at line {line}",
                    severity="warning",
                    message=(
                        f"the random module at line {line} is not cryptographically "
                        "secure. For tokens, salts, or nonces use the secrets module."
                    ),
                )
            )

        # command-injection: os.system / os.popen, and subprocess(..., shell=True).
        for rx, what in (
            (_PY_OS_COMMAND_RE, "os.system/os.popen"),
            (_PY_SUBPROCESS_SHELL_RE, "subprocess(shell=True)"),
        ):
            for m in rx.finditer(scan):
                line = _position_to_line(scan, m.start())
                violations.append(
                    Violation(
                        rule="command-injection",
                        expected="<no shell string>",
                        actual=f"{what} at line {line}",
                        severity="warning",
                        message=(
                            f"shell command execution at line {line}. If any part "
                            "reaches user input this is command injection; pass an "
                            "argument list and avoid shell=True."
                        ),
                    )
                )

        # insecure-deserialization: pickle.load(s) and yaml.load (non-safe).
        for rx, what in ((_PY_PICKLE_RE, "pickle.load"), (_PY_YAML_LOAD_RE, "yaml.load")):
            for m in rx.finditer(scan):
                line = _position_to_line(scan, m.start())
                violations.append(
                    Violation(
                        rule="insecure-deserialization",
                        expected="<safe deserialization>",
                        actual=f"{what} at line {line}",
                        severity="warning",
                        message=(
                            f"untrusted deserialization at line {line} can execute "
                            "code. Use a safe loader (yaml.safe_load, json) and never "
                            "unpickle untrusted data."
                        ),
                    )
                )

    if language == "ruby":
        # Paren-less Kernel#eval (`eval "..."` / `eval s` / `eval %(...)`).
        # Every call is the dynamic-eval sink, so it fires at error severity like
        # `eval(` -- there is no benign block form to exempt.
        for m in _RUBY_PARENLESS_EVAL_RE.finditer(scan):
            if not _RUBY_PARENLESS_EVAL_ARG_RE.match(content[m.end() : m.end() + 40]):
                continue
            line = _position_to_line(scan, m.start())
            violations.append(
                Violation(
                    rule="eval-call",
                    expected="<no dynamic eval>",
                    actual=f"eval at line {line}",
                    severity="error",
                    message=(
                        f"dynamic eval at line {line} executes arbitrary code. "
                        "If the argument can reach user input this is remote code "
                        "execution; replace it with an explicit parser or dispatch "
                        "table."
                    ),
                )
            )
        # String-argument *_eval forms. The method name is matched in the
        # stripped scan (comment/string mentions are blanked there); the
        # argument shape is read from the ORIGINAL content at the same offset,
        # because the stripper blanks the very string literal that makes the
        # call dangerous. Block/variable arguments do not fire.
        for m in _RUBY_EVAL_VARIANT_RE.finditer(scan):
            arg_is_literal = bool(_RUBY_EVAL_STRING_ARG_RE.match(content[m.end() : m.end() + 40]))
            # A non-literal argument that carries request input is checked on
            # the STRIPPED scan, not the original content: a `params[` in a
            # trailing comment or inside a string literal is blanked there,
            # while a real code-level argument survives at the same offsets.
            arg_has_request_input = not arg_is_literal and bool(
                _RUBY_EVAL_REQUEST_INPUT_ARG_RE.match(scan[m.end() : m.end() + 200])
            )
            if not arg_is_literal and not arg_has_request_input:
                continue
            line = _position_to_line(scan, m.start())
            method = m.group(1)
            if arg_has_request_input:
                violations.append(
                    Violation(
                        rule="eval-call",
                        expected="<no dynamic eval>",
                        actual=f"{method}( at line {line}",
                        # Error severity, unlike the string-literal form below:
                        # params/request data in the argument is user input
                        # reaching an eval sink — the injection itself, not an
                        # established metaprogramming idiom. is_hard_class
                        # gates eval-call hardness on this severity.
                        severity="error",
                        message=(
                            f"dynamic {method} receiving request input at line "
                            f"{line} executes user-controlled code — remote code "
                            "execution. Never pass params/request data to an eval "
                            "sink; use an explicit allowlist or dispatch table."
                        ),
                    )
                )
                continue
            violations.append(
                Violation(
                    rule="eval-call",
                    expected="<no dynamic eval>",
                    actual=f"{method}( at line {line}",
                    # Advisory severity on purpose: `class_eval <<~RUBY` is an
                    # established Rails metaprogramming idiom, and content
                    # scans are never calibrated, so the error/hard form would
                    # block legitimate committed patterns. is_hard_class gates
                    # eval-call hardness on severity.
                    severity="warning",
                    message=(
                        f"dynamic {method} with a string argument at line {line} "
                        "executes arbitrary code. If the string can reach user "
                        "input this is remote code execution; use the block form "
                        "or define_method instead."
                    ),
                )
            )

        # send(:eval, ...) / send("eval", ...) — dynamic dispatch to the same
        # sink. The string form is blanked in the stripped scan, so the match
        # runs on the original content and is confirmed real code by checking
        # the stripped scan still carries the call at that offset.
        for m in _RUBY_SEND_EVAL_RE.finditer(content):
            if scan[m.start(1) : m.end(1)] != m.group(1):
                continue
            line = _position_to_line(content, m.start())
            violations.append(
                Violation(
                    rule="eval-call",
                    expected="<no dynamic eval>",
                    actual=f"{m.group(1)}(:eval at line {line}",
                    severity="error",
                    message=(
                        f"{m.group(1)} dispatching to eval at line {line} executes "
                        "arbitrary code. If the argument can reach user input this "
                        "is remote code execution; replace it with an explicit "
                        "dispatch table."
                    ),
                )
            )

        # insecure-random: rand()/Random.rand in a crypto context. SecureRandom
        # is the secure target. Matched on the stripped scan (call survives).
        for m in _RUBY_INSECURE_RANDOM_RE.finditer(scan):
            if not _sink_security_context(scan, m.start(), m.end()):
                continue
            line = _position_to_line(scan, m.start())
            violations.append(
                Violation(
                    rule="insecure-random",
                    expected="<cryptographic randomness>",
                    actual=f"rand at line {line}",
                    severity="warning",
                    message=(
                        f"rand at line {line} is not cryptographically secure. For "
                        "tokens, salts, or nonces use SecureRandom."
                    ),
                )
            )

        # insecure-deserialization: Marshal.load / YAML.load (YAML.safe_load and
        # Marshal.dump are safe). Matched on the stripped scan (call survives).
        for rx, what in (
            (_RUBY_MARSHAL_LOAD_RE, "Marshal.load"),
            (_RUBY_YAML_LOAD_RE, "YAML.load"),
        ):
            for m in rx.finditer(scan):
                line = _position_to_line(scan, m.start())
                violations.append(
                    Violation(
                        rule="insecure-deserialization",
                        expected="<safe deserialization>",
                        actual=f"{what} at line {line}",
                        severity="warning",
                        message=(
                            f"untrusted deserialization at line {line} can execute "
                            "code. Use a safe loader (YAML.safe_load, JSON) and never "
                            "Marshal.load untrusted data."
                        ),
                    )
                )

        # command-injection: an interpolated shell construct (see the regex
        # comment). Each runs on RAW content (the `#{` lives inside the string the
        # stripper blanks). A construct whose START sits inside a string / heredoc
        # / comment is suppressed via a mask that blanks DQ/SQ strings, heredocs,
        # and comments -- but NOT %x{} -- so a top-level %x is still visible while
        # an embedded one is not.
        cmd_mask = _RUBY_STRING_DQ.sub(lambda mm: " " * len(mm.group(0)), content)
        cmd_mask = _RUBY_STRING_SQ.sub(lambda mm: " " * len(mm.group(0)), cmd_mask)
        cmd_mask = _blank_ruby_heredocs(cmd_mask)
        cmd_mask = _RUBY_LINE_COMMENT.sub(lambda mm: " " * len(mm.group(0)), cmd_mask)
        cmd_mask = _RUBY_BLOCK_COMMENT.sub(lambda mm: " " * len(mm.group(0)), cmd_mask)

        cmd_seen: set[int] = set()
        for what, rx in _RUBY_SHELL_INTERP_RES:
            for m in rx.finditer(content):
                pos = m.start()
                if pos in cmd_seen or (pos < len(cmd_mask) and cmd_mask[pos] == " "):
                    continue
                cmd_seen.add(pos)
                line = _position_to_line(content, pos)
                violations.append(
                    Violation(
                        rule="command-injection",
                        expected="<no interpolated shell string>",
                        actual=f"{what} at line {line}",
                        severity="warning",
                        message=(
                            f"interpolated shell command at line {line}. If the "
                            "interpolated value reaches user input this is command "
                            "injection; pass an argument list (system(cmd, *args)) "
                            "instead of building a shell string."
                        ),
                    )
                )

    if language == "typescript":
        for m in _MATH_RANDOM_RE.finditer(scan):
            if not _sink_security_context(scan, m.start(), m.end()):
                continue
            line = _position_to_line(scan, m.start())
            violations.append(
                Violation(
                    rule="insecure-random",
                    expected="<cryptographic randomness>",
                    actual=f"Math.random() at line {line}",
                    severity="warning",
                    message=(
                        f"Math.random() at line {line} is not cryptographically "
                        "secure. For tokens, salts, or nonces use crypto."
                        "randomBytes / crypto.getRandomValues instead."
                    ),
                )
            )

        # Node crypto API weak digest: the algo name is a string arg, so match RAW
        # content (the stripper blanked it out of `scan`). Same ±200 crypto gate.
        for m in _TS_WEAK_CRYPTO_API_RE.finditer(content):
            if not _sink_security_context(content, m.start(), m.end()):
                continue
            line = _position_to_line(content, m.start())
            algo = m.group("algo").upper()
            violations.append(
                Violation(
                    rule="weak-hash",
                    expected="<strong hash>",
                    actual=f"{algo} at line {line}",
                    severity="warning",
                    message=(
                        f"{algo} at line {line} is a weak digest for a security "
                        "use. Prefer SHA-256 or stronger, and a password KDF "
                        "(bcrypt/argon2/scrypt) for credentials."
                    ),
                )
            )

    # Weak hashes apply to all three languages; the security-context gate keeps
    # benign non-crypto MD5/SHA1 uses (cache keys, content fingerprints) quiet.
    if language in ("typescript", "ruby", "python"):
        for m in _WEAK_HASH_RE.finditer(scan):
            if not _sink_security_context(scan, m.start(), m.end()):
                continue
            line = _position_to_line(scan, m.start())
            algo = m.group(0)
            violations.append(
                Violation(
                    rule="weak-hash",
                    expected="<strong hash>",
                    actual=f"{algo} at line {line}",
                    severity="warning",
                    message=(
                        f"{algo} at line {line} is a weak digest for a security "
                        "use. Prefer SHA-256 or stronger, and a password KDF "
                        "(bcrypt/argon2/scrypt) for credentials."
                    ),
                )
            )

    if language == "ruby":
        # SQL interpolation lives inside a string literal, which the stripper
        # above blanks out. Scan the raw content for this rule so the `#{...}`
        # survives; the query-method anchor keeps it from matching arbitrary
        # interpolated strings. A commented-out query would still match the raw
        # text, so blank string literals first (without touching comments) and
        # take real `#` comment spans from that copy to suppress those matches.
        no_strings = _RUBY_STRING_DQ.sub(
            lambda mm: " " * len(mm.group(0)),
            _RUBY_STRING_SQ.sub(lambda mm: " " * len(mm.group(0)), content),
        )
        comment_spans = [(cm.start(), cm.end()) for cm in _RUBY_LINE_COMMENT.finditer(no_strings)]
        comment_spans += [(cm.start(), cm.end()) for cm in _RUBY_BLOCK_COMMENT.finditer(no_strings)]

        def _in_comment(pos: int) -> bool:
            return any(lo <= pos < hi for lo, hi in comment_spans)

        seen_spans: set[tuple[int, int]] = set()
        for pat in (_RUBY_SQL_INTERP_RE, _RUBY_SQL_INTERP_BARE_RE):
            for m in pat.finditer(content):
                span = (m.start(), m.end())
                if span in seen_spans or _in_comment(m.start()):
                    continue
                seen_spans.add(span)
                line = _position_to_line(content, m.start())
                violations.append(
                    Violation(
                        rule="sql-string-interpolation",
                        expected="<parameterized query>",
                        actual=f"interpolated query string at line {line}",
                        severity="warning",
                        message=(
                            f"string interpolation inside a query at line {line} "
                            "splices the value directly into SQL. Use a bind "
                            'parameter instead: where("name = ?", value).'
                        ),
                    )
                )

    return violations


# Default per-file cap on style-rule-violation emissions, read lazily from the
# threshold module so an operator override is picked up at call time. Mirrors the
# secret-scan cap: a misformatted paste can violate a rule on every line, so the
# advisory list is bounded and a summary row reports the remainder.
_STYLE_RULE_CAP_NAME = "STYLE_RULE_VIOLATIONS_PER_FILE"


def _style_rule_cap() -> int:
    try:
        from chameleon_mcp._thresholds import threshold_int

        return threshold_int(_STYLE_RULE_CAP_NAME)
    except Exception:
        return 20


def _rules_section(rules, key: str) -> dict | None:
    """Return ``rules["rules"][key]["rules"]`` if it is a dict, else None.

    rules.json nests each tool under ``rules.<tool>.rules`` (the verbatim config
    body), with ``rules.<tool>.source`` alongside. Bootstrap writes prettier
    under ``formatting``, rubocop under ``rubocop``, and editorconfig under
    ``editorconfig`` (the parsed ``{section: {key: value}}`` map). Tolerant of a
    missing key or a non-dict payload so a partial / hand-edited rules.json never
    raises here.
    """
    if not isinstance(rules, dict):
        return None
    top = rules.get("rules")
    if not isinstance(top, dict):
        return None
    tool = top.get(key)
    if not isinstance(tool, dict):
        return None
    body = tool.get("rules")
    return body if isinstance(body, dict) else None


def _editorconfig_value(rules, key: str) -> str | None:
    """First value for ``key`` across all .editorconfig sections, lowercased.

    The parser keeps each glob section's settings separately and we have no
    file-glob matcher here, so take the first declared value for the key in
    section order (root first). Returns None when unset. Only used for indent /
    line-length, where a repo's editorconfig is overwhelmingly one global rule.
    """
    body = _rules_section(rules, "editorconfig")
    if not body:
        return None
    for section in body.values():
        if isinstance(section, dict):
            val = section.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().lower()
    return None


def _rubocop_cop(rules, cop: str) -> dict | None:
    body = _rules_section(rules, "rubocop")
    if not body:
        return None
    val = body.get(cop)
    return val if isinstance(val, dict) else None


def _rubocop_exclude_globs(rules, cop: str | None) -> list[str]:
    """rubocop ``Exclude`` globs that apply to ``cop``, or just ``AllCops`` when None.

    ``AllCops.Exclude`` removes a path from EVERY cop; a per-cop ``Exclude`` removes
    it from that one cop. When ``cop`` is given the returned set is the union of
    both, so a caller can ask "is this file out of scope for Layout/LineLength?"
    and get a yes if either the whole-file or the per-cop exclude matches. When
    ``cop`` is None only the whole-file ``AllCops.Exclude`` is returned. Tolerant
    of a missing section / non-list payload: a malformed config yields no globs
    rather than raising on the hot path.
    """
    out: list[str] = []
    all_cops = _rubocop_cop(rules, "AllCops")
    if all_cops:
        raw = all_cops.get("Exclude")
        if isinstance(raw, list):
            out.extend(g for g in raw if isinstance(g, str))
    if cop:
        cop_body = _rubocop_cop(rules, cop)
        if cop_body:
            raw = cop_body.get("Exclude")
            if isinstance(raw, list):
                out.extend(g for g in raw if isinstance(g, str))
    return out


def _rubocop_glob_matches(rel_path: str, glob: str) -> bool:
    """True if ``rel_path`` (repo-relative POSIX) matches a rubocop ``Exclude`` glob.

    rubocop globs use ``**`` to span directories and ``*`` to match within one
    segment, the same semantics Ruby's ``File.fnmatch(..., File::FNM_PATHNAME)``
    gives. Python's :mod:`fnmatch` treats ``*`` as matching ``/`` too, which would
    over-match, so translate the glob to a regex by hand: ``**/`` (or a trailing
    ``**``) spans any number of segments, a lone ``*`` matches within a segment.
    A glob that fails to translate matches nothing rather than raising.
    """
    import re as _re

    g = glob.strip()
    if not g:
        return False
    # rubocop matches `lib/**/*` against `lib/foo.rb` AND `lib/a/b.rb`; the `**/`
    # is allowed to consume zero segments. Build the regex segment by segment.
    out = ["^"]
    i = 0
    n = len(g)
    while i < n:
        c = g[i]
        if g.startswith("**/", i):
            # Any number of leading segments, including none.
            out.append(r"(?:[^/]+/)*")
            i += 3
        elif g.startswith("**", i):
            # Trailing `**`: any remaining path.
            out.append(r".*")
            i += 2
        elif c == "*":
            # Single segment wildcard: no `/`.
            out.append(r"[^/]*")
            i += 1
        elif c == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(_re.escape(c))
            i += 1
    out.append("$")
    try:
        return _re.match("".join(out), rel_path) is not None
    except _re.error:
        return False


def _rubocop_rel_path(file_path: str, repo_root: Path | str | None) -> str:
    """Repo-relative POSIX path for the rubocop Exclude match.

    rubocop globs are relative to the repo root, so resolve ``file_path`` against
    ``repo_root`` when it lands inside. Falls back to the path's own POSIX form
    when the root is unknown or the file resolves outside it: a relative input
    like ``db/migrate/x.rb`` then still matches a ``db/migrate/*`` glob.
    """
    try:
        p = Path(file_path)
        if repo_root is not None:
            root = Path(repo_root)
            try:
                if p.is_absolute() and root.is_absolute():
                    return p.resolve().relative_to(root.resolve()).as_posix()
            except (ValueError, OSError):
                pass
        return p.as_posix()
    except (OSError, ValueError):
        return file_path


def _rubocop_excluded(rel_path: str | None, rules, cop: str | None) -> bool:
    """True if ``rel_path`` is excluded from ``cop`` (or all cops) by rubocop config.

    The repo's own CI rubocop never inspects a path under ``AllCops.Exclude`` (or a
    per-cop ``Exclude``), so the style baseline must not flag it either. Returns
    False when the path is unknown (no file_path threaded through) so behavior is
    unchanged for callers that don't supply one.
    """
    if not rel_path:
        return False
    for glob in _rubocop_exclude_globs(rules, cop):
        if _rubocop_glob_matches(rel_path, glob):
            return True
    return False


def _coerce_int(value) -> int | None:
    """Parse a positive int from a config value (int or numeric string)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            n = int(value.strip())
        except ValueError:
            return None
        return n if n > 0 else None
    return None


def _declared_indent(rules, language: str) -> tuple[str, int | None] | None:
    """Resolve the declared indent style/width, or None if no config declares it.

    Returns ("tab", None) for tab indentation or ("space", width|None) for space
    indentation. Precedence is the language's primary formatter first, then
    .editorconfig: prettier for TypeScript, rubocop for Ruby, ruff for Python.
    Only declared values count -- an absent cop / key contributes nothing, so a
    repo with no indent config gets no indent findings.
    """
    if language == "typescript":
        prettier = _rules_section(rules, "formatting")
        if prettier:
            if prettier.get("useTabs") is True:
                return ("tab", None)
            if prettier.get("useTabs") is False:
                return ("space", _coerce_int(prettier.get("tabWidth")))
    elif language == "ruby":
        style_cop = _rubocop_cop(rules, "Layout/IndentationStyle")
        width_cop = _rubocop_cop(rules, "Layout/IndentationWidth")
        enforced = None
        if style_cop:
            enforced = str(style_cop.get("EnforcedStyle") or "").lower()
        width = _coerce_int(width_cop.get("Width")) if width_cop else None
        if enforced == "tabs":
            return ("tab", None)
        if enforced == "spaces" or width is not None:
            return ("space", width)
    elif language == "python":
        pf = _rules_section(rules, "python_format")
        if pf:
            style = pf.get("indent_style")
            if style == "tab":
                return ("tab", None)
            width = _coerce_int(pf.get("indent_width"))
            if style == "space" or width is not None:
                return ("space", width)

    ec_style = _editorconfig_value(rules, "indent_style")
    if ec_style == "tab":
        return ("tab", None)
    if ec_style == "space":
        return ("space", _coerce_int(_editorconfig_value(rules, "indent_size")))
    return None


def _declared_quote(rules, language: str) -> str | None:
    """Resolve the declared quote preference: "single", "double", or None.

    TypeScript reads prettier ``singleQuote``; Ruby reads rubocop
    ``Style/StringLiterals`` ``EnforcedStyle``. Returns None when the config does
    not declare a quote preference, so no quote findings fire on those repos.
    """
    if language == "typescript":
        prettier = _rules_section(rules, "formatting")
        if prettier:
            if prettier.get("singleQuote") is True:
                return "single"
            if prettier.get("singleQuote") is False:
                return "double"
    elif language == "ruby":
        cop = _rubocop_cop(rules, "Style/StringLiterals")
        if cop:
            style = str(cop.get("EnforcedStyle") or "").lower()
            if style == "single_quotes":
                return "single"
            if style == "double_quotes":
                return "double"
    elif language == "python":
        pf = _rules_section(rules, "python_format")
        if pf:
            qs = pf.get("quote_style")
            if qs in ("single", "double"):
                return qs
    return None


# JSX presence: a closing tag (`</div`), a self-close (`/>`), or an opening tag
# whose name is followed by whitespace then an attribute (`<input type=`). The
# required whitespace between tag and attribute is what keeps a TS generic
# (`Array<string>`, `Foo<T="x">`) from matching -- those have no space there,
# and no `</`/`/>` either.
_JSX_PRESENCE_RE = re.compile(r"</[A-Za-z]|/>|<[A-Za-z][\w.]*\s+[\w-]+\s*=")


def _content_has_jsx(content: str) -> bool:
    """Whether the source appears to contain JSX (so a `name="x"` literal may be a
    JSX attribute rather than a compact assignment)."""
    return bool(_JSX_PRESENCE_RE.search(content))


def _jsx_double_quotes_ok(rules) -> bool:
    """Whether double-quoted JSX attribute values are correct per prettier.

    prettier's ``jsxSingleQuote`` governs JSX attribute quoting SEPARATELY from
    ``singleQuote`` and defaults to False, so ``className="x"`` is correctly
    double-quoted even under ``singleQuote: true``. Returns True (JSX attributes
    keep double quotes) unless the config explicitly sets ``jsxSingleQuote: true``.
    """
    prettier = _rules_section(rules, "formatting")
    return not (prettier and prettier.get("jsxSingleQuote") is True)


def _declared_max_line_length(rules, language: str) -> int | None:
    """Resolve the declared max line length, or None if no config declares it.

    Precedence mirrors indent: prettier ``printWidth`` for TypeScript, rubocop
    ``Layout/LineLength`` ``Max`` for Ruby, then .editorconfig
    ``max_line_length``. rubocop's LineLength ``Max`` defaults to 120 when the
    cop is present but sets no explicit value; we do NOT assume that default,
    only a value the config states outright.
    """
    if language == "typescript":
        prettier = _rules_section(rules, "formatting")
        if prettier:
            n = _coerce_int(prettier.get("printWidth"))
            if n is not None:
                return n
    elif language == "ruby":
        cop = _rubocop_cop(rules, "Layout/LineLength")
        if cop:
            n = _coerce_int(cop.get("Max"))
            if n is not None:
                return n
    elif language == "python":
        pf = _rules_section(rules, "python_format")
        if pf:
            n = _coerce_int(pf.get("line_length"))
            if n is not None:
                return n
    ec = _editorconfig_value(rules, "max_line_length")
    if ec and ec != "off":
        return _coerce_int(ec)
    return None


def _line_length_allowed_patterns(rules, language: str) -> list[re.Pattern]:
    """Compiled regexes for lines the declared line-length config exempts.

    rubocop's ``Layout/LineLength`` carries ``AllowedPatterns`` (line patterns
    that never count, commonly ``'(\\A|\\s)#'`` to exempt comment lines) and
    ``AllowedURI: true`` (lines containing a URL). Honoring them keeps the style
    baseline from flagging a long comment or docs-URL line that the repo's own
    rubocop run leaves clean -- a pure false positive a user would act on.

    Only Ruby declares these; prettier's ``printWidth`` applies uniformly with no
    exemption, and .editorconfig ``max_line_length`` has none either, so this
    returns nothing for those. A pattern that fails to compile is skipped rather
    than raised on the hot path.
    """
    if language != "ruby":
        return []
    cop = _rubocop_cop(rules, "Layout/LineLength")
    if not cop:
        return []
    out: list[re.Pattern] = []
    raw = cop.get("AllowedPatterns") or cop.get("IgnoredPatterns") or []
    if isinstance(raw, list):
        for pat in raw:
            if not isinstance(pat, str):
                continue
            try:
                out.append(re.compile(pat))
            except re.error:
                continue
    if cop.get("AllowedURI") is True or cop.get("URISchemes"):
        # rubocop exempts a line that contains a URL when AllowedURI is on (the
        # default). Match a bare scheme://… token anywhere on the line.
        out.append(re.compile(r"\b[a-z][a-z0-9+.-]*://\S+"))
    return out


# Leading-whitespace run at the start of a line, used for the indent check.
_LEADING_WS_RE = re.compile(r"^([ \t]+)")
# Combined comment+string scanners for the quote-style check. A single left-to-
# right pass matches comments and string literals in one alternation so a quote
# char inside a comment is consumed as part of the comment and never mistaken for
# a string literal. The string alternative is captured in a named group so the
# caller can tell which kind matched. Comment alternatives come first so they win
# when both could start at the same offset.
_TS_TOKEN_RE = re.compile(
    r"/\*.*?\*/"  # block comment
    r"|//[^\n]*"  # line comment
    r"|(?P<str>"
    r'"(?:\\.|[^"\\])*"'
    r"|'(?:\\.|[^'\\])*'"
    r"|`(?:\\.|[^`\\])*`"
    r")",
    re.DOTALL,
)
_RUBY_TOKEN_RE = re.compile(
    r"#[^\n]*"  # line comment
    r"|(?P<str>"
    r'"(?:\\.|[^"\\])*"'
    r"|'(?:\\.|[^'\\])*'"
    r")",
    re.DOTALL,
)
# Python tokenizer for the quote scan. Comments, triple-quoted strings, and
# PREFIXED single-line strings (f/r/b/u) are matched but NOT in the `str` group,
# so the quote check only fires on a plain single-line '...'/"..." literal --
# triple-quoted docstrings and f/r/b-strings are left alone (conservative). Order
# matters: triple-quoted and prefixed forms precede the plain `str` alternative.
_PY_TOKEN_RE = re.compile(
    r"#[^\n]*"  # line comment
    r'|[rRbBfFuU]{0,3}"""[\s\S]*?"""'  # triple double-quoted
    r"|[rRbBfFuU]{0,3}'''[\s\S]*?'''"  # triple single-quoted
    r'|[rRbBfFuU]{1,3}"(?:\\.|[^"\\\n])*"'  # prefixed double (not str-group)
    r"|[rRbBfFuU]{1,3}'(?:\\.|[^'\\\n])*'"  # prefixed single (not str-group)
    r'|(?P<str>"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\')',  # plain single-line string
    re.DOTALL,
)


def scan_style_rules(
    content: str,
    *,
    language: str | None,
    rules,
    file_path: str | None = None,
    repo_root: Path | str | None = None,
) -> list[Violation]:
    """Flag edits that break the repo's own declared formatter config.

    Archetype-independent, advisory-only style baseline. It reads ONLY the
    declared tool-config values bootstrap already lifted into rules.json
    (prettier / rubocop / .editorconfig) -- never a statistically inferred rule
    -- and checks the edited content against them. Like ``scan_secrets`` and the
    dangerous-sink scan it fires regardless of whether the file resolved to an
    archetype, so a sparse repo where every cluster is too small to ground an
    archetype still gets indent / quote / line-length feedback.

    Three checks, each silent unless the config declares the rule:

    - indentation style/width (prettier useTabs/tabWidth, rubocop
      Layout/IndentationStyle + Layout/IndentationWidth, ruff
      indent-style/indent-width, .editorconfig indent_style/indent_size)
    - quote style (prettier singleQuote, rubocop Style/StringLiterals), checked
      against real string literals so a quote inside a comment never flags
    - max line length (prettier printWidth, rubocop Layout/LineLength Max,
      .editorconfig max_line_length)

    ``file_path`` (with ``repo_root``) lets the Ruby checks honor rubocop's
    ``AllCops.Exclude`` and per-cop ``Exclude`` globs: a path the repo's own
    rubocop never inspects (db/migrate, lib, config, app/views, ...) gets no style
    findings, so the baseline does not nag a long line CI deliberately exempts.
    When the path is not supplied the exclude check is a no-op (behavior
    unchanged), so existing callers keep their semantics.

    Always emitted at ``warning``. This rule is never block-eligible (absent from
    BLOCK_ELIGIBLE_RULES): a formatter disagreement is a nudge, not a turn-stop,
    and CI's own formatter is the enforcing authority. Emissions are capped per
    file; past the cap a single summary row reports the remainder. Pure function
    -- no I/O, never executes the scanned code.
    """
    if not content or language not in ("typescript", "ruby", "python"):
        return []

    # Repo-relative POSIX path for the rubocop Exclude check. Resolve against the
    # repo root when both are absolute; fall back to the raw path otherwise so a
    # bare relative path still matches a `db/migrate/*` glob.
    rel_path: str | None = None
    if language == "ruby" and file_path:
        rel_path = _rubocop_rel_path(file_path, repo_root)
        # AllCops.Exclude removes the file from every cop: skip the whole scan.
        if _rubocop_excluded(rel_path, rules, None):
            return []

    indent = _declared_indent(rules, language)
    quote = _declared_quote(rules, language)
    # A JSX-attribute-shaped literal (`name="x"`, no space before the quote) can
    # only be a JSX attribute in a file that actually contains JSX -- anywhere else
    # that shape is a compact JS assignment (`const x="y"`, which prettier rewrites
    # to single) that MUST still flag. Gating on JSX presence in the content (not
    # the extension) covers a JSX-in-.js React file while keeping plain .js/.ts
    # assignments flagging; the whitespace-before-attribute signature never
    # false-matches a TS generic (`Array<string>` has no `</`, `/>`, or `<Tag a=`).
    jsx_double_ok = (
        quote == "single"
        and language == "typescript"
        and _content_has_jsx(content)
        and _jsx_double_quotes_ok(rules)
    )
    max_len = _declared_max_line_length(rules, language)
    line_len_allowed = _line_length_allowed_patterns(rules, language) if max_len is not None else []
    # A per-cop Exclude on Layout/LineLength (without an AllCops match) drops only
    # the line-length check; indent/quote still run.
    if max_len is not None and rel_path and _rubocop_excluded(rel_path, rules, "Layout/LineLength"):
        max_len = None
    if indent is None and quote is None and max_len is None:
        return []

    if language == "ruby":
        stripped = _strip_ruby_strings_and_comments(content)
        token_re = _RUBY_TOKEN_RE
    elif language == "python":
        stripped = _strip_python_strings_and_comments(content)
        token_re = _PY_TOKEN_RE
    else:
        stripped = _strip_ts_strings_and_comments(content)
        token_re = _TS_TOKEN_RE

    cap = _style_rule_cap()
    violations: list[Violation] = []
    total = 0

    def _emit(rule_actual: str, message: str) -> bool:
        """Append a violation if under the cap. Returns False once the cap hits."""
        nonlocal total
        total += 1
        if len(violations) >= cap:
            return False
        violations.append(
            Violation(
                rule="style-rule-violation",
                expected="<matches declared formatter config>",
                actual=rule_actual,
                severity="warning",
                message=message,
            )
        )
        return True

    lines = content.splitlines()
    # The strippers blank a multi-line string/comment to spaces INCLUDING its
    # newlines, which would collapse the line structure. Restore the original
    # newline positions (the strip is length-preserving, so offsets line up) so
    # line N of `indent_scan` aligns with line N of `content`. Using this copy for
    # the indent scan keeps a tab/space inside a multi-line literal from being
    # read as code indentation while keeping per-line alignment intact.
    if len(stripped) == len(content):
        indent_scan = "".join(
            "\n" if oc == "\n" else sc for sc, oc in zip(stripped, content, strict=False)
        )
    else:
        indent_scan = content
    stripped_lines = indent_scan.splitlines()

    # _emit keeps counting (`total`) past the cap but stops appending, so the
    # summary row reports the true remainder. Both loops run to completion rather
    # than breaking, which keeps the count honest at the cost of scanning the rest
    # of an already-100KB-capped buffer.
    for idx, raw_line in enumerate(lines):
        line_no = idx + 1

        if indent is not None:
            scan_line = stripped_lines[idx] if idx < len(stripped_lines) else raw_line
            m = _LEADING_WS_RE.match(scan_line)
            if m:
                lead = m.group(1)
                want_style, want_width = indent
                if want_style == "tab" and " " in lead:
                    _emit(
                        f"space indentation at line {line_no}",
                        f"line {line_no} indents with spaces; this repo's config "
                        "declares tab indentation.",
                    )
                elif want_style == "space" and "\t" in lead:
                    _emit(
                        f"tab indentation at line {line_no}",
                        f"line {line_no} indents with a tab; this repo's config "
                        f"declares {want_width or 'space'}-space indentation."
                        if want_width
                        else f"line {line_no} indents with a tab; this repo's "
                        "config declares space indentation.",
                    )

        if max_len is not None and len(raw_line) > max_len:
            # Skip a line the declared config exempts (rubocop AllowedPatterns /
            # AllowedURI), so a long comment or docs-URL line the repo's own
            # rubocop leaves clean is not flagged here.
            if not any(p.search(raw_line) for p in line_len_allowed):
                _emit(
                    f"line {line_no} is {len(raw_line)} cols (max {max_len})",
                    f"line {line_no} is {len(raw_line)} columns; this repo's config "
                    f"sets a max of {max_len}.",
                )

    # Quote-style runs over the located string literals rather than per line, so
    # one violation per offending literal. The stripper has already removed
    # comments, so a quote char inside a comment cannot be mistaken for a literal.
    if quote is not None:
        want_char = "'" if quote == "single" else '"'
        other_label = "double" if quote == "single" else "single"
        for m in token_re.finditer(content):
            literal = m.group("str")
            if literal is None:
                # The match was a comment; skip it so a quote char inside a
                # comment never reads as a string literal.
                continue
            opener = literal[0]
            if opener not in ("'", '"') or opener == want_char:
                continue
            # A double-quoted JSX attribute value (`className="x"`) is CORRECT
            # under prettier's default jsxSingleQuote:false even when singleQuote is
            # true, so flagging it steers the model to break prettier-conforming
            # code. The attribute signature is a name char abutting `=` with no
            # space before the opening quote. That signature also matches a compact
            # JS assignment (`const x="y"`), which prettier DOES rewrite to single,
            # so ``jsx_double_ok`` is gated to .tsx/.jsx files -- JSX attributes can
            # only validly appear there, so a plain .ts/.js assignment still flags.
            if jsx_double_ok and opener == '"' and m.start() >= 2:
                before = content[m.start() - 1]
                name_char = content[m.start() - 2]
                if before == "=" and (name_char.isalnum() or name_char in "_$"):
                    continue
            # A literal that must contain the preferred quote char (so switching
            # would force escapes) is a legitimate exception both prettier and
            # rubocop allow; do not flag it.
            if want_char in literal[1:-1]:
                continue
            # A Ruby double-quoted string that INTERPOLATES (#{...}) or carries
            # an escape sequence (\n, \t) cannot be single-quoted at all --
            # rubocop's own double_quotes/single_quotes styles both accept it.
            # Flagging it steers the model to break working code.
            if language == "ruby" and opener == '"':
                body = literal[1:-1]
                if "#{" in body or "\\" in body:
                    continue
            line_no = _position_to_line(content, m.start())
            _emit(
                f"{other_label}-quoted string at line {line_no}",
                f"line {line_no} uses a {other_label}-quoted string; this repo's "
                f"config prefers {quote} quotes.",
            )

    if total > len(violations):
        remaining = total - len(violations)
        violations.append(
            Violation(
                rule="style-rule-violation",
                expected="<matches declared formatter config>",
                actual=f"+{remaining} more (capped at {cap})",
                severity="warning",
                message=(
                    f"file has {total} style-config deviations; reporting the "
                    f"first {cap}. Run the repo's formatter to fix them all."
                ),
            )
        )

    return violations


_TS_IMPORT_FROM_RE = re.compile(r"import\s+.*?\bfrom\s+['\"]([^'\"]+)['\"]", re.MULTILINE)


def _module_specifier_matches(spec: str, module: str) -> bool:
    """True when an import specifier names `module` as a whole module path.

    A package name matches the specifier when they are equal or when the
    specifier is a subpath of the package (``react-query/devtools`` matches
    ``react-query``). It must NOT match when the package name is only a trailing
    segment of a longer scoped name: a plain ``\\b`` search treats the ``/`` and
    quotes as word boundaries, so ``react-query`` would wrongly match inside
    ``@tanstack/react-query``, false-flagging the preferred import and defeating
    the preferred-present skip guard. Comparing whole path segments avoids that.
    """
    if spec == module:
        return True
    return spec.startswith(module + "/")


_RUBY_REQUIRE_RE = re.compile(
    r"^[ \t]*require(?:_relative)?\s*\(?\s*['\"]([^'\"]+)['\"]", re.MULTILINE
)
# Python imports: the module path of an `import x.y` or a `from x.y import z`.
# One capture group per branch; the matcher reads whichever matched. Dotted
# paths are preserved (django.db, requests.adapters) so a taught module keys on
# its full root.
_PY_IMPORT_RE = re.compile(
    r"^[ \t]*(?:import[ \t]+([\w.]+)|from[ \t]+([\w.]+)[ \t]+import)", re.MULTILINE
)


def _python_module_in_use(mod: str, import_specs: list[str]) -> bool:
    """True when ``mod`` is imported (exact module or a submodule of it).

    ``requests`` matches ``import requests`` and ``from requests.adapters import
    ...``; it must NOT match an unrelated module that merely shares the prefix
    string (``requests_oauthlib``), which the dotted-boundary check enforces.
    """
    for spec in import_specs:
        if spec == mod or spec.startswith(mod + "."):
            return True
    return False


# A competing pair taught on a Ruby repo names either a require path
# ('net/http') or a constant path (Net::HTTP). This shape-check picks the
# matching strategy per entry.
_RUBY_CONSTANT_PATH_RE = re.compile(r"[A-Z]\w*(?:::[A-Z]\w*)*\Z")


def _ruby_module_in_use(mod: str, import_specs: list[str], scan_content: str) -> bool:
    """True when `mod` is required or referenced in a Ruby file.

    Require paths compare whole specifiers (same segment rules as the TS
    matcher). Constant paths match word-bounded references in the
    strings/comments-stripped content, so usage with no explicit require
    (Rails autoloading, transitive requires) still counts while mentions in
    comments and string literals do not. A reference is bounded on the left so
    ``Foo::Net::HTTP`` does not count as ``Net::HTTP``; an explicit top-level
    ``::Net::HTTP`` does.
    """
    if any(_module_specifier_matches(spec, mod) for spec in import_specs):
        return True
    if _RUBY_CONSTANT_PATH_RE.fullmatch(mod):
        pattern = rf"(?<![\w:])(?:::)?{re.escape(mod)}\b"
        return re.search(pattern, scan_content) is not None
    return False


# Match the declared name whatever its casing. An uppercase-only class would skip
# the most blatant violation -- a lowercase `interface params` in an I-prefix
# repo -- so the prefix/casing check downstream gets the real name and can flag it.
_TS_INTERFACE_DECL_RE = re.compile(r"\binterface\s+([A-Za-z_$]\w*)")

# `declare global { ... }` and `declare module "x" { ... }` augment external or
# lib-global types (e.g. `interface Window`) whose names cannot be renamed, so
# their interfaces are exempt from the repo's I-prefix naming convention. Match
# the block opener; the caller brace-matches the body to find its extent. Run
# over string/comment-stripped content so braces inside literals do not skew the
# depth count.
_TS_AMBIENT_BLOCK_OPENER = re.compile(r"\bdeclare\s+(?:global\b|module\b[^{]*)")


def _ts_ambient_block_spans(content: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for m in _TS_AMBIENT_BLOCK_OPENER.finditer(content):
        brace = content.find("{", m.end())
        if brace == -1:
            continue
        depth = 0
        for i in range(brace, len(content)):
            ch = content[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    spans.append((m.start(), i))
                    break
    return spans


# A `.then(` on a line that also carries no `.catch`. Scoped to a single line on
# purpose: a `.then().catch()` chain split across lines, a `.catch` on the same
# statement but a later line, or rejection handled by an enclosing try/await are
# all common and legitimate, so a multi-line scan would false-positive heavily.
# The narrow single-line case (`p.then(fn);` with nothing else) is the only one
# precise enough to nudge on, and only ever as advisory.
_TS_THEN_RE = re.compile(r"\.then\s*\(")


_THEN_CATCH_LOOKAHEAD = 3


def _then_without_catch_violations(scan_content: str) -> list[Violation]:
    """Flag a bare ``x.then(fn)`` statement whose promise rejection is unhandled.

    Advisory only. A ``.then`` is NOT a smell when the promise is RETURNED,
    AWAITED, or arrow-returned (rejection is delegated to the caller / an enclosing
    try), nor when a ``.catch`` is chained on the same line or the next few lines
    (a multi-line promise chain). Those exemptions are the dominant real patterns
    (`return api.get(x).then(...)`, `() => import('./x').then(...)`, a
    `.then(...)` immediately followed by `.catch(...)`), where flagging is a false
    positive. Only a truly bare statement-level ``x.then(fn)`` with no rejection
    handler anywhere near it is flagged.
    """
    out: list[Violation] = []
    lines = scan_content.splitlines()
    for i, line in enumerate(lines):
        if ".catch" in line:
            continue
        m = _TS_THEN_RE.search(line)
        if not m:
            continue
        head = line[: m.start()]
        # returned / awaited / voided / arrow-returned promise -> rejection is the
        # caller's or an enclosing try's responsibility, not an unhandled smell.
        if head.lstrip().startswith(("return ", "await ", "void ")) or "=>" in head:
            continue
        # `.catch` chained on a following line within a small window handles it.
        if any(
            ".catch" in lines[j]
            for j in range(i + 1, min(i + 1 + _THEN_CATCH_LOOKAHEAD, len(lines)))
        ):
            continue
        out.append(
            Violation(
                rule="then-without-catch",
                expected=".catch handler",
                actual=".then with no .catch",
                severity="info",
                message=(
                    "ASYNC: .then on this line has no .catch; an unhandled "
                    "rejection is silent -- chain .catch or await inside try"
                ),
            )
        )
    return out


# --- Test-quality heuristics -------------------------------------------------
#
# These fire only for archetypes whose name marks them as tests (see
# lint_conventions' archetype_name gate). A generated test that asserts nothing
# or leans on a known flake source (real clock, real network, real randomness)
# clears a "needs a test" demand while making coverage worse, and nothing else
# in the pipeline models assertion shape. Every rule here is advisory: regex
# assertion detection has genuine edge cases (custom matchers, helper-wrapped
# asserts, tests that legitimately exercise sleep/random), so none is precise
# enough to block. All scans run on the strings/comments-stripped copy so a
# `sleep` or `expect` inside a description string or a comment never fires.

# Skip / pending markers. The TS forms cover the jest/vitest/mocha family
# (it.skip, xit, describe.skip, test.skip, xdescribe) plus a bare `pending(`
# call. The Ruby forms cover RSpec (`pending`, `skip`, `xit`/`xdescribe`).
_TS_SKIPPED_TEST_RE = re.compile(
    r"\b(?:x(?:it|describe|test)|(?:it|describe|test|context)\s*\.\s*skip|pending)\b"
)
_RUBY_SKIPPED_TEST_RE = re.compile(r"^[ \t]*(?:x(?:it|describe)|skip|pending)\b", re.MULTILINE)

# `expect(<literal>).toBe(<same literal>)` and the equality variants. A test
# asserting a constant against itself proves nothing; the body is whitespace-
# tolerant so `expect( true ).toEqual(true)` still matches. We only treat the
# self-comparing boolean/number/string-literal shapes as tautological, which is
# where the near-zero-FP signal lives.
_TS_TAUTOLOGY_RE = re.compile(
    r"expect\s*\(\s*(true|false|\d+|null|undefined)\s*\)\s*"
    r"\.\s*(?:toBe|toEqual|toStrictEqual)\s*\(\s*\1\s*\)"
)

# A blocking real sleep inside a test body. TS: an awaited promise that resolves
# on a real setTimeout (the canonical "wait N ms" hack) or a bare `sleep(`
# helper call. Ruby: a top-of-statement `sleep` call. Fake-timer / freeze
# helpers are call expressions the witness check handles separately, so these
# patterns target only the real-clock wait.
_TS_REAL_SLEEP_RE = re.compile(r"setTimeout\s*\([^,]*,\s*\d+\s*\)|(?<![.\w])sleep\s*\(\s*\d")
_RUBY_REAL_SLEEP_RE = re.compile(r"(?<![.\w])sleep\s+\d|(?<![.\w])sleep\s*\(\s*\d")

# Real randomness inside a test makes assertions order/seed dependent. TS:
# Math.random. Ruby: rand(), Random.rand, SecureRandom.* (the last seeds from
# the OS so it is just as non-deterministic for a test fixture).
_TS_RANDOM_RE = re.compile(r"\bMath\s*\.\s*random\s*\(")
_RUBY_RANDOM_RE = re.compile(r"(?<![.\w])rand\s*\(|\bRandom\s*\.\s*rand\b|\bSecureRandom\s*\.")

# Tokens that mark a test as using fake timers / a frozen clock. If the witness
# uses one of these and the candidate uses none, the candidate likely hits the
# real clock (flaky on date-sensitive assertions). Whole-file scope on purpose:
# the freeze often lives in a beforeEach / setup helper, not the assertion block.
_CLOCK_FREEZE_TOKENS = (
    "useFakeTimers",
    "jest.useFakeTimers",
    "vi.useFakeTimers",
    "sinon.useFakeTimers",
    "MockDate",
    "freeze_time",
    "travel_to",
    "Timecop",
    # Python: freezegun (freeze_time, already above) + time-machine.
    "freezegun",
    "time_machine",
    # pytest-freezegun exposes the same library through a `freezer` fixture
    # rather than the decorator, so a suite that freezes exclusively that way
    # looked like a suite that never freezes.
    "freezer.move_to",
)
# Tokens that mark a test as stubbing the network. Same whole-file rationale.
_NETWORK_STUB_TOKENS = (
    "nock",
    "WebMock",
    "stub_request",
    "fetchMock",
    "fetch-mock",
    "msw",
    "setupServer",
    "mockServer",
    "VCR",
    # Python network-stub libs. `responses` and `vcr` are plain English words
    # that substring-collide with common identifiers (expected_responses,
    # vcr_cassette), so they are matched on a word boundary below, not here.
    "respx",
    "httpretty",
    "requests_mock",
    "aioresponses",
)
# Word-boundary-matched stub tokens: bare substring matching would misfire on
# expected_responses / mock_responses / vcr_cassette, but `\bresponses\b` only
# matches the standalone name (import responses, @responses.activate, vcr.use_*).
_NETWORK_STUB_WORD_RE = re.compile(r"\b(?:responses|vcr)\b")
# Tokens that indicate a candidate touches the real network at all. Without one
# of these present there is nothing to stub, so the unstubbed-network rule stays
# silent regardless of the witness.
_NETWORK_CALL_TOKENS = (
    "fetch(",
    "axios",
    "http.get",
    "http.post",
    "https.get",
    "https.request",
    "XMLHttpRequest",
    "Net::HTTP",
    "HTTParty",
    "RestClient",
    "Faraday",
    # Python HTTP clients.
    "requests.",
    "httpx",
    "urllib",
    "aiohttp",
    "urlopen",
)
# Tokens that indicate a candidate reads the real clock. Without one of these
# there is nothing to freeze, so the unfrozen-clock rule stays silent.
_CLOCK_READ_TOKENS = (
    "Date.now",
    "new Date(",
    "Time.now",
    "Time.current",
    "Date.today",
    "DateTime.now",
    # Python real-clock reads. django.utils.timezone.now/localtime are listed
    # because Django mandates them over datetime.now() in a timezone-aware
    # project -- on a Django/DRF repo they ARE the wall-clock read, so omitting
    # them left the rule blind to the framework's own canonical idiom.
    "datetime.now",
    "datetime.utcnow",
    "datetime.today",
    "date.today",
    "time.time",
    "timezone.now",
    "timezone.localtime",
)

# Assertion tokens. Presence of any of these in a test block means the block
# asserts something via a recognized framework matcher, so assertion-free does
# not fire. The set spans jest/vitest/chai (expect/assert) and RSpec/minitest
# (expect/should/assert_*). Matched as call-ish tokens to avoid a stray word.
_TS_ASSERTION_RE = re.compile(
    r"\bexpect\s*\(|\bassert\b|\.should\b|\.to(?:Be|Equal|Throw|Match|Contain|Have)"
)
_RUBY_ASSERTION_RE = re.compile(
    r"\bexpect\s*\(|\bassert(?:_\w+)?\b|\.should\b|\bis_expected\b|\brefute(?:_\w+)?\b"
)

# A test block opener. TS uses an `it(`/`test(` call with a brace body; Ruby
# uses `it ... do`/`it ... {`. The scan runs on the strings/comments-stripped
# copy where the description argument is blanked to spaces, so the opener cannot
# rely on the quote being present. We span only the immediate block so a sibling
# block's assertion does not mask an assertion-free neighbor.
_TS_TEST_BLOCK_RE = re.compile(r"(?:^|[^.\w])(?:it|test)\s*\(")
_RUBY_TEST_BLOCK_RE = re.compile(r"^[ \t]*(?:it|specify|example)\b.*\b(?:do|\{)\s*$", re.MULTILINE)

# Python (pytest / unittest) test-quality patterns.
_PY_SKIPPED_TEST_RE = re.compile(
    r"@(?:pytest\.mark\.(?:skip|skipif|xfail)|unittest\.skip(?:If|Unless)?|skip(?:If|Unless)?)\b"
    r"|(?<![.\w])pytest\.skip\s*\("
)
# Self-comparing assertion: `assert <lit> == <same lit>` or `assertEqual(<lit>, <same>)`.
_PY_TAUTOLOGY_RE = re.compile(
    r"assert\s+(True|False|None|\d+)\s*==\s*\1\b"
    r"|(?:self\.)?assertEqual\s*\(\s*(True|False|None|\d+)\s*,\s*\2\s*\)"
)
# Ruby self-comparing assertions: RSpec `expect(<lit>).to eq/eql/be(<same>)` (the
# matcher arg may be parenthesized or bare, `eq 1`) and Minitest `assert_equal
# <lit>, <same>`. Only literal-vs-same-literal, the near-zero-FP shape.
_RUBY_TAUTOLOGY_RE = re.compile(
    r"expect\s*\(\s*(true|false|nil|\d+)\s*\)\s*\.\s*to\s+(?:eql|equal|eq|be)\s*\(?\s*\1\b"
    r"|assert_equal\s*\(?\s*(true|false|nil|\d+)\s*,\s*\2\b"
)
_PY_REAL_SLEEP_RE = re.compile(r"\b(?:time\.sleep|asyncio\.sleep)\s*\(\s*\d")
_PY_TEST_RANDOM_RE = re.compile(
    r"(?<![.\w])random\.\w+\s*\(|\b(?:np|numpy)\.random\.|(?<![.\w])secrets\.\w+\s*\("
    r"|\buuid\.uuid[14]\s*\("
)
_PY_ASSERTION_RE = re.compile(
    r"(?<![.\w])assert\b|\bpytest\.(?:raises|warns)\b|\bself\.assert\w+\b|\bassert\w+\s*\("
)
# A pytest/unittest test function opener (test_* def), the block whose body the
# assertion-free check spans.
_PY_TEST_BLOCK_RE = re.compile(r"^[ \t]*(?:async\s+)?def\s+test\w*\s*\(", re.MULTILINE)

# A call expression: NAME( ... . Used to derive the witness's assertion-helper
# vocabulary (assertOk(res), expectUser(u)) so a candidate that wraps its
# asserts in the same helpers is not mis-flagged as assertion-free.
_CALL_TOKEN_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
# Generic call names we never treat as assertion helpers: the test framework's
# own block/lifecycle/setup calls. Including these would make almost any block
# look "asserting" and gut the rule.
_NON_ASSERT_CALL_NAMES = frozenset(
    {
        "it",
        "test",
        "describe",
        "context",
        "specify",
        "example",
        "beforeEach",
        "afterEach",
        "beforeAll",
        "afterAll",
        "before",
        "after",
        "setup",
        "teardown",
        "require",
        "import",
        "console",
        "fn",
        "jest",
        "vi",
        "expect",
        "function",
        "return",
        "if",
        "for",
        "while",
        "switch",
    }
)


def _ts_block_span(content: str, open_paren_idx: int) -> str:
    """Return the brace body of the test block whose opener starts near `idx`.

    Walks forward from the test call to its first `{`, then balances braces to
    find the matching close. Falls back to the rest of the file (capped) if no
    brace body is found, which keeps the assertion scan conservative (a missing
    brace cannot make a genuinely-asserting block look empty).
    """
    brace_start = content.find("{", open_paren_idx)
    if brace_start == -1:
        return content[open_paren_idx : open_paren_idx + 2000]
    depth = 0
    for i in range(brace_start, min(len(content), brace_start + 20000)):
        ch = content[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return content[brace_start : i + 1]
    return content[brace_start : brace_start + 20000]


def _ruby_block_span(lines: list[str], start_idx: int) -> str:
    """Return the `do`/`end` (or `{`/`}`) body of a Ruby test block.

    Tracks indentation off the opener line: the block ends at the first line at
    or below the opener's indent that is an `end` (or a `}` for a brace block).
    Capped so a malformed file cannot run the scan away.
    """
    opener = lines[start_idx]
    base_indent = len(opener) - len(opener.lstrip())
    brace_block = opener.rstrip().endswith("{")
    body: list[str] = [opener]
    for ln in lines[start_idx + 1 : start_idx + 1 + 4000]:
        stripped = ln.strip()
        indent = len(ln) - len(ln.lstrip())
        body.append(ln)
        if brace_block and stripped == "}":
            break
        if not brace_block and stripped == "end" and indent <= base_indent:
            break
    return "\n".join(body)


def _py_block_span(lines: list[str], start_idx: int) -> str:
    """Return the indented body of a Python `def test_*` block.

    The body is the run of lines more-indented than the def opener; it ends at
    the first non-blank line at or below the opener's indent. Capped so a
    malformed file cannot run the scan away.
    """
    opener = lines[start_idx]
    base_indent = len(opener) - len(opener.lstrip())
    body: list[str] = [opener]
    for ln in lines[start_idx + 1 : start_idx + 1 + 4000]:
        if ln.strip():
            indent = len(ln) - len(ln.lstrip())
            if indent <= base_indent:
                break
        body.append(ln)
    return "\n".join(body)


def _assertion_re_for(language: str) -> re.Pattern[str]:
    """The assertion-token regex for a language."""
    if language == "ruby":
        return _RUBY_ASSERTION_RE
    if language == "python":
        return _PY_ASSERTION_RE
    return _TS_ASSERTION_RE


# Call-name prefixes that read as an assertion helper rather than a setup call.
# A witness call like assertUser()/expectOk()/verifyState() is almost certainly
# the team's own matcher wrapper; makeUser()/create()/save() are not.
_ASSERT_HELPER_PREFIXES = ("assert", "expect", "should", "verify", "check", "refute")


def _witness_assert_helpers(witness_content: str, *, language: str) -> set[str]:
    """Derive the call names the witness uses as assertion helpers.

    A team's canonical test often wraps asserts in a helper (assertOk(res),
    expectUser(u)). We collect two sources from the witness: call names that
    share a line with a recognized assertion token, and call names whose own
    spelling reads as an assertion helper (an assert*/expect*/verify* prefix).
    Framework block/lifecycle calls are excluded. The candidate's
    assertion-free check then also passes if it calls one of these, so a
    helper-wrapped assert is not mistaken for no assertion at all.
    """
    if not witness_content:
        return set()
    assert_re = _assertion_re_for(language)
    helpers: set[str] = set()
    for line in witness_content.splitlines():
        line_has_assert = bool(assert_re.search(line))
        for m in _CALL_TOKEN_RE.finditer(line):
            name = m.group(1)
            if name in _NON_ASSERT_CALL_NAMES:
                continue
            if line_has_assert or name.lower().startswith(_ASSERT_HELPER_PREFIXES):
                helpers.add(name)
    return helpers


def _block_asserts(block: str, *, language: str, witness_helpers: set[str]) -> bool:
    """True when a test block contains a recognized assertion or helper call."""
    assert_re = _assertion_re_for(language)
    if assert_re.search(block):
        return True
    if witness_helpers:
        for m in _CALL_TOKEN_RE.finditer(block):
            if m.group(1) in witness_helpers:
                return True
    return False


def _test_quality_violations(
    scan_content: str,
    *,
    language: str,
    witness_content: str | None,
) -> list[Violation]:
    """Advisory test-quality lints for a test/spec-archetype file.

    Operates on the strings/comments-stripped copy so tokens inside descriptions
    or comments do not fire. Every rule is advisory; none is block-eligible.
    The two whole-file rules (unstubbed-network, unfrozen-clock) require a
    witness that uses the stub/freeze token and a candidate that uses none,
    self-calibrating to the team's own style.
    """
    out: list[Violation] = []

    if language == "typescript":
        skipped_re = _TS_SKIPPED_TEST_RE
        sleep_re = _TS_REAL_SLEEP_RE
        random_re = _TS_RANDOM_RE
        random_label = "Math.random"
    elif language == "python":
        skipped_re = _PY_SKIPPED_TEST_RE
        sleep_re = _PY_REAL_SLEEP_RE
        random_re = _PY_TEST_RANDOM_RE
        random_label = "random/secrets/uuid4"
    else:
        skipped_re = _RUBY_SKIPPED_TEST_RE
        sleep_re = _RUBY_REAL_SLEEP_RE
        random_re = _RUBY_RANDOM_RE
        random_label = "rand/SecureRandom"

    if skipped_re.search(scan_content):
        out.append(
            Violation(
                rule="skipped-test",
                expected="an executed test",
                actual="a skipped/pending test",
                severity="info",
                message=(
                    "TEST: this file marks a test skipped or pending; a disabled "
                    "test asserts nothing -- remove the skip or finish the test"
                ),
            )
        )

    tautology = (
        (language == "typescript" and _TS_TAUTOLOGY_RE.search(scan_content))
        or (language == "python" and _PY_TAUTOLOGY_RE.search(scan_content))
        or (language == "ruby" and _RUBY_TAUTOLOGY_RE.search(scan_content))
    )
    if tautology:
        out.append(
            Violation(
                rule="tautological-assertion",
                expected="an assertion about the code under test",
                actual="a self-comparing assertion",
                severity="info",
                message=(
                    "TEST: an assertion compares a literal to itself (e.g. "
                    "expect(true).toBe(true) / assert 1 == 1); it always passes "
                    "and proves nothing"
                ),
            )
        )

    if sleep_re.search(scan_content):
        out.append(
            Violation(
                rule="real-sleep-in-test",
                expected="a fake timer / awaited condition",
                actual="a real sleep",
                severity="info",
                message=(
                    "TEST: a real sleep makes the test slow and flaky -- use fake "
                    "timers or await the condition instead of a fixed delay"
                ),
            )
        )

    if random_re.search(scan_content):
        out.append(
            Violation(
                rule="random-in-test",
                expected="a fixed/seeded value",
                actual=f"{random_label} in a test",
                severity="info",
                message=(
                    f"TEST: {random_label} makes assertions seed-dependent and "
                    "flaky -- use a fixed value or a seeded generator"
                ),
            )
        )

    out.extend(
        _assertion_free_violations(scan_content, language=language, witness_content=witness_content)
    )
    out.extend(
        _witness_gated_setup_violations(
            scan_content, language=language, witness_content=witness_content
        )
    )
    return out


def _assertion_free_violations(
    scan_content: str,
    *,
    language: str,
    witness_content: str | None,
) -> list[Violation]:
    """Flag a test block that sets up state but never asserts.

    Gated on BOTH (a) no recognized assertion token in the block AND (b) no call
    to a helper the witness uses around its own asserts. The helper gate keeps a
    competently helper-wrapped assert (assertOk(res)) from misfiring. Advisory
    only: regex assertion detection still has edge cases this gate cannot cover.
    """
    witness_helpers = _witness_assert_helpers(witness_content or "", language=language)

    blocks: list[str] = []
    if language == "typescript":
        for m in _TS_TEST_BLOCK_RE.finditer(scan_content):
            blocks.append(_ts_block_span(scan_content, m.end()))
    elif language == "python":
        lines = scan_content.splitlines()
        for i, line in enumerate(lines):
            if _PY_TEST_BLOCK_RE.match(line):
                blocks.append(_py_block_span(lines, i))
    else:
        lines = scan_content.splitlines()
        for i, line in enumerate(lines):
            if _RUBY_TEST_BLOCK_RE.match(line):
                blocks.append(_ruby_block_span(lines, i))

    flagged = False
    for block in blocks:
        if not _block_asserts(block, language=language, witness_helpers=witness_helpers):
            flagged = True
            break

    if not flagged:
        return []
    return [
        Violation(
            rule="assertion-free-test",
            expected="at least one assertion per test",
            actual="a test block with no assertion",
            severity="info",
            message=(
                "TEST: a test block sets up state but never asserts; add an "
                "expect/assert (or call the team's assertion helper) so the "
                "test can actually fail"
            ),
        )
    ]


def _witness_gated_setup_violations(
    scan_content: str,
    *,
    language: str,
    witness_content: str | None,
) -> list[Violation]:
    """Whole-file unstubbed-network / unfrozen-clock advisories.

    Each fires only when the witness file uses the relevant stub/freeze token
    and the candidate uses none of them while still touching the real
    network / clock. The witness gate makes this self-calibrating: a repo that
    never stubs the network produces no witness token, so the rule stays silent.
    """
    if not witness_content:
        return []

    def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
        return any(tok in text for tok in tokens)

    def _has_stub(text: str) -> bool:
        return _has_any(text, _NETWORK_STUB_TOKENS) or bool(_NETWORK_STUB_WORD_RE.search(text))

    out: list[Violation] = []

    if (
        _has_stub(witness_content)
        and not _has_stub(scan_content)
        and _has_any(scan_content, _NETWORK_CALL_TOKENS)
    ):
        out.append(
            Violation(
                rule="unstubbed-network",
                expected="a stubbed network (sibling tests stub it)",
                actual="a real network call with no stub",
                severity="info",
                message=(
                    "TEST: sibling tests stub the network but this file makes a "
                    "real request -- stub it (nock/WebMock/msw) to keep the test "
                    "hermetic and fast"
                ),
            )
        )

    if (
        _has_any(witness_content, _CLOCK_FREEZE_TOKENS)
        and not _has_any(scan_content, _CLOCK_FREEZE_TOKENS)
        and _has_any(scan_content, _CLOCK_READ_TOKENS)
    ):
        out.append(
            Violation(
                rule="unfrozen-clock",
                expected="a frozen clock (sibling tests freeze it)",
                actual="a real clock read with no freeze",
                severity="info",
                message=(
                    "TEST: sibling tests freeze the clock but this file reads the "
                    "real time -- freeze it (fake timers/freeze_time) so date "
                    "assertions stay stable"
                ),
            )
        )

    return out


def _blank_string_embedded_imports(content: str) -> str:
    """Blank `import ... from ...` runs that live entirely inside a string
    literal, so a code snippet stored as a string value is not mistaken for a
    real import.

    The import-preference scan runs on RAW content because it needs the literal
    `from "<module>"` specifier (the strings/comments strip blanks it). A real
    import keeps its `import`/`from` keywords in unmasked code; a string-embedded
    fake has the `import` keyword itself sitting inside a quoted run. We compare
    against the strings/comments-stripped copy: an import whose keyword position
    is blanked there lived inside a literal, so we blank that run in the working
    copy while leaving real imports (and their module specifiers) intact.

    Shared by the PreToolUse pre-write scan and the convention scan so both
    enforcement surfaces agree on string-embedded imports.
    """
    stripped = _strip_ts_strings_and_comments(content)
    chars = list(content)
    for m in _TS_IMPORT_FROM_RE.finditer(content):
        start = m.start()
        # The keyword position is blanked in `stripped` only when it lived inside
        # a string/comment. A genuine top-level import keeps its keyword visible.
        if start < len(stripped) and stripped[start] == " " and content[start] != " ":
            for i in range(m.start(), m.end()):
                if i < len(chars) and chars[i] != "\n":
                    chars[i] = " "
    return "".join(chars)


def _import_keyword_is_real(content: str, stripped: str, m: re.Match) -> bool:
    """True when an import-statement match is real code, not a competing import
    quoted inside a docstring / heredoc / string literal.

    The Ruby/Python import-preference scans read module specifiers off RAW content
    (the module name lives inside the require/import string, which the strip would
    blank). To suppress an import sitting inside a string, we check the keyword
    against a length-preserving strings/comments-stripped copy: a genuine
    `import`/`from`/`require` keeps its keyword char visible there; a
    string-embedded one has that char blanked. Both regexes are ``^[ \\t]*``-
    anchored, so we walk past leading indentation to the keyword first.
    """
    i = m.start()
    while i < m.end() and content[i] in " \t":
        i += 1
    return i >= len(stripped) or stripped[i] != " "


def _basename(file_path: str) -> str:
    """Return the trailing path component, tolerating either separator."""
    return file_path.replace("\\", "/").rsplit("/", 1)[-1]


def _directory_dominant_suffix(file_path: str) -> tuple[str, float] | None:
    """The dominant compound-suffix token (and its consistency) among the edited
    file's SAME-LANGUAGE source siblings in its OWN directory, or None when the
    directory is unreadable, too small, or has no suffix clearing the floor.

    A semantic archetype can span directories with different suffix
    sub-conventions -- e.g. Rails ``app/workers/*_worker.rb`` alongside
    ``app/workers/scheduler/*_scheduler.rb``, all one ``worker`` archetype whose
    archetype-wide suffix is ``_worker.rb``. Judging a scheduler file against the
    archetype-wide suffix is a false positive; the file's own directory is the
    correct scope for the suffix convention. Only siblings in the edited file's
    own language group vote -- a ``.rb`` file can never satisfy a ``.py``-dominant
    directory's suffix, so pooling languages would manufacture a guaranteed false
    positive. Filesystem-backed and best-effort: any error (a relative/hypothetical
    path, a missing directory) returns None, so the caller falls back to the
    archetype-wide suffix -- the prior behavior.
    """
    import os
    from collections import Counter

    from chameleon_mcp._thresholds import threshold_int

    try:
        basename = _basename(file_path)
        own_group: tuple[str, ...] | None = None
        for group in (tuple(_TS_EXTENSIONS), tuple(_RUBY_EXTENSIONS), tuple(_PY_EXTENSIONS)):
            if basename.endswith(group):
                own_group = group
                break
        if own_group is None:
            return None
        directory = os.path.dirname(file_path)
        if not directory or not os.path.isdir(directory):
            return None
        suffix_counts: Counter[str] = Counter()
        total = 0
        with os.scandir(directory) as it:
            for entry in it:
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                if not entry.name.endswith(own_group):
                    continue
                stem, suffix = _split_compound_suffix(entry.name)
                if not stem:
                    continue
                total += 1
                if suffix:
                    suffix_counts[suffix] += 1
        if total < threshold_int("FILE_NAMING_DIR_MIN_SAMPLE") or not suffix_counts:
            return None
        dominant, hits = suffix_counts.most_common(1)[0]
        consistency = hits / total
        if consistency >= 0.60:
            return dominant, consistency
        return None
    except Exception:
        return None


def _file_naming_violations(file_path: str, file_naming: dict) -> list[Violation]:
    """Emit a violation when the edited file's basename breaks the archetype's
    dominant casing or compound-suffix token.

    ``file_naming`` is the per-archetype ``{"casing", "casing_consistency",
    "sample_size", optional "suffix", "suffix_consistency"}`` slice derived at
    bootstrap. The check is path-only: a basename whose casing bucket differs
    from the dominant one, or which omits a dominant suffix token, is flagged.
    A basename with no casing signal (``index.ts``, ``.eslintrc.js``) is not
    flagged — it contributed nothing to the convention either.
    """
    expected_casing = file_naming.get("casing")
    if not expected_casing or file_naming.get("casing_consistency", 0) < 0.60:
        return []

    basename = _basename(file_path)
    if not basename:
        return []
    # Only judge files of a profiled language. A Makefile/README/config dropped
    # into a governed cluster carries no source-naming obligation, and judging
    # its casing against a kebab `.service.ts` convention is a pure false
    # positive. Both branches share this gate so they stay consistent.
    if not basename.endswith(
        tuple(_TS_EXTENSIONS) + tuple(_RUBY_EXTENSIONS) + tuple(_PY_EXTENSIONS)
    ):
        return []
    stem, suffix = _split_compound_suffix(basename)
    out: list[Violation] = []

    actual_casing = _classify_casing(stem)
    if actual_casing is not None and actual_casing != expected_casing:
        out.append(
            Violation(
                rule="file-naming-convention-violation",
                expected=expected_casing,
                actual=actual_casing,
                severity="warning",
                message=(
                    f"NAMING: file {basename} uses {actual_casing}; sibling files "
                    f"use {expected_casing} "
                    f"({file_naming.get('casing_consistency', 0):.0%} convention)"
                ),
            )
        )

    expected_suffix = file_naming.get("suffix")
    if (
        expected_suffix
        and file_naming.get("suffix_consistency", 0) >= 0.60
        and suffix != expected_suffix
        # A dot-prefixed config file (``.eslintrc.js``) has an empty stem and
        # carries no naming signal, so it didn't vote in the suffix tally and
        # must not be flagged for "missing" the suffix either.
        and stem
    ):
        # The file's own directory can hold a suffix sub-convention distinct from
        # the archetype-wide one (app/workers/*_worker.rb vs
        # app/workers/scheduler/*_scheduler.rb). Use it ONLY to SUPPRESS the false
        # positive when the file already matches its directory -- never to invent
        # or redirect the suggested suffix. Suppress-only guarantees the suggestion
        # is always the archetype-wide suffix (a convention that cleared the 60%
        # floor across the archetype), so a directory of misnamed files can neither
        # legitimize a typo nor point a new file at a wrong local majority.
        dir_convention = _directory_dominant_suffix(file_path)
        if dir_convention is None or suffix != dir_convention[0]:
            out.append(
                Violation(
                    rule="file-naming-convention-violation",
                    expected=expected_suffix,
                    actual=suffix or "(none)",
                    severity="warning",
                    message=(
                        f"NAMING: file {basename} is missing the {expected_suffix} suffix "
                        f"sibling files use "
                        f"({file_naming.get('suffix_consistency', 0):.0%} convention)"
                    ),
                )
            )

    return out


# Lint-side Ruby declaration captures. Unlike the derivation regexes in
# conventions.py (which require the uppercase start Ruby enforces for class
# names), these are permissive on purpose: the lint must SEE a lowercase class
# name to flag it. `(?!<<)` keeps `class << self` out; operator defs
# (`def ==`) carry no casing to assert and stay excluded by the identifier
# start.
_RUBY_METHOD_DEF_LINT_RE = re.compile(
    r"^[ \t]*def\s+(?:self\s*\.\s*)?([a-zA-Z_]\w*[!?=]?)", re.MULTILINE
)
_RUBY_CLASS_DECL_LINT_RE = re.compile(
    r"^[ \t]*(?:class|module)\s+(?!<<)([A-Za-z_][\w:]*)", re.MULTILINE
)
# Matches a constant assignment at line start. The LHS may be a single constant
# (``CONST = 1``), a namespaced one (``Foo::BAR = 1``), or a multiple assignment
# whose targets are ALL constants (``A, B = 1, 2``). Every segment must be
# uppercase-led, so a mixed/destructuring LHS (``a, B = ...``) or a setter call
# (``Foo.bar = ...``) never matches — a conservative miss beats a false flag on
# the block-eligible naming rule.
_RUBY_CONSTANT_ASSIGN_LINT_RE = re.compile(
    r"^[ \t]*([A-Z]\w*(?:(?:::|\s*,\s*)[A-Z]\w*)*)\s*=[^=~]", re.MULTILINE
)


# Lint-side Python declaration captures — permissive (must SEE a PascalCase
# def or a snake class to flag it), mirroring the Ruby lint captures.
_PY_FUNC_DEF_LINT_RE = re.compile(r"^[ \t]*(?:async\s+)?def\s+([A-Za-z_]\w*)", re.MULTILINE)
# A property-family decorator (a @property/@cached_property/@computed_field, or a
# @x.setter/@x.getter/@x.deleter accessor) makes the def an ATTRIBUTE accessor, not
# a function: its name legitimately follows attribute/constant casing (a pydantic
# @computed_field named SQLALCHEMY_DATABASE_URI), so the function snake_case rule
# must not flag it.
_PY_PROPERTY_DECORATOR_RE = re.compile(
    r"^[ \t]*@(?:[\w.]+\.)?(?:property|cached_property|computed_field)\b"
    r"|^[ \t]*@[\w.]+\.(?:setter|getter|deleter)\b",
    re.MULTILINE,
)
_PY_DECORATOR_LINE_RE = re.compile(r"^[ \t]*@")


def _py_def_is_property_accessor(scan_content: str, def_start: int) -> bool:
    """Whether the ``def`` at ``def_start`` carries a property-family decorator.

    The def regex anchors at the start of its line, so the text before
    ``def_start`` is exactly the whole lines above it. Walks those decorator lines
    upward (a def may stack several, e.g. ``@computed_field`` then ``@property``)
    and stops at the first non-decorator, non-blank line, so an unrelated
    ``@property`` elsewhere in the file cannot exempt this def.
    """
    for line in reversed(scan_content[:def_start].splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue  # blank or a comment between a decorator and its def is allowed
        if _PY_DECORATOR_LINE_RE.match(line):
            if _PY_PROPERTY_DECORATOR_RE.match(line):
                return True
            continue  # another decorator (@abstractmethod, @override) -- keep looking
        break  # a non-decorator, non-blank, non-comment line -- decorator block ended
    return False


_PY_CLASS_DECL_LINT_RE = re.compile(r"^[ \t]*class\s+([A-Za-z_]\w*)", re.MULTILINE)
# Captures the base list too (group 3, None when bare) for the inheritance check.
# The optional `\[...\]` after the name is a PEP 695 (3.12+) type-parameter list
# (`class Foo[T](Base):`); without it a generic class would silently skip the lint.
_PY_CLASS_BASES_LINT_RE = re.compile(
    r"^([ \t]*)class\s+([A-Za-z_]\w*)(?:\s*\[[^\]]*\])?\s*(?:\(([^)]*)\))?\s*:", re.MULTILINE
)


# Framework-mandated camelCase methods (unittest / Django TestCase hooks) a class
# MUST name exactly as the framework calls them -- they cannot be renamed to
# snake_case, so the PEP 8 method-casing rule exempts them rather than flagging an
# unfixable "violation".
_PY_FRAMEWORK_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "setUp",
        "tearDown",
        "setUpClass",
        "tearDownClass",
        "setUpModule",
        "tearDownModule",
        "setUpTestData",
        "asyncSetUp",
        "asyncTearDown",
        "runTest",
    }
)


def _python_naming_violations(scan_content: str, naming: dict) -> list[Violation]:
    """In-source Python (PEP 8) naming checks against the derived casing.

    Functions/methods must be snake_case, classes PascalCase — each fires only
    when the profile derived that canonical casing at >= 0.60 consistency. Reuses
    the Ruby classifiers (the rules coincide) so what is measured is what is
    checked. Dunder/underscore-prefixed names (``__init__``, ``_helper``) are not
    flagged: they are valid PEP 8 regardless of the snake rule.
    """
    from chameleon_mcp.conventions import (
        _classify_ruby_class_casing,
        _classify_ruby_method_casing,
    )

    out: list[Violation] = []
    method_entry = naming.get("method_casing") or {}
    if method_entry.get("pattern") == "snake_case" and method_entry.get("consistency", 0) >= 0.60:
        for m in _PY_FUNC_DEF_LINT_RE.finditer(scan_content):
            name = m.group(1)
            if name.startswith("_"):
                continue
            if name in _PY_FRAMEWORK_METHOD_NAMES:
                continue
            if _py_def_is_property_accessor(scan_content, m.start()):
                continue
            if _classify_ruby_method_casing(name) != "snake_case":
                out.append(
                    Violation(
                        rule="naming-convention-violation",
                        expected="snake_case",
                        actual=name,
                        severity="warning",
                        message=(
                            f"NAMING: function {name} should use snake_case "
                            f"({method_entry['consistency']:.0%} convention)"
                        ),
                    )
                )
    class_entry = naming.get("class_casing") or {}
    if class_entry.get("pattern") == "PascalCase" and class_entry.get("consistency", 0) >= 0.60:
        for m in _PY_CLASS_DECL_LINT_RE.finditer(scan_content):
            name = m.group(1)
            if name.startswith("_"):
                continue
            if _classify_ruby_class_casing(name) != "PascalCase":
                out.append(
                    Violation(
                        rule="naming-convention-violation",
                        expected="PascalCase",
                        actual=name,
                        severity="warning",
                        message=(
                            f"NAMING: class {name} should use PascalCase "
                            f"({class_entry['consistency']:.0%} convention)"
                        ),
                    )
                )
    return out


def _ruby_naming_violations(scan_content: str, naming: dict) -> list[Violation]:
    """In-source Ruby naming checks against the derived casing conventions.

    Each dimension fires only when the profile derived the canonical casing
    for it (snake_case methods, PascalCase classes, SCREAMING_SNAKE constants)
    at >= 0.60 consistency, mirroring the TS interface-prefix gate. The
    classifiers are shared with the derivation so what is measured is exactly
    what is checked. PascalCase constant assignments are never flagged — a
    `Result = Struct.new` class alias is legitimate in any Ruby repo.
    """
    from chameleon_mcp.conventions import (
        _classify_ruby_class_casing,
        _classify_ruby_constant_casing,
        _classify_ruby_method_casing,
    )

    out: list[Violation] = []

    method_entry = naming.get("method_casing") or {}
    if method_entry.get("pattern") == "snake_case" and method_entry.get("consistency", 0) >= 0.60:
        for m in _RUBY_METHOD_DEF_LINT_RE.finditer(scan_content):
            name = m.group(1)
            if _classify_ruby_method_casing(name) != "snake_case":
                out.append(
                    Violation(
                        rule="naming-convention-violation",
                        expected="snake_case",
                        actual=name,
                        severity="warning",
                        message=(
                            f"NAMING: method {name} should use snake_case "
                            f"({method_entry['consistency']:.0%} convention)"
                        ),
                    )
                )

    class_entry = naming.get("class_casing") or {}
    if class_entry.get("pattern") == "PascalCase" and class_entry.get("consistency", 0) >= 0.60:
        for m in _RUBY_CLASS_DECL_LINT_RE.finditer(scan_content):
            name = m.group(1)
            if _classify_ruby_class_casing(name) != "PascalCase":
                out.append(
                    Violation(
                        rule="naming-convention-violation",
                        expected="PascalCase",
                        actual=name,
                        severity="warning",
                        message=(
                            f"NAMING: class {name} should use PascalCase "
                            f"({class_entry['consistency']:.0%} convention)"
                        ),
                    )
                )

    constant_entry = naming.get("constant_casing") or {}
    if (
        constant_entry.get("pattern") == "SCREAMING_SNAKE_CASE"
        and constant_entry.get("consistency", 0) >= 0.60
    ):
        for m in _RUBY_CONSTANT_ASSIGN_LINT_RE.finditer(scan_content):
            # One match may carry several constants (multiple assignment) and
            # namespaced names; classify each defined constant by its trailing
            # segment (``Foo::BAR`` defines ``BAR``).
            for raw_name in m.group(1).split(","):
                name = raw_name.strip().rsplit("::", 1)[-1]
                if not name:
                    continue
                if _classify_ruby_constant_casing(name) == "other":
                    out.append(
                        Violation(
                            rule="naming-convention-violation",
                            expected="SCREAMING_SNAKE_CASE",
                            actual=name,
                            severity="warning",
                            message=(
                                f"NAMING: constant {name} should use SCREAMING_SNAKE_CASE "
                                f"({constant_entry['consistency']:.0%} convention)"
                            ),
                        )
                    )

    return out


# The Rails-convention application root base classes. Each inherits a framework
# root (ActionController::API, ActiveRecord::Base, ...) rather than an
# archetype's dominant base, so the inheritance-convention check must exempt
# them: a profile whose controllers descend from Api::V1::BaseController would
# otherwise tell ApplicationController to inherit a class that descends FROM it.
_RAILS_APP_ROOT_BASES: frozenset[str] = frozenset(
    {
        "ApplicationController",
        "ApplicationRecord",
        "ApplicationJob",
        "ApplicationMailer",
    }
)


def lint_conventions(
    content: str,
    conventions: dict | None,
    *,
    language: str | None = None,
    file_path: str | None = None,
    archetype_name: str | None = None,
    witness_content: str | None = None,
) -> list[Violation]:
    """Check file content against convention rules.

    ``file_path`` enables the file-naming-convention check, which compares the
    edited file's basename against the archetype's dominant casing/suffix. It is
    optional so callers that only have content still run every other rule.

    ``archetype_name`` enables the test-quality pass. It runs only when the name
    marks the file as a test (starts with ``test`` / ``spec``); the default of
    ``None`` leaves the pass inert so existing callers are unaffected.
    ``witness_content`` is the archetype's canonical test, used to self-calibrate
    the assertion-helper, stub, and freeze checks to the team's own style; when
    absent those gated rules degrade to silent.
    """
    if not conventions:
        return []

    # Inline-ignore directives gate whole checks here; the violations these
    # scans emit carry no line numbers, so the file-wide directive scope is
    # the one that applies. Parsed by the shared violation_class parser so a
    # directive embedded in a string literal, or prose that merely mentions
    # one, does not switch a check off.
    from chameleon_mcp.violation_class import ignored_rules as _parse_ignored_rules

    ignored_rules: set[str] = (
        _parse_ignored_rules(content, file_path=file_path, language=language) or set()
    )

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
    elif language == "python":
        # A `class Foo(Bar):` or `def fooBar` inside a docstring is prose, not a
        # declaration; strip strings + comments so the naming/inheritance scans
        # don't false-match it. Length-preserving, so line numbers stay truthful.
        scan_content = _strip_python_strings_and_comments(content)
    else:
        scan_content = content

    # The import-preference scan needs raw content (it reads the `from "<module>"`
    # / `require "<module>"` specifier, which the strip blanks), but a competing
    # import sitting inside a string literal is a code snippet, not a real import.
    # TS blanks those runs up front; Ruby/Python instead filter each match by
    # _import_keyword_is_real against `scan_content` (their module name lives
    # inside the string the strip blanks, so the run can't be pre-blanked). All
    # three converge across PreToolUse, PostToolUse / lint_file, and calibration.
    if language == "typescript":
        import_scan_content = _blank_string_embedded_imports(content)
    else:
        import_scan_content = content

    violations: list[Violation] = []

    # Accept both the short directive token and the full emitted rule name. The
    # PreToolUse deny message tells users to add the rule name
    # (`import-preference-violation`); the short `import-preference` token
    # predates it. Honor either so the advertised escape hatch actually clears
    # the scan.
    if not ignored_rules & {"import-preference", "import-preference-violation"}:
        # Resolve the import specifiers once so both the preferred-present skip
        # guard and the banned-import scan compare whole module paths, not raw
        # substrings. A `\b` search over the statement text treats `/` and the
        # quotes as word boundaries, so a banned name that is a trailing segment
        # of the preferred scoped package (`react-query` inside
        # `@tanstack/react-query`) would both false-flag the preferred import and
        # defeat the preferred-present guard. Extraction is language-gated: the
        # ES regex on Ruby content matched pasted ES snippets while real
        # `require` statements and constant references never registered, so the
        # rule was inert on exactly the language it was taught for.
        if language == "ruby":
            import_specs = [
                m.group(1)
                for m in _RUBY_REQUIRE_RE.finditer(content)
                if _import_keyword_is_real(content, scan_content, m)
            ]
        elif language == "python":
            import_specs = [
                m.group(1) or m.group(2)
                for m in _PY_IMPORT_RE.finditer(content)
                if _import_keyword_is_real(content, scan_content, m)
            ]
        else:
            import_specs = [m.group(1) for m in _TS_IMPORT_FROM_RE.finditer(import_scan_content)]
        for competing in (conventions.get("imports") or {}).get("competing", []):
            if not isinstance(competing, dict):
                continue
            over_mod = competing.get("over")
            preferred_mod = competing.get("preferred")
            if not over_mod or not preferred_mod:
                continue
            if language == "ruby":
                over_used = _ruby_module_in_use(over_mod, import_specs, scan_content)
                preferred_used = _ruby_module_in_use(preferred_mod, import_specs, scan_content)
            elif language == "python":
                over_used = _python_module_in_use(over_mod, import_specs)
                preferred_used = _python_module_in_use(preferred_mod, import_specs)
            else:
                over_used = any(_module_specifier_matches(s, over_mod) for s in import_specs)
                preferred_used = any(
                    _module_specifier_matches(s, preferred_mod) for s in import_specs
                )
            if preferred_used:
                continue
            if over_used:
                violations.append(
                    Violation(
                        rule="import-preference-violation",
                        expected=preferred_mod,
                        actual=over_mod,
                        severity="warning",
                        message=f"IMPORT: {over_mod} imported - replace with {preferred_mod} (all usages)",
                    )
                )

    if language == "typescript" and not (
        ignored_rules & {"naming-convention", "naming-convention-violation"}
    ):
        naming = conventions.get("naming") or {}
        prefix_entry = naming.get("interface_prefix")
        if prefix_entry and prefix_entry.get("consistency", 0) >= 0.60:
            expected_prefix = prefix_entry["pattern"]
            ambient_spans = _ts_ambient_block_spans(scan_content)
            for m in _TS_INTERFACE_DECL_RE.finditer(scan_content):
                # Interfaces inside `declare global`/`declare module` augment
                # external types and cannot be renamed -- exempt from I-prefix.
                if any(s <= m.start() <= e for s, e in ambient_spans):
                    continue
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

    if language == "ruby" and not (
        ignored_rules & {"naming-convention", "naming-convention-violation"}
    ):
        violations.extend(_ruby_naming_violations(scan_content, conventions.get("naming") or {}))

    if language == "python" and not (
        ignored_rules & {"naming-convention", "naming-convention-violation"}
    ):
        violations.extend(_python_naming_violations(scan_content, conventions.get("naming") or {}))

    if language == "typescript" and "then-without-catch" not in ignored_rules:
        violations.extend(_then_without_catch_violations(scan_content))

    # File-naming is path-only and language-agnostic: it compares the edited
    # file's basename casing/suffix against the archetype's dominant pattern.
    # Skipped when the caller passed no file_path (content-only lint) or the
    # archetype has no derived file-naming convention.
    if file_path and not (
        ignored_rules & {"file-naming-convention", "file-naming-convention-violation"}
    ):
        naming = conventions.get("naming") or {}
        fn = naming.get("file_naming")
        if isinstance(fn, dict):
            violations.extend(_file_naming_violations(file_path, fn))

    if language == "ruby" and not (
        ignored_rules & {"inheritance-convention", "inheritance-convention-violation"}
    ):
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
            # A class declared inside its module names the base in namespace-
            # relative short form (`< BaseController`) while the stored bases are
            # fully qualified (`Api::V1::BaseController`). Accept a match on the
            # unqualified tail so the idiomatic short form is not mis-flagged as
            # a wrong base (a false positive that, if a repo's calibration sample
            # happened to be all-fully-qualified, would harden into a block on
            # conforming code).
            known_base_tails = {b.rsplit("::", 1)[-1] for b in known_bases}
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
                # A class that IS one of the archetype's established bases defines
                # the convention boundary: it inherits the framework root
                # (ActionController::API), intentionally absent from known_bases
                # because the root appears once. Flagging it — telling
                # BaseController to inherit Api::V1::BaseController, i.e. itself —
                # is a false positive that inflates the rule's calibration fp_rate
                # and erodes trust. Skip any class whose own name (or tail) is a
                # known base.
                class_tail = class_name.rsplit("::", 1)[-1]
                if (
                    class_name in known_bases
                    or class_tail in known_base_tails
                    or class_tail in _RAILS_APP_ROOT_BASES
                ):
                    continue
                # Inheriting DIRECTLY from a Rails framework root base
                # (`ApplicationController`, `ApplicationRecord`, ...) is the
                # idiomatic top-level convention, not a deviation. A large
                # controller archetype folds api/admin/settings namespaces into one
                # cluster whose dominant base is some `*::BaseController`; a real,
                # non-trivial subgroup of top-level web controllers correctly
                # extends `ApplicationController` and must not be steered onto an
                # API base (which carries API-specific auth/rendering). The
                # framework root is the shared ancestor of every namespace base, so
                # extending it is always legitimate.
                superclass_tail = superclass.rsplit("::", 1)[-1] if superclass else None
                if superclass_tail in _RAILS_APP_ROOT_BASES:
                    continue
                # A superclass namespaced under a module unrelated to the
                # archetype's own base family and to this class's own namespace is a
                # deliberate third-party/gem base (a Lookbook preview controller
                # extending Lookbook::PreviewController, a Devise/Doorkeeper mount):
                # a one-off integration, not a deviation onto the wrong in-house
                # base. Steering it to the dominant base would break the gem.
                if superclass and "::" in superclass:
                    _sc_root = superclass.split("::", 1)[0]
                    _known_roots = {b.split("::", 1)[0] for b in known_bases}
                    _known_roots |= {t.split("::", 1)[0] for t in known_base_tails}
                    if dominant_base:
                        _known_roots.add(dominant_base.split("::", 1)[0])
                    _class_root = class_name.split("::", 1)[0] if "::" in class_name else None
                    if _sc_root not in _known_roots and _sc_root != _class_root:
                        continue
                # A superclass of the SAME role as the dominant base (`FooController`
                # extending a project `Admin::SettingsController`) is intra-role reuse
                # of a sibling/intermediate that itself roots at the archetype's base,
                # not a wrong-base deviation. Only a CROSS-role base (a controller
                # extending a Model) is a real deviation.
                if superclass is not None and _superclass_shares_base_role(
                    superclass, dominant_base
                ):
                    continue
                # A base-less `class Foo` (no `< Base`) is a legitimate standalone
                # class (middleware, a config module, a plain service), not a missed
                # inheritance -- the Python check (`_python_inheritance_violations`)
                # already exempts it, so align Ruby to it. Only a class that DOES
                # extend something outside the archetype's known bases is a deviation.
                if superclass is not None and (
                    superclass not in known_bases
                    and superclass.rsplit("::", 1)[-1] not in known_base_tails
                ):
                    # Suggest the known base sharing the deepest namespace with
                    # the class, not blindly the repo-wide dominant: an
                    # Api::V1::Admin:: controller belongs on the Admin base —
                    # steering it to the parent namespace's base would route
                    # around namespace-scoped auth.
                    suggested = _namespace_local_base(class_name, known_bases, dominant_base)
                    if suggested == dominant_base:
                        detail = f"({inheritance['frequency']:.0%} convention)"
                    else:
                        detail = f"(namespace convention; repo-wide dominant: {dominant_base})"
                    violations.append(
                        Violation(
                            rule="inheritance-convention-violation",
                            expected=suggested,
                            actual=superclass or "none",
                            severity="warning",
                            message=f"INHERITANCE: class {class_name} should inherit {suggested} {detail}",
                        )
                    )

    if language == "python" and not (
        ignored_rules & {"inheritance-convention", "inheritance-convention-violation"}
    ):
        violations.extend(
            _python_inheritance_violations(scan_content, conventions.get("inheritance") or {})
        )

    # Post-edit consumer of the class_contract artifact: a class extending the
    # archetype's dominant base that omits a >= 95%-frequency required method (an
    # ApplicationJob without `perform`). The pre-edit block already advertises this
    # contract; before this check nothing verified it post-edit.
    if "missing-required-method" not in ignored_rules:
        violations.extend(
            _required_method_violations(
                scan_content, conventions.get("class_contract") or {}, language
            )
        )

    if "required-guard-convention" not in ignored_rules:
        if language == "ruby":
            violations.extend(_required_guard_violations(scan_content, conventions))
        elif language == "python":
            violations.extend(_python_guard_violations(scan_content, conventions))

    # Test-quality lints, scoped to test/spec archetypes so the rules only judge
    # files that are actually tests. The witness for the assertion-helper /
    # stub / freeze gates is also stripped of strings & comments so its token
    # vocabulary lines up with the candidate scan.
    if (
        archetype_name
        and archetype_name.startswith(("test", "spec"))
        and language in ("typescript", "ruby", "python")
        and "test-quality" not in ignored_rules
    ):
        if language == "ruby":
            witness_scan = (
                _strip_ruby_strings_and_comments(witness_content) if witness_content else None
            )
        elif language == "python":
            witness_scan = (
                _strip_python_strings_and_comments(witness_content) if witness_content else None
            )
        else:
            witness_scan = (
                _strip_ts_strings_and_comments(witness_content) if witness_content else None
            )
        violations.extend(
            _test_quality_violations(
                scan_content,
                language=language,
                witness_content=witness_scan,
            )
        )

    return violations


def _required_guard_violations(scan_content: str, conventions: dict) -> list[Violation]:
    """Advisory hint when a controller lacks an authz guard its archetype expects.

    Rails controllers usually authorize via a blanket ``before_action`` callback;
    a new controller that forgets it (while keeping some other callback) matches
    its archetype's coarse shape fine, so the gap is invisible to the structural
    lint. This surfaces it as an ``info`` so the model confirms authorization is
    handled.

    Strictly advisory: authz is routinely inherited from a base controller, so a
    clean controller can legitimately omit the line. When the controller extends
    one of the archetype's known bases (the base most likely carries the guard)
    the hint is suppressed; a guard removed here via ``skip_before_action`` is
    likewise treated as a deliberate, legitimate absence and not flagged.
    """
    guards = conventions.get("required_guards") or {}
    if not isinstance(guards, dict):
        return []
    required = guards.get("required_guards") or []
    if not isinstance(required, list) or not required:
        return []

    known_bases = {b for b in (guards.get("known_bases") or ()) if isinstance(b, str)}
    if known_bases:
        for m in re.finditer(r"^[ \t]*class\s+[\w:]+\s*<\s*([\w:]+)", scan_content, re.MULTILINE):
            if m.group(1) in known_bases:
                # Extends a base the archetype establishes -- authz is inherited.
                return []

    present: set[str] = set()
    skipped: set[str] = set()
    for m in _RUBY_BEFORE_ACTION_LINT_RE.finditer(scan_content):
        call, symbol, rest = m.group(1), m.group(2), m.group(3)
        if call == "skip_before_action":
            skipped.add(symbol)
        elif not _RUBY_GUARD_SCOPE_LINT_RE.search(rest):
            # Only a blanket callback satisfies the requirement; a scoped guard
            # runs on a subset of actions and leaves the rest unguarded.
            present.add(symbol)

    out: list[Violation] = []
    for guard in required:
        if not isinstance(guard, str):
            continue
        if guard in present or guard in skipped:
            continue
        out.append(
            Violation(
                rule="required-guard-convention",
                expected=guard,
                actual="none",
                severity="info",
                message=(
                    f"AUTHZ: controllers in this archetype usually call "
                    f"before_action :{guard}; this file does not -- confirm "
                    f"authorization is inherited or intentionally skipped"
                ),
            )
        )
    return out


# Python authz-decision signals at lint time (presence-semantics, read off the
# strings/comments-stripped scan so a mention in a docstring does not satisfy).
_PY_AUTHZ_ATTR_RE = re.compile(
    r"^[ \t]*(?:permission_classes|authentication_classes)\s*[:=]", re.MULTILINE
)
_PY_AUTHZ_DECORATOR_RE = re.compile(
    r"@\s*(?:[\w.]+\.)?(?:login_required|permission_required|user_passes_test|"
    r"staff_member_required)\b"
)
# Project authz decorators AST analysis cannot enumerate: a repo defines its own
# `@allow_permission(...)`, `@require_permission`, `@authorize`, `@has_access`
# per action. A decorator whose name carries an authz token IS an in-file access
# decision and must satisfy the convention, or a fully-guarded DRF viewset (every
# action decorated) false-fires "declares none". Loose by design, matching the
# base-hint philosophy: over-recognizing an authz decorator only silences the
# advisory (the safe direction), it never blocks.
_PY_AUTHZ_DECORATOR_HINT_RE = re.compile(
    r"@\s*(?:[\w.]+\.)?\w*(?:permission|allow|authoriz|authent|login|access|"
    r"require_role|require_scope|require_auth|has_perm|guard)\w*",
    re.IGNORECASE,
)
# The optional `\[...\]` after the name is a PEP 695 (3.12+) type-parameter list
# (`class Foo[T](Base):`); without it a generic view skips the mixin-base check and
# a properly-guarded view is falsely flagged. Mirrors _PY_CLASS_BASES_LINT_RE.
_PY_CLASS_DEF_BASES_RE = re.compile(
    r"^[ \t]*class\s+\w+(?:\s*\[[^\]]*\])?\s*\(([^)]*)\)", re.MULTILINE
)
_PY_CLASS_OR_DEF_RE = re.compile(r"^[ \t]*(?:class|def|async\s+def)\s+\w+", re.MULTILINE)
_PY_AUTHZ_MIXIN_TAILS = frozenset(
    {"LoginRequiredMixin", "PermissionRequiredMixin", "UserPassesTestMixin"}
)
# A cohort's known bases include its GENERIC dominant base (DRF's APIView, which
# every view extends and which carries no authz), so a known base only counts as
# authz-inheritance when its NAME indicates authz. Loose on purpose: over-
# suppressing misses an unguarded view (a false negative -- silence), which is
# the safe direction vs flagging an intentionally-guarded view.
_PY_AUTHZ_BASE_HINT_RE = re.compile(r"(?i)(auth|login|permission|secure|protected|restricted)")
_PY_IDENT_RE = re.compile(r"[A-Za-z_][\w.]*")


def _python_guard_violations(scan_content: str, conventions: dict) -> list[Violation]:
    """Advisory hint when a Python view omits the cohort's authz decision.

    The Python analog of ``_required_guard_violations``. Fires only when the
    archetype's ``required_guards`` carries ``authz_required`` (the cohort
    conventionally restricts access). PRESENCE of any authz decision satisfies
    it: a ``permission_classes`` / ``authentication_classes`` assignment (any
    value, including an explicit ``AllowAny``), a ``@login_required`` /
    ``@permission_required`` decorator, an authz-mixin base, or an authz-named
    cohort base (a project base that itself carries auth). The cohort's GENERIC
    dominant base (DRF's APIView) does NOT satisfy it -- it carries no authz, so
    a view extending only it with no permission_classes is the outlier this
    flags. Emits at most one advisory ``info`` per file, never block-eligible.
    """
    guards = conventions.get("required_guards") or {}
    if not isinstance(guards, dict) or not guards.get("authz_required"):
        return []
    # A real authz decision anywhere in the file satisfies the convention: a
    # permission_classes assignment, a Django-builtin authz decorator, or a
    # project authz decorator recognized by its name (`@allow_permission`, etc.).
    if (
        _PY_AUTHZ_ATTR_RE.search(scan_content)
        or _PY_AUTHZ_DECORATOR_RE.search(scan_content)
        or _PY_AUTHZ_DECORATOR_HINT_RE.search(scan_content)
    ):
        return []
    # Authz mixins always count; a known cohort base counts only when its name
    # indicates authz (the generic dominant base does not).
    accepted_bases = set(_PY_AUTHZ_MIXIN_TAILS)
    for b in guards.get("known_bases") or ():
        if isinstance(b, str):
            tail = b.rsplit(".", 1)[-1]
            if _PY_AUTHZ_BASE_HINT_RE.search(tail):
                accepted_bases.add(tail)
    has_class = False
    for m in _PY_CLASS_DEF_BASES_RE.finditer(scan_content):
        has_class = True
        base_tails = {b.rsplit(".", 1)[-1] for b in _PY_IDENT_RE.findall(m.group(1))}
        if base_tails & accepted_bases:
            return []
    # No authz signal. Only advise when the file actually declares a view
    # (a class or def); an unrelated mis-bucketed file is left alone.
    if not has_class and not _PY_CLASS_OR_DEF_RE.search(scan_content):
        return []
    return [
        Violation(
            rule="required-guard-convention",
            expected="permission_classes / @login_required / an authz base",
            actual="none",
            severity="info",
            message=(
                "AUTHZ: views in this archetype usually restrict access "
                "(permission_classes, @login_required, or a LoginRequiredMixin "
                "base); this file declares none -- confirm access is intended to "
                "be open or is inherited from a base view"
            ),
        )
    ]


def _split_top_level(s: str) -> list[str]:
    """Split on commas that are NOT inside a bracketed group.

    A generic base carries its own commas (``Generic[T, U]``, ``Dict[str, int]``)
    and a call/param list nests parens; a naive ``str.split(",")`` fractures
    those. Track ``([{`` depth so only top-level commas separate items. Shared by
    the Python base-list parse here and hook_helper's signature-param split.
    """
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _py_positional_bases(bases_raw: str | None) -> list[str]:
    """Positional base names from a class's base list, dropping keyword args
    (``metaclass=...``), unpackings (``*bases``), and the universal ``object``.

    A subscripted generic base (``BaseRepository[Shipment]``, ``Generic[T]``) is
    normalized to its own name via ``_strip_type_params`` -- the SAME strip the
    derivation applies before counting the dominant base, so the lint compares
    like against like. Without it a typed cohort's ``BaseRepository[Shipment]``
    never matched the derived ``BaseRepository`` and the inheritance rule
    false-positived on every generic class on every edit.
    """
    out: list[str] = []
    if bases_raw:
        for part in _split_top_level(bases_raw):
            part = part.strip()
            if not part or "=" in part or part.startswith("*"):
                continue
            part = _strip_type_params(part)
            if not part or part in ("object", "builtins.object"):
                continue
            out.append(part)
    return out


def _python_inheritance_violations(scan_content: str, inheritance: dict) -> list[Violation]:
    """Advisory hint when a Python class inherits a base outside the archetype's.

    Mirrors the Ruby inheritance check: fires only when a dominant base clears
    the 60% convention floor. A class with NO positional base is left alone -- a
    plain ``class Foo:`` is valid Python, not a missed inheritance -- so only a
    class inheriting something OTHER than an established base is flagged. A class
    that IS one of the known bases (it defines the convention boundary, e.g. a
    project's own ``BaseModel``) is exempt. Bases are matched on the full dotted
    name or its tail, so ``models.Model`` accepts a bare ``Model`` import.
    """
    dominant_base = inheritance.get("dominant_base")
    if not dominant_base or inheritance.get("frequency", 0) < 0.60:
        return []
    known_bases = set(inheritance.get("known_bases") or ())
    known_bases.add(dominant_base)
    known_tails = {b.rsplit(".", 1)[-1] for b in known_bases}
    # The dominant base's MODULE family (`serializers` in `serializers.Model
    # Serializer`). Other bases from the same framework module (`serializers.
    # RelatedField`, `serializers.Serializer`, `models.Manager` alongside
    # `models.Model`) are legitimate variants of the same convention, not
    # deviations -- the Python analog of the Ruby `*BaseController` namespace
    # family. Only set when the dominant base is module-qualified, so a bare-name
    # dominant (`BaseModel`) never matches everything.
    dominant_module = dominant_base.rsplit(".", 1)[0] if "." in dominant_base else None

    # Every top-level class DEFINED in this file. Extending a same-file peer is
    # legitimate local composition (textbook DRF: an `Admin`/`ReadOnly` variant
    # extends its base serializer, which itself roots at `serializers.Model
    # Serializer`); the deviation, if any, surfaces at the peer that directly
    # extends a non-known base, not at every descendant. Without this, a
    # single-inheritance-from-a-sibling chain is flagged at each link even though
    # the whole chain is compliant.
    local_classes = {
        m.group(2) for m in _PY_CLASS_BASES_LINT_RE.finditer(scan_content) if len(m.group(1)) == 0
    }

    # A base >= 2 top-level classes in THIS file inherit is a deliberate shared
    # base for the file (a DRF `FlexFieldsModelSerializer` that 6 sibling
    # serializers extend), even when derivation -- which counts a base's support
    # per-FILE, so 6 classes in one file score just 1 -- never admitted it to
    # known_bases. Exempt it like a same-file peer so a cohesive module of
    # same-based classes is not flagged en masse; a lone off-base class
    # (`WidgetSerializer(ExternalThing)`, count 1) still fires.
    _base_counts: dict[str, int] = {}
    for m in _PY_CLASS_BASES_LINT_RE.finditer(scan_content):
        if len(m.group(1)) != 0:
            continue
        for b in _py_positional_bases(m.group(3)):
            _base_counts[b] = _base_counts.get(b, 0) + 1
    file_shared_bases = {b for b, c in _base_counts.items() if c >= 2}
    file_shared_tails = {b.rsplit(".", 1)[-1] for b in file_shared_bases}

    out: list[Violation] = []
    for m in _PY_CLASS_BASES_LINT_RE.finditer(scan_content):
        indent = len(m.group(1))
        class_name = m.group(2)
        bases_raw = m.group(3)
        # Only a top-level class carries the archetype's convention. A column-0
        # check is exact: a function-nested helper, an `if TYPE_CHECKING:` class,
        # and a nested class are all indented and correctly skipped. (A running
        # minimum mis-fires when the first class in the file is itself nested.)
        if indent != 0:
            continue
        if class_name in known_bases or class_name in known_tails:
            continue
        # ``class Foo(object)`` is identical to ``class Foo`` in Python 3, which
        # the no-base branch below already leaves alone, so the shared parser drops
        # the universal ``object`` too (the extension-point wrapper false positive).
        bases = _py_positional_bases(bases_raw)
        if not bases:
            continue
        # A convention base ANYWHERE in the base list (known/local/module family)
        # means the class inherits the convention -- exempt.
        if any(
            b in known_bases
            or b.rsplit(".", 1)[-1] in known_tails
            or b in local_classes
            or (dominant_module is not None and "." in b and b.rsplit(".", 1)[0] == dominant_module)
            for b in bases
        ):
            continue
        # Same-role reuse: a serializer extending another *Serializer (its primary
        # base) is intra-role reuse of a sibling/intermediate that itself roots at the
        # archetype's base, not a cross-role deviation. Mirrors the Ruby role exemption.
        if _superclass_shares_base_role(bases[0], dominant_base):
            continue
        # An enum is a different KIND of class, not a deviation from the
        # archetype's model/serializer base: a StrEnum co-located in a pydantic
        # schema module is idiomatic and cannot meaningfully inherit BaseModel.
        if any(
            b.rsplit(".", 1)[-1] in ("Enum", "StrEnum", "IntEnum", "IntFlag", "Flag", "TextChoices")
            for b in bases
        ):
            continue
        # The file-shared exemption is PRIMARY-base only: a class IS one of the
        # file's shared types when its FIRST base is the shared one. A foreign
        # primary base with a merely-shared SECONDARY mixin (`class W(ExternalThing,
        # TimestampMixin)`) is still a deviation on the primary and must flag.
        primary = bases[0]
        if primary in file_shared_bases or primary.rsplit(".", 1)[-1] in file_shared_tails:
            continue
        out.append(
            Violation(
                rule="inheritance-convention-violation",
                expected=dominant_base,
                actual=", ".join(bases),
                severity="warning",
                message=(
                    f"INHERITANCE: class {class_name} should inherit "
                    f"{dominant_base} ({inheritance['frequency']:.0%} convention)"
                ),
            )
        )
    return out


# A required method is enforced only when EVERY cohort member defines it (derived
# frequency exactly 1.0), and only for an archetype with a real derivation sample.
# A genuine abstract-method contract is 1.0 by construction (the base raises
# NotImplementedError, so every subclass implements it -- perform, call, validate,
# render, up/down). A method in the 0.95-0.99 band is instead a commonly-overridden
# method WITH a base default (a SystemCheck#show_error at 0.96) or one some members
# inherit via a mixin -- flagging it is a false positive the file-level check cannot
# see through. The variance that drags frequency below 1.0 IS the mixin/inheritance
# signal, so a 1.0 gate is self-protecting (measured ~0 FP at 1.0, ~91% FP below it).
_CONTRACT_METHOD_MIN_FREQUENCY = 1.0
_CONTRACT_METHOD_MIN_SAMPLE = 8
_RUBY_TOPLEVEL_CLASS_RE = re.compile(r"^[ \t]*class\s+([\w:]+)(?:\s*<\s*([\w:]+))?", re.MULTILINE)


def _contract_base_tail(name: str) -> str:
    """Unqualified tail of a base name across Ruby (``A::B``) and Python (``a.b``)."""
    return name.rsplit("::", 1)[-1].rsplit(".", 1)[-1]


def _defines_method(scan_content: str, method: str, language: str) -> bool:
    """True when ``method`` is defined ANYWHERE in the file. File-level on purpose:
    if the method exists on any class in the file, the contract nag is suppressed --
    the maximally false-positive-safe direction for an advisory."""
    esc = re.escape(method)
    if language == "ruby":
        # `def perform` / `def self.perform`. A Ruby method name may end in ? or !,
        # so the required name must be a WHOLE name, not a prefix (`call` != `caller`).
        return re.search(rf"\bdef\s+(?:self\.)?{esc}(?![A-Za-z0-9_?!])", scan_content) is not None
    return re.search(rf"\bdef\s+{esc}\s*\(", scan_content) is not None


def _required_method_violations(
    scan_content: str, class_contract: dict, language: str | None
) -> list[Violation]:
    """Advisory when a class extending the archetype's dominant base omits a method
    the cohort defines at >= 95% frequency (an ApplicationJob without ``perform``, a
    BaseService without ``call``). This is the post-edit consumer of the
    ``class_contract`` artifact, which the pre-edit block already advertises but no
    lint verified before.

    False-positive-safe by construction: only a top-level class DIRECTLY extending
    the dominant base is in the cohort (a helper, or a subclass of a sibling that
    inherits the method, is not); the base class itself is exempt; only a >= 95%
    method is enforced (a 66%-common one is a legitimate option); and the method is
    flagged only when it is defined nowhere in the file. Ruby + Python only. Never
    block-eligible."""
    if language not in ("ruby", "python") or not isinstance(class_contract, dict):
        return []
    required = class_contract.get("required_methods")
    base = class_contract.get("base")
    freqs = class_contract.get("frequencies") or {}
    sample = class_contract.get("sample_size") or 0
    if not required or not isinstance(base, str) or sample < _CONTRACT_METHOD_MIN_SAMPLE:
        return []
    mandatory = [
        m
        for m in required
        if isinstance(m, str) and (freqs.get(m) or 0) >= _CONTRACT_METHOD_MIN_FREQUENCY
    ]
    if not mandatory:
        return []
    base_tail = _contract_base_tail(base)

    # Is a top-level class DIRECTLY extending the dominant base present (and not the
    # base itself)? Only such a class is in the cohort the contract governs. A
    # subclass of a sibling (`ChildJob < BackfillJob`) inherits the method and is not
    # flagged. scan_content is already strings/comments-stripped by the caller, so a
    # class decl inside a heredoc cannot trip this.
    # A superclass is the contract base when it matches the FULL qualified name, or
    # is the UNqualified short form (`< Base` inside the base's own module). A
    # DIFFERENT qualified class that merely shares the tail (`< Foo::Base` vs the
    # contract `ActiveInteraction::Base`) is NOT the base -- it is an intermediate the
    # class inherits the method through, so a bare-tail match would wrongly pull it
    # into the cohort. `Base` is a very common Ruby tail, so this guard is load-bearing.
    def _sup_is_base(sup: str | None) -> bool:
        if not sup:
            return False
        if sup == base:
            return True
        return "::" not in sup and "." not in sup and sup == base_tail

    def _extends_base() -> bool:
        if language == "ruby":
            for m in _RUBY_TOPLEVEL_CLASS_RE.finditer(scan_content):
                cls, sup = m.group(1), m.group(2)
                if _contract_base_tail(cls) == base_tail:
                    continue  # the base class itself
                if _sup_is_base(sup):
                    return True
            return False
        for m in _PY_CLASS_BASES_LINT_RE.finditer(scan_content):
            if len(m.group(1)) != 0:
                continue  # only a column-0 class is top-level in Python
            cls = m.group(2)
            if _contract_base_tail(cls) == base_tail:
                continue
            if any(_sup_is_base(b) for b in _py_positional_bases(m.group(3))):
                return True
        return False

    if not _extends_base():
        return []

    out: list[Violation] = []
    for method in mandatory:
        if not _defines_method(scan_content, method, language):
            out.append(
                Violation(
                    rule="missing-required-method",
                    expected=f"def {method}",
                    actual="absent",
                    severity="warning",
                    message=(
                        f"CONTRACT: every class extending {base} in this archetype "
                        f"defines {method}; this subclass does not."
                    ),
                )
            )
    return out
