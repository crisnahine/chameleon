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

import re
from pathlib import Path

from chameleon_mcp.lint_engine import Violation

_RULE = "phantom-import"

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

_RUBY_REQUIRE_RELATIVE_RE = re.compile(
    r"^\s*require_relative\s+['\"]([^'\"]+)['\"]", re.MULTILINE
)
_IGNORE_RUBY_RE = re.compile(r"#\s*chameleon-ignore\s+([\w-]+)")
_RUBY_SUFFIXES = ("", ".rb", ".so", ".bundle")

# Maximum import specifiers checked per file. A real module has a few dozen; a
# generated barrel/index can have thousands. Capping bounds stat() fan-out on
# the PostToolUse hot path (each TS spec probes up to ~14 candidate paths).
_MAX_SPECS = 200


def _strip_ts_noise(content: str) -> tuple[str, list[bool]]:
    """Blank line/block comments and backtick template literals (so imports
    embedded in fixtures/snapshots/docs aren't matched), preserving single- and
    double-quoted strings so real import specifiers survive.

    Returns the rewritten text plus a parallel mask: ``mask[i]`` is True when
    output char ``i`` was inside a single-/double-quoted string literal. The
    caller skips a regex match whose keyword sits in a masked region, so a code
    snippet stored *as a string value* (e.g. ``const c = "import x from './y'"``)
    is not mistaken for a real import.

    Single linear pass with explicit lexer states - no regex backtracking, so
    an unterminated template literal cannot trigger catastrophic backtracking
    (ReDoS). Only ever removes text, so it cannot manufacture a false positive.
    """
    out: list[str] = []
    mask: list[bool] = []
    n = len(content)
    i = 0
    NORMAL, LINE, BLOCK, TMPL, SQ, DQ = range(6)
    state = NORMAL

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
            elif c == "`":
                state, i = TMPL, i + 1
                emit(" ", False)
            elif c == "'":
                state, i = SQ, i + 1
                emit(c, False)
            elif c == '"':
                state, i = DQ, i + 1
                emit(c, False)
            else:
                emit(c, False)
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
        elif state == TMPL:
            if c == "\\":
                emit("  ", False)
                i += 2
            elif c == "`":
                state, i = NORMAL, i + 1
                emit(" ", False)
            else:
                emit(c if c == "\n" else " ", False)
                i += 1
        else:  # SQ or DQ: preserve content so import specifiers survive
            emit(c, True)
            if c == "\\":
                if i + 1 < n:
                    emit(content[i + 1], True)
                i += 2
            else:
                if (state == SQ and c == "'") or (state == DQ and c == '"'):
                    state = NORMAL
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


def _ts_resolves(base: Path) -> bool:
    """True if `base` (path without assumed extension) resolves to a real file
    via a standard TS/JS candidate suffix or index file, or is a directory.

    On any OSError, returns True (treat ambiguity as resolved, no flag)."""
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


def _safe_is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
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
            middle = spec[len(prefix): len(spec) - len(suffix)] if suffix else spec[len(prefix):]
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
    fp = Path(file_path)
    if not fp.is_absolute():
        return []
    try:
        root = Path(repo_root).resolve() if repo_root else None
    except OSError:
        return []
    if not _under_repo(fp, root):
        return []
    file_dir = fp.parent

    violations: list[Violation] = []
    if language == "typescript":
        if _RULE in {m.group(1) for m in _IGNORE_TS_RE.finditer(content)}:
            return []
        ts_rules = ((rules or {}).get("rules") or {}).get("typescript") or {}
        paths = ts_rules.get("paths") or {}
        source = ts_rules.get("source") or "tsconfig.json"
        # Anchor aliases to the nearest tsconfig (monorepo-correct); fall back to
        # the profile's stored tsconfig dir only if none is found walking up.
        ts_config_dir = _nearest_tsconfig_dir(file_dir, root) or (
            (root / source).parent if root else file_dir
        )
        stripped, str_mask = _strip_ts_noise(content)
        seen: set[str] = set()
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
            if not spec or spec in seen:
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
                if not _ts_resolves(base):
                    violations.append(_violation(spec, base.parent, root))
                continue
            # tsconfig path alias?
            if root is None:
                continue
            targets = [t for t in _alias_targets(spec, paths, ts_config_dir) if _under_repo(t, root)]
            if not targets:
                continue  # bare package, unmapped alias, or out-of-repo -> skip
            if any(_ts_resolves(t) for t in targets):
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
    return violations
