"""Per-symbol signature + definition-span index for forward dependency hydration.

The correctness judge already reads the diff, the canonical witness, and the
REVERSE caller facts (who calls the changed functions). What it does not get is
the FORWARD direction: the definitions of the symbols the edited file IMPORTS and
calls. Without them the reviewer reasons about a call site blind to the contract
it must satisfy. This index supplies those definitions cheaply.

It builds and reads a committed ``symbol_signatures.json`` recording, per
top-level/exported callable, its parameter shape, best-effort DECLARED param and
return type text (TypeScript and Python -- the dump is a pure parse with no type
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
import re
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
    collected_classes: list[tuple[str, dict]] = []
    for pf in files or ():
        extras = getattr(pf, "extras", None) or {}
        raw = extras.get("callable_signatures")
        raw_cls = extras.get("class_shapes")
        has_callables = isinstance(raw, list) and raw
        has_classes = isinstance(raw_cls, list) and raw_cls
        if not has_callables and not has_classes:
            continue
        try:
            rel = Path(pf.path).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        by_name: dict[str, dict] = {}
        for entry in raw if has_callables else ():
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
            if entry.get("is_async") is True:
                row["is_async"] = True
            by_name[name] = row
        if by_name:
            collected.append((rel, by_name))
        # Searchable class/module definitions (name -> file:line), sourced from the
        # per-language class_shapes (TS/Python) plus Ruby class/module nodes. Only a
        # name with an integer start_line is recorded, so search can cite file:line.
        cls_by_name: dict[str, dict] = {}
        for entry in raw_cls if has_classes else ():
            if len(cls_by_name) >= per_file_cap:
                break
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            # A Ruby block-form nested class records its full constant path
            # additively under `qualified` (the leaf `name` keys the contract
            # join); the qualified path is the searchable identity, and a leaf
            # query still hits it on the substring tier.
            qualified = entry.get("qualified")
            if isinstance(qualified, str) and qualified:
                name = qualified
            start = entry.get("start_line")
            if not isinstance(name, str) or not name or not isinstance(start, int):
                continue
            if name in cls_by_name:
                continue
            crow: dict = {"start_line": start}
            ext = entry.get("extends")
            if isinstance(ext, str) and ext:
                crow["extends"] = _truncate_type(ext)
            # `keyword` distinguishes a Ruby module from a class so search renders
            # the truthful `module X` / `class X`; TS/Python omit it (all classes).
            kw = entry.get("kind")
            if kw in ("module", "class"):
                crow["keyword"] = kw
            cls_by_name[name] = crow
        if cls_by_name:
            collected_classes.append((rel, cls_by_name))

    collected.sort(key=lambda item: item[0])
    collected_classes.sort(key=lambda item: item[0])
    out = {rel: names for rel, names in collected[:file_cap]}
    out_classes = {rel: names for rel, names in collected_classes[:file_cap]}
    return {"schema_version": SCHEMA_VERSION, "files": out, "classes": out_classes}


class SymbolSignatures:
    """Repo-relative path -> name -> signature row, loaded from the artifact.

    Carries a parallel ``classes`` map (rel -> classname -> {start_line, extends?})
    so a comprehension search can locate a class/module by name, not only
    callables. It is a SEPARATE section: the callable views (:meth:`for_file`,
    :meth:`items`, :meth:`__len__`) are unchanged, so describe/nearby-signature
    consumers that count or render callables are unaffected by class entries.
    """

    def __init__(
        self, entries: dict[str, dict[str, dict]], classes: dict[str, dict[str, dict]] | None = None
    ) -> None:
        self._entries = entries
        self._classes = classes or {}

    def lookup(self, rel: str, name: str) -> dict | None:
        """The signature row for ``name`` defined in ``rel``, or None."""
        return (self._entries.get(rel) or {}).get(name)

    def for_file(self, rel: str) -> dict[str, dict]:
        """All ``name -> signature row`` entries defined in ``rel`` (empty dict
        when the file carries no recorded signatures)."""
        return self._entries.get(rel) or {}

    def items(self):
        """``(rel, {name: row})`` pairs for every file with recorded signatures.

        The whole-index walk a comprehension search needs (locate a symbol by
        name across the repo); the per-edit hot path uses :meth:`for_file`.
        """
        return self._entries.items()

    def class_items(self):
        """``(rel, {classname: {start_line, extends?}})`` pairs for every file
        with recorded class/module definitions -- the class-name search walk.
        Empty for an artifact built before class definitions were indexed."""
        return self._classes.items()

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
    # Follow a linked git worktree to the main worktree's profile, mirroring
    # load_calls_index -- without this, the nearby-collaborator-signature and
    # inbound-caller hydration silently reads the worktree's absent .chameleon.
    from chameleon_mcp.worktree import resolve_profile_root

    root = resolve_profile_root(root)
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

    # The class/module section is additive: an artifact written before it existed
    # simply has no "classes" key, so class search stays empty until a refresh.
    classes: dict[str, dict[str, dict]] = {}
    raw_classes = data.get("classes")
    if isinstance(raw_classes, dict):
        for rel, by_name in raw_classes.items():
            if not isinstance(rel, str) or not isinstance(by_name, dict):
                continue
            cnames: dict[str, dict] = {}
            for name, row in by_name.items():
                if isinstance(name, str) and isinstance(row, dict):
                    cnames[name] = row
            if cnames:
                classes[rel] = cnames

    index = SymbolSignatures(entries, classes)
    _CACHE[key] = (token, index)
    return index


# ---------------------------------------------------------------------------
# Forward definition hydration (tool/Stop-time consumer; TypeScript-centric)
# ---------------------------------------------------------------------------

# Only TS-family files carry the named ``import_symbols`` rows this resolves; a
# Ruby require has no named-binding to hydrate, and Ruby carries no types.
_TS_IMPORT_EXTS = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
_PY_IMPORT_EXTS = frozenset({".py", ".pyi"})


def _language_for_path(abs_path) -> str | None:
    """Resolver language for one edited file, by suffix; None when ungoverned."""
    suffix = Path(abs_path).suffix.lower()
    if suffix in _TS_IMPORT_EXTS:
        return "typescript"
    if suffix in _PY_IMPORT_EXTS:
        return "python"
    return None


def render_imported_definition(name: str, entry: dict, target_rel: str) -> str:
    """A one-line ``name(params): return — path`` signature from a stored entry.

    Renders ``param: type`` (``?`` for optional) when the declared type is
    present, the bare name otherwise. The body is intentionally NOT sliced: the
    stored span can drift from the current file, and the signature alone carries
    the contract the caller must satisfy.

    Param syntax is caller-correct per language, because the whole point of this
    section is the contract a call must satisfy: a Ruby keyword argument is
    ``name:`` (calling it positionally raises ArgumentError), a Python
    keyword-only argument sits behind a ``*`` separator (calling it positionally
    raises TypeError), a keyword-rest is ``**`` / ``**kwargs``, and a splat is
    ``*args`` in Ruby/Python but ``...args`` in TS/JS. The language is inferred
    from the definition file's suffix.
    """
    suffix = target_rel.rsplit(".", 1)[-1].lower() if "." in target_rel else ""
    is_ruby = suffix == "rb"
    is_python = suffix in ("py", "pyi")
    splat = "*" if (is_ruby or is_python) else "..."
    parts: list[str] = []
    star_sep_emitted = False
    for p in entry.get("params") or []:
        if not isinstance(p, dict):
            continue
        kind = p.get("kind")
        pn = p.get("name") if isinstance(p.get("name"), str) else "_"
        if kind == "rest":
            # A variadic/splat param, not an optional one: `*args` (Ruby/Python) or
            # `...args` (TS), never `args?`. Also satisfies Python's `*` separator.
            # An anonymous rest is stored as the bare splat token ("*" in Ruby,
            # "..." in TS); concatenating the name onto it would double the token
            # ("**", "......"), which in Ruby reads as a keyword-rest and misstates
            # the contract. Mirror the keyword_rest branch's literal-token guard.
            s = splat if pn == splat else f"{splat}{pn}"
            star_sep_emitted = True
        elif kind == "keyword_rest":
            # Ruby stores the literal "**"; Python stores the bare kwargs name.
            s = "**" if pn == "**" else f"**{pn}"
        elif kind == "keyword":
            if is_ruby:
                # Ruby keyword argument: the call site must pass `name:`.
                s = f"{pn}:"
            else:
                # Python keyword-only argument: emit the `*` separator once so the
                # model passes it by keyword, not positionally.
                if not star_sep_emitted:
                    parts.append("*")
                    star_sep_emitted = True
                s = pn + ("?" if p.get("optional") else "")
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
    # Python's `async def` changes the required call syntax (an unawaited
    # coroutine silently no-ops), so the marker must survive into the rendered
    # signature the reviewer/search result sees.
    prefix = "async " if entry.get("is_async") else ""
    sig = f"{prefix}{name}({', '.join(parts)})"
    if isinstance(ret, str) and ret:
        sig += f": {ret}"
    # Append the definition's line so the reviewer can locate it (uses the stored
    # span); the value reflects the profile's last bootstrap.
    start = entry.get("start_line")
    loc = f"{target_rel}:{start}" if isinstance(start, int) else target_rel
    return f"{sig} — {loc}"


def symbol_presence_in_source(lines: list[str], name: str, stored_line) -> tuple[bool, bool]:
    """`(present, keep_stored_line)` — verify a stored signature against current source.

    The stored ``start_line`` can be stale: signatures derive from the pinned
    production ref (or predate a local edit), so the checkout being edited may have
    moved or removed the symbol. This is a bounded, pure re-verify that never
    fabricates a line:

    - `present=False` when the symbol name appears nowhere in the file — a phantom
      the caller drops (never inject a call to a symbol the checkout no longer has).
    - `keep_stored_line=True` only when the name is still on the stored line, so the
      location is trustworthy. Otherwise the caller keeps the signature (the
      contract is still useful) but drops the misleading `:line`.

    Word-boundary match (``$`` is a JS identifier char) so a substring of a longer
    name never counts. Pure and bounded; caller reads the file once, capped.
    """
    if not name:
        return False, False
    pat = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
    on_stored = (
        isinstance(stored_line, int)
        and 1 <= stored_line <= len(lines)
        and bool(pat.search(lines[stored_line - 1]))
    )
    if on_stored:
        return True, True
    return any(pat.search(ln) for ln in lines), False


def _parse_import_symbols(repo_root, abs_path) -> list[tuple[str, str]]:
    """``[(imported_name, module_specifier)]`` for one TS-family or Python edited file.

    The Python dump preserves a relative module's leading dots (``from .svc import
    x`` -> ``.svc``), the exact form the Python module resolver consumes.
    """
    lang = _language_for_path(abs_path)
    if lang is None:
        return []
    try:
        if lang == "python":
            from chameleon_mcp.extractors.python import PythonExtractor

            result = PythonExtractor().parse_repo(Path(repo_root), paths=[Path(abs_path)])
        else:
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

    For each edited TS or Python file, resolves every named import to its in-repo
    defining module and, when that module's symbol is in the signature index,
    renders the symbol's signature. Bounded by ``max_items`` and de-duplicated per
    (module, name). Fail-open: returns [] when the index is absent or anything
    raises. Tool/Stop-time only -- it spawns the extractor to read imports.
    """
    try:
        index = load_symbol_signatures(repo_root)
        if index is None:
            return []
        from chameleon_mcp.symbol_index import make_module_resolver

        root = Path(repo_root).resolve()
        # One resolver per language: a TS specifier and a Python dotted module
        # resolve by different rules, so each edited file uses the resolver for
        # its own suffix.
        resolvers: dict[str, object] = {}

        def _resolver_for(lang: str):
            if lang not in resolvers:
                resolvers[lang] = make_module_resolver(root, lang)
            return resolvers[lang]

        out: list[str] = []
        seen: set[tuple[str, str]] = set()
        for ap in edited_abs_paths or ():
            ap = Path(ap)
            lang = _language_for_path(ap)
            if lang is None:
                continue
            resolver = _resolver_for(lang)
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
