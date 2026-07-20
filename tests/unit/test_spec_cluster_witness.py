"""Co-located *.spec.ts files form one cross-directory archetype WITH a witness.

Jest/Vitest co-locate specs beside their source (orders/orders.service.spec.ts),
so each feature dir's lone spec used to land in its own 1-member sparse cluster:
no archetype, no canonical, no guidance. The spec role bucket merges them; the
canonical pool excludes ``*.spec.*`` by leaf glob, so the cluster's witness must
come from select_canonicals' empty-pool retry (``allow_tests=True`` re-admits
ONLY the test-reason exclusions).
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.bootstrap.canonical import select_canonicals
from chameleon_mcp.bootstrap.clustering import cluster_files
from chameleon_mcp.bootstrap.discovery import is_eligible_as_canonical
from chameleon_mcp.extractors._base import ParsedFile

_SPEC_BODY = (
    "import { OrdersService } from './orders.service';\n"
    "describe('OrdersService', () => {\n"
    "  it('creates an order', () => {\n"
    "    expect(new OrdersService().create()).toBeDefined();\n"
    "  });\n"
    "});\n"
)


def _spec_pf(path: Path) -> ParsedFile:
    return ParsedFile(
        path=path,
        content_first_200_bytes=_SPEC_BODY[:200],
        top_level_node_kinds=("ImportDeclaration", "ExpressionStatement"),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(("./orders.service", "named"),),
        has_jsx=False,
    )


def _write(repo: Path, rel: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_SPEC_BODY, encoding="utf-8")
    return p


class TestSpecGlobEligibility:
    def test_spec_basename_excluded_from_canonical_pool(self):
        assert is_eligible_as_canonical("src/orders/orders.service.spec.ts") is False

    def test_allow_tests_retry_readmits_spec_basename(self):
        # The predicate the empty-pool retry uses: allow_tests must re-admit the
        # leaf-glob exclusion too, or a colocated spec cluster can never get a
        # witness.
        assert is_eligible_as_canonical("src/orders/orders.service.spec.ts", allow_tests=True) is (
            True
        )

    def test_legacy_dir_stays_excluded_under_retry(self):
        assert is_eligible_as_canonical("legacy/orders.service.spec.ts", allow_tests=True) is False


class TestColocatedSpecCluster:
    def _bootstrap_pieces(self, tmp_path):
        repo = tmp_path / "repo"
        paths = [
            _write(repo, "src/orders/orders.service.spec.ts"),
            _write(repo, "src/inventory/inventory.service.spec.ts"),
            _write(repo, "src/shipments/shipments.service.spec.ts"),
        ]
        pfs = [_spec_pf(p) for p in paths]
        return repo, cluster_files(pfs, repo, min_cluster_size=3)

    def test_specs_merge_into_one_dense_cluster(self, tmp_path):
        _repo, result = self._bootstrap_pieces(tmp_path)
        dense = result.dense_clusters
        assert len(dense) == 1
        assert dense[0].key.path_pattern_bucket == "spec:ts"
        assert len(dense[0].members) == 3

    def test_spec_cluster_gets_witness_via_retry(self, tmp_path):
        repo, result = self._bootstrap_pieces(tmp_path)
        sel = select_canonicals(result.dense_clusters, repo)
        assert sel.selections, "the spec cluster must get a canonical witness"
        assert not sel.clusters_without_eligible_canonical
        witnesses = {s.witness_path.name for s in sel.selections.values()}
        assert witnesses <= {
            "orders.service.spec.ts",
            "inventory.service.spec.ts",
            "shipments.service.spec.ts",
        }
