"""Build-time barrel-chase resolution (#12).

Pins the two pure helpers that attribute a named re-export chain
(``export { x as y } from './impl'``) to the file that DEFINES the symbol:

- ``build_reexport_map`` turns each barrel's ``re_exports`` rows into
  ``barrel -> exported -> (origin, target)``, dropping ambiguous and
  out-of-repo edges.
- ``chase_reexport`` follows a name through those barrels to the definer,
  bounded, cycle-safe, and name-mapping per hop.

The build/query integration (reverse + calls index additive edges, ``via``
surfaced through the tools) is covered end-to-end against a real repo; these
tests pin the deterministic core so a future edit can't silently regress it.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.symbol_index import build_reexport_map, chase_reexport


class _PF:
    def __init__(self, path: str, re_exports: list[dict]):
        self.path = path
        self.extras = {"re_exports": re_exports}


def _resolver(mapping: dict[str, str]):
    """Fake module resolver: (module, importer_dir) -> rel key or None."""

    def resolve(module: str, importer_dir: Path) -> str | None:
        return mapping.get(module)

    return resolve


# --- chase_reexport ----------------------------------------------------------


def test_chase_single_hop_with_alias_maps_name():
    # barrel re-exports { Impl as Public } from ./impl -> a consumer of Public
    # lands on impl.ts under the ORIGIN name Impl, via the barrel.
    rmap = {"barrel.ts": {"Public": ("Impl", "impl.ts")}}
    final, name, via = chase_reexport("barrel.ts", "Public", rmap)
    assert (final, name, via) == ("impl.ts", "Impl", ["barrel.ts"])


def test_chase_multi_hop_chains_and_maps_each_name():
    rmap = {
        "index.ts": {"A": ("B", "mid.ts")},
        "mid.ts": {"B": ("C", "impl.ts")},
    }
    final, name, via = chase_reexport("index.ts", "A", rmap)
    assert final == "impl.ts"
    assert name == "C"
    assert via == ["index.ts", "mid.ts"]


def test_chase_not_a_barrel_returns_input_unchanged():
    final, name, via = chase_reexport("plain.ts", "foo", {})
    assert (final, name, via) == ("plain.ts", "foo", [])


def test_chase_is_cycle_safe():
    # a re-exports from b, b re-exports the same name back to a: must terminate.
    rmap = {
        "a.ts": {"X": ("X", "b.ts")},
        "b.ts": {"X": ("X", "a.ts")},
    }
    final, name, via = chase_reexport("a.ts", "X", rmap)
    # Stops at the first re-visit rather than looping; bounded via chain.
    assert final in {"a.ts", "b.ts"}
    assert len(via) <= 2


def test_chase_respects_hop_cap():
    # A chain longer than max_hops stops at the last file reached, never loops.
    rmap = {f"h{i}.ts": {"N": ("N", f"h{i + 1}.ts")} for i in range(10)}
    final, _name, via = chase_reexport("h0.ts", "N", rmap, max_hops=3)
    assert len(via) == 3
    assert final == "h3.ts"


# --- build_reexport_map ------------------------------------------------------


def test_build_map_records_alias_and_plain(tmp_path):
    root = tmp_path
    files = [
        _PF(
            str(root / "barrel.ts"),
            [
                {"exported": "Public", "origin": "Impl", "module": "./impl", "line": 1},
                {"exported": "util", "origin": "util", "module": "./util", "line": 2},
            ],
        )
    ]
    rmap = build_reexport_map(files, root, _resolver({"./impl": "impl.ts", "./util": "util.ts"}))
    assert rmap["barrel.ts"]["Public"] == ("Impl", "impl.ts")
    assert rmap["barrel.ts"]["util"] == ("util", "util.ts")


def test_build_map_drops_ambiguous_exported_name(tmp_path):
    # Same exported name re-exported from two distinct in-repo sources: never
    # guess, drop it so the chase stops at the barrel.
    root = tmp_path
    files = [
        _PF(
            str(root / "barrel.ts"),
            [
                {"exported": "X", "origin": "X", "module": "./a", "line": 1},
                {"exported": "X", "origin": "X", "module": "./b", "line": 2},
            ],
        )
    ]
    rmap = build_reexport_map(files, root, _resolver({"./a": "a.ts", "./b": "b.ts"}))
    assert "X" not in rmap.get("barrel.ts", {})


def test_build_map_omits_out_of_repo_target(tmp_path):
    # A bare package / unresolved alias resolves to None: omit so the chase
    # stops at the barrel (edge stays attributed to the named module).
    root = tmp_path
    files = [
        _PF(
            str(root / "barrel.ts"),
            [{"exported": "cloneDeep", "origin": "cloneDeep", "module": "lodash", "line": 1}],
        )
    ]
    rmap = build_reexport_map(files, root, _resolver({}))
    assert rmap == {}


def test_build_map_duplicate_same_source_kept(tmp_path):
    # The same (origin, target) appearing twice is idempotent, not ambiguous.
    root = tmp_path
    files = [
        _PF(
            str(root / "barrel.ts"),
            [
                {"exported": "X", "origin": "X", "module": "./a", "line": 1},
                {"exported": "X", "origin": "X", "module": "./a", "line": 5},
            ],
        )
    ]
    rmap = build_reexport_map(files, root, _resolver({"./a": "a.ts"}))
    assert rmap["barrel.ts"]["X"] == ("X", "a.ts")
