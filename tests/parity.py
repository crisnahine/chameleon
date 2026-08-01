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

Usage:
    python3 tests/parity.py --chameleon /path/to/chameleon --limit 200
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Fields both sides are expected to agree on exactly. `path` and
# `content_first_200_bytes` are echoes of the input, so comparing them measures
# nothing; `sha_hint` is host-computed on chameleon's side.
COMPARED = [
    "top_level_node_kinds",
    "named_export_count",
    "export_set_open",
    "import_specifiers",
    "import_symbols",
    "namespace_imports",
    "has_jsx",
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
    proc = subprocess.run(
        [str(venv), str(script)],
        input="\n".join(paths) + "\n",
        capture_output=True,
        text=True,
        timeout=600,
    )
    return {
        r["path"]: r
        for r in (json.loads(line) for line in proc.stdout.splitlines() if line.strip())
    }


def run_engine(binary: Path, paths: list[str]) -> dict[str, dict]:
    proc = subprocess.run(
        [str(binary), "dump"],
        input="\n".join(paths) + "\n",
        capture_output=True,
        text=True,
        timeout=600,
    )
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
        rate = hits / len(shared) * 100 if shared else 0.0
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
