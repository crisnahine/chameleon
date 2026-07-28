#!/usr/bin/env python3
"""Fail when the package's largest import cycle grows.

Most of chameleon_mcp sits in one strongly-connected component. That is why
tools.py cannot simply be split: nearly anything it would move out still imports
back into it, so the cycle is the real constraint on the module layout, and the
deferred function-level imports scattered through the package are the symptom.

"Break the cycle" is not a task anyone can finish in one pass. A ratchet is:
record today's size, refuse to let it grow, and every later extraction lowers
the ceiling permanently. That converts an unbounded refactor into a one-way
gate no future change can quietly undo.

The number is only meaningful relative to itself. Different edge-counting
choices give different answers -- whether TYPE_CHECKING-only imports count,
whether a package `__init__` is its own node -- and analyses of this repo have
reported 48, 55, and 57 for what is the same underlying tangle. So this script
IS the definition: it fixes one methodology, and the committed baseline is
whatever it measures. Do not reconcile it against a number in a doc; docs drift
and this does not.

Counted: every `import chameleon_mcp.x` / `from chameleon_mcp.x import y` that
resolves to a module in the package, at any nesting depth -- a function-level
import still couples the two modules at run time, and pretending otherwise
would let the cycle grow through exactly the workaround it already uses.

Exit 0 = at or below baseline. Exit 1 = grew.
"""

from __future__ import annotations

import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "plugin" / "mcp" / "chameleon_mcp"
BASELINE_FILE = ROOT / "scripts" / "import-cycle-baseline.json"
PREFIX = "chameleon_mcp"


def module_name(path: Path) -> str:
    rel = path.relative_to(PKG).with_suffix("")
    name = ".".join(rel.parts)
    if name.endswith(".__init__"):
        name = name[: -len(".__init__")]
    return name or "__init__"


def build_graph() -> tuple[dict[str, Path], dict[str, set[str]]]:
    mods = {module_name(p): p for p in PKG.rglob("*.py")}
    edges: dict[str, set[str]] = defaultdict(set)
    for name, path in mods.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == PREFIX:
                    # `from chameleon_mcp import x` -- x may itself be a module.
                    targets += [a.name for a in node.names]
                elif node.module.startswith(PREFIX + "."):
                    stem = node.module[len(PREFIX) + 1 :]
                    targets.append(stem)
                    targets += [f"{stem}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith(PREFIX + "."):
                        targets.append(a.name[len(PREFIX) + 1 :])
            for t in targets:
                if t in mods and t != name:
                    edges[name].add(t)
    return mods, edges


def largest_scc(mods: dict[str, Path], edges: dict[str, set[str]]) -> list[str]:
    """Tarjan, iterative -- the graph is deep enough to blow the recursion limit."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    best: list[str] = []
    counter = 0

    for root in mods:
        if root in index:
            continue
        work: list[tuple[str, object]] = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:  # type: ignore[union-attr]
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(edges.get(w, ())))))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > len(best):
                    best = comp
    return sorted(best)


def main() -> int:
    if not PKG.is_dir():
        print(f"package not found: {PKG}")
        return 0

    mods, edges = build_graph()
    comp = largest_scc(mods, edges)
    size = len(comp)
    total_edges = sum(len(v) for v in edges.values())

    print(f"modules={len(mods)}  internal edges={total_edges}  largest cycle={size}")

    if "--write-baseline" in sys.argv:
        BASELINE_FILE.write_text(
            json.dumps({"largest_import_cycle": size, "modules": len(mods)}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"baseline written: {size}")
        return 0

    if not BASELINE_FILE.is_file():
        print(f"no baseline at {BASELINE_FILE}; run with --write-baseline")
        return 1
    try:
        baseline = int(
            json.loads(BASELINE_FILE.read_text(encoding="utf-8"))["largest_import_cycle"]
        )
    except (ValueError, KeyError, OSError):
        print("baseline unreadable")
        return 1

    if size > baseline:
        print(f"\nImport cycle GREW: {baseline} -> {size}\n")
        print("  A new edge pulled more modules into the tangle. Either route the")
        print("  new dependency the other way, or move the shared piece into a leaf")
        print("  module both sides can import.\n")
        print(f"  Cycle members ({size}):")
        for m in comp:
            print(f"    {m}")
        return 1

    if size < baseline:
        print(f"\nCycle SHRANK: {baseline} -> {size}. Lower the baseline:")
        print("  python3 scripts/check-import-cycle.py --write-baseline")
        return 1

    print(f"ok: largest cycle {size}, at baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
