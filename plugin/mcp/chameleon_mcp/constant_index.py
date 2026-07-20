"""Ruby constant-reference reverse index.

Ruby has no static named-import surface: ``require 'foo'`` pulls a whole file by
side effect, so the TS/Python named-export reverse index (symbol_index) is not
constructible for Ruby. The cross-file signal Ruby DOES carry is the constant
graph -- a class/module IS a constant, and ``Const.method`` / ``Const.new``
reference it by that constant. This index inverts that graph: for each constant,
the files that DEFINE it and the files that REFERENCE it (via a constant-receiver
call site). That yields Rails blast-radius ("rename this service class, here are
its callers") from data already in the parse extras, with no new extraction.

Join semantics mirror the calls_index ``constant_receiver`` grade exactly:
``defined_in`` is keyed on the fully qualified ``enclosing_class_path`` and a
reference is resolved against those keys lexically outward from its call
site's recorded module nesting (a bare ``Foo`` inside ``module App`` tries
``App::Foo`` before ``Foo``; a ``::``-anchored receiver is absolute), then
recorded under the winning qualified key. A reference whose nesting levels
match different defining files is ambiguous and recorded nowhere -- the index
asserts only what it can pin to one file. A reference no candidate defines
keeps its literal receiver key (a framework class with an empty ``defined_in``
is harmless); a reference whose one matching key is a constant reopened
across several files keeps that key (the consumer sees the multi-file
``defined_in`` and treats that ambiguity itself).
"""

from __future__ import annotations

import json
from pathlib import Path

from chameleon_mcp.calls_index import lexical_candidates, resolve_constant_receiver

SCHEMA_VERSION = 1

_ARTIFACT_NAME = "constant_index.json"

# mtime/size-keyed load cache, mirroring symbol_index.load_reverse_index.
_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}


def build_constant_index(files, repo_root: Path | str, language: str = "ruby") -> dict:
    """Build the constant-reference index from parse records.

    Each ``pf`` carries ``.path`` and ``.extras``; ``callable_signatures`` rows
    give the defining file of each class (constant), and ``call_sites`` rows with
    ``kind == "constant"`` give the files that reference a constant by name.
    Returns an empty index for any language other than ruby (the inversion is
    only meaningful where the constant graph is the cross-file surface).
    """
    if language != "ruby":
        return {"schema_version": SCHEMA_VERSION, "language": language, "constants": {}}

    root = Path(repo_root).resolve() if not isinstance(repo_root, Path) else repo_root.resolve()
    defined_in: dict[str, set[str]] = {}
    # (receiver, nesting, referencing rel), resolved only after every file's
    # definitions are collected: a reference's lexical candidates are matched
    # against the WHOLE dump's definition keys, not the files seen so far.
    reference_sites: list[tuple[str, list | None, str]] = []

    for pf in files or ():
        path = getattr(pf, "path", None)
        if path is None:
            continue
        try:
            rel = Path(path).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        extras = getattr(pf, "extras", None) or {}

        for row in extras.get("callable_signatures") or ():
            if not isinstance(row, dict):
                continue
            class_path = row.get("enclosing_class_path")
            cls = (
                class_path
                if isinstance(class_path, str) and class_path
                else row.get("enclosing_class")
            )
            if isinstance(cls, str) and cls:
                defined_in.setdefault(cls, set()).add(rel)

        for site in extras.get("call_sites") or ():
            if not isinstance(site, dict) or site.get("kind") != "constant":
                continue
            receiver = site.get("receiver")
            if isinstance(receiver, str) and receiver:
                nesting = site.get("nesting")
                reference_sites.append(
                    (receiver, nesting if isinstance(nesting, list) else None, rel)
                )

    referenced_by: dict[str, set[str]] = {}
    for receiver, nesting, rel in reference_sites:
        resolved = resolve_constant_receiver(receiver, nesting, defined_in)
        if resolved is not None:
            # Unify the reference onto the qualified entry it resolves to, so a
            # bare `Foo` written inside `module App` joins App::Foo's blast
            # radius instead of dangling as a disjoint bare-name entry.
            referenced_by.setdefault(resolved[1], set()).add(rel)
            continue
        matched = [k for k in lexical_candidates(receiver, nesting) if k in defined_in]
        if len(matched) == 1:
            # The one matching key is a constant reopened across several files
            # (the resolver refuses a multi-file pin). The join target is
            # still unambiguous; the consumer sees the multi-file defined_in
            # and treats that ambiguity itself.
            referenced_by.setdefault(matched[0], set()).add(rel)
        elif not matched:
            # Nothing in the repo defines any candidate: a framework class.
            # Keep the literal receiver key -- an empty defined_in is harmless.
            referenced_by.setdefault(receiver, set()).add(rel)
        # Several keys matched with disagreeing files: recording any join
        # would knowingly pick a maybe-wrong winner, so the reference is
        # dropped -- the index asserts only what it can pin.

    # Index every constant that is DEFINED locally (an editable file has a blast
    # radius) plus every constant that is REFERENCED (so a referenced-but-defined
    # constant still lists its callers). A reference to a constant the repo never
    # defines (a framework class) carries an empty defined_in and is harmless.
    names = set(defined_in) | set(referenced_by)
    constants = {
        name: {
            "defined_in": sorted(defined_in.get(name, ())),
            "referenced_by": sorted(referenced_by.get(name, ())),
        }
        for name in sorted(names)
    }
    return {"schema_version": SCHEMA_VERSION, "language": "ruby", "constants": constants}


def load_constant_index(repo_root: Path | str | None) -> dict | None:
    """Load ``.chameleon/constant_index.json``, mtime-cached. Returns None when
    absent, unreadable, or a future/unknown schema version."""
    if repo_root is None:
        return None
    try:
        root = Path(repo_root).resolve()
    except OSError:
        return None
    # Follow a linked git worktree to the main worktree's profile, mirroring
    # load_calls_index -- without this, the Ruby constant-graph cross-file
    # existence check silently reads the worktree's absent .chameleon.
    from chameleon_mcp.worktree import resolve_profile_root

    root = resolve_profile_root(root)
    artifact = root / ".chameleon" / _ARTIFACT_NAME
    try:
        st = artifact.stat()
    except OSError:
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
    if not isinstance(data.get("constants"), dict):
        return None
    _CACHE[key] = (token, data)
    return data


def constants_defined_in(index: dict | None, rel: str) -> list[str]:
    """The constants defined in ``rel`` (the file being edited)."""
    if not index:
        return []
    out = [
        name
        for name, entry in (index.get("constants") or {}).items()
        if isinstance(entry, dict) and rel in (entry.get("defined_in") or ())
    ]
    return sorted(out)


def referencing_files(index: dict | None, constant: str) -> list[str]:
    """The files that reference ``constant`` (its blast radius)."""
    if not index:
        return []
    entry = (index.get("constants") or {}).get(constant)
    if not isinstance(entry, dict):
        return []
    return list(entry.get("referenced_by") or ())
