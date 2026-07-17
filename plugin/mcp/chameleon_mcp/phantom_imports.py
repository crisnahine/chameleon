"""Phantom-import detection for chameleon's PostToolUse lint path.

Flags relative imports / require_relatives whose target resolves to no file on
disk - a high-precision hallucination signal (typo'd or invented file paths).
This is the ONE lint function that touches the filesystem; it lives outside
lint_engine.py to keep that module pure / no-I/O.

Conservative by construction: bare packages, unmapped tsconfig aliases,
non-code extensions, out-of-repo targets, and any I/O ambiguity are skipped (no
violation) rather than risk a false positive. Advisory only; never blocks an
edit.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

from chameleon_mcp.lint_engine import Violation
from chameleon_mcp.symbol_index import (
    exports_index_mtime,
    load_exports_index,
    load_reverse_index,
    module_key_for_path,
    resolve_index_key,
    resolve_python_index_key,
)

_RULE = "phantom-import"
_SYMBOL_RULE = "phantom-symbol"
# Advisory: editing a module surfaces who imports its bindings (cross-file
# blast radius), and a deterministic flag for an export removed/renamed out from
# under an indexed call site. Both read the prebuilt reverse index only -- no
# caller is re-parsed on the hot path. Suppress per-rule with chameleon-ignore.
_CROSSFILE_RULE = "cross-file-importers"
_BROKEN_EXPORT_RULE = "removed-export-breaks-importers"

# One `{ a, b as c, type D }` specifier inside an import clause. Captures the
# IMPORTED name (left of `as`) and an optional inline `type` prefix; the local
# alias is irrelevant to whether the source module exports the name.
_NAMED_SPEC_RE = re.compile(
    r"(?:(?P<inline_type>\btype\b)\s+)?(?P<name>[A-Za-z_$][\w$]*)(?:\s+as\s+[A-Za-z_$][\w$]*)?"
)
# Maximum named specifiers checked per import statement. A real import lists a
# handful; a generated re-export can list hundreds. The brace body is also
# length-bounded before parsing so a pathological clause can't drive the hot
# path.
_MAX_NAMED_SPECS = 200

# import/export ... from '<s>' | import('<s>') | require('<s>'). The
# `[^;'"]{0,8000}?` run is bounded so a pathological no-anchor "import <huge>"
# run cannot drive quadratic backtracking on the hot path. 8000 comfortably
# clears the largest real multiline import header observed across the test
# corpus (~3.5k chars, a big barrel re-export).
_TS_IMPORT_SPEC_RE = re.compile(
    r"""(?:import|export)\s[^;'"]{0,8000}?\bfrom\s*['"]([^'"]+)['"]"""
    r"""|\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)"""
    r"""|\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)""",
    re.MULTILINE,
)

# React Router v7 / Remix typegen: ./+types/page etc. are generated and
# resolved via tsconfig rootDirs, never real on-disk siblings; skip them.
_PLUS_SEGMENT_RE = re.compile(r"(^|/)\+")

_TS_CODE_SUFFIXES = ("", ".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".json")
_TS_INDEX_SUFFIXES = ("/index.ts", "/index.tsx", "/index.js", "/index.jsx", "/index.mjs")
# NodeNext/ESM: a .js/.jsx/.mjs/.cjs specifier commonly maps to a .ts source.
_JS_TO_TS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
_NON_CODE_EXT_RE = re.compile(
    r"\.(css|scss|sass|less|svg|png|jpe?g|gif|webp|avif|md|mdx|graphql|gql|ya?ml|vue|wasm)$",
    re.IGNORECASE,
)
_IGNORE_TS_RE = re.compile(r"//\s*chameleon-ignore\s+([\w-]+)")

_RUBY_REQUIRE_RELATIVE_RE = re.compile(r"^\s*require_relative\s+['\"]([^'\"]+)['\"]", re.MULTILINE)
_IGNORE_RUBY_RE = re.compile(r"#\s*chameleon-ignore\s+([\w-]+)")
# A Python relative import: `from .mod import x`, `from ..pkg.sub import y, z`,
# `from . import z`. group(1) = leading dots (relative level), group(2) = the
# dotted module after the dots (empty for `from . import`), group(3) = the
# imported-names clause to the end of the line (for the phantom-SYMBOL check).
# Absolute imports (`import os`, `from django.db import ...`) are not relative
# and unverifiable without sys.path, so they are not matched.
# The import clause is either a parenthesized body -- which may SPAN LINES (the
# dominant multi-name style: `from x import (\n a,\n b,\n)`) -- or the rest of a
# single line. The `\([^)]*\)` alternative crosses newlines because a negated
# char class matches newlines regardless of DOTALL, so a hallucinated name inside
# a parenthesized multi-line import is no longer invisible to the symbol check.
_PY_RELATIVE_IMPORT_RE = re.compile(
    r"^[ \t]*from\s+(\.+)([\w.]*)\s+import[ \t]*(\([^)]*\)|.*)$", re.MULTILINE
)
# Absolute `from pkg.mod import x` (first char not a dot). Only the SYMBOL
# check consumes these: an unresolvable absolute spec may be stdlib or a
# dependency, so the module itself is never flagged.
_PY_ABSOLUTE_IMPORT_RE = re.compile(
    r"^[ \t]*from\s+([A-Za-z_][\w.]*)\s+import[ \t]*(\([^)]*\)|.*)$", re.MULTILINE
)


def _py_imported_names(clause: str) -> list[str]:
    """Imported names from an ``import a, b as c`` clause, single- or multi-line.

    Returns the SOURCE name (left of any ``as``) of each binding -- the name the
    target module must export. Both the single-line parenthesized form
    (``from m import (a, b)``) and the multi-line form (``from m import (\\n a,\\n
    b,\\n)``, which PEP 8 and the formatters favor) are handled: the wrapping
    parens are stripped and each physical line's trailing ``# comment`` is dropped
    before splitting on commas, so a name is not lost to a per-line comment. A
    star import yields nothing.
    """
    # Strip a per-LINE inline comment (a multi-line clause can carry one per row),
    # then strip the wrapping parens. Rejoin to a single string for the comma
    # split; newlines between names are just whitespace the per-part strip drops.
    lines = [ln.split("#", 1)[0] for ln in clause.splitlines()]
    clause = " ".join(lines).strip().lstrip("(").rstrip(")").strip()
    if not clause or clause == "*":
        return []
    names: list[str] = []
    for part in clause.split(","):
        part = part.strip()
        if not part or part == "*":
            continue
        src = part.split(" as ", 1)[0].strip()
        if src.isidentifier():
            names.append(src)
    return names


_RUBY_SUFFIXES = ("", ".rb", ".so", ".bundle")

# Maximum import specifiers checked per file. A real module has a few dozen; a
# generated barrel/index can have thousands. Capping bounds stat() fan-out on
# the PostToolUse hot path (each TS spec probes up to ~14 candidate paths).
_MAX_SPECS = 200


def _scan_quoted_string(content: str, start: int) -> int | None:
    """Index just past a single-/double-quoted string literal opening at ``start``.

    Returns the index after the closing quote, or None when no closing quote
    appears before an unescaped newline. A real single-/double-quoted JS/TS
    literal never spans a raw newline, so a quote with no same-line close is NOT
    a string start: it is a JSX-text apostrophe (``It's broken``), a possessive
    in doc text, or a regex artifact. Treating it as a string would open a fake
    literal that swallows the rest of the file and masks every later export.
    Bounded by the same-line constraint, so no ReDoS risk.
    """
    quote = content[start]
    n = len(content)
    j = start + 1
    while j < n:
        ch = content[j]
        if ch == "\\":
            # An escaped char (incl. a line continuation) is consumed as part of
            # the literal; advance past it.
            j += 2
            continue
        if ch == "\n":
            return None
        if ch == quote:
            return j + 1
        j += 1
    return None


# A `/` opens a regex literal (not division) only in a position where an
# expression is expected. We approximate that position by the last significant
# non-space character before the slash: after an operator, an opening bracket, a
# comma, a return/typeof/etc. keyword, or at the start of input, a `/` is a
# regex. After an identifier, a `)`, a `]`, or a number it is division. This is
# the standard slash-disambiguation heuristic; getting it slightly wrong only
# changes whether a `/...` run is blanked, never whether real code is masked as
# a string.
_REGEX_PREV_OK = frozenset("(,=:[{;!&|?+-*%^~<>")


def _regex_allowed_at(prev_significant: str) -> bool:
    if prev_significant == "":
        return True  # start of input
    return prev_significant in _REGEX_PREV_OK


def _scan_regex_literal(content: str, start: int) -> int | None:
    """Index just past a regex literal opening with ``/`` at ``start``.

    Walks to the closing unescaped ``/`` on the same line, honoring character
    classes (``[...]`` where a ``/`` is literal). Returns None when no close is
    found before a newline (then the ``/`` was division, not a regex). Bounded
    to one line, so no backtracking.
    """
    n = len(content)
    j = start + 1
    in_class = False
    while j < n:
        ch = content[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "\n":
            return None
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            return j + 1
        j += 1
    return None


def _strip_ts_noise(content: str) -> tuple[str, list[bool]]:
    """Blank line/block comments, backtick template literals, and regex literals
    (so imports/exports embedded in fixtures/snapshots/docs/patterns aren't
    matched), preserving single- and double-quoted strings so real import
    specifiers survive.

    Returns the rewritten text plus a parallel mask: ``mask[i]`` is True when
    output char ``i`` was inside a single-/double-quoted string literal. The
    caller skips a regex match whose keyword sits in a masked region, so a code
    snippet stored *as a string value* (e.g. ``const c = "import x from './y'"``)
    is not mistaken for a real import.

    A single-/double-quoted string is only opened when its closing quote sits on
    the same line. A lone apostrophe in JSX text (``It's broken``) or a regex
    character class (``/[a-z'-]/``) therefore stays a normal char instead of
    opening a fake literal that would mask every export after it.

    Single linear pass with explicit lexer states - no regex backtracking, so
    an unterminated template literal cannot trigger catastrophic backtracking
    (ReDoS). Only ever removes text, so it cannot manufacture a false positive.
    """
    out: list[str] = []
    mask: list[bool] = []
    n = len(content)
    i = 0
    NORMAL, LINE, BLOCK, TMPL = range(4)
    state = NORMAL
    # Last non-space, non-newline char emitted in NORMAL state, used to decide
    # whether a `/` opens a regex literal or is division.
    prev_significant = ""

    def emit(s: str, in_str: bool) -> None:
        out.append(s)
        mask.extend([in_str] * len(s))

    while i < n:
        c = content[i]
        nxt = content[i + 1] if i + 1 < n else ""
        if state == NORMAL:
            if c == "/" and nxt == "/":
                state, i = LINE, i + 2
                emit("  ", False)
            elif c == "/" and nxt == "*":
                state, i = BLOCK, i + 2
                emit("  ", False)
            elif c == "/" and _regex_allowed_at(prev_significant):
                end = _scan_regex_literal(content, i)
                if end is not None:
                    # Blank the regex body so a quote/keyword inside it can't be
                    # read as a string or import.
                    emit(" " * (end - i), False)
                    i = end
                    prev_significant = "/"
                    continue
                # Not a regex (no same-line close) -> treat `/` as division.
                emit(c, False)
                prev_significant = c
                i += 1
            elif c == "`":
                state, i = TMPL, i + 1
                emit(" ", False)
                prev_significant = "`"
            elif c == "'" or c == '"':
                end = _scan_quoted_string(content, i)
                if end is not None:
                    emit(content[i:end], True)
                    i = end
                    prev_significant = c
                else:
                    # A quote with no same-line close is not a string start
                    # (JSX-text apostrophe, possessive, regex artifact); emit it
                    # as an ordinary char so the rest of the file stays visible.
                    emit(c, False)
                    prev_significant = c
                    i += 1
            else:
                emit(c, False)
                if not c.isspace():
                    prev_significant = c
                i += 1
        elif state == LINE:
            emit(c if c == "\n" else " ", False)
            if c == "\n":
                state = NORMAL
            i += 1
        elif state == BLOCK:
            if c == "*" and nxt == "/":
                state, i = NORMAL, i + 2
                emit("  ", False)
            else:
                emit(c if c == "\n" else " ", False)
                i += 1
        else:  # TMPL
            if c == "\\":
                emit("  ", False)
                i += 2
            elif c == "`":
                state, i = NORMAL, i + 1
                emit(" ", False)
                prev_significant = "`"
            else:
                emit(c if c == "\n" else " ", False)
                i += 1
    return "".join(out), mask


def _violation(spec: str, search_dir: Path, root: Path | None) -> Violation:
    try:
        disp = str(search_dir.relative_to(root)) if root is not None else str(search_dir)
    except (ValueError, OSError):
        disp = str(search_dir)
    return Violation(
        rule=_RULE,
        expected=disp,
        actual=spec,
        severity="warning",
        message=(
            f"phantom-import: '{spec}' resolves to no file on disk. "
            "Likely a typo or a file that doesn't exist; verify the path."
        ),
    )


def _symbol_violation(name: str, spec: str) -> Violation:
    return Violation(
        rule=_SYMBOL_RULE,
        expected=spec,
        actual=name,
        severity="warning",
        message=(
            f"phantom-symbol: '{name}' is not exported by '{spec}'. "
            "Likely a hallucinated or renamed binding; the import resolves to a "
            "real file but the name is missing from its exports."
        ),
    )


def _named_specifiers(import_text: str) -> list[str] | None:
    """Imported names from an import statement's ``{ ... }`` clause.

    Returns the list of IMPORTED names (left of any `as`), or None when the
    statement carries no checkable named clause: a side-effect import, a
    type-only `import type { ... }`, or a namespace import. Default and namespace
    bindings outside the braces are ignored (default/namespace imports are not
    symbol-checkable here). Inline `type` specifiers and a `default as X`
    re-bind are dropped -- the former is a type position and the latter targets
    the default export, which the named index does not record.
    """
    # `import type { ... }` is a pure type import; the whole clause is types.
    if re.match(r"\s*import\s+type\b", import_text):
        return None
    open_brace = import_text.find("{")
    if open_brace == -1:
        return None  # side-effect, default-only, or namespace import
    close_brace = import_text.find("}", open_brace)
    if close_brace == -1:
        return None
    body = import_text[open_brace + 1 : close_brace]
    if len(body) > 16_000:
        return None  # implausibly large clause; skip rather than parse on hot path
    names: list[str] = []
    for part in body.split(","):
        part = part.strip()
        if not part:
            continue
        m = _NAMED_SPEC_RE.match(part)
        if not m:
            continue
        if m.group("inline_type"):
            continue  # `type Foo` inline specifier -> type position, not a value
        name = m.group("name")
        if name == "default":
            continue  # `default as X` targets the default export (not indexed)
        names.append(name)
        if len(names) >= _MAX_NAMED_SPECS:
            break
    return names or None


def _exists_with_suffix(base: Path) -> bool:
    """True if `base` (a path without an assumed extension) resolves to a real
    file via a standard TS/JS candidate suffix or index file, or is a directory.

    On any OSError, returns True (treat ambiguity as resolved, no flag). This is
    the shared suffix-probing used both for relative specifiers and for resolved
    tsconfig path-alias targets, so the two paths can't disagree."""
    try:
        s = str(base)
        # NodeNext/ESM: a .js-family specifier may map to a .ts source on disk.
        for js_ext, ts_exts in _JS_TO_TS.items():
            if s.endswith(js_ext):
                stem = s[: -len(js_ext)]
                for te in ts_exts:
                    if Path(stem + te).is_file():
                        return True
        for suf in _TS_CODE_SUFFIXES:
            if suf == "":
                if base.is_file():
                    return True
            elif Path(s + suf).is_file():
                return True
        for suf in _TS_INDEX_SUFFIXES:
            if Path(s + suf).is_file():
                return True
        return base.is_dir()
    except OSError:
        return True


def _resolved_file_mtime(base: Path) -> float | None:
    """mtime of the on-disk file ``base`` resolves to (TS/JS suffix probe), else None.

    Mirrors ``_exists_with_suffix``'s candidate order but returns the mtime of the
    first real file so the phantom-symbol check can tell whether the target module
    was edited since the exports index was built. None on any ambiguity/error."""
    try:
        s = str(base)
        for js_ext, ts_exts in _JS_TO_TS.items():
            if s.endswith(js_ext):
                stem = s[: -len(js_ext)]
                for te in ts_exts:
                    p = Path(stem + te)
                    if p.is_file():
                        return p.stat().st_mtime
        for suf in _TS_CODE_SUFFIXES:
            p = base if suf == "" else Path(s + suf)
            if p.is_file():
                return p.stat().st_mtime
        for suf in _TS_INDEX_SUFFIXES:
            p = Path(s + suf)
            if p.is_file():
                return p.stat().st_mtime
    except OSError:
        return None
    return None


def _py_target_stale(root: Path | None, key: str | None, index_mtime: float | None) -> bool:
    """True if the Python target module (``root/key``) was edited AFTER the exports
    index was built -- so its indexed export set is stale and must not drive a
    false phantom-symbol flag. False on any ambiguity (fail open, i.e. still flag
    only when the index is trustworthy)."""
    if index_mtime is None or key is None or root is None:
        return False
    try:
        return (root / key).stat().st_mtime > index_mtime
    except OSError:
        return False


def _load_tsconfig_paths(
    repo_root_str: str,
) -> tuple[str | None, tuple[tuple[str, tuple[str, ...]], ...]]:
    """(baseUrl, ((pattern, (targets,...)),...)) from tsconfig/jsconfig.

    Reads the repo-root tsconfig directly so alias resolution works even when the
    caller's profile rules carry no `paths` (e.g. calibration runs against a
    fresh checkout).

    ``baseUrl`` is ``None`` when neither config file is present or readable --
    distinct from a config that exists and simply omits ``baseUrl`` (which
    defaults to ``"."``, the project root, per tsconfig semantics). A caller
    resolving a bare specifier against baseUrl must be able to tell "no
    baseUrl was ever configured" from "baseUrl is the project root": both are
    falsy vs. truthy only if the no-config case does NOT also collapse to
    ``"."``.

    Read fresh each call (no cache) so a tsconfig edited mid-session is picked up
    immediately, matching the cache-free filesystem walk in _nearest_tsconfig_dir.
    """
    root = Path(repo_root_str)
    for name in ("tsconfig.json", "jsconfig.json"):
        p = root / name
        try:
            if not p.is_file():
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        co = data.get("compilerOptions")
        co = co if isinstance(co, dict) else {}
        base = co.get("baseUrl") or "."
        paths = co.get("paths")
        paths = paths if isinstance(paths, dict) else {}
        norm = tuple((k, tuple(v)) for k, v in paths.items() if isinstance(v, list))
        return base, norm
    return None, ()


def _resolves_via_alias(
    spec: str,
    repo_root: Path,
    tsconfig_paths: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
) -> bool:
    """True if `spec` matches a tsconfig/jsconfig `paths` alias at the repo root,
    whether or not the mapped target resolves to a real file.

    A specifier that matches a declared alias pattern but resolves to nothing is
    ambiguous (generated output dir, build artifact, etc.), so it is treated as
    resolved rather than flagged as phantom. Only the pattern match matters; the
    mapped target need not exist on disk.

    `tsconfig_paths` lets a caller that scans many specifiers in one pass load
    the tsconfig once and reuse it, avoiding a per-specifier disk read. When
    omitted the config is read here so the function stays usable standalone."""
    if tsconfig_paths is None:
        _, tsconfig_paths = _load_tsconfig_paths(str(repo_root))
    for pattern, _ in tsconfig_paths:
        if pattern.endswith("/*"):
            # Wildcard: the trailing slash anchors the prefix so `@app/*` matches
            # `@app/x` but not `@apple/x`.
            if spec.startswith(pattern[:-1]):
                return True
        elif spec == pattern:
            # Exact alias: match only the bare specifier, not `@application/foo`
            # for an `@app` alias.
            return True
    return False


def _ruby_resolves(base: Path) -> bool:
    try:
        for suf in _RUBY_SUFFIXES:
            if suf == "":
                if base.is_file():
                    return True
            elif Path(str(base) + suf).is_file():
                return True
        return base.is_dir()
    except OSError:
        return True


def _py_resolves(base: Path) -> bool:
    """True if a Python relative-import module ``base`` resolves to a file.

    A module is ``base.py`` / ``base.pyi``; a package is ``base/__init__.py``.
    Fails open (returns True) on any OS error so an unreadable tree never flags.
    """
    try:
        for suf in (".py", ".pyi"):
            if Path(str(base) + suf).is_file():
                return True
        return (base / "__init__.py").is_file() or (base / "__init__.pyi").is_file()
    except OSError:
        return True


def _py_first_party_top(top: str, src_roots: list[Path]) -> Path | None:
    """The source root under which ``top`` (an absolute import's leading dotted
    segment) exists as a package directory or a single-file module, or None.

    Deliberately looser than ``_py_resolves``: a PEP 420 namespace package has
    no ``__init__.py`` to key on, so this checks plain directory presence
    rather than requiring one. It answers a narrower question than resolution
    does -- not "does the full spec resolve" but "does the repo own this
    top-level name at all" -- which is what separates a first-party absolute
    import from an external dependency or stdlib module, both of which have no
    directory here to find.
    """
    for src_root in src_roots:
        try:
            if (
                (src_root / top).is_dir()
                or (src_root / f"{top}.py").is_file()
                or (src_root / f"{top}.pyi").is_file()
            ):
                return src_root
        except OSError:
            continue
    return None


def _safe_is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def _safe_is_file(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


def _under_repo(fp: Path, root: Path | None) -> bool:
    """True if `fp` resolves to a path inside `root` (or root is unknown).

    Used both for the edited file and for resolved import targets, so a `../`
    spec that escapes the repo is skipped (never statted outside, never
    flagged)."""
    if root is None:
        return True
    try:
        fp.resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _nearest_tsconfig_dir(file_dir: Path, root: Path | None) -> Path | None:
    """Directory of the nearest tsconfig.json walking up from file_dir to root
    (inclusive), or None. In a monorepo each app has its own tsconfig, so the
    nearest one, not the profile's single stored tsconfig, is the correct
    anchor for `@/*`-style aliases (which are relative to their tsconfig dir)."""
    cur = file_dir
    while True:
        try:
            if (cur / "tsconfig.json").is_file():
                return cur
        except OSError:
            return None
        if root is None or cur == root or cur.parent == cur:
            return None
        cur = cur.parent


def _alias_targets(spec: str, paths: dict, ts_config_dir: Path) -> list[Path]:
    """Candidate absolute base paths for a tsconfig path-alias spec.

    Empty when no alias key matches (bare package or unmapped alias). Targets
    are resolved relative to the tsconfig directory (baseUrl default)."""
    out: list[Path] = []
    for key, targets in (paths or {}).items():
        if not isinstance(targets, list):
            continue
        if "*" in key:
            prefix, _, suffix = key.partition("*")
            if not (spec.startswith(prefix) and spec.endswith(suffix)):
                continue
            if len(spec) < len(prefix) + len(suffix):
                continue
            middle = spec[len(prefix) : len(spec) - len(suffix)] if suffix else spec[len(prefix) :]
            for t in targets:
                if isinstance(t, str):
                    out.append(ts_config_dir / t.replace("*", middle, 1))
        elif spec == key:
            for t in targets:
                if isinstance(t, str):
                    out.append(ts_config_dir / t)
    return out


def lint_phantom_imports(
    content: str,
    *,
    file_path: str | None,
    repo_root: Path | str | None,
    language: str | None,
    rules: dict | None = None,
) -> list[Violation]:
    """Flag relative / tsconfig-alias imports (and Ruby require_relatives) whose
    target resolves to no file on disk. Returns `[]` on any ambiguity."""
    if not file_path or not language:
        return []
    try:
        root = Path(repo_root).resolve() if repo_root else None
    except OSError:
        return []
    fp = Path(file_path)
    if not fp.is_absolute():
        # Accept a repo-relative path by anchoring it to repo_root; without a
        # root there is no anchor and the location can't be resolved -> skip.
        if root is None:
            return []
        fp = root / fp
    if not _under_repo(fp, root):
        return []
    file_dir = fp.parent

    violations: list[Violation] = []
    if language == "typescript":
        _ignored = {m.group(1) for m in _IGNORE_TS_RE.finditer(content)}
        if _RULE in _ignored:
            return []
        # phantom-symbol can be suppressed independently of phantom-import; the
        # symbol check resolves and loads the export index only when it is on.
        symbol_check_on = _SYMBOL_RULE not in _ignored
        exports_index = load_exports_index(root) if symbol_check_on else None
        symbol_check_on = symbol_check_on and exports_index is not None
        _ts_index_mtime = exports_index_mtime(root) if symbol_check_on else None
        ts_rules = ((rules or {}).get("rules") or {}).get("typescript") or {}
        paths = ts_rules.get("paths") or {}
        source = ts_rules.get("source") or "tsconfig.json"
        # Anchor aliases to the nearest tsconfig (monorepo-correct); fall back to
        # the profile's stored tsconfig dir only if none is found walking up.
        ts_config_dir = _nearest_tsconfig_dir(file_dir, root) or (
            (root / source).parent if root else file_dir
        )
        # Read the on-disk tsconfig/jsconfig alias map once per call. The alias
        # branch below consults it for every non-relative specifier; re-reading
        # per specifier made calibration O(N*M) in tsconfig reads.
        tsconfig_alias_paths: tuple[tuple[str, tuple[str, ...]], ...] = ()
        if root is not None:
            _, tsconfig_alias_paths = _load_tsconfig_paths(str(root))

        # Memoize suffix probes within this call so a path probed for one
        # specifier (relative base or alias target) is not re-stat'd for another.
        _exists_memo: dict[str, bool] = {}

        def _exists_cached(p: Path) -> bool:
            key = str(p)
            if key not in _exists_memo:
                _exists_memo[key] = _exists_with_suffix(p)
            return _exists_memo[key]

        stripped, str_mask = _strip_ts_noise(content)
        seen: set[str] = set()
        # The resolved-key cache is per-file: two imports from the same module
        # resolve to the same on-disk path, so the index key is computed once.
        _key_memo: dict[str, str | None] = {}

        def _symbol_check(m, base: Path, spec: str) -> None:
            # Each named specifier of a resolved-path `import` is checked against
            # the target module's exported set. Runs once per import STATEMENT
            # (not once per module path): two imports from the same module carry
            # different bindings, so the second must be checked even when the
            # path-resolution dedup already saw the spec. Skips `export { x }
            # from` re-exports (group(1) covers both; the `import` prefix narrows
            # to the binding the editing file references) and any target whose
            # set is open or absent from the index.
            if not symbol_check_on or m.group(1) is None:
                return
            import_text = m.group(0)
            if not import_text.lstrip().startswith("import"):
                return
            names = _named_specifiers(import_text)
            if not names:
                return
            if spec not in _key_memo:
                _key_memo[spec] = resolve_index_key(base, root)  # type: ignore[arg-type]
            key = _key_memo[spec]
            entry = exports_index.lookup(key) if key is not None else None
            if entry is None or entry.open:
                return
            # Stale-index guard: if the TARGET module file was edited since the
            # exports index was built (a same-turn export rename, or a genuinely
            # out-of-date committed index), its indexed name set is not
            # authoritative -- fail open (no phantom-symbol flag) rather than emit a
            # false "not exported" for a name the file now DOES export. Cheap mtime
            # check, only paid on the pre-flag path; mirrors the Stop cross-file
            # live re-verify.
            if _ts_index_mtime is not None:
                _tgt_mtime = _resolved_file_mtime(base)
                if _tgt_mtime is not None and _tgt_mtime > _ts_index_mtime:
                    return
            for nm in names:
                if nm not in entry.names:
                    violations.append(_symbol_violation(nm, spec))

        for m in _TS_IMPORT_SPEC_RE.finditer(stripped):
            if len(seen) >= _MAX_SPECS:
                break
            # Skip a match whose keyword sits inside a string literal: that's a
            # code snippet stored as a string value, not a real import.
            if str_mask[m.start()]:
                continue
            raw = m.group(1) or m.group(2) or m.group(3)
            if not raw:
                continue
            # Drop bundler query/fragment suffixes (vite/webpack): svgr's
            # `./icon.svg?react`, `?url`, `?raw`, `?worker`, etc.
            spec = raw.split("?", 1)[0].split("#", 1)[0]
            if not spec:
                continue
            # A module path already path-checked this file is not stat'd again
            # (the phantom-import dedup), but a repeat relative `import` still
            # carries its own named bindings, so symbol-check it before skipping.
            if spec in seen:
                if spec.startswith("."):
                    base = file_dir / spec
                    if _under_repo(base, root) and _exists_cached(base):
                        _symbol_check(m, base, spec)
                continue
            seen.add(spec)
            if _NON_CODE_EXT_RE.search(spec):
                continue
            if _PLUS_SEGMENT_RE.search(spec):
                continue  # framework-generated typegen (e.g. ./+types/page)
            if spec.startswith("."):
                base = file_dir / spec
                if not _under_repo(base, root):
                    continue  # escapes the repo -> don't stat outside, don't flag
                if not _exists_cached(base):
                    violations.append(_violation(spec, base.parent, root))
                    continue
                _symbol_check(m, base, spec)
                continue
            # tsconfig path alias?
            if root is None:
                continue
            # Symbol-check the alias target before any phantom-import skip. Build
            # candidate base paths from the on-disk tsconfig `paths` (the
            # authoritative source, present even when the profile rules carry no
            # `paths`), falling back to the profile `paths`. When a candidate
            # resolves to a real file, run the same named-binding check the
            # relative branch runs, so a hallucinated symbol in an alias import
            # (the dominant import style in many repos) is caught, not silently
            # passed. File-existence flagging below stays exactly as conservative
            # as before -- the symbol check never widens phantom-import.
            on_disk_paths = {k: list(v) for k, v in tsconfig_alias_paths}
            symbol_targets = [
                t
                for t in _alias_targets(spec, on_disk_paths, ts_config_dir)
                if _under_repo(t, root)
            ]
            if not symbol_targets:
                symbol_targets = [
                    t for t in _alias_targets(spec, paths, ts_config_dir) if _under_repo(t, root)
                ]
            resolved = next((t for t in symbol_targets if _exists_cached(t)), None)
            if resolved is not None:
                _symbol_check(m, resolved, spec)

            # Consult the on-disk tsconfig/jsconfig directly: an aliased import
            # whose pattern is declared there must never be flagged as a phantom
            # file, even when the caller's profile rules carry no `paths` (e.g.
            # calibration on a fresh checkout). Aliases routinely point at
            # generated output / build dirs, so a declared-but-unresolved alias
            # is treated as resolved.
            if _resolves_via_alias(spec, root, tsconfig_alias_paths):
                continue
            targets = [
                t for t in _alias_targets(spec, paths, ts_config_dir) if _under_repo(t, root)
            ]
            if not targets:
                continue  # bare package, unmapped alias, or out-of-repo -> skip
            if any(_exists_cached(t) for t in targets):
                continue
            # baseUrl uncertainty guard: only flag when a resolved parent dir
            # actually exists (typo within a real dir); else skip.
            if any(_safe_is_dir(t.parent) for t in targets):
                violations.append(_violation(spec, targets[0].parent, root))
    elif language == "ruby":
        if _RULE in {m.group(1) for m in _IGNORE_RUBY_RE.finditer(content)}:
            return []
        seen_rb: set[str] = set()
        for m in _RUBY_REQUIRE_RELATIVE_RE.finditer(content):
            if len(seen_rb) >= _MAX_SPECS:
                break
            spec = m.group(1)
            if spec in seen_rb:
                continue
            seen_rb.add(spec)
            if spec.startswith("/") or "#{" in spec:
                continue  # absolute path or string interpolation -> can't verify
            base = file_dir / spec
            if not _under_repo(base, root):
                continue
            if _ruby_resolves(base):
                continue
            # Conservative guard (mirrors the TS alias path): only flag when the
            # immediate parent dir exists; a missing parent means a generated /
            # cross-checkout tree (e.g. EE/CE split) or a heredoc fixture, not a
            # typo we can be confident about.
            if not _safe_is_dir(base.parent):
                continue
            violations.append(_violation(spec, base.parent, root))
    elif language == "python":
        # Blank string literals (keep comments) before both scans: a `from .x
        # import y` inside a docstring is not an import, and a `# chameleon-ignore`
        # inside a string is not a directive. Length-preserving, so the line a
        # phantom-import reports stays truthful.
        from chameleon_mcp.lint_engine import _blank_python_strings

        scan = _blank_python_strings(content)
        _ignored_py = {m.group(1) for m in _IGNORE_RUBY_RE.finditer(scan)}
        if _RULE in _ignored_py:
            return []
        symbol_check_on = _SYMBOL_RULE not in _ignored_py
        exports_index = load_exports_index(root) if symbol_check_on else None
        symbol_check_on = symbol_check_on and exports_index is not None
        _py_index_mtime = exports_index_mtime(root) if symbol_check_on else None
        seen_py: set[str] = set()
        for m in _PY_RELATIVE_IMPORT_RE.finditer(scan):
            if len(seen_py) >= _MAX_SPECS:
                break
            dots, module, names_clause = m.group(1), m.group(2), m.group(3)
            spec = dots + module
            if not module:
                # `from . import x` targets the current package (always present),
                # so it is never a phantom-IMPORT; the bound name is checked below.
                base = file_dir
            else:
                base = file_dir
                for _ in range(len(dots) - 1):
                    base = base.parent
                base = base / Path(module.replace(".", "/"))
            if not _under_repo(base, root):
                continue
            resolves = _py_resolves(base) if module else True
            # phantom-import: a relative module that resolves to no file on disk.
            if module and not resolves and spec not in seen_py:
                # Same conservative guard as the Ruby/TS paths: only flag when the
                # immediate parent dir exists.
                if _safe_is_dir(base.parent):
                    violations.append(_violation(spec, base.parent, root))
            seen_py.add(spec)
            # phantom-symbol: the module resolves, but a named binding it imports
            # is absent from that module's (closed) export set.
            if symbol_check_on and resolves and module:
                key = resolve_python_index_key(base, root)
                entry = exports_index.lookup(key) if key is not None else None
                if (
                    entry is not None
                    and not entry.open
                    and not _py_target_stale(root, key, _py_index_mtime)
                ):
                    for nm in _py_imported_names(names_clause):
                        if nm not in entry.names:
                            violations.append(_symbol_violation(nm, spec))
        # Absolute first-party `from pkg.mod import name`. An unresolvable spec
        # whose top-level segment is NOT one of the repo's own source roots is
        # never flagged (it may be stdlib or a dependency, both invisible to the
        # repo); but one whose top-level segment IS a first-party root package
        # is a phantom MODULE, and a spec resolving to a real in-repo module
        # whose CLOSED export set lacks a bound name is a phantom SYMBOL — the
        # highest-frequency hallucination shape, and a repo whose own idiom is
        # absolute imports (most Flask/Django apps) previously got no check at
        # all for either, because only relative forms were scanned.
        if symbol_check_on:
            _resolve_abs = None
            _first_party_roots: list[Path] = []
            for m in _PY_ABSOLUTE_IMPORT_RE.finditer(scan):
                if len(seen_py) >= _MAX_SPECS:
                    break
                module, names_clause = m.group(1), m.group(2)
                if module in seen_py:
                    continue
                seen_py.add(module)
                names = _py_imported_names(names_clause)
                if not names:
                    continue
                if _resolve_abs is None:
                    # Built on first need: construction scans the repo root's
                    # top-level dirs for Python source roots, a cost an edit
                    # with no absolute imports must not pay.
                    from chameleon_mcp.symbol_index import (
                        _python_source_roots,
                        make_module_resolver,
                    )

                    _resolve_abs = make_module_resolver(root, "python")
                    _first_party_roots = _python_source_roots(root)
                key = _resolve_abs(module, file_dir)
                if key is None:
                    top = module.split(".", 1)[0]
                    match_root = _py_first_party_top(top, _first_party_roots)
                    if match_root is not None:
                        # A first-party top-level segment is necessary but NOT
                        # sufficient: the resolver returns None for a real PEP 420
                        # namespace subpackage too (a directory with no
                        # __init__.py, e.g. readthedocs/proxito/views/), which is a
                        # valid import target, not a phantom. Only flag when the
                        # FULL dotted path resolves to no directory or module file.
                        rel_mod = Path(module.replace(".", "/"))
                        mod_path = match_root / rel_mod
                        if not (
                            _safe_is_dir(mod_path)
                            or _safe_is_file(mod_path.with_suffix(".py"))
                            or _safe_is_file(mod_path.with_suffix(".pyi"))
                        ):
                            violations.append(_violation(module, match_root / rel_mod.parent, root))
                    continue
                entry = exports_index.lookup(key)
                if entry is None or entry.open:
                    continue
                # Stale-index guard: the target module was edited since the index
                # was built (same-turn rename or an out-of-date committed index),
                # so its export set is not authoritative -- fail open.
                if _py_target_stale(root, key, _py_index_mtime):
                    continue
                # A package __init__'s export set lists sibling submodules, but
                # a PEP 420 namespace subpackage (a directory with no __init__)
                # is unenumerable at dump time, and an index built by an older
                # engine may predate submodule listing. Reality on disk beats
                # the index: a name that exists as the package's submodule file
                # or subpackage directory is a real import, not a phantom.
                pkg_dir = (
                    root / Path(key).parent
                    if key.endswith(("__init__.py", "__init__.pyi"))
                    else None
                )
                for nm in names:
                    if nm in entry.names:
                        continue
                    if pkg_dir is not None and (
                        _safe_is_dir(pkg_dir / nm)
                        or (pkg_dir / f"{nm}.py").is_file()
                        or (pkg_dir / f"{nm}.pyi").is_file()
                    ):
                        continue
                    violations.append(_symbol_violation(nm, module))
    return violations


# Direct named exports off a single statement: `export const|let|var|function|
# class|interface|type|enum|namespace foo`. `default` is excluded by the keyword
# alternation. Async/generator function modifiers sit before `function`, so the
# name capture anchors on the declaring keyword, not the `export` token.
_TS_EXPORT_DECL_RE = re.compile(
    r"\bexport\s+(?:abstract\s+|declare\s+|async\s+)*"
    r"(?:const|let|var|function\s*\*?|class|interface|type|enum|namespace)\s+"
    r"([A-Za-z_$][\w$]*)"
)
# `export { a, b as c }` / `export { x } from './m'` / `export type { T } from './m'`:
# each element's EXPORTED name is the one to the right of any `as`, which is what
# an importer references. The optional `type` modifier (`export type { ... }`, a
# type-only re-export) must be matched -- missing it dropped those names, so an
# importer of a re-exported type read as a broken existence-break on a clean file.
_TS_EXPORT_CLAUSE_RE = re.compile(r"\bexport\s*(?:type\s+)?\{([^}]*)\}")
# A leading inline `type ` modifier on a single specifier (`export { type Foo }` /
# `export { type Foo as Bar }`), stripped so the real name is read (not the `type`
# keyword). A bare `type` specifier (a value literally named `type`) has no
# following identifier, so the lookahead leaves it intact.
_TS_INLINE_TYPE_MODIFIER_RE = re.compile(r"^type\s+(?=[A-Za-z_$])")
# `export * from './m'` (no `as`): pulls in an unenumerable set, so the current
# export set can't be trusted -- skip both cross-file checks for the file.
_TS_EXPORT_STAR_RE = re.compile(r"\bexport\s*\*\s*from\b")
# `export * as ns from './m'`: unlike the bare star form this exports exactly
# ONE enumerable name (the namespace alias). Missing it made the export set
# both incomplete AND closed, so importers of the alias read as existence
# breaks on pristine files.
_TS_EXPORT_STAR_AS_RE = re.compile(r"\bexport\s*\*\s*as\s+([A-Za-z_$][\w$]*)\s+from\b")
_CLAUSE_NAME_RE = re.compile(r"[A-Za-z_$][\w$]*(?:\s+as\s+([A-Za-z_$][\w$]*))?")
# `export const|let|var { a, b: c, ...rest } = fn()` / `[a, , b] = arr`:
# destructuring binds the names ts_dump.mjs records via Object/ArrayBindingPattern.
# The live re-parse must extract them too, or an importer of a destructured export
# is falsely flagged as a broken existence-break on an unmodified file.
_TS_EXPORT_DESTRUCTURE_RE = re.compile(r"\bexport\s+(?:declare\s+)?(?:const|let|var)\s+([{\[])")
_LEADING_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_MAX_EXPORT_NAMES = 1000


def _balanced_pattern(stripped: str, open_idx: int) -> tuple[str | None, str | None]:
    """Body inside the destructuring pattern opening at ``open_idx``, and its outer
    bracket char. Counts both bracket families so ``{ a: [b] }`` balances; returns
    ``(None, None)`` if unbalanced within a sane bound (caller treats as open)."""
    outer = stripped[open_idx]
    depth = 0
    for i in range(open_idx, min(len(stripped), open_idx + 16_000)):
        c = stripped[i]
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return stripped[open_idx + 1 : i], outer
    return None, None


def _destructured_names(body: str, outer: str) -> tuple[set[str], bool]:
    """Bound names from a FLAT destructuring pattern body, plus an open flag.

    A nested pattern (a `{`/`[` inside the body) is not confidently enumerable
    here, so it returns ``(set(), True)`` and the caller marks the whole file's
    export set non-authoritative -- suppressing the broken-export check rather
    than risking a false positive. For a flat object, ``prop: local`` binds the
    local (right of `:`), defaults (`= x`) and rest (`...r`) are handled; array
    holes (empty parts) are skipped."""
    if "{" in body or "[" in body:
        return set(), True
    out: set[str] = set()
    for part in body.split(","):
        part = part.strip()
        if not part:
            continue  # array hole
        if part.startswith("..."):
            part = part[3:].strip()
        lhs = part.split("=", 1)[0].strip()  # drop a default value
        if outer == "{" and ":" in lhs:
            lhs = lhs.split(":", 1)[1].strip()  # the bound local, not the property key
        m = _LEADING_IDENT_RE.match(lhs)
        if m:
            out.add(m.group(0))
    return out, False


# Memo for _current_export_names: a PURE function of the file text whose
# _strip_ts_noise pass costs milliseconds per file, called once per finding by
# the crossfile passes (the same module text recurs across findings and
# calls). The cached value is a tiny (frozenset, bool); keyed by content
# digest, so a stale entry is impossible by construction. Bounded.
_EXPORT_NAMES_CACHE: dict = {}
_EXPORT_NAMES_CACHE_CAP = 2048
_EXPORT_NAMES_CACHE_MAX_TEXT = 256 * 1024


def _current_export_names(content: str) -> tuple[frozenset[str], bool]:
    """Names the edited TS file currently exports, plus an open-set flag.

    Regex over comment/template-stripped content (so an export written inside a
    string or doc comment doesn't count). Mirrors the export shapes ts_dump.mjs
    records for the bootstrap index, so the live read and the stored index agree
    on what "exported" means. Returns ``(names, open)``; ``open`` True means the
    file does ``export * from`` and its set can't be enumerated -- the caller
    then skips the cross-file checks rather than reason off a partial set.
    """
    import hashlib

    cache_key = None
    if len(content) <= _EXPORT_NAMES_CACHE_MAX_TEXT:
        cache_key = hashlib.blake2b(
            content.encode("utf-8", "surrogatepass"), digest_size=16
        ).digest()
        hit = _EXPORT_NAMES_CACHE.get(cache_key)
        if hit is not None:
            return hit
    result = _current_export_names_uncached(content)
    if cache_key is not None:
        if len(_EXPORT_NAMES_CACHE) >= _EXPORT_NAMES_CACHE_CAP:
            _EXPORT_NAMES_CACHE.pop(next(iter(_EXPORT_NAMES_CACHE)))
        _EXPORT_NAMES_CACHE[cache_key] = result
    return result


def _current_export_names_uncached(content: str) -> tuple[frozenset[str], bool]:
    stripped, mask = _strip_ts_noise(content)

    def _masked(idx: int) -> bool:
        # _strip_ts_noise preserves quoted-string CONTENT (so import specifiers
        # survive) and flags it in the mask; an `export` token inside a string is
        # a code-as-data snippet, not a real export, so skip it.
        return 0 <= idx < len(mask) and mask[idx]

    star = _TS_EXPORT_STAR_RE.search(stripped)
    if star and not _masked(star.start()):
        return frozenset(), True
    names: set[str] = set()
    for m in _TS_EXPORT_STAR_AS_RE.finditer(stripped):
        if _masked(m.start()):
            continue
        names.add(m.group(1))
        if len(names) >= _MAX_EXPORT_NAMES:
            return frozenset(names), False
    for m in _TS_EXPORT_DECL_RE.finditer(stripped):
        if _masked(m.start()):
            continue
        names.add(m.group(1))
        if len(names) >= _MAX_EXPORT_NAMES:
            return frozenset(names), False
    for clause in _TS_EXPORT_CLAUSE_RE.finditer(stripped):
        if _masked(clause.start()):
            continue
        body = clause.group(1)
        if len(body) > 16_000:
            # An implausibly large clause; treat the set as non-authoritative
            # rather than parse it on the hot path.
            return frozenset(), True
        for part in body.split(","):
            part = part.strip()
            if not part:
                continue
            # Drop a leading inline `type ` modifier so `export { type Foo }` reads
            # `Foo`, not the `type` keyword (and does not drop `Foo`).
            part = _TS_INLINE_TYPE_MODIFIER_RE.sub("", part)
            mm = _CLAUSE_NAME_RE.match(part)
            if not mm:
                continue
            # The exported name is the alias when `as` is present, else the bare
            # identifier; `_CLAUSE_NAME_RE` group(1) is the alias.
            exported = mm.group(1) or part.split()[0]
            if exported and exported != "default":
                names.add(exported)
            if len(names) >= _MAX_EXPORT_NAMES:
                return frozenset(names), False
    for dm in _TS_EXPORT_DESTRUCTURE_RE.finditer(stripped):
        if _masked(dm.start()):
            continue
        body, outer = _balanced_pattern(stripped, dm.start(1))
        if body is None or outer is None:
            # Unbalanced within bound: don't trust a partial parse, mark open.
            return frozenset(), True
        dnames, dopen = _destructured_names(body, outer)
        if dopen:
            return frozenset(), True
        names.update(dnames)
        if len(names) >= _MAX_EXPORT_NAMES:
            return frozenset(names), False
    return frozenset(names), False


def _crossfile_violation(name: str, count: int, sample: list) -> Violation:
    """Advisory: how many files import `name` from the edited module."""
    noun = "file" if count == 1 else "files"
    where = ""
    if sample:
        shown = ", ".join(s for s in sample[:3])
        more = " ..." if count > len(sample[:3]) else ""
        where = f" ({shown}{more})"
    return Violation(
        rule=_CROSSFILE_RULE,
        expected=name,
        actual=str(count),
        severity="info",
        message=(
            f"cross-file: {count} {noun} import '{name}' from this module{where}. "
            "Renaming or removing it changes their call sites; update them in the "
            "same change."
        ),
    )


def _broken_export_violation(name: str, importers: list) -> Violation:
    """Deterministic: the edited module no longer exports `name`, but an indexed
    importer still references it -- the call site is now broken."""
    sites = ", ".join(s for s in importers[:5])
    more = " ..." if len(importers) > 5 else ""
    return Violation(
        rule=_BROKEN_EXPORT_RULE,
        expected=name,
        actual="<removed>",
        severity="warning",
        message=(
            f"removed-export: '{name}' is no longer exported by this module, but "
            f"{len(importers)} importer(s) still reference it ({sites}{more}). "
            "Restore the export or update the importers."
        ),
    )


def _python_current_export_names(
    content: str, file_path: str | Path | None = None
) -> tuple[frozenset[str], bool]:
    """Names the edited Python file currently exports, plus an open-set flag.

    Parsed with the stdlib ``ast`` (the same parser the dimension extractor uses),
    so multi-line / parenthesized imports and bindings inside top-level
    ``try``/``if``/``with`` blocks are read exactly as the dump's
    ``_module_exports`` records them -- the live read and the stored index cannot
    drift on the same content. A def/class body is a new scope and is not
    descended. An ``__init__`` module also re-exports its sibling submodules.
    Returns ``(names, open)``; ``open`` True (a ``from x import *``, or an
    unparseable in-progress edit) means the set is non-authoritative and the
    existence check is skipped.
    """
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return frozenset(), True

    names: set[str] = set()
    open_set = False
    _try_types = (ast.Try, getattr(ast, "TryStar", ast.Try))

    def _walk(stmts) -> None:
        nonlocal open_set
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    names.add(node.target.id)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        open_set = True
                    else:
                        names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.If):
                _walk(node.body)
                _walk(node.orelse)
            elif isinstance(node, _try_types):
                _walk(node.body)
                for handler in node.handlers:
                    _walk(handler.body)
                _walk(node.orelse)
                _walk(node.finalbody)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                _walk(node.body)
                _walk(node.orelse)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                _walk(node.body)

    _walk(tree.body)

    if file_path is not None:
        base = os.path.basename(str(file_path))
        if base in ("__init__.py", "__init__.pyi"):
            try:
                pkg_dir = os.path.dirname(str(file_path))
                # Mirror the dump's _module_exports exactly so the live read and
                # the stored index cannot drift: a PEP 562 __getattr__ opens the
                # set (lazy exports are unenumerable), and compiled .so/.pyd
                # submodules are importable siblings.
                if "__getattr__" in names:
                    open_set = True
                for entry in os.listdir(pkg_dir):
                    if entry.startswith("__"):
                        continue
                    if entry.endswith((".py", ".pyi")):
                        names.add(entry.rsplit(".", 1)[0])
                    elif entry.endswith((".so", ".pyd")):
                        names.add(entry.split(".", 1)[0])
                    elif os.path.isfile(
                        os.path.join(pkg_dir, entry, "__init__.py")
                    ) or os.path.isfile(os.path.join(pkg_dir, entry, "__init__.pyi")):
                        names.add(entry)
            except OSError:
                pass

    return frozenset(names), open_set


def lint_cross_file_imports(
    content: str,
    *,
    file_path: str | None,
    repo_root: Path | str | None,
    language: str | None,
    content_truncated: bool = False,
) -> list[Violation]:
    """Cross-file context for the edited module, read from the prebuilt reverse
    index only (no caller is re-parsed).

    Two advisory findings (TypeScript and Python), silent on any ambiguity:

    - ``cross-file-importers``: for each name the file currently exports that has
      indexed importers, "N files import `name` from this module" -- the
      blast-radius hint a reviewer gives before a rename.
    - ``removed-export-breaks-importers``: a name the index records importers for
      that the file NO LONGER exports. Deterministic existence break; warning
      severity, but advisory at edit time (Stop/PR-review consume the same index
      for the gate-eligible surfacing).

    Returns ``[]`` when the language is neither TypeScript nor Python, the path
    can't be resolved, the reverse index is absent/corrupt, or the file's export
    set is open (``export * from``). ``content_truncated`` means the caller
    capped the content read: everything defined past the cap is invisible, so
    the removed-export check is skipped (a name absent from a truncated prefix
    is not evidence of removal — when the truncation happens to parse cleanly,
    every tail export would otherwise read as removed). Suppress with
    ``// chameleon-ignore <rule>`` (``# chameleon-ignore <rule>`` in Python).
    """
    if language not in ("typescript", "python") or not file_path:
        return []
    try:
        root = Path(repo_root).resolve() if repo_root else None
    except OSError:
        return []
    if root is None:
        return []
    fp = Path(file_path)
    if not fp.is_absolute():
        fp = root / fp
    if not _under_repo(fp, root):
        return []

    # Python uses `#` comment directives; TS uses `//`. Both ignore patterns are
    # `<comment> chameleon-ignore <rule>`; pick the one for the language. For
    # Python, blank string literals first so a directive inside a docstring is
    # not read as author intent.
    if language == "python":
        from chameleon_mcp.lint_engine import _blank_python_strings

        _ignore_scan = _blank_python_strings(content)
        _ignore_re = _IGNORE_RUBY_RE
    else:
        _ignore_scan = content
        _ignore_re = _IGNORE_TS_RE
    ignored = {m.group(1) for m in _ignore_re.finditer(_ignore_scan)}
    crossfile_on = _CROSSFILE_RULE not in ignored
    broken_on = _BROKEN_EXPORT_RULE not in ignored
    if not crossfile_on and not broken_on:
        return []

    index = load_reverse_index(root)
    if index is None:
        return []
    target_key = module_key_for_path(fp, root)
    if target_key is None:
        return []
    indexed = index.names_for(target_key)
    if not indexed:
        return []  # nothing imports this module by name -> no cross-file context

    if language == "python":
        current, open_set = _python_current_export_names(content, fp)
    else:
        current, open_set = _current_export_names(content)
    if open_set:
        # `export * from` re-exports an unknown set; a name absent from the
        # statically-visible set may still be re-exported, so the existence
        # check would false-positive. Skip both checks, matching the
        # skip-on-ambiguity stance the path/symbol checks already take.
        return []

    violations: list[Violation] = []
    for name, importers in sorted(indexed.items()):
        in_exports = name in current
        if in_exports and crossfile_on:
            sample = [imp.path for imp in importers]
            violations.append(_crossfile_violation(name, len(importers), sample))
        elif not in_exports and broken_on and not content_truncated:
            sites = [
                (f"{imp.path}:{imp.line}" if imp.line is not None else imp.path)
                for imp in importers
            ]
            violations.append(_broken_export_violation(name, sites))
    return violations
