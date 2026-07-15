"""B4: bounded multi-hop transitive caller-impact facts for the judge.

The one-hop caller facts show who DIRECTLY calls a changed function. B4 walks
the caller graph upward (callers of callers) to surface the transitive impact
chain (changed_fn <- service <- controller) the correctness judge is documented
to be weakest at. The walk is hard-bounded (depth, fanout, total nodes, char
cap), cycle-safe, deterministic, and fails open to "".
"""

from __future__ import annotations

import json
from pathlib import Path

from chameleon_mcp import judge
from chameleon_mcp.calls_index import SCHEMA_VERSION as _CALLS_SCHEMA
from chameleon_mcp.function_catalog import ParsedFn
from chameleon_mcp.judge import FileDiff


class _FakeIndex:
    """Minimal CallsIndex stand-in: graph maps (path, name) -> caller rows."""

    def __init__(self, graph: dict):
        self.graph = graph

    def callers_of(self, path: str, name: str):
        rows = [
            {"path": p, "caller": c, "line": ln} for (p, c, ln) in self.graph.get((path, name), [])
        ]
        if not rows:
            return None
        return {"callers": rows, "total": len(rows), "truncated": False}


def _fn(name: str, start=1, end=9) -> ParsedFn:
    return ParsedFn(name, "function", 0, 0, start, None, None, "", end_line=end)


def _diff(rel: str, *, whole: bool = True) -> FileDiff:
    return FileDiff(rel_path=rel, archetype=None, diff_text="", is_whole_file=whole)


def _write_calls_index(repo: Path, callees: dict) -> None:
    d = repo / ".chameleon"
    d.mkdir(parents=True, exist_ok=True)
    (d / "calls_index.json").write_text(
        json.dumps({"schema_version": _CALLS_SCHEMA, "callees": callees}),
        encoding="utf-8",
    )
    # The caller-facts/transitive blocks now re-verify each cited caller against
    # the working tree (a deleted/no-longer-calling caller is dropped), so the
    # synthetic callers must exist on disk and still name the callee at the
    # recorded line.
    by_file: dict[str, dict[int, str]] = {}
    for _callee_rel, fns in callees.items():
        for fn_name, entry in fns.items():
            for c in entry.get("callers", []):
                path = c.get("path")
                if not isinstance(path, str):
                    continue
                line = c.get("line")
                ln = (
                    line
                    if isinstance(line, int) and not isinstance(line, bool) and line >= 1
                    else 1
                )
                by_file.setdefault(path, {})[ln] = fn_name
    for path, line_map in by_file.items():
        fp = repo / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        last = max(line_map)
        out = [
            f"  return {line_map[i]}();" if i in line_map else "  // x" for i in range(1, last + 1)
        ]
        fp.write_text("\n".join(out) + "\n", encoding="utf-8")


# --- the bounded walk --------------------------------------------------------


def test_walk_returns_two_hop_chain():
    idx = _FakeIndex(
        {
            ("repo.ts", "fetchUser"): [("service.ts", "getUser", 10)],
            ("service.ts", "getUser"): [("controller.ts", "handle", 5)],
        }
    )
    chains, _ = judge._transitive_caller_chains(
        idx, "repo.ts", "fetchUser", max_depth=2, fanout=10, total_nodes=50
    )
    # The deepest chain reaches the controller at depth 2.
    deep = [c for c in chains if len(c) == 3]
    assert deep, "expected a depth-2 chain"
    names = [hop[1] for hop in deep[0]]
    assert names == ["fetchUser", "getUser", "handle"]


def test_walk_is_cycle_safe():
    # A <- B <- A : a mutual-recursion cycle must terminate, not loop forever.
    idx = _FakeIndex(
        {
            ("a.ts", "A"): [("b.ts", "B", 1)],
            ("b.ts", "B"): [("a.ts", "A", 2)],
        }
    )
    chains, _ = judge._transitive_caller_chains(
        idx, "a.ts", "A", max_depth=5, fanout=10, total_nodes=50
    )
    assert chains  # returned, did not hang
    # 'A' (the start) is never re-expanded: it appears once, as the root.
    for c in chains:
        assert [h for h in c if (h[0], h[1]) == ("a.ts", "A")][:2] == [c[0]] or c[0] == (
            "a.ts",
            "A",
            None,
        )


def test_walk_respects_total_nodes_cap():
    # A node with many callers, each with many callers; total cap must bound it.
    graph = {("root.ts", "f"): [(f"c{i}.ts", f"g{i}", i) for i in range(20)]}
    for i in range(20):
        graph[(f"c{i}.ts", f"g{i}")] = [(f"d{i}_{j}.ts", f"h{i}_{j}", j) for j in range(20)]
    idx = _FakeIndex(graph)
    chains, _ = judge._transitive_caller_chains(
        idx, "root.ts", "f", max_depth=2, fanout=10, total_nodes=15
    )
    # Total distinct non-root nodes visited never exceeds the cap.
    nodes = {(h[0], h[1]) for c in chains for h in c[1:]}
    assert len(nodes) <= 15


def test_fanout_clip_is_signalled_when_total_cap_not_hit():
    # Regression: a node with MORE direct callers than the per-node fanout cap
    # must report the clip even when the total-node cap is nowhere near hit (the
    # shallow-but-wide case), so the caller's `truncated` is honest.
    from chameleon_mcp.blast_radius import transitive_caller_chains

    idx = _FakeIndex({("root.ts", "f"): [(f"c{i}.ts", f"g{i}", i) for i in range(4)]})
    _chains, clipped = transitive_caller_chains(
        idx, "root.ts", "f", max_depth=2, fanout=2, total_nodes=50
    )
    assert clipped is True
    _chains2, clipped2 = transitive_caller_chains(
        idx, "root.ts", "f", max_depth=2, fanout=10, total_nodes=50
    )
    assert clipped2 is False


def test_compute_blast_radius_truncated_on_fanout_clip(monkeypatch):
    from chameleon_mcp.blast_radius import compute_blast_radius

    monkeypatch.setenv("CHAMELEON_JUDGE_TRANSITIVE_FANOUT_PER_NODE", "2")
    idx = _FakeIndex({("root.ts", "f"): [(f"c{i}.ts", f"g{i}", i) for i in range(5)]})
    out = compute_blast_radius(idx, "root.ts", "f", depth=2)
    # Only 2 of 5 callers fit the fanout cap; reached < total_nodes, but the
    # result must still flag truncated rather than claim a complete blast radius.
    assert out["truncated"] is True
    assert out["reached"] == 2


def test_walk_is_deterministic():
    idx = _FakeIndex(
        {
            ("repo.ts", "f"): [("z.ts", "Z", 1), ("a.ts", "A", 2)],
            ("a.ts", "A"): [("m.ts", "M", 3)],
            ("z.ts", "Z"): [("n.ts", "N", 4)],
        }
    )
    a = judge._transitive_caller_chains(idx, "repo.ts", "f", max_depth=2, fanout=10, total_nodes=50)
    b = judge._transitive_caller_chains(idx, "repo.ts", "f", max_depth=2, fanout=10, total_nodes=50)
    assert a == b


# --- the rendered block (integration) ----------------------------------------


def test_transitive_block_renders_impact_chain(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(
        repo,
        {
            "repo.ts": {
                "fetchUser": {
                    "callers": [
                        {
                            "path": "service.ts",
                            "caller": "getUser",
                            "line": 10,
                            "grade": "import",
                        }
                    ],
                    "total": 1,
                    "truncated": False,
                }
            },
            "service.ts": {
                "getUser": {
                    "callers": [
                        {
                            "path": "controller.ts",
                            "caller": "handle",
                            "line": 5,
                            "grade": "import",
                        }
                    ],
                    "total": 1,
                    "truncated": False,
                }
            },
        },
    )
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("fetchUser")])
    block = judge.caller_facts_transitive_for_diffs(repo, [_diff("repo.ts")])
    assert block
    assert "fetchUser()" in block
    assert "getUser() [service.ts:10]" in block
    assert "handle() [controller.ts:5]" in block
    assert "<-" in block


def test_transitive_block_empty_when_only_one_hop(tmp_path, monkeypatch):
    # fetchUser <- getUser, but getUser has no callers => no depth-2 chain.
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_calls_index(
        repo,
        {
            "repo.ts": {
                "fetchUser": {
                    "callers": [
                        {
                            "path": "service.ts",
                            "caller": "getUser",
                            "line": 10,
                            "grade": "import",
                        }
                    ],
                    "total": 1,
                    "truncated": False,
                }
            }
        },
    )
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("fetchUser")])
    block = judge.caller_facts_transitive_for_diffs(repo, [_diff("repo.ts")])
    assert block == ""


def test_transitive_block_absent_index_returns_empty(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("f")])
    assert judge.caller_facts_transitive_for_diffs(repo, [_diff("a.ts")]) == ""


# --- config + thresholds -----------------------------------------------------


def test_config_judge_transitive_impact_defaults_true():
    from chameleon_mcp.profile.config import EnforcementConfig, _coerce_enforcement

    assert EnforcementConfig().judge_transitive_impact is True
    assert _coerce_enforcement({}).judge_transitive_impact is True
    assert _coerce_enforcement({"judge_transitive_impact": False}).judge_transitive_impact is False


def test_transitive_thresholds_present():
    from chameleon_mcp._thresholds import threshold_int

    assert threshold_int("JUDGE_TRANSITIVE_DEPTH") >= 1
    assert threshold_int("JUDGE_TRANSITIVE_FANOUT_PER_NODE") >= 1
    assert threshold_int("JUDGE_TRANSITIVE_TOTAL_NODES") >= 1
    assert threshold_int("JUDGE_TRANSITIVE_CHAR_CAP") >= 1


# --- review hardening: DAG diamonds, noise filter, bounds, telemetry ---------


def test_walk_preserves_dag_diamond_paths():
    # root <- A and root <- B, both A and B call C. BOTH paths to C must survive
    # (a global visited set would drop one).
    idx = _FakeIndex(
        {
            ("root.ts", "f"): [("a.ts", "A", 1), ("b.ts", "B", 2)],
            ("a.ts", "A"): [("c.ts", "C", 3)],
            ("b.ts", "B"): [("c.ts", "C", 4)],
        }
    )
    chains, _ = judge._transitive_caller_chains(
        idx, "root.ts", "f", max_depth=2, fanout=10, total_nodes=50
    )
    deep = {tuple(h[1] for h in c) for c in chains if len(c) == 3}
    assert ("f", "A", "C") in deep
    assert ("f", "B", "C") in deep  # the diamond's second path is kept


def test_walk_counts_anonymous_edges_but_does_not_expand_into_them():
    # Anonymous/module-scope callers are real caller EDGES (get_blast_radius must
    # count them for parity with get_callers), so the walk keeps them as terminal
    # hops -- but it never expands INTO them, since their placeholder name does not
    # identify one actionable scope.
    idx = _FakeIndex(
        {
            ("repo.ts", "f"): [("svc.ts", "getThing", 1)],
            ("svc.ts", "getThing"): [
                ("h.ts", "<anonymous>", 2),
                ("m.ts", "<module>", 3),
            ],
            # A caller OF the anonymous scope must NOT be reached: the walk stops.
            ("h.ts", "<anonymous>"): [("deep.ts", "shouldNotAppear", 4)],
        }
    )
    chains, _ = judge._transitive_caller_chains(
        idx, "repo.ts", "f", max_depth=3, fanout=10, total_nodes=50
    )
    names = {h[1] for c in chains for h in c}
    assert "<anonymous>" in names and "<module>" in names
    assert "shouldNotAppear" not in names


def test_transitive_block_fanout_cap_bounds_rendered(tmp_path, monkeypatch):
    # A node with many named callers, each two hops up; fanout caps the breadth.
    repo = tmp_path / "repo"
    repo.mkdir()
    callees = {
        "repo.ts": {
            "f": {
                "callers": [
                    {
                        "path": f"s{i}.ts",
                        "caller": f"g{i}",
                        "line": i,
                        "grade": "import",
                    }
                    for i in range(8)
                ],
                "total": 8,
                "truncated": False,
            }
        }
    }
    for i in range(8):
        callees[f"s{i}.ts"] = {
            f"g{i}": {
                "callers": [
                    {
                        "path": f"c{i}.ts",
                        "caller": f"h{i}",
                        "line": i,
                        "grade": "import",
                    }
                ],
                "total": 1,
                "truncated": False,
            }
        }
    _write_calls_index(repo, callees)
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("f")])
    monkeypatch.setenv("CHAMELEON_JUDGE_TRANSITIVE_FANOUT_PER_NODE", "3")
    block = judge.caller_facts_transitive_for_diffs(repo, [_diff("repo.ts")])
    # At most 3 first-level branches expand into depth-2 chains.
    chain_lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
    assert 1 <= len(chain_lines) <= 3


def test_transitive_block_char_cap_emits_tail(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    callees = {
        "repo.ts": {
            "f": {
                "callers": [
                    {
                        "path": f"service_with_long_name_{i}.ts",
                        "caller": f"handler{i}",
                        "line": i,
                        "grade": "import",
                    }
                    for i in range(6)
                ],
                "total": 6,
                "truncated": False,
            }
        }
    }
    for i in range(6):
        callees[f"service_with_long_name_{i}.ts"] = {
            f"handler{i}": {
                "callers": [
                    {
                        "path": f"controller_long_{i}.ts",
                        "caller": f"entry{i}",
                        "line": i,
                        "grade": "import",
                    }
                ],
                "total": 1,
                "truncated": False,
            }
        }
    _write_calls_index(repo, callees)
    monkeypatch.setattr(judge, "_parse_changed_file", lambda root, path: [_fn("f")])
    # The header is ~200 chars; this cap fits the header plus a couple of chains
    # but not all six, forcing the dropped-count tail.
    monkeypatch.setenv("CHAMELEON_JUDGE_TRANSITIVE_CHAR_CAP", "450")
    block = judge.caller_facts_transitive_for_diffs(repo, [_diff("repo.ts")])
    assert block
    assert len(block) <= 450
    assert "more transitive" in block  # the dropped-count tail


# code-review remediation: bool-as-int in the finding parser


def test_coerce_findings_rejects_bool_line_and_confidence():
    from chameleon_mcp.judge import _coerce_findings

    out = _coerce_findings([{"message": "x", "line": True, "confidence": True}])
    assert len(out) == 1
    assert out[0].line is None  # bool not treated as int 1
    assert out[0].confidence == 0.0  # bool not coerced to 1.0


def test_live_transitive_chain_truncates_at_stale_edge(tmp_path):
    # leaf <- mid [mid.ts:2] <- top [top.ts:2]; mid.ts is deleted, so the edge
    # leaf<-mid is stale and the chain truncates to just the root (then a caller
    # that drops below the hop threshold is dropped by the builder).
    (tmp_path / "top.ts").write_text("return mid();\n", encoding="utf-8")  # top.ts exists, refs mid
    # mid.ts intentionally NOT created -> the leaf<-mid edge cannot verify
    chain = [("leaf.ts", "leaf", None), ("mid.ts", "mid", 2), ("top.ts", "top", 2)]
    kept = judge._live_transitive_chain(tmp_path, chain)
    assert kept == [("leaf.ts", "leaf", None)]


def test_live_transitive_chain_keeps_fully_live_chain(tmp_path):
    (tmp_path / "mid.ts").write_text("x\nreturn leaf();\n", encoding="utf-8")  # line 2 refs leaf
    (tmp_path / "top.ts").write_text("y\nreturn mid();\n", encoding="utf-8")  # line 2 refs mid
    chain = [("leaf.ts", "leaf", None), ("mid.ts", "mid", 2), ("top.ts", "top", 2)]
    assert judge._live_transitive_chain(tmp_path, chain) == chain
