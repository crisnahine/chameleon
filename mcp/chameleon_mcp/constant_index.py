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
``defined_in`` is keyed on the fully qualified ``enclosing_class_path`` and
``referenced_by`` on the literal receiver string, matched by exact equality. A
bare ``Foo`` receiver matches a top-level ``Foo`` only, never a namespaced
``App::Foo`` (a call site carries no lexical nesting), and a constant defined in
two files is ambiguous -- the same accepted undercoverage as the grade.
"""

from __future__ import annotations

import json
from pathlib import Path

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
    referenced_by: dict[str, set[str]] = {}

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
                referenced_by.setdefault(receiver, set()).add(rel)

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
    artifact = Path(repo_root) / ".chameleon" / _ARTIFACT_NAME
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
