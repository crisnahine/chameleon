"""Shared bounded transitive caller walk + structured blast-radius reach.

The upward caller-graph walk is used in two places: the turn-end correctness
judge (``caller_facts_transitive_for_diffs``, which renders an advisory impact
block over the changed functions) and the ``get_blast_radius`` read tool (which
answers "what transitively calls this symbol" for pr-review and the human).
Extracting the walk here keeps both consumers on ONE deterministic,
conservatively-graded traversal of the committed ``calls_index`` rather than two
divergent ones. The judge re-binds ``transitive_caller_chains`` under its prior
private name, so its behavior is unchanged by the extraction.

Honesty posture (carried verbatim from the judge): the calls index is a
committed snapshot with deterministic grades only, and the absence of a caller
edge is never evidence of dead code -- dynamic dispatch, runtime reflection,
metaprogramming, and callers added after the last bootstrap are all invisible.
The reach is a grounding fact for deterministic review, not a reachability
oracle.
"""

from __future__ import annotations

from chameleon_mcp._thresholds import threshold_int

# Caller "names" that don't identify an actionable function: a chain that hops
# into one reads as noise to the reviewer, so the walk stops at the last named
# function instead of extending into them.
_UNINFORMATIVE_CALLERS = frozenset({"<anonymous>", "<module>"})

# The honesty note returned alongside every blast-radius result, so a consumer
# never reads an empty or short reach as proof of dead code.
BLAST_RADIUS_NOTE = (
    "Callers-of-callers from the committed calls snapshot (deterministic grades "
    "only). Absence of a caller is NOT evidence of dead code: dynamic dispatch, "
    "reflection, and callers added since the last refresh are invisible. A stale "
    "intermediate edge can shorten a chain. This is a grounding fact for review, "
    "not a reachability oracle; run /chameleon-refresh to update the snapshot."
)


def transitive_caller_chains(
    index, start_path: str, start_name: str, *, max_depth: int, fanout: int, total_nodes: int
) -> list[list[tuple]]:
    """Bounded upward caller chains from ``(start_path, start_name)``.

    Returns a list of chains; each chain is ``[(path, name, line), ...]`` root
    first (the root carries no call-site line), deepest hop last. Walks up the
    caller graph at most ``max_depth`` hops, expanding at most ``fanout`` named
    callers per node and ``total_nodes`` nodes total.

    Cycle-safety is per CHAIN, not global: a node is never revisited within the
    same chain (so ``A <- B <- A`` terminates), but a node reached by two
    distinct paths (a DAG diamond) is preserved in BOTH chains -- a global
    visited set would silently drop one path and understate the impact. The
    ``total_nodes`` counter still hard-bounds total work. Anonymous / module-scope
    callers are not extended into. Caller rows are sorted before expansion and the
    chains sorted before returning, so the result is deterministic.
    """
    chains: list[list[tuple]] = []
    nodes = 0
    # DFS; each stack item is (chain so far, set of (path,name) already in it).
    stack: list[tuple[list[tuple], set]] = [
        ([(start_path, start_name, None)], {(start_path, start_name)})
    ]
    while stack:
        chain, seen = stack.pop()
        cur_path, cur_name, _line = chain[-1]
        if len(chain) - 1 >= max_depth or nodes >= total_nodes:
            chains.append(chain)
            continue
        entry = index.callers_of(cur_path, cur_name)
        callers = (entry or {}).get("callers") if entry else None
        ordered = sorted(
            (r for r in (callers or []) if r.get("caller") not in _UNINFORMATIVE_CALLERS),
            key=lambda r: (
                str(r.get("path") or ""),
                str(r.get("caller") or ""),
                r.get("line") if isinstance(r.get("line"), int) else -1,
            ),
        )
        expanded: list[tuple[list[tuple], set]] = []
        for r in ordered:
            if len(expanded) >= fanout or nodes >= total_nodes:
                break
            key = (r.get("path"), r.get("caller"))
            if key in seen:  # a cycle within THIS chain
                continue
            nodes += 1
            expanded.append(
                (chain + [(r.get("path"), r.get("caller"), r.get("line"))], seen | {key})
            )
        if not expanded:
            chains.append(chain)  # entry point, depth cap, or all-cyclic: terminal
        else:
            # Push reversed so the deterministic caller order is preserved on pop.
            for e in reversed(expanded):
                stack.append(e)
    chains.sort(
        key=lambda c: tuple((p or "", n or "", ln if isinstance(ln, int) else -1) for p, n, ln in c)
    )
    return chains


def compute_blast_radius(index, file_rel: str, function_name: str, *, depth: int) -> dict:
    """Structured transitive caller reach for ``(file_rel, function_name)``.

    Walks ``index`` upward with the shared judge caps (fanout / total-nodes), to
    ``depth`` hops, and returns ``{"chains", "reached", "truncated"}`` where each
    chain is a list of ``{"path", "name", "line"}`` hops root-first. Root-only
    chains (a symbol with no recorded callers along that branch) are dropped, so
    an empty ``chains`` means no deterministic callers were recorded. ``reached``
    is the count of distinct caller ``(path, name)`` nodes; ``truncated`` is True
    when the total-nodes cap bounded the walk. Returns raw (unsanitized) strings;
    the read tool sanitizes before the model surface.
    """
    fanout = threshold_int("JUDGE_TRANSITIVE_FANOUT_PER_NODE")
    total = threshold_int("JUDGE_TRANSITIVE_TOTAL_NODES")
    raw = transitive_caller_chains(
        index, file_rel, function_name, max_depth=depth, fanout=fanout, total_nodes=total
    )
    chains: list[list[dict]] = []
    reached: set[tuple] = set()
    for c in raw:
        if len(c) < 2:
            continue  # root-only branch: no recorded caller here
        chains.append([{"path": p, "name": n, "line": ln} for (p, n, ln) in c])
        for p, n, _ln in c[1:]:
            reached.add((p, n))
    return {"chains": chains, "reached": len(reached), "truncated": len(reached) >= total}
