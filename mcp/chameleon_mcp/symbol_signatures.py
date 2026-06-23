"""Per-symbol signature + definition-span index for forward dependency hydration.

The correctness judge already reads the diff, the canonical witness, and the
REVERSE caller facts (who calls the changed functions). What it does not get is
the FORWARD direction: the definitions of the symbols the edited file IMPORTS and
calls. Without them the reviewer reasons about a call site blind to the contract
it must satisfy. This index supplies those definitions cheaply.

It builds and reads a committed ``symbol_signatures.json`` recording, per
top-level/exported callable, its parameter shape, best-effort DECLARED param and
return type text (TypeScript only -- the dump is a pure parse with no type
checker, so untyped params and inferred returns yield empty cells; Ruby has no
static types), and its body span. A tool/Stop-time consumer resolves each symbol
the edited file imports to its defining file, looks it up here, and injects a
compact "this is the definition you are calling" block into the judge prompt.

Two halves share one schema so the build (bootstrap-time) and the read
(tool-time) cannot drift, mirroring :mod:`chameleon_mcp.function_catalog`:

- :func:`build_symbol_signatures` turns parsed files into the artifact payload.
- :func:`load_symbol_signatures` reads the committed artifact, cached on
  (mtime, size) so a mid-session refresh is picked up without re-reading.

Conservative and bounded. Only named callables WITH a body span are recorded (a
span is needed to slice the definition); anonymous callables are skipped. A name
appearing more than once in one file keeps the FIRST declaration (an overload set
shows one signature; the rare two-same-named-exports case is acceptable for an
advisory hydration). File and per-file callable counts are capped so one
generated file cannot bloat the artifact. Loading fails open to None on any
ambiguity -- missing, corrupt, future-schema, oversized, or any I/O error -- so
the hydration simply does not fire rather than crash or fabricate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int

SYMBOL_SIGNATURES_FILENAME = "symbol_signatures.json"
SCHEMA_VERSION = 1


def _truncate_type(text: str) -> str:
    """Bound a declared type annotation so a giant inline type cannot bloat the
    artifact or the judge prompt. Over the cap it is cut with an ellipsis."""
    cap = threshold_int("SYMBOL_SIG_TYPE_MAX_CHARS")
    s = " ".join(text.split())  # collapse multi-line type literals to one line
    return s if len(s) <= cap else s[: cap - 1] + "…"


def _clean_params(params: object) -> list[dict]:
    """Keep the dump param shapes a consumer can render, dropping junk.

    Each retained entry carries name / optional / kind, and ``type`` (length-
    capped) when the dump emitted a declared annotation (TypeScript or Python).
    Non-dict entries drop out.
    """
    if not isinstance(params, list):
        return []
    out: list[dict] = []
    for p in params:
        if not isinstance(p, dict):
            continue
        row: dict = {
            "name": p.get("name") if isinstance(p.get("name"), str) else "_",
            "optional": bool(p.get("optional")),
            "kind": p.get("kind") if isinstance(p.get("kind"), str) else "positional",
        }
        t = p.get("type")
        if isinstance(t, str) and t:
            row["type"] = _truncate_type(t)
        out.append(row)
    return out


def build_symbol_signatures(files, repo_root: Path | str) -> dict:
    """Build the ``symbol_signatures.json`` payload from parsed files.

    ``files`` is the bootstrap's parsed-file list; each entry's ``extras`` may
    carry ``callable_signatures`` (emitted for TypeScript/JS and Ruby). Only
    named callables with an integer ``start_line``/``end_line`` span are
    recorded. Keys are repo-relative POSIX paths so the artifact is portable and
    reproducible byte-for-byte (it is hashed into the trust SHA). File and
    per-file counts are capped for a deterministic, bounded artifact.
    """
    try:
        root = Path(repo_root).resolve()
    except OSError:
        root = Path(repo_root)

    file_cap = threshold_int("DUPLICATION_CATALOG_MAX_FILES")
    per_file_cap = threshold_int("DUPLICATION_CATALOG_MAX_FNS_PER_FILE")

    collected: list[tuple[str, dict]] = []
    for pf in files or ():
        extras = getattr(pf, "extras", None) or {}
        raw = extras.get("callable_signatures")
        if not isinstance(raw, list) or not raw:
            continue
        try:
            rel = Path(pf.path).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        by_name: dict[str, dict] = {}
        for entry in raw:
            if len(by_name) >= per_file_cap:
                break
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            start = entry.get("start_line")
            end = entry.get("end_line")
            if not isinstance(name, str) or not name:
                continue
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            if name in by_name:
                # First declaration wins (overload set / same-named methods).
                continue
            row: dict = {
                "params": _clean_params(entry.get("params")),
                "start_line": start,
                "end_line": end,
            }
            rt = entry.get("return_type")
            if isinstance(rt, str) and rt:
                row["return_type"] = _truncate_type(rt)
            by_name[name] = row
        if by_name:
            collected.append((rel, by_name))

    collected.sort(key=lambda item: item[0])
    out = {rel: names for rel, names in collected[:file_cap]}
    return {"schema_version": SCHEMA_VERSION, "files": out}


class SymbolSignatures:
    """Repo-relative path -> name -> signature row, loaded from the artifact."""

    def __init__(self, entries: dict[str, dict[str, dict]]) -> None:
        self._entries = entries

    def lookup(self, rel: str, name: str) -> dict | None:
        """The signature row for ``name`` defined in ``rel``, or None."""
        return (self._entries.get(rel) or {}).get(name)

    def for_file(self, rel: str) -> dict[str, dict]:
        """All ``name -> signature row`` entries defined in ``rel`` (empty dict
        when the file carries no recorded signatures)."""
        return self._entries.get(rel) or {}

    def __len__(self) -> int:
        return len(self._entries)


# Process-global cache keyed on the artifact path, carrying the (mtime, size) the
# index was parsed at so a refresh that rewrites the artifact is picked up.
_CACHE: dict[str, tuple[tuple[int, int], SymbolSignatures]] = {}


def load_symbol_signatures(repo_root: Path | str | None) -> SymbolSignatures | None:
    """Load the committed ``symbol_signatures.json`` for ``repo_root``, or None.

    Returns None on any ambiguity: no repo_root, no artifact, a corrupt or
    future-schema payload, an oversized file, or any I/O error. The hydration
    only ADDS context, so failing open here means it simply does not fire.
    """
    if repo_root is None:
        return None
    try:
        root = Path(repo_root).resolve()
    except OSError:
        return None
    artifact = root / ".chameleon" / SYMBOL_SIGNATURES_FILENAME
    try:
        st = os.stat(artifact)
    except OSError:
        return None
    if not st.st_size or st.st_size > 16_000_000:
        return None

    key = str(artifact)
    token = (int(st.st_mtime_ns), int(st.st_size))
    cached = _CACHE.get(key)
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

    entries: dict[str, dict[str, dict]] = {}
    for rel, by_name in raw_files.items():
        if not isinstance(rel, str) or not isinstance(by_name, dict):
            continue
        names: dict[str, dict] = {}
        for name, row in by_name.items():
            if isinstance(name, str) and isinstance(row, dict):
                names[name] = row
        if names:
            entries[rel] = names

    index = SymbolSignatures(entries)
    _CACHE[key] = (token, index)
    return index


# ---------------------------------------------------------------------------
# Forward definition hydration (tool/Stop-time consumer; TypeScript-centric)
# ---------------------------------------------------------------------------

# Only TS-family files carry the named ``import_symbols`` rows this resolves; a
# Ruby require has no named-binding to hydrate, and Ruby carries no types.
_TS_IMPORT_EXTS = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})


def render_imported_definition(name: str, entry: dict, target_rel: str) -> str:
    """A one-line ``name(params): return — path`` signature from a stored entry.

    Renders ``param: type`` (``?`` for optional) when the declared type is
    present, the bare name otherwise. The body is intentionally NOT sliced: the
    stored span can drift from the current file, and the signature alone carries
    the contract the caller must satisfy.
    """
    parts: list[str] = []
    for p in entry.get("params") or []:
        if not isinstance(p, dict):
            continue
        kind = p.get("kind")
        pn = p.get("name") if isinstance(p.get("name"), str) else "_"
        if kind == "rest":
            # A variadic/splat param, not an optional one: `...args`, never `args?`.
            s = f"...{pn}"
        elif kind == "destructured":
            # No single binding name; show the destructure shape, the type carries
            # the expected object/array.
            s = "{…}"
        else:
            s = pn + ("?" if p.get("optional") else "")
        t = p.get("type")
        if isinstance(t, str) and t:
            s += f": {t}"
        parts.append(s)
    ret = entry.get("return_type")
    sig = f"{name}({', '.join(parts)})"
    if isinstance(ret, str) and ret:
        sig += f": {ret}"
    # Append the definition's line so the reviewer can locate it (uses the stored
    # span); the value reflects the profile's last bootstrap.
    start = entry.get("start_line")
    loc = f"{target_rel}:{start}" if isinstance(start, int) else target_rel
    return f"{sig} — {loc}"


def _parse_import_symbols(repo_root, abs_path) -> list[tuple[str, str]]:
    """``[(imported_name, module_specifier)]`` for one TS-family edited file."""
    if Path(abs_path).suffix.lower() not in _TS_IMPORT_EXTS:
        return []
    try:
        from chameleon_mcp.extractors.typescript import TypeScriptExtractor

        result = TypeScriptExtractor().parse_repo(Path(repo_root), paths=[Path(abs_path)])
    except Exception:
        return []
    rows: list[tuple[str, str]] = []
    for pf in getattr(result, "files", None) or ():
        for r in (getattr(pf, "extras", None) or {}).get("import_symbols") or ():
            if isinstance(r, dict):
                nm, mod = r.get("name"), r.get("module")
                if isinstance(nm, str) and nm and isinstance(mod, str) and mod:
                    rows.append((nm, mod))
    return rows


def hydrate_imported_definitions(repo_root, edited_abs_paths, *, max_items: int = 20) -> list[str]:
    """Definition signatures of the symbols the edited files import, for the judge.

    For each edited TS file, resolves every named import to its in-repo defining
    module and, when that module's symbol is in the signature index, renders the
    symbol's signature. Bounded by ``max_items`` and de-duplicated per
    (module, name). Fail-open: returns [] when the index is absent or anything
    raises. Tool/Stop-time only -- it spawns the extractor to read imports.
    """
    try:
        index = load_symbol_signatures(repo_root)
        if index is None:
            return []
        from chameleon_mcp.symbol_index import make_module_resolver

        root = Path(repo_root).resolve()
        resolver = make_module_resolver(root)
        out: list[str] = []
        seen: set[tuple[str, str]] = set()
        for ap in edited_abs_paths or ():
            ap = Path(ap)
            importer_dir = ap.parent
            for name, module in _parse_import_symbols(repo_root, ap):
                target_rel = resolver(module, importer_dir)
                if target_rel is None:
                    continue
                key = (target_rel, name)
                if key in seen:
                    continue
                entry = index.lookup(target_rel, name)
                if entry is None:
                    continue
                seen.add(key)
                out.append(render_imported_definition(name, entry, target_rel))
                if len(out) >= max_items:
                    return out
        return out
    except Exception:
        return []
