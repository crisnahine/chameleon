"""Differential harness: tree-sitter extractor vs the dump script it replaces.

This is the acceptance gate for the extractor swap, and it is deliberately the
first thing built. The question that decides the project is not "can tree-sitter
parse Ruby" -- it can -- but "does it produce the SAME ParsedFile the committed
profiles were derived from". Cluster signatures, ast_query witnesses, and kind
labels are all keyed on the dump scripts' vocabulary, so any unreported field
drift silently re-clusters real repos.

So both backends run over the same corpus and every field is compared. A field
at 100% is done; anything less prints the first disagreeing file with both
values, because in practice every gap so far has been a missing table entry
that the diff names outright.

    PYTHONPATH=".:plugin/mcp" plugin/mcp/.venv/bin/python \
        tests/differential_treesitter.py --language ruby

    # any corpus, not just the fixtures
    ... tests/differential_treesitter.py --language ruby --root /path/to/repo
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Fields compared. `path` is the join key, and sha_hint / content_first_200_bytes
# are byte-identical by construction (both read the same file), so neither says
# anything about extraction fidelity.
#
# The list is NOT hand-picked: `--strict` derives it from the union of keys the
# dumper actually emits, because a curated list is exactly how the first pass of
# this harness reported PARITY while seven fields (import_symbols,
# named_export_names, export_set_open, namespace_imports, re_exports,
# class_property_types, value_export_bindings) were unimplemented. Those feed the
# exports index, the reverse index, and the phantom-symbol checks, and nothing
# flagged their absence until the extractor was wired into the registry.
CORE_FIELDS = (
    "top_level_node_kinds",
    "default_export_kind",
    "named_export_count",
    "import_specifiers",
    "parse_diagnostics_count",
    "function_scopes",
    "callable_signatures",
    "class_shapes",
    "class_body_calls",
    "call_sites",
    "call_sites_total",
    "call_sites_truncated",
    "has_jsx",
)

# Never compared: the join key, and two fields both sides read straight off the
# same bytes.
_NOT_A_FIELD = frozenset({"path", "content_first_200_bytes", "sha_hint", "error"})

DUMPERS = {
    "ruby": ("ruby", "plugin/scripts/prism_dump.rb"),
    "python": (sys.executable, "plugin/scripts/libcst_dump.py"),
    "typescript": ("node", "plugin/scripts/ts_dump.mjs"),
}

CORPUS_GLOBS = {
    "ruby": ("*.rb",),
    "python": ("*.py",),
    "typescript": ("*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs"),
}


def discover(roots: list[Path], language: str) -> list[Path]:
    """Every corpus file of ``language`` under ``roots``, deduped and sorted."""
    found: set[Path] = set()
    for root in roots:
        for pattern in CORPUS_GLOBS[language]:
            for path in root.rglob(pattern):
                if path.is_file() and "node_modules" not in path.parts:
                    found.add(path.resolve())
    return sorted(found)


def run_dumper(language: str, paths: list[Path]) -> dict[str, dict]:
    """Ground truth: the existing dump script's NDJSON, keyed by path."""
    interpreter, script = DUMPERS[language]
    proc = subprocess.run(
        [interpreter, str(REPO_ROOT / script)],
        input="\n".join(str(p) for p in paths),
        capture_output=True,
        text=True,
        timeout=600,
    )
    records: dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "path" in record:
            records[record["path"]] = record
    if not records and proc.stderr:
        print(f"dumper stderr: {proc.stderr[:500]}", file=sys.stderr)
    return records


def run_treesitter(paths: list[Path]) -> dict[str, dict]:
    """Candidate: the in-process extractor, flattened to the dumper's JSON shape.

    The dump scripts emit extras inline at the top level while ParsedFile nests
    them under `extras`; flattening here keeps the comparison honest without
    teaching the extractor a wire format it does not otherwise need.
    """
    from chameleon_mcp.extractors.treesitter.extractor import parse_file

    records: dict[str, dict] = {}
    for path in paths:
        parsed, skip = parse_file(path)
        if parsed is None:
            records[str(path)] = {"path": str(path), "error": skip}
            continue
        record = {
            "path": str(parsed.path),
            "content_first_200_bytes": parsed.content_first_200_bytes,
            "top_level_node_kinds": list(parsed.top_level_node_kinds),
            "default_export_kind": parsed.default_export_kind,
            "named_export_count": parsed.named_export_count,
            "import_specifiers": [list(s) for s in parsed.import_specifiers],
            "has_jsx": parsed.has_jsx,
            "parse_diagnostics_count": parsed.parse_diagnostics_count,
        }
        record.update(parsed.extras)
        records[str(parsed.path)] = record
    return records


def normalize(value):
    """Collapse representations that are equal but not identical.

    The dump scripts emit import specifiers as JSON arrays and the extractor
    holds tuples; that difference is a serialization artifact, not extraction
    drift, and treating it as a mismatch would bury the real ones.
    """
    if isinstance(value, tuple):
        return [normalize(v) for v in value]
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    return value


def compare(truth: dict[str, dict], cand: dict[str, dict], strict: bool = False) -> int:
    """Print the per-field match table. Returns the count of imperfect fields.

    In strict mode the compared set is every key the DUMPER emitted, so a
    field the extractor never produces shows up as a 0% row instead of being
    silently outside the comparison.
    """
    common = sorted(set(truth) & set(cand))
    fields = CORE_FIELDS
    if strict:
        emitted: set[str] = set()
        for rec in truth.values():
            emitted.update(k for k in rec if k not in _NOT_A_FIELD)
        fields = tuple(CORE_FIELDS) + tuple(sorted(emitted - set(CORE_FIELDS)))
    match: Counter[str] = Counter()
    total: Counter[str] = Counter()
    examples: dict[str, tuple[str, str, str]] = {}

    cand_errors = [p for p in common if "error" in cand[p]]

    for path in common:
        a, b = truth[path], cand[path]
        if "error" in b or "error" in a:
            continue
        for f in fields:
            av, bv = normalize(a.get(f)), normalize(b.get(f))
            total[f] += 1
            if av == bv:
                match[f] += 1
            elif f not in examples:
                examples[f] = (path, json.dumps(av)[:400], json.dumps(bv)[:400])

    print(f"corpus: {len(common)} files compared (dumper {len(truth)}, tree-sitter {len(cand)})")
    if cand_errors:
        print(
            f"tree-sitter skipped {len(cand_errors)}: "
            f"{', '.join(Path(p).name for p in cand_errors[:5])}"
        )
    print()
    print(f"{'field':<28} {'match':>13}   rate")
    print("-" * 58)

    imperfect = 0
    for f in fields:
        if not total[f]:
            continue
        rate = match[f] / total[f] * 100
        if rate < 100:
            imperfect += 1
        flag = "" if rate == 100 else "   <-- gap"
        print(f"{f:<28} {match[f]:>6}/{total[f]:<6} {rate:6.1f}%{flag}")

    if examples:
        print()
        for f, (path, av, bv) in examples.items():
            print(f"### first mismatch: {f}   ({Path(path).name})")
            print(f"  dumper     : {av}")
            print(f"  tree-sitter: {bv}")
            print()

    return imperfect


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", required=True, choices=sorted(DUMPERS))
    parser.add_argument(
        "--root",
        action="append",
        type=Path,
        help="corpus root (repeatable); defaults to the committed fixtures",
    )
    parser.add_argument("--limit", type=int, help="cap files compared")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="compare EVERY field the dumper emits, not just the core set",
    )
    args = parser.parse_args()

    roots = args.root or [
        REPO_ROOT / "tests" / "journey" / "fixtures",
        REPO_ROOT / "tests" / "effectiveness" / "fixtures",
    ]
    roots = [r for r in roots if r.exists()]
    if not roots:
        print("no corpus roots exist", file=sys.stderr)
        return 2

    paths = discover(roots, args.language)
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        print(f"no {args.language} files under {[str(r) for r in roots]}", file=sys.stderr)
        return 2

    truth = run_dumper(args.language, paths)
    cand = run_treesitter(paths)
    imperfect = compare(truth, cand, strict=args.strict)

    print(f"{'PARITY' if imperfect == 0 else str(imperfect) + ' FIELD(S) SHORT'}")
    return 0 if imperfect == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
