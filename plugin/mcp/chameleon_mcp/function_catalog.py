"""Per-function catalog and the duplication-candidate prefilter.

The flat key_exports list catches a new ``formatDate`` colliding with an
existing ``formatDate`` by exact name, but it cannot see that a new
``toDisplayDate`` re-implements the existing ``formatDate`` under a different
name -- the single most common "this already exists, call X instead"
maintainability comment. Catching that needs the repo's functions cataloged by
more than name.

This module builds and reads a committed ``function_catalog.json`` recording,
per top-level/exported function or method, its name, kind, normalized signature
shape (positional arity + which slots are optional), and the file it lives in.
The signature is shape-only; no body is stored. The catalog is the cheap
candidate-narrowing layer for cross-file duplication: given the functions a file
defines, :func:`select_candidates` returns the handful of existing functions
whose signature shape and name tokens overlap, and the LLM caller (PR-review /
the turn-end judge) does the actual semantic-equivalence judging against those
candidates' real bodies read from disk. The prefilter never decides duplication;
it only bounds what the judge has to look at.

Plain Python throughout: arity comparison and name-token overlap, no MinHash.
Same-intent functions with different implementations share almost no token
shingles, so a syntactic near-duplicate index would miss exactly the renamed
re-implementations this targets; name tokens plus signature shape narrow far
better for that case.

Two halves live here so the build (bootstrap-time, populates the artifact) and
the read (tool-time, consumes it) share one schema and cannot drift:

- :func:`build_function_catalog` turns parsed files into the artifact payload.
- :func:`load_function_catalog` reads the committed artifact, cached on
  (mtime, size) so a mid-session refresh is picked up without re-reading.

Conservative and bounded by construction. The number of files and the functions
recorded per file are capped (see :mod:`chameleon_mcp._thresholds`) so one
generated file cannot bloat the artifact. Anonymous callables carry no stable
name and are never recorded (the dump scripts already skip them). Loading fails
open to None on any ambiguity -- missing, corrupt, future-schema, oversized, or
any I/O error -- so the duplication read simply does not fire rather than crash
or fabricate.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int

FUNCTION_CATALOG_FILENAME = "function_catalog.json"
SCHEMA_VERSION = 1

# A camelCase / PascalCase / snake_case / kebab boundary splitter. A name is
# lowered and split into word tokens so toDisplayDate and formatDate compare on
# {to, display, date} vs {format, date} -- the overlap on "date" is the reuse
# hint. Single-character fragments are dropped as noise.
_TOKEN_BOUNDARY_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# Generic name tokens carry no reuse signal: nearly every helper "gets",
# "builds", or "handles" something, so overlap on these would pair unrelated
# functions. They are stripped before the overlap test so a match must rest on a
# domain token (date, slug, total, price), not a verb every function shares.
_STOPWORD_TOKENS = frozenset(
    {
        "get",
        "set",
        "is",
        "has",
        "to",
        "of",
        "the",
        "a",
        "an",
        "do",
        "make",
        "build",
        "create",
        "new",
        "handle",
        "process",
        "run",
        "fn",
        "func",
        "method",
        "value",
        "val",
        "data",
        "item",
        "obj",
        "self",
        # Connector / preposition tokens carry no reuse signal on their own; a
        # match resting only on `in`/`on`/`for` (e.g. `shuffleDeckInPlace` vs
        # `updateAccountInCache`) is pure noise that crowds out the real
        # counterpart under the candidate cap.
        "in",
        "on",
        "at",
        "by",
        "for",
        "from",
        "into",
        "with",
        "and",
        "or",
        "as",
        "via",
    }
)


def name_tokens(name: str) -> frozenset[str]:
    """Lowered domain-word tokens of a callable name, stopwords removed.

    ``toDisplayDate`` -> {display, date}; ``format_date`` -> {date} (format is
    not a stopword, so actually {format, date}); ``getX`` -> {x}. Used to score
    name overlap between a new function and a catalog candidate. Single-character
    tokens and the generic-verb stopwords are dropped so overlap rests on a real
    domain word.
    """
    if not isinstance(name, str) or not name:
        return frozenset()
    raw = (t for t in _TOKEN_BOUNDARY_RE.split(name) if t)
    out = {t.lower() for t in raw if len(t) > 1}
    return frozenset(out - _STOPWORD_TOKENS)


def _signature_shape(params: object) -> tuple[int, int]:
    """Reduce a param list to (positional arity, required arity).

    Shape-only: parameter NAMES are intentionally discarded here (they feed the
    name-token test, not the arity test). Required arity is positional arity
    minus the optional slots, so two functions with the same total arity but a
    different required/optional split are distinguished. A rest/destructured slot
    counts toward arity like any positional.
    """
    if not isinstance(params, list):
        return (0, 0)
    arity = 0
    required = 0
    for p in params:
        if not isinstance(p, dict):
            continue
        arity += 1
        if not bool(p.get("optional")):
            required += 1
    return (arity, required)


@dataclass(frozen=True)
class CatalogedFunction:
    """One function recorded in the catalog.

    ``arity`` / ``required`` are the signature shape; ``tokens`` are the lowered
    domain-word tokens of the name, precomputed at load so the prefilter does not
    re-tokenize every candidate per query. ``body_hash`` is the normalized-body
    fingerprint (None for rows built before spans were recorded, or for bodies
    too short to be a meaningful identity).
    """

    name: str
    kind: str
    file: str
    arity: int
    required: int
    tokens: frozenset[str]
    body_hash: str | None = None
    body_hash_pnorm: str | None = None
    # A STRUCTURE-preserving body fingerprint computed by the cheap hot-path
    # extractor (indent/brace span + relative-indent-aware normalization), stored
    # at bootstrap so the pre-write hot path -- which cannot spawn a parser -- can
    # reproduce it byte-for-byte for a VERBATIM method and match it (the parser
    # body_hash reproduces only 61-93% from a regex walk, and its full whitespace
    # collapse also merges two Python bodies that differ only by a statement's block
    # membership). Independent of body_hash. Absent on catalogs built before this
    # field; the body-dup pass simply does not fire for those until a refresh
    # backfills it (no regression, no false match).
    body_hash_lax: str | None = None


_IDENTIFIER_RE = re.compile(r"[A-Za-z_$][\w$]*\Z")


def _param_names(params: object) -> list[str]:
    """Positional parameter identifiers from a callable_signatures entry.

    Non-identifier slots (destructured ``{}``, anonymous ``_``, rest ``*``)
    keep their position as an empty string so positional alpha-renaming stays
    aligned between a clone and its original.
    """
    if not isinstance(params, list):
        return []
    names: list[str] = []
    for prm in params:
        name = prm.get("name") if isinstance(prm, dict) else None
        if isinstance(name, str) and name not in ("_", "{}", "*") and _IDENTIFIER_RE.match(name):
            names.append(name)
        else:
            names.append("")
    return names


_RUBY_BLOCK_PARAMS_RE = re.compile(r"(?:\bdo\b|\{)\s*\|([^|\n]*)\|")
_TS_ARROW_PARAMS_RE = re.compile(r"\(([^()]*)\)\s*=>")
_PY_LAMBDA_PARAMS_RE = re.compile(r"\blambda\b([^:\n]*):")
_BLOCK_PARAM_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


def _lang_from_path(path: str) -> str | None:
    """Coarse language tag from a file extension, for body-hash normalization."""
    p = path.lower()
    if p.endswith(".rb"):
        return "ruby"
    if p.endswith((".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")):
        return "typescript"
    if p.endswith((".py", ".pyi")):
        return "python"
    return None


def _block_param_names(text: str, language: str | None) -> list[str]:
    """Ordered block/closure parameter identifiers declared inside a body.

    The dumper records only the callable's own ``def`` / ``function`` signature
    params, so block parameters (Ruby ``each do |row|``, a TS arrow callback, a
    Python ``lambda x:``) never reach the param rename. Best-effort over the
    collapsed body text: Ruby ``do |..|`` / ``{ |..| }`` block params, simple
    untyped TypeScript arrow params, and simple Python lambda params.
    Typed/generic/destructured/defaulted TS param lists, and defaulted/starred
    Python lambda lists, are skipped so a misparse cannot corrupt the
    fingerprint; block-LOCAL variables (including comprehension targets, which
    are locals not parameters) are not collected (renaming arbitrary locals
    would over-merge distinct bodies).
    """
    names: list[str] = []
    if language == "ruby":
        for m in _RUBY_BLOCK_PARAMS_RE.finditer(text):
            for tok in m.group(1).split(","):
                tok = tok.strip().lstrip("*&(").strip()
                im = _BLOCK_PARAM_IDENT_RE.match(tok)
                if im and im.group(0) != "_":
                    names.append(im.group(0))
    elif language == "typescript":
        for m in _TS_ARROW_PARAMS_RE.finditer(text):
            group = m.group(1)
            # Skip typed/generic/destructured/defaulted lists — pulling bare
            # identifiers out of them risks renaming the wrong tokens.
            if any(c in group for c in ":<>{}=?"):
                continue
            for tok in group.split(","):
                tok = tok.strip().lstrip(".").strip()
                im = _BLOCK_PARAM_IDENT_RE.match(tok)
                if im and im.group(0) != "_":
                    names.append(im.group(0))
    elif language == "python":
        for m in _PY_LAMBDA_PARAMS_RE.finditer(text):
            group = m.group(1)
            # Skip defaulted/starred lists — a default value can carry tokens
            # that are not parameter names, and *args/**kwargs are not renamed.
            if any(c in group for c in "=*"):
                continue
            for tok in group.split(","):
                tok = tok.strip()
                im = _BLOCK_PARAM_IDENT_RE.match(tok)
                if im and im.group(0) != "_":
                    names.append(im.group(0))
    return names


_PY_DOCSTRING_LINE_RE = re.compile(
    r'^\s*(?:"""(?:.*)"""|\'\'\'(?:.*)\'\'\'|"[^"\\]*"|\'[^\'\\]*\')\s*$'
)
_PY_DOCSTRING_OPEN_RE = re.compile(r"^\s*(\"\"\"|''')")


def _strip_python_docstring(body_lines: list[str]) -> list[str]:
    """Drop a docstring that opens a Python function body, if one is there.

    A per-function docstring almost always names the specific thing the
    function does (its own transform, its own field), so leaving it in the
    hashed span defeats the exact-clone fallback for any documented Python
    function the same way an un-dropped ``def`` line would: two functions
    with identical logic but their own docstring text hash differently for a
    reason that carries no semantic weight. Only a docstring that is the
    SOLE content of the leading line(s) is dropped, mirroring how Python
    itself recognizes one (a bare string literal as the first statement) —
    a real first statement that merely evaluates to a bare string is
    indistinguishable from a docstring at this level and is treated the same
    way.
    """
    if not body_lines:
        return body_lines
    first = body_lines[0].strip()
    if not first:
        return body_lines
    if _PY_DOCSTRING_LINE_RE.match(first):
        return body_lines[1:]
    opener = _PY_DOCSTRING_OPEN_RE.match(first)
    if opener:
        # A triple-quote opener whose OWN line has no closing triple-quote
        # after it (the common `"""` on its own line, text below, `"""` to
        # close style): scan forward for it. Checking for the closer only in
        # what follows the opener -- not merely whether the line "ends with"
        # the token -- matters because an opener-only line trivially ends
        # with itself.
        quote = opener.group(1)
        if quote not in first[opener.end() :]:
            for i in range(1, len(body_lines)):
                if quote in body_lines[i]:
                    return body_lines[i + 1 :]
            # Unterminated within the recorded span -- leave the body
            # untouched rather than guess at where it would have closed.
    return body_lines


_RUBY_DEF_NAME_RE = re.compile(r"^\s*def\s+(?:self\.)?[A-Za-z_]\w*(?:[?!]|=(?=\())?")
_RUBY_ENDLESS_SEP_RE = re.compile(r"^\s*=(?!=)\s*")
_PY_DEF_HEAD_RE = re.compile(r"^\s*(?:async\s+)?def\s+[A-Za-z_]\w*\s*\(")


def _ruby_endless_body(line: str) -> str | None:
    """Split a Ruby endless-method line (``def name(...) = expr``) at its ``=``.

    Skips the method name and, if present, a single balanced parameter
    parenthesis before hunting for the separating ``=`` -- a default
    parameter value's own ``=`` inside those parens must not be mistaken for
    it. Returns None for anything that does not scan as a plain endless
    method (unbalanced parens, or no top-level ``=`` left after them), so an
    unrecognized shape is never hashed with its name still attached.
    """
    m = _RUBY_DEF_NAME_RE.match(line)
    if not m:
        return None
    rest = line[m.end() :]
    stripped = rest.lstrip()
    if stripped.startswith("("):
        lead = len(rest) - len(stripped)
        depth = 0
        close = None
        for i in range(lead, len(rest)):
            if rest[i] == "(":
                depth += 1
            elif rest[i] == ")":
                depth -= 1
                if depth == 0:
                    close = i
                    break
        if close is None:
            return None
        rest = rest[close + 1 :]
    sep = _RUBY_ENDLESS_SEP_RE.match(rest)
    if not sep:
        return None
    return rest[sep.end() :]


def _python_one_liner_body(line: str) -> str | None:
    """Split a Python one-line ``def name(...): body`` at its top-level ``:``.

    Scans past the balanced parameter parenthesis (so a default value's own
    ``:`` in a dict literal, or a ``->`` return-type annotation, cannot be
    mistaken for the signature/body separator) before taking the first
    colon that follows. Returns None when the parens never balance or no
    colon follows them.
    """
    m = _PY_DEF_HEAD_RE.match(line)
    if not m:
        return None
    depth = 1
    i = m.end()
    while i < len(line) and depth:
        if line[i] == "(":
            depth += 1
        elif line[i] == ")":
            depth -= 1
        i += 1
    if depth != 0:
        return None
    colon = line.find(":", i)
    if colon == -1:
        return None
    return line[colon + 1 :]


def _single_line_body(line: str, language: str | None) -> str | None:
    """Split a one-physical-line callable's signature head from its body.

    A Ruby endless method, a TS/JS arrow one-liner (``const name = (...) =>
    expr``), or a brace-bodied one-liner packs the whole callable onto its
    ``def``/``function`` line, so there is no separate first line to drop the
    way a multi-line span does. Returns None for a shape it cannot
    confidently split (no recognized separator, or an unrecognized
    language), matching this module's fail-open-to-None contract rather than
    guessing wrong and hashing the signature -- name included -- along with
    the body.
    """
    if language == "ruby":
        return _ruby_endless_body(line)
    if language == "typescript":
        idx = line.find("=>")
        if idx != -1:
            return line[idx + 2 :]
        # A brace-bodied one-liner (`function foo() { ... }` / method
        # shorthand): only the head up to the opening brace is dropped, the
        # closing brace stays -- the same "don't touch the tail" convention
        # the multi-line span already follows for its last line.
        idx = line.find("{")
        return line[idx + 1 :] if idx != -1 else None
    if language == "python":
        return _python_one_liner_body(line)
    return None


def normalized_body_hash(
    source_lines: list[str],
    start_line: object,
    end_line: object,
    *,
    param_names: list[str] | None = None,
    language: str | None = None,
) -> str | None:
    """Fingerprint a function body for the exact-clone fallback, or None.

    Slices the 1-based inclusive ``start_line``..``end_line`` span, DROPS the
    first line (it carries the function's name, which differs between a clone
    and its original), collapses all whitespace, and hashes. Bodies shorter
    than the minimum normalized length return None: trivial one-expression
    bodies collide across half a codebase and would flood the candidate list
    with noise rather than reuse leads.

    A single-physical-line span (``start_line == end_line``: a Ruby endless
    method, a TS/JS arrow one-liner) has no separate first line to drop --
    name and body share the one line -- so it is split with a ``language``-
    aware head/body scan instead; an unrecognized shape fails open to None
    the same as any other ambiguity here, rather than hashing the name along
    with the body. With ``language`` set to ``"python"``, a docstring that
    opens the (multi-line) body is dropped too, for the same reason the def
    line itself is: it differs between a clone and its original by design.

    With ``param_names``, each parameter identifier is alpha-renamed to its
    positional slot before hashing, so a clone whose only difference is
    renamed parameters still pairs with its original. With ``language`` set
    (the param-normalized variant only), block/closure parameters are renamed
    the same way, so a clone whose only change is a renamed block parameter
    also pairs. The rename is textual (word-bounded), which can over-match a
    shadowing outer name — acceptable for a prefilter whose candidates are
    verified against real bodies by the caller. Block-LOCAL variables (declared
    inside a block, not parameters) are intentionally NOT renamed: collapsing
    arbitrary locals would over-merge distinct bodies.
    """
    if not isinstance(start_line, int) or not isinstance(end_line, int):
        return None
    if start_line < 1 or end_line < start_line or start_line > len(source_lines):
        return None
    if start_line == end_line:
        body_text = _single_line_body(source_lines[start_line - 1], language)
        if body_text is None:
            return None
        body_lines = [body_text]
    else:
        body_lines = source_lines[start_line : min(end_line, len(source_lines))]
        if not body_lines:
            return None
        if language == "python":
            body_lines = _strip_python_docstring(body_lines)
    if not body_lines:
        return None
    normalized = " ".join("\n".join(body_lines).split())
    if len(normalized) < threshold_int("DUPLICATION_BODY_HASH_MIN_CHARS"):
        return None
    # Signature params take positional slots 0..n-1; block/closure parameters
    # take the slots after them, in source order. ``language`` is passed only on
    # the param-normalized variant, so the exact body_hash stays exact.
    rename_names = list(param_names) if param_names else []
    if language:
        rename_names = rename_names + _block_param_names(normalized, language)
    if rename_names:
        for i, pname in enumerate(rename_names):
            if not pname:
                continue
            normalized = re.sub(
                rf"(?<![\w$]){re.escape(pname)}(?![\w$])", f"\x00p{i}\x00", normalized
            )
    import hashlib

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


# The "lax" body-fingerprint extractor: a cheap regex header match + indent/brace
# body-span walk, deliberately NOT a parser span. Its whole purpose is to be
# IDENTICAL on both sides -- bootstrap runs it over committed source to store
# body_hash_lax, and the pre-write hot path runs it over the code the model is
# about to write -- so a verbatim method duplicate produces the same digest on
# both sides by construction (research: an exact-clone fingerprint only needs the
# same deterministic function on both sides, not the "true" AST span). Bump
# LAX_FINGERPRINT_VERSION when the extraction rules change so a stale lax hash is
# ignored rather than mis-compared.
LAX_FINGERPRINT_VERSION = 2
_LAX_TS_EXTS = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"})


def _lax_body_fingerprint(source_lines: list[str], start_line: int, end_line: int) -> str | None:
    """A STRUCTURE-preserving body fingerprint for the lax extractor.

    ``normalized_body_hash`` collapses every whitespace run, which merges two
    Python bodies that differ only in a statement's block membership (Python's
    blocks are indentation-delimited) -- a false "same body" match. This keeps each
    non-blank line's RELATIVE indent depth as a token, so a statement inside a
    block vs after it produces a different fingerprint. Drops the first (def) line
    and blank lines; internal whitespace within a line is still collapsed, so
    reflowing a long call across the same indentation still matches. Returns None
    below the min-chars floor (trivial bodies collide across a codebase).
    """
    if not isinstance(start_line, int) or not isinstance(end_line, int):
        return None
    if start_line < 1 or end_line < start_line or start_line > len(source_lines):
        return None
    body = source_lines[start_line : min(end_line, len(source_lines))]  # drops the def line
    norm: list[tuple[int, str]] = []
    for ln in body:
        stripped = ln.strip()
        if not stripped:
            continue
        norm.append((len(ln) - len(ln.lstrip()), " ".join(stripped.split())))
    if not norm:
        return None
    base = min(i for i, _ in norm)
    canon = "\n".join(f"{i - base}:{c}" for i, c in norm)
    if len(canon) < threshold_int("DUPLICATION_BODY_HASH_MIN_CHARS"):
        return None
    import hashlib

    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


_LAX_PY_HDR_RE = re.compile(r"^([ \t]*)(?:async[ \t]+)?def[ \t]+([A-Za-z_]\w*)")
_LAX_RB_HDR_RE = re.compile(r"^([ \t]*)def[ \t]+(?:self\.)?([A-Za-z_]\w*[?!=]?)")
_LAX_TS_HDR_RE = re.compile(
    r"^([ \t]*)(?:public |private |protected |static |async |readonly |get |set |override )*"
    r"([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^;]*\)\s*(?::[^{;=]+)?\{"
)
# Control-flow keywords the method-header regex matches as a `name(...) {` shape.
_LAX_TS_KEYWORDS = frozenset(
    {"if", "for", "while", "switch", "catch", "do", "else", "return", "function", "with"}
)
# Ruby heredoc delimiters are legal in any case (`<<~sql`, `<<-query`, `<<HTML`).
# The delimiter must follow `<<` (plus an optional `~`/`-`/quote) with NO space --
# a spaced `array << item` append never matches, so broadening to lower-case costs
# no recall while catching the flush-left-heredoc truncation for every delimiter.
_RB_HEREDOC_OPEN_RE = re.compile(r"<<[-~]?['\"]?([A-Za-z_]\w*)['\"]?")
# A captured span ending on one of these trailing tokens continues onto the next
# (flush-left) line -- a complete Ruby statement never ends on a bare binary
# operator / comma, so this only fires on a truncated span (recall-safe).
_RB_TRAILING_CONT_RE = re.compile(r"(?:&&|\|\||=>|->|::|[,+\-*/%&|^<>=~.])$")
# Preceding significant code chars after which a `/` opens a regex literal (an
# expression position) rather than a division. Postfix chars (`)`, `]`, `}`,
# identifiers, digits) mean division and are deliberately excluded.
_TS_REGEX_PREV = frozenset("(,=:[{!&|?;+-*%<>~^")


def _span_string_truncated(body_text: str, ruby: bool, term_line: str = "") -> bool:
    """True when the indent-scoped span appears to have been cut off INSIDE a
    multi-line construct (a flush-left line masquerading as the method terminator),
    so the captured body is a partial prefix. Hashing a truncated body would collide
    two methods that share only their pre-construct prefix -> a false "same body"
    nudge. Detecting truncation and skipping (no hash) is the safe direction: recall
    loss, never a false match, and identical on both index sides. ``term_line`` is
    the flush-left line that ended the indent walk (empty at EOF); some Ruby
    continuations only reveal the truncation through that next line.
    """
    # A span ending on a dangling line-continuation backslash was cut off mid-
    # statement (a flush-left continuation line terminated the indent walk before the
    # statement finished) -- a complete body never ends on a `\`. Covers a backslash-
    # continued string ("... \) or statement (foo = a + \) written at column 0, in
    # either language.
    if body_text.rstrip().endswith("\\"):
        return True
    # A span with unbalanced brackets was cut off mid-construct: a multi-line array /
    # hash / call / percent-literal (`%w[`, `%q{`, `func(`, `[`) whose closing bracket
    # sits flush-left terminates the indent walk before the construct closes, so only
    # the shared pre-construct prefix gets hashed. This one balance check subsumes the
    # whole family of flush-left-continuation truncations that the per-construct guards
    # above do not. Counting brackets naively also counts brackets inside string/char
    # literals, but that only ever OVER-skips (recall-safe) and costs ~0.2% on real
    # code -- far cheaper than the false "same body" nudge an unguarded truncation
    # would emit.
    opens = body_text.count("(") + body_text.count("[") + body_text.count("{")
    closes = body_text.count(")") + body_text.count("]") + body_text.count("}")
    if opens != closes:
        return True
    # Known residual: a Ruby plain "..." / '...' string carrying a literal newline
    # with flush-left content and balanced brackets is NOT caught here. A quote-parity
    # guard costs ~3% recall (apostrophes in comments, char literals, interpolation),
    # and a precise detector needs a full Ruby lexer -- not worth it for a construct
    # heredocs idiomatically replace (zero occurrences across a 10k-method real-repo
    # sweep). Left as a bounded gap; a truncated span here fails safe unless two
    # methods share a 40+ char prefix and the same multi-line string.
    if ruby:
        # Ruby continues a statement across a bare newline when a line ends in a
        # binary operator / comma, when the next line leads with a `.`/`&.` method
        # chain, or through a `=begin`..`=end` block comment (forced to column 0).
        # None of these carry a bracket / heredoc / backslash signal, so a flush-left
        # continuation truncates the span with nothing above to catch it. The next
        # line (term_line) or a dangling trailing operator reveals it.
        t = term_line.strip()
        if t == "=begin" or t.startswith((".", "&.")):
            return True
        if _RB_TRAILING_CONT_RE.search(body_text.rstrip()):
            return True
        for m in _RB_HEREDOC_OPEN_RE.finditer(body_text):
            delim = m.group(1)
            if not re.search(rf"^[ \t]*{re.escape(delim)}[ \t]*$", body_text[m.end() :], re.M):
                return True  # heredoc opened, its terminator not inside the span
        return False
    # Python: an odd count of either triple-quote delimiter means one opened
    # without a close in the captured span.
    return body_text.count('"""') % 2 == 1 or body_text.count("'''") % 2 == 1


def _ts_body_end(lines: list[str], start: int) -> int:
    """The line index of a TS/JS method's closing brace, via a STRING-, COMMENT-,
    and REGEX-aware brace scan. A naive per-char `{`/`}` count miscounts a brace
    inside a string (`"a}b"`), a template, a comment, or a regex literal (`/}/`),
    truncating the span early -- which would collide two methods sharing only the
    pre-token prefix. Braces inside a string/template/`//`/`/* */`/regex are
    skipped. Templates persist across lines (backtick); a `${...}` interpolation's
    braces are treated as string content, which is fine for finding the method's
    own closing brace. A `/` is read as a regex literal (not division) only when
    the preceding significant code char is empty or expression-opening
    (`_TS_REGEX_PREV`). A keyword-preceded regex (`return /}/`) is the residual gap:
    its brace-bearing literal can still truncate the span -- a recall loss when the
    regex is the last statement, and rarely a false match when a divergent tail
    follows. The gap is narrow (a bare brace-bearing regex used mid-method); the
    common assignment/argument regex positions are covered.
    """
    depth = 0
    started = False
    in_str = ""  # "", or one of ' " `
    in_block = False
    prev = ""  # last significant (non-space) code char, for regex-vs-division
    for j in range(start, min(len(lines), start + 400)):
        line = lines[j]
        n = len(line)
        k = 0
        while k < n:
            c = line[k]
            nxt = line[k + 1] if k + 1 < n else ""
            if in_block:
                if c == "*" and nxt == "/":
                    in_block = False
                    k += 2
                else:
                    k += 1
                continue
            if in_str:
                if c == "\\":
                    k += 2
                elif c == in_str:
                    in_str = ""
                    k += 1
                else:
                    k += 1
                continue
            if c == "/" and nxt == "/":
                break  # line comment -- rest of the line is inert
            if c == "/" and nxt == "*":
                in_block = True
                k += 2
                continue
            if c in ("'", '"', "`"):
                in_str = c
                prev = c
                k += 1
                continue
            if c == "/" and (prev == "" or prev in _TS_REGEX_PREV):
                # Regex literal: consume to the closing unescaped '/', treating a
                # `[...]` char class as opaque (a '/' inside it does not terminate).
                # Regex literals never span lines, so this stays within the line.
                k += 1
                in_class = False
                while k < n:
                    rc = line[k]
                    if rc == "\\":
                        k += 2
                        continue
                    if rc == "[":
                        in_class = True
                    elif rc == "]":
                        in_class = False
                    elif rc == "/" and not in_class:
                        k += 1
                        break
                    k += 1
                prev = "/"
                continue
            if c == "{":
                depth += 1
                started = True
            elif c == "}":
                depth -= 1
            if c not in (" ", "\t"):
                prev = c
            k += 1
        if started and depth <= 0:
            return j
    return min(len(lines) - 1, start + 399)


def extract_method_body_hashes(content: str, file_path: str) -> list[tuple[str, str]]:
    """(name, lax_body_hash) per method/def in ``content``, using the shared cheap
    span extractor. Python/Ruby are indent-scoped (Ruby's span INCLUDES the
    terminating ``end`` line, matching prism); TS is brace-scoped. Skips bodies
    below the hash floor. No parser spawn. Reproduces ``body_hash_lax`` exactly."""
    ext = Path(file_path).suffix.lower()
    lines = content.splitlines()
    out: list[tuple[str, str]] = []
    if ext in (".py", ".pyi", ".rb"):
        hdr = _LAX_PY_HDR_RE if ext != ".rb" else _LAX_RB_HDR_RE
        ruby = ext == ".rb"
        for i, ln in enumerate(lines):
            m = hdr.match(ln)
            if not m:
                continue
            indent = len(m.group(1))
            end = i
            term = ""
            for j in range(i + 1, len(lines)):
                s = lines[j]
                if not s.strip():
                    continue
                if len(s) - len(s.lstrip()) <= indent:
                    stripped = s.strip()
                    if stripped.startswith("#"):
                        continue  # a flush-left comment doesn't end a method; keep scanning
                    if ruby and stripped == "end":
                        end = j
                    term = s
                    break
                end = j
            if _span_string_truncated("\n".join(lines[i + 1 : end + 1]), ruby, term):
                continue  # span cut off inside a multi-line construct -> skip
            bh = _lax_body_fingerprint(lines, i + 1, end + 1)
            if bh:
                out.append((m.group(2), bh))
    elif ext in _LAX_TS_EXTS:
        for i, ln in enumerate(lines):
            m = _LAX_TS_HDR_RE.match(ln)
            if not m or m.group(2) in _LAX_TS_KEYWORDS:
                continue  # `if (x) {` / `for (...) {` etc. read as a pseudo-method
            end = _ts_body_end(lines, i)
            bh = _lax_body_fingerprint(lines, i + 1, end + 1)
            if bh:
                out.append((m.group(2), bh))
    return out


def _function_rows(pf, root: Path) -> tuple[str | None, list[dict]]:
    """Turn one parsed file's callable_signatures into catalog rows.

    Returns (repo_relative_posix_path, rows). The path is None when the file
    cannot be made repo-relative (out-of-repo, I/O error); the caller drops it.
    Each row is the minimal record the artifact stores: name, kind, and the two
    arity numbers. Anonymous callables are already absent from the dump, and a
    row without a string name is skipped.
    """
    extras = getattr(pf, "extras", None) or {}
    raw = extras.get("callable_signatures")
    if not isinstance(raw, list) or not raw:
        return None, []
    try:
        rel = Path(pf.path).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return None, []

    per_file_cap = threshold_int("DUPLICATION_CATALOG_MAX_FNS_PER_FILE")
    # Read the source ONCE (bounded) and derive both the line list (parser-span
    # body hashing) and the lax fingerprint map (the cheap extractor, run so each
    # row carries a body_hash_lax the pre-write hot path reproduces exactly). Keyed
    # by name; same-name occurrences consume in file order. A file that cannot be
    # read yields neither -- body_hash and body_hash_lax are simply absent.
    source_lines: list[str] | None = None
    lax_map: dict[str, list[str]] = {}
    lax_pos: dict[str, int] = {}
    try:
        _content = Path(pf.path).read_bytes()[:1_000_000].decode("utf-8", errors="replace")
        source_lines = _content.splitlines()
        for _n, _h in extract_method_body_hashes(_content, str(pf.path)):
            lax_map.setdefault(_n, []).append(_h)
    except OSError:
        source_lines = []
    rows: list[dict] = []
    seen: set[tuple[str, int, int]] = set()
    for entry in raw:
        if len(rows) >= per_file_cap:
            break
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        arity, required = _signature_shape(entry.get("params"))
        # An overload set declares the same name+shape repeatedly in one file;
        # record each distinct (name, shape) once so a single overloaded helper
        # does not crowd out other functions under the per-file cap.
        key = (name, arity, required)
        if key in seen:
            continue
        seen.add(key)
        kind = entry.get("kind")
        body_hash: str | None = None
        body_hash_pnorm: str | None = None
        if isinstance(entry.get("start_line"), int) and isinstance(entry.get("end_line"), int):
            body_hash = normalized_body_hash(
                source_lines, entry.get("start_line"), entry.get("end_line")
            )
            body_hash_pnorm = normalized_body_hash(
                source_lines,
                entry.get("start_line"),
                entry.get("end_line"),
                param_names=_param_names(entry.get("params")),
                language=_lang_from_path(str(pf.path)),
            )
        row = {
            "name": name,
            "kind": kind if isinstance(kind, str) else "function",
            "arity": arity,
            "required": required,
        }
        if body_hash is not None:
            row["body_hash"] = body_hash
        if body_hash_pnorm is not None:
            row["body_hash_pnorm"] = body_hash_pnorm
        _pos = lax_pos.get(name, 0)
        _lax = lax_map.get(name)
        if _lax and _pos < len(_lax):
            row["body_hash_lax"] = _lax[_pos]
            lax_pos[name] = _pos + 1
        rows.append(row)
    return rel, rows


def build_function_catalog(files, repo_root: Path | str) -> dict:
    """Build the ``function_catalog.json`` payload from parsed files.

    ``files`` is the bootstrap's parsed-file list; each entry's ``extras`` may
    carry ``callable_signatures`` (emitted for both TypeScript/JS and Ruby).
    Files with no recorded callable are omitted. Keys are repo-relative POSIX
    paths so the artifact is portable across checkouts and reproducible
    byte-for-byte (it is hashed into the trust SHA). The total number of files
    recorded is capped so a huge monorepo cannot bloat the artifact; files are
    taken in sorted-path order for a deterministic truncation.
    """
    try:
        root = Path(repo_root).resolve()
    except OSError:
        root = Path(repo_root)

    file_cap = threshold_int("DUPLICATION_CATALOG_MAX_FILES")
    collected: list[tuple[str, list[dict]]] = []
    for pf in files or ():
        rel, rows = _function_rows(pf, root)
        if rel is None or not rows:
            continue
        collected.append((rel, rows))

    collected.sort(key=lambda item: item[0])
    out: dict[str, list[dict]] = {rel: rows for rel, rows in collected[:file_cap]}
    # lax_fingerprint_version travels ALONGSIDE (not inside) schema_version so a
    # change to the lax extraction rules invalidates only the lax hashes -- the
    # loader ignores body_hash_lax on a stale version and falls back to the parser
    # body_hash -- without triggering the schema mismatch that would empty the
    # whole catalog.
    return {
        "schema_version": SCHEMA_VERSION,
        "lax_fingerprint_version": LAX_FINGERPRINT_VERSION,
        "files": out,
    }


class FunctionCatalog:
    """Repo-wide function records, loaded from the committed artifact.

    Holds the flat list of every cataloged function so the prefilter can scan it
    once per query. ``functions`` is the public read; the list is small relative
    to a repo because only named top-level/exported callables are recorded and
    both the file count and per-file function count are capped at build time.
    """

    def __init__(self, functions: list[CatalogedFunction]) -> None:
        self._functions = functions

    @property
    def functions(self) -> list[CatalogedFunction]:
        return self._functions

    def __len__(self) -> int:
        return len(self._functions)


# Process-global cache of parsed catalogs, keyed on the artifact path, carrying
# the (mtime, size) the catalog was parsed at so a refresh that rewrites the
# artifact is picked up without re-reading on every call.
_CATALOG_CACHE: dict[str, tuple[tuple[int, int], FunctionCatalog]] = {}


def load_function_catalog(repo_root: Path | str | None) -> FunctionCatalog | None:
    """Load the committed ``function_catalog.json`` for ``repo_root``, or None.

    Returns None (no candidates, no finding) on any ambiguity: no repo_root, no
    artifact, a corrupt or future-schema payload, an oversized file, or any I/O
    error. The duplication read only ADDS context; failing open here means it
    simply does not fire -- never a crash, never a fabricated candidate.
    """
    if repo_root is None:
        return None
    try:
        root = Path(repo_root).resolve()
    except OSError:
        return None
    # Follow a linked git worktree to the main worktree's profile, mirroring
    # load_calls_index -- without this, get_duplication_candidates silently
    # reads the worktree's absent .chameleon and returns found=False.
    from chameleon_mcp.worktree import resolve_profile_root

    root = resolve_profile_root(root)
    # Honor the atomic-commit sentinel like every other profile loader: a torn
    # .chameleon must read as no-catalog, never feed "reuse function X"
    # recommendations while the sibling tools report profile_corrupted.
    from chameleon_mcp.bootstrap.transaction import is_committed

    if not is_committed(root / ".chameleon"):
        return None
    artifact = root / ".chameleon" / FUNCTION_CATALOG_FILENAME
    try:
        st = os.stat(artifact)
    except OSError:
        return None
    if not st.st_size or st.st_size > 16_000_000:
        # Empty or implausibly large (a real catalog is well under this); skip
        # rather than read a pathological file.
        return None

    key = str(artifact)
    token = (int(st.st_mtime_ns), int(st.st_size))
    cached = _CATALOG_CACHE.get(key)
    if cached is not None and cached[0] == token:
        return cached[1]

    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    raw_files = data.get("files")
    if not isinstance(raw_files, dict):
        return None
    # Only trust body_hash_lax when it was written by the running extractor's rules
    # (a stale version would mis-compare against the current hot-path fingerprint);
    # otherwise drop it and fall back to the parser body_hash.
    lax_ok = data.get("lax_fingerprint_version") == LAX_FINGERPRINT_VERSION

    functions: list[CatalogedFunction] = []
    for rel, rows in raw_files.items():
        if not isinstance(rel, str) or not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            if not isinstance(name, str) or not name:
                continue
            kind = row.get("kind")
            arity = row.get("arity")
            required = row.get("required")
            body_hash = row.get("body_hash")
            body_hash_pnorm = row.get("body_hash_pnorm")
            body_hash_lax = row.get("body_hash_lax")
            functions.append(
                CatalogedFunction(
                    name=name,
                    kind=kind if isinstance(kind, str) else "function",
                    file=rel,
                    arity=int(arity) if isinstance(arity, int) else 0,
                    required=int(required) if isinstance(required, int) else 0,
                    tokens=name_tokens(name),
                    body_hash=body_hash if isinstance(body_hash, str) and body_hash else None,
                    body_hash_pnorm=(
                        body_hash_pnorm
                        if isinstance(body_hash_pnorm, str) and body_hash_pnorm
                        else None
                    ),
                    body_hash_lax=(
                        body_hash_lax
                        if lax_ok and isinstance(body_hash_lax, str) and body_hash_lax
                        else None
                    ),
                )
            )

    catalog = FunctionCatalog(functions)
    _CATALOG_CACHE[key] = (token, catalog)
    return catalog


def _arity_close(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """True when two signature shapes are close enough to be reuse candidates.

    A duplicate re-implementation usually keeps the same call shape, but a
    rename can add or drop a defaulted argument, so the positional arity may
    differ by one. Require the arities within 1 of each other. Zero-arity is
    matched only against zero-arity: a no-arg getter and a 3-arg builder are
    never the same intent.
    """
    arity_a, _req_a = a
    arity_b, _req_b = b
    if arity_a == 0 or arity_b == 0:
        return arity_a == arity_b
    return abs(arity_a - arity_b) <= 1


def _overlap_score(new_tokens: frozenset[str], cand: CatalogedFunction) -> int:
    """Count of shared domain tokens between a new function and a candidate."""
    return len(new_tokens & cand.tokens)


def _jaccard(new_tokens: frozenset[str], cand_tokens: frozenset[str]) -> float:
    """Token-set Jaccard similarity, used as the candidate ranking tiebreak.

    When several candidates share the same raw overlap count (commonly a single
    very-frequent token like ``name``), the one whose whole token set is closest
    to the query is the better reuse lead. Ranking purely by overlap then
    alphabetically buried the real counterpart (``getFullName`` for
    ``buildDisplayName``) below same-overlap noise like ``EventName`` and longer
    multi-token names. Jaccard pushes the closest-shaped names up so they land
    inside the candidate cap.
    """
    union = new_tokens | cand_tokens
    if not union:
        return 0.0
    return len(new_tokens & cand_tokens) / len(union)


@dataclass(frozen=True)
class NewFunction:
    """A function defined in the file under review, the prefilter's query side."""

    name: str
    kind: str
    arity: int
    required: int
    body_hash: str | None = None
    body_hash_pnorm: str | None = None


@dataclass(frozen=True)
class ParsedFn:
    """A function parsed from an edited file, with the spans the catalog drops.

    NewFunction carries only the hashes for matching; ParsedFn additionally
    carries the 1-based start line and a body excerpt so the duplication gate can
    cite a line and feed the judge a body without a second parse.

    ``arity`` / ``required`` mirror the signature shape so callers can map
    directly to NewFunction without a second pass over the params list.
    ``start_line`` / ``end_line`` are None for entries whose dump predates span
    recording; ``end_line`` (1-based, inclusive) lets span consumers intersect
    a function with diff hunks without re-parsing.
    """

    name: str
    kind: str
    arity: int
    required: int
    start_line: int | None
    body_hash: str | None
    body_hash_pnorm: str | None
    excerpt: str
    end_line: int | None = None


def select_candidates(
    catalog: FunctionCatalog,
    new_functions: list[NewFunction],
    *,
    exclude_file: str | None = None,
) -> list[dict]:
    """Prefilter the catalog to likely duplication candidates per new function.

    For each function in ``new_functions``, score every cataloged function by
    name-token overlap and keep those that (a) share at least the minimum number
    of domain tokens AND (b) have a close signature shape, EXCLUDING the file
    under review itself (a function never duplicates itself) and exact same-name
    matches in OTHER files (an exact-name collision is the flat key_exports
    signal's job, not the near-duplicate prefilter's). Candidates are ranked by
    overlap, then required-arity closeness, then name, and capped.

    When the file under review is production code, candidates that live in
    test files are dropped unless their body is an exact/param-normalized
    clone: a production function cannot legitimately reuse a test helper, and
    with the per-function candidate cap a token-overlap test match crowds a
    real production lead out of the list. A test file under review keeps its
    test-file candidates (re-implemented test helpers are a real dup class).

    Returns one entry per new function that has any candidate:
    ``{"function": {...}, "candidates": [{name, file, kind, arity, required,
    shared_tokens}, ...]}``. The caller reads each candidate's real body from
    disk and judges semantic equivalence; this list only narrows the search.
    """
    from chameleon_mcp.comprehension import _is_test_path

    min_tokens = threshold_int("DUPLICATION_MIN_SHARED_TOKENS")
    max_candidates = threshold_int("DUPLICATION_MAX_CANDIDATES_PER_FN")
    reviewing_production = exclude_file is not None and not _is_test_path(exclude_file)

    results: list[dict] = []
    for nf in new_functions:
        new_tokens = name_tokens(nf.name)
        # Generic-verb names (run, handle, process) tokenize to nothing, which
        # is exactly the naming a renamed clone hides behind — keep them in
        # play whenever a body fingerprint exists to pair on.
        if not new_tokens and not (nf.body_hash or nf.body_hash_pnorm):
            continue
        new_shape = (nf.arity, nf.required)
        scored: list[tuple[int, int, float, int, CatalogedFunction]] = []
        for cand in catalog.functions:
            if exclude_file is not None and cand.file == exclude_file:
                continue
            if cand.name == nf.name:
                # Exact-name collision is the flat key_exports / name-collision
                # check's responsibility; the near-duplicate prefilter targets
                # the DIFFERENT-name re-implementation case.
                continue
            # Identical normalized bodies pair regardless of name tokens: a
            # body-exact clone renamed with zero shared tokens is exactly the
            # LLM-duplication case the name prefilter cannot see. The
            # param-normalized hash extends this to clones whose only body
            # difference is renamed parameters.
            body_match = (bool(nf.body_hash) and nf.body_hash == cand.body_hash) or (
                bool(nf.body_hash_pnorm) and nf.body_hash_pnorm == cand.body_hash_pnorm
            )
            overlap = _overlap_score(new_tokens, cand)
            if not body_match:
                if overlap < min_tokens:
                    continue
                if not _arity_close(new_shape, (cand.arity, cand.required)):
                    continue
                # Production code cannot reuse a test helper, and the candidate
                # cap means a token-overlap test match evicts a real production
                # lead. A byte/param-identical clone (body_match) still passes:
                # copy-paste from a test into production is worth surfacing.
                if reviewing_production and _is_test_path(cand.file):
                    continue
            req_distance = abs(nf.required - cand.required)
            similarity = _jaccard(new_tokens, cand.tokens)
            scored.append((1 if body_match else 0, overlap, similarity, req_distance, cand))

        if not scored:
            continue
        # Rank body-identical matches first (strongest possible reuse lead),
        # then raw token overlap, then token-set similarity (so the closest-
        # shaped name wins the tie instead of the alphabetically-first one),
        # then required-arity closeness, then a stable name/file tiebreak.
        scored.sort(key=lambda t: (-t[0], -t[1], -t[2], t[3], t[4].name, t[4].file))
        candidates = [
            {
                "name": cand.name,
                "file": cand.file,
                "kind": cand.kind,
                "arity": cand.arity,
                "required": cand.required,
                "shared_tokens": sorted(new_tokens & cand.tokens),
                "body_match": bool(body_flag),
            }
            for body_flag, _overlap, _sim, _dist, cand in scored[:max_candidates]
        ]
        results.append(
            {
                "function": {
                    "name": nf.name,
                    "kind": nf.kind,
                    "arity": nf.arity,
                    "required": nf.required,
                },
                "candidates": candidates,
            }
        )
    return results
