"""Unit tests for the repo-level import-layering graph.

Builds a small on-disk repo so the resolver can stat real targets, then checks
the forbidden-upward edge derivation and the static cycle report. Both outputs
are advisory; these tests assert content, not enforcement.
"""

from __future__ import annotations

import json
from pathlib import Path

from chameleon_mcp.bootstrap.import_graph import _find_cycles, build_layering
from chameleon_mcp.extractors._base import ParsedFile


def _pf(path: Path, imports: list[tuple[str, str]]) -> ParsedFile:
    return ParsedFile(
        path=path,
        content_first_200_bytes="",
        top_level_node_kinds=(),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=tuple(imports),
        has_jsx=False,
    )


def _write(p: Path, text: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class TestForbiddenUpwardEdges:
    def test_unanimous_direction_yields_forbidden_reverse(self, tmp_path: Path):
        # Three controller files import the same model. The model never imports a
        # controller -> the reverse (model->controller) is forbidden.
        model = _write(tmp_path / "models" / "user.ts")
        ctrl_files = []
        for i in range(3):
            cf = _write(tmp_path / "controllers" / f"c{i}.ts")
            ctrl_files.append(_pf(cf, [("../models/user", "named")]))
        model_pf = _pf(model, [])
        layering = build_layering(
            files_by_archetype={"controller": ctrl_files, "model": [model_pf]},
            repo_root=tmp_path,
            language="typescript",
        )
        edges = layering["forbidden_upward_edges"]
        assert len(edges) == 1
        assert edges[0]["from"] == "model"
        assert edges[0]["to"] == "controller"
        assert edges[0]["observed_direction"]["files"] == 3

    def test_existing_reverse_crossing_disqualifies(self, tmp_path: Path):
        # The model imports a controller in one file, so the direction is not
        # unanimous and no forbidden edge is derived.
        model = _write(tmp_path / "models" / "user.ts")
        ctrl_files = [
            _pf(_write(tmp_path / "controllers" / f"c{i}.ts"), [("../models/user", "named")])
            for i in range(3)
        ]
        model_pf = _pf(model, [("../controllers/c0", "named")])
        layering = build_layering(
            files_by_archetype={"controller": ctrl_files, "model": [model_pf]},
            repo_root=tmp_path,
            language="typescript",
        )
        assert "forbidden_upward_edges" not in layering

    def test_below_min_edge_files_skipped(self, tmp_path: Path):
        model = _write(tmp_path / "models" / "user.ts")
        # Only one controller crosses; default min edge files is 3.
        ctrl = _pf(_write(tmp_path / "controllers" / "c0.ts"), [("../models/user", "named")])
        layering = build_layering(
            files_by_archetype={"controller": [ctrl], "model": [_pf(model, [])]},
            repo_root=tmp_path,
            language="typescript",
        )
        assert layering == {}

    def test_bare_package_import_skipped(self, tmp_path: Path):
        # A bare-package import resolves to no in-repo file -> no edge.
        ctrl_files = [
            _pf(_write(tmp_path / "controllers" / f"c{i}.ts"), [("react", "default")])
            for i in range(3)
        ]
        layering = build_layering(
            files_by_archetype={"controller": ctrl_files},
            repo_root=tmp_path,
            language="typescript",
        )
        assert layering == {}


class TestAliasResolution:
    def test_tsconfig_alias_resolves_to_archetype(self, tmp_path: Path):
        _write(
            tmp_path / "tsconfig.json",
            json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"@models/*": ["models/*"]}}}),
        )
        model = _write(tmp_path / "models" / "user.ts")
        ctrl_files = [
            _pf(_write(tmp_path / "controllers" / f"c{i}.ts"), [("@models/user", "named")])
            for i in range(3)
        ]
        layering = build_layering(
            files_by_archetype={"controller": ctrl_files, "model": [_pf(model, [])]},
            repo_root=tmp_path,
            language="typescript",
        )
        assert layering["forbidden_upward_edges"][0]["from"] == "model"


class TestPythonSourceRoots:
    def test_absolute_import_resolves_under_src_root(self, tmp_path: Path):
        # The PyPA src/ layout writes an absolute intra-package import WITHOUT
        # the src/ prefix, because src/ is the source root, not a package.
        # Probing only the repo root found nothing, so the edge was silently
        # dropped as "external" and layering, cycles and reexport-chase were
        # empty on every src-layout repo. symbol_index._python_source_roots
        # already models this (the calls index and reverse index both use it);
        # the layering resolver simply did not.
        from chameleon_mcp.bootstrap.import_graph import _resolve_python

        target = _write(tmp_path / "src" / "coldchain" / "config.py")
        importer = _write(tmp_path / "src" / "coldchain" / "db.py")
        assert _resolve_python("coldchain.config", importer, tmp_path) == target

    def test_absolute_import_resolves_under_non_package_source_root(self, tmp_path: Path):
        # The other shape the shared helper models: a backend/ service dir that
        # is not itself a package but directly contains one.
        from chameleon_mcp.bootstrap.import_graph import _resolve_python

        target = _write(tmp_path / "backend" / "app" / "models.py")
        _write(tmp_path / "backend" / "app" / "__init__.py")
        importer = _write(tmp_path / "backend" / "app" / "readers.py")
        assert _resolve_python("app.models", importer, tmp_path) == target

    def test_src_layout_layering_edge_is_built(self, tmp_path: Path):
        cfg = _write(tmp_path / "src" / "coldchain" / "config.py")
        svc_files = [
            _pf(
                _write(tmp_path / "src" / "coldchain" / "services" / f"s{i}.py"),
                [("coldchain.config", "named")],
            )
            for i in range(3)
        ]
        layering = build_layering(
            files_by_archetype={"service": svc_files, "config": [_pf(cfg, [])]},
            repo_root=tmp_path,
            language="python",
        )
        assert layering["forbidden_upward_edges"], "no edge built for a src-layout import"

    def test_flat_layout_still_resolves(self, tmp_path: Path):
        # The flat layout (the other documented option) must be unaffected.
        from chameleon_mcp.bootstrap.import_graph import _resolve_python

        target = _write(tmp_path / "coldchain" / "config.py")
        importer = _write(tmp_path / "coldchain" / "db.py")
        assert _resolve_python("coldchain.config", importer, tmp_path) == target


class TestRubyResolution:
    def test_require_relative_resolves(self, tmp_path: Path):
        base = _write(tmp_path / "lib" / "base.rb")
        svc_files = [
            _pf(_write(tmp_path / "services" / f"s{i}.rb"), [("../lib/base", "namespace")])
            for i in range(3)
        ]
        layering = build_layering(
            files_by_archetype={"service": svc_files, "lib": [_pf(base, [])]},
            repo_root=tmp_path,
            language="ruby",
        )
        assert layering["forbidden_upward_edges"][0]["from"] == "lib"

    def test_bare_require_skipped(self, tmp_path: Path):
        svc_files = [
            _pf(_write(tmp_path / "services" / f"s{i}.rb"), [("active_support", "default")])
            for i in range(3)
        ]
        layering = build_layering(
            files_by_archetype={"service": svc_files},
            repo_root=tmp_path,
            language="ruby",
        )
        assert layering == {}


class TestCycleReport:
    def test_two_cluster_cycle_detected(self, tmp_path: Path):
        a = _write(tmp_path / "a" / "a.ts")
        b = _write(tmp_path / "b" / "b.ts")
        a_pf = _pf(a, [("../b/b", "named")])
        b_pf = _pf(b, [("../a/a", "named")])
        layering = build_layering(
            files_by_archetype={"alpha": [a_pf], "beta": [b_pf]},
            repo_root=tmp_path,
            language="typescript",
        )
        cycles = layering["import_cycles"]
        assert len(cycles) == 1
        assert sorted(cycles[0]) == ["alpha", "beta"]

    def test_self_edge_is_not_a_cycle(self):
        assert _find_cycles({"a": {"a"}}, max_cycles=10) == []

    def test_cycle_normalized_and_deduped(self):
        adjacency = {"b": {"a"}, "a": {"b"}}
        cycles = _find_cycles(adjacency, max_cycles=10)
        assert len(cycles) == 1
        assert cycles[0][0] == "a"  # rotated to smallest member

    def test_cycle_cap_respected(self):
        # A fully-connected 4-node graph has many cycles; the cap bounds output.
        nodes = ["a", "b", "c", "d"]
        adjacency = {n: set(m for m in nodes if m != n) for n in nodes}
        cycles = _find_cycles(adjacency, max_cycles=2)
        assert len(cycles) == 2


class TestRobustness:
    def test_empty_input_returns_empty(self, tmp_path: Path):
        assert build_layering(files_by_archetype={}, repo_root=tmp_path) == {}

    def test_single_archetype_no_cross_edges(self, tmp_path: Path):
        f = _write(tmp_path / "a" / "a.ts")
        g = _write(tmp_path / "a" / "b.ts")
        files = [_pf(f, [("./b", "named")]), _pf(g, [])]
        # Both files share one archetype; intra-archetype edges carry no signal.
        assert build_layering(files_by_archetype={"alpha": files}, repo_root=tmp_path) == {}
