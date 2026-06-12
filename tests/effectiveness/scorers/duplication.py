"""Duplication scorer.

Parses functions in the session-changed files (the same parse the turn-end
duplication gate uses), counts the ones absent from the fixture's committed
function catalog as "added", and counts body-hash matches against catalog
entries in OTHER files as duplicates. Reuse credit: any changed SOURCE file
(other than the bait's own file) references the task's declared existing
helper by word-bounded grep — non-source files (docs, harness artifacts)
must never mint the credit.

Unscored when the catalog is missing (nothing to compare against) or when
the parse extractor is unavailable (parsing the changed files would silently
yield [] and fake a perfect score — probed via a catalog-known file).
"""

from __future__ import annotations

import re

from tests.effectiveness.scorers.base import ScoreContext, unscored

_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".rb")


def _load_catalog(repo_root):
    """Seam: tests monkeypatch this."""
    from chameleon_mcp.function_catalog import load_function_catalog

    return load_function_catalog(repo_root)


def _parse(repo_root, path: str):
    """Seam: tests monkeypatch this."""
    from chameleon_mcp.tools import parse_edited_functions

    return parse_edited_functions(repo_root, path)


def _word_re(name: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])")


def score(ctx: ScoreContext) -> dict:
    catalog = _load_catalog(ctx.worktree)
    if catalog is None:
        return unscored("no function catalog in worktree profile")

    from chameleon_mcp.duplication_review import CandidateIndex

    index = CandidateIndex()
    existing_pairs = set()
    for fn in catalog.functions:
        existing_pairs.add((fn.file, fn.name))
        index.add_function(
            fn.file, fn.name, body_hash=fn.body_hash, body_hash_pnorm=fn.body_hash_pnorm
        )

    changed_source = [f for f in ctx.changed_files if f.endswith(_SOURCE_SUFFIXES)]

    # Extractor probe: if a file the catalog knows parses to [], the dump
    # toolchain (node / prism) is unavailable here and "no added functions"
    # would be a fabricated zero.
    if changed_source and catalog.functions:
        probe_rel = catalog.functions[0].file
        probe_abs = ctx.worktree / probe_rel
        if probe_abs.is_file() and not _parse(ctx.worktree, str(probe_abs)):
            return unscored(f"function parse unavailable (probe {probe_rel} yielded nothing)")

    added = 0
    duplicates = 0
    duplicate_names: list[str] = []
    for rel in changed_source:
        abs_path = ctx.worktree / rel
        if not abs_path.is_file():
            continue
        for pf in _parse(ctx.worktree, str(abs_path)):
            if (rel, pf.name) in existing_pairs:
                continue
            added += 1
            hit, _match = index.lookup(pf, exclude_file=rel)
            if hit is not None:
                duplicates += 1
                duplicate_names.append(f"{pf.name}~{hit.name}")

    out: dict = {"added_functions": added, "body_hash_duplicates": duplicates}
    if duplicate_names:
        out["duplicate_pairs"] = ";".join(sorted(duplicate_names)[:10])

    target = ctx.pack.duplication_targets.get(ctx.task.task_id)
    if target is not None:
        needle = _word_re(target.get("needle") or target["existing_name"])
        reuse = False
        for rel in changed_source:
            if rel == target["existing_file"]:
                continue
            abs_path = ctx.worktree / rel
            try:
                text = abs_path.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
            except OSError:
                continue
            if needle.search(text):
                reuse = True
                break
        out["reuse_credit"] = reuse
    return out
