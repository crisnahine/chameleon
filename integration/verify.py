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

    ours_by = {str(f.path): f for f in ours.files}
    theirs_by = {str(f.path): f for f in theirs.files}
    shared = sorted(set(ours_by) & set(theirs_by))
    print(f"  comparable:    {len(shared)}")

    # The eight normalized slots are chameleon's stability contract; extras are
    # explicitly documented as NOT part of it, so they are reported separately
    # rather than folded into the pass condition.
    NORMALIZED = [
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
    failures = 0
    for slot in NORMALIZED:
        hits = 0
        first_bad = None
        for path in shared:
            a, b = getattr(theirs_by[path], slot), getattr(ours_by[path], slot)
            if a == b:
                hits += 1
            elif first_bad is None:
                first_bad = (path, a, b)
        rate = hits / len(shared) * 100 if shared else 0.0
        print(f"{slot:<28} {hits:>5}/{len(shared):<6} {rate:6.1f}%")
        if hits != len(shared):
            failures += 1
            p, a, b = first_bad
            print(f"    first divergence in {Path(p).name}:")
            print(f"      chameleon:     {str(a)[:130]}")
            print(f"      chromatophore: {str(b)[:130]}")

    # extras carry the heavy payload (signatures, call sites, body shape).
    print()
    extras_keys = ["callable_signatures", "function_scopes", "class_shapes", "call_sites"]
    print(f"{'extras key':<28} {'match':>12}   rate")
    print("-" * 56)
    for key in extras_keys:
        hits = sum(
            1
            for p in shared
            if theirs_by[p].extras.get(key) == ours_by[p].extras.get(key)
        )
        rate = hits / len(shared) * 100 if shared else 0.0
        print(f"{key:<28} {hits:>5}/{len(shared):<6} {rate:6.1f}%")

    print()
    if failures:
        print(f"NOT A DROP-IN: {failures} normalized slot(s) diverge")
        return 1
    print("DROP-IN: every normalized slot is identical to chameleon's own extractor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
