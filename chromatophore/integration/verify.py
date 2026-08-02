#!/usr/bin/env python3
"""Verify the shim really is a drop-in for chameleon's own extractor.

Two claims are worth checking, and neither is provable by reading code:

1. The engine's records survive chameleon's `ParsedFile` conversion, populating
   the same eight normalized slots its own extractors populate.
2. Swapping the backend changes nothing downstream -- the `ParsedFile` objects
   this shim produces equal, field for field, the ones chameleon's in-process
   tree-sitter extractor produces for the same corpus.

Claim 2 is the real one. Parity on the wire format is necessary but not
sufficient: what matters is what chameleon's clustering and convention
derivation actually see.

Usage:
    CHROMATOPHORE_BIN=./target/release/chromatophore \\
      python3 integration/verify.py --chameleon /path/to/chameleon
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chameleon", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    sys.path.insert(0, str(args.chameleon / "plugin/mcp"))
    sys.path.insert(0, str(Path(__file__).parent))

    from chameleon_extractor import ChromatophoreExtractor
    from chameleon_mcp.extractors.treesitter.extractor import TreeSitterExtractor

    corpus_root = args.chameleon / "plugin/mcp/chameleon_mcp"
    corpus = sorted(p for p in corpus_root.rglob("*.py") if "__pycache__" not in str(p))[
        : args.limit
    ]
    print(f"corpus: {len(corpus)} files")

    ours = ChromatophoreExtractor("python").parse_repo(corpus_root, paths=corpus)
    theirs = TreeSitterExtractor("python").parse_repo(corpus_root, paths=corpus)
    print(f"  chromatophore: {len(ours.files)} parsed, {len(ours.skipped)} skipped")
    print(f"  chameleon:     {len(theirs.files)} parsed, {len(theirs.skipped)} skipped")

    # Absolute on both sides: the shim feeds the engine abspaths and the engine
    # echoes them, while the backend keeps the candidate path as given -- so a
    # relative --chameleon made the two key sets disjoint and a working engine
    # reported as dead.
    ours_by = {os.path.abspath(f.path): f for f in ours.files}
    theirs_by = {os.path.abspath(f.path): f for f in theirs.files}
    shared = sorted(set(ours_by) & set(theirs_by))
    print(f"  comparable:    {len(shared)}")

    # A file the backend parsed and the engine refused leaves `shared` silently,
    # and every rate below is then a fraction of what survived. A drop-in that
    # drops files is not a drop-in.
    missing = sorted(set(theirs_by) - set(ours_by))
    if missing:
        print(f"  ENGINE REFUSED {len(missing)} file(s) chameleon parsed:")
        for path in missing[:5]:
            print(f"    {Path(path).name}")

    if not shared:
        # 0 == 0 makes every per-slot check vacuously true, so a totally dead
        # engine -- a malformed spec, a missing binary -- would otherwise print
        # the pass line and exit 0.
        print("\nNOTHING COMPARED: the engine returned no records for this corpus")
        return 1

    # Every normalized slot chameleon's ParsedFile carries. Exhaustive on
    # purpose: an earlier version of the sibling parity harness silently omitted
    # one field and reported a clean 13/13 while that field was wrong on five
    # files, so a slot left out here is a slot nothing measures.
    # `content_first_200_bytes` is included and IS comparable against this
    # backend: chameleon's tree-sitter extractor slices bytes
    # (`src[:200].decode(...)`) exactly as the engine does.
    NORMALIZED = [
        "content_first_200_bytes",
        "sha_hint",
        "top_level_node_kinds",
        "default_export_kind",
        "named_export_count",
        "import_specifiers",
        "has_jsx",
        "parse_diagnostics_count",
    ]

    print()
    print(f"{'normalized slot':<28} {'match':>12}   rate")
    print("-" * 56)
    failures = len(missing)
    for slot in NORMALIZED:
        hits = 0
        first_bad = None
        for path in shared:
            a, b = getattr(theirs_by[path], slot), getattr(ours_by[path], slot)
            if a == b:
                hits += 1
            elif first_bad is None:
                first_bad = (path, a, b)
        rate = hits / len(shared) * 100
        print(f"{slot:<28} {hits:>5}/{len(shared):<6} {rate:6.1f}%")
        if hits != len(shared):
            failures += 1
            p, a, b = first_bad
            print(f"    first divergence in {Path(p).name}:")
            print(f"      chameleon:     {str(a)[:130]}")
            print(f"      chromatophore: {str(b)[:130]}")

    # extras carry the heavy payload (signatures, call sites, body shape).
    print()
    extras_keys = [
        "callable_signatures",
        "function_scopes",
        "class_shapes",
        "call_sites",
        "call_sites_total",
        "call_sites_truncated",
        "import_symbols",
        "namespace_imports",
        "named_export_names",
        "export_set_open",
    ]
    print(f"{'extras key':<28} {'match':>12}   rate")
    print("-" * 56)
    for key in extras_keys:
        hits = sum(
            1
            for p in shared
            if theirs_by[p].extras.get(key) == ours_by[p].extras.get(key)
        )
        rate = hits / len(shared) * 100
        print(f"{key:<28} {hits:>5}/{len(shared):<6} {rate:6.1f}%")
        # Counted, not just printed. A loop that only prints is the same
        # manufactured confidence as a slot left out of the list.
        if hits != len(shared):
            failures += 1

    print()
    if failures:
        print(f"NOT A DROP-IN: {failures} compared field(s) diverge")
        return 1
    print("DROP-IN: every normalized slot is identical to chameleon's own extractor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
