#!/usr/bin/env python3
"""Differential parity: chromatophore's `dump` vs chameleon's reference dumper.

The engine claims it can stand in for chameleon's AST extractors. That claim is
only worth what a field-by-field comparison against the real dumper says, so
this runs both over the same corpus and reports a per-field match rate.

It is deliberately a *report*, not a pass/fail gate. Byte-exact parity across
every field is not the goal and pretending otherwise would hide the interesting
part: some fields are structurally identical, some differ for reasons worth
knowing (libcst counts a decorated function's span from the decorator; the
tree-sitter grammar starts at `def`), and the difference between those two cases
is the whole engineering question.

The one thing it does exit non-zero on is having compared nothing at all. A
report over an empty set is not a lenient report, it is a fabricated one: every
rate reads `0/0 0.0%` and a dead engine looks like a quiet one.

Usage:
    python3 tests/parity.py --chameleon /path/to/chameleon --limit 200
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Fields both sides are expected to agree on exactly.
#
# Keep this list exhaustive over the record. An earlier version omitted
# `default_export_kind`, which reported a clean 13/13 while that field was
# wrong on five files -- a harness that silently skips a field is worse than no
# harness, because it manufactures confidence. A later one omitted
# `named_export_names` and `parse_diagnostics_count`, the same way.
#
# Exactly two fields are excluded, both for a stated reason rather than a
# judgment call:
#
# - `path` is the join key.
# - `content_first_200_bytes` is not comparable AGAINST THIS REFERENCE. libcst
#   decodes first and slices 200 CHARACTERS; the engine slices 200 BYTES. So
#   does chameleon's own default tree-sitter backend
#   (`src[:200].decode("utf-8", "replace")`), which is what `integration/
#   verify.py` compares against -- and it DOES check this field.
COMPARED = [
    "top_level_node_kinds",
    "default_export_kind",
    "named_export_count",
    "named_export_names",
    "export_set_open",
    "import_specifiers",
    "import_symbols",
    "namespace_imports",
    "has_jsx",
    "parse_diagnostics_count",
    "function_scopes",
    "callable_signatures",
    "class_shapes",
    "call_sites",
    "call_sites_total",
    "call_sites_truncated",
]


def run_reference(chameleon: Path, paths: list[str]) -> dict[str, dict]:
    """Chameleon's own libcst dumper, as ground truth."""
    venv = chameleon / "plugin/mcp/.venv/bin/python"
    script = chameleon / "plugin/scripts/libcst_dump.py"
    if not venv.exists() or not script.exists():
        sys.exit(f"chameleon reference dumper not found under {chameleon}")
    try:
        proc = subprocess.run(
            [str(venv), str(script)],
            input="\n".join(paths) + "\n",
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        # Every other failure here leaves through a formatted message. A hung
        # dumper is a corpus problem the reader can act on, not a bug in this
        # harness, and a raw traceback says the opposite.
        sys.exit(f"reference dumper timed out after 600s over {len(paths)} file(s)")
    # A reference that died (libcst not importable in that venv, say) writes
    # nothing to stdout, and an empty ground truth makes every rate below read
    # `0/0 0.0%` rather than failing.
    if proc.returncode != 0:
        sys.exit(f"reference dumper failed: {proc.stderr[:400]}")
    return {
        r["path"]: r
        for r in (json.loads(line) for line in proc.stdout.splitlines() if line.strip())
    }


def run_engine(binary: Path, paths: list[str]) -> dict[str, dict]:
    try:
        proc = subprocess.run(
            [str(binary), "dump"],
            input="\n".join(paths) + "\n",
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        sys.exit(f"engine timed out after 600s over {len(paths)} file(s)")
    if proc.returncode != 0:
        sys.exit(f"engine failed: {proc.stderr[:400]}")
    return {
        r["path"]: r
        for r in (json.loads(line) for line in proc.stdout.splitlines() if line.strip())
    }


def normalize(value):
    """Tuples and lists are the same thing on the wire."""
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    return value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chameleon", type=Path, required=True)
    ap.add_argument("--binary", type=Path, default=Path("target/release/chromatophore"))
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    corpus = sorted(
        str(p)
        for p in (args.chameleon / "plugin/mcp/chameleon_mcp").rglob("*.py")
        if "__pycache__" not in str(p)
    )[: args.limit]
    if not corpus:
        sys.exit("no corpus files found")

    truth = run_reference(args.chameleon, corpus)
    cand = run_engine(args.binary, corpus)

    # Only files BOTH sides parsed can be compared. A file one side refused is
    # reported separately rather than scored, so a refusal can never inflate or
    # deflate a match rate.
    shared = [
        p
        for p in corpus
        if p in truth and p in cand and "error" not in truth[p] and "error" not in cand[p]
    ]
    ref_only = [p for p in corpus if p in truth and "error" in truth[p]]
    eng_only = [p for p in corpus if p in cand and "error" in cand.get(p, {})]

    print(f"corpus: {len(corpus)} files; compared: {len(shared)}")
    if ref_only:
        print(f"  reference refused {len(ref_only)}: " + ", ".join(Path(p).name for p in ref_only[:5]))
    if eng_only:
        print(f"  engine refused {len(eng_only)}: " + ", ".join(Path(p).name for p in eng_only[:5]))
    if not shared:
        # The table below divides by len(shared), so an empty set prints a full
        # page of `0/0 0.0%` and reads as a clean run. A missing binary, a
        # malformed spec, a corpus neither side could parse -- each looks
        # identical to perfect agreement, which is the manufactured confidence
        # the COMPARED list exists to prevent.
        print("\nNOTHING COMPARED: no file was parsed by both sides")
        return 1
    print()
    print(f"{'field':<28} {'match':>12}   rate")
    print("-" * 56)

    worst: dict[str, tuple[str, object, object]] = {}
    for field in COMPARED:
        hits = 0
        for p in shared:
            a, b = normalize(truth[p].get(field)), normalize(cand[p].get(field))
            if a == b:
                hits += 1
            elif field not in worst:
                worst[field] = (p, a, b)
        rate = hits / len(shared) * 100
        print(f"{field:<28} {hits:>5}/{len(shared):<6} {rate:6.1f}%")

    print()
    for field, (p, a, b) in worst.items():
        sa, sb = json.dumps(a)[:150], json.dumps(b)[:150]
        print(f"first divergence in {field} ({Path(p).name}):")
        print(f"    reference: {sa}")
        print(f"    engine:    {sb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
