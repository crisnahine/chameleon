"""Unit tests for chameleon_mcp.bootstrap.clustering helpers."""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.bootstrap.clustering import _member_sub_bucket
from chameleon_mcp.extractors._base import ParsedFile


def _pf(path) -> ParsedFile:
    return ParsedFile(
        path=path,
        content_first_200_bytes="",
        top_level_node_kinds=("ClassNode",),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
    )


class TestMemberSubBucket:
    def test_relative_to_repo_root_matches_initial_clustering(self):
        repo = Path("/repo")
        pf = _pf(repo / "app/models/concerns/searchable.rb")
        assert _member_sub_bucket(pf, repo) == "concerns"

    def test_absolute_path_without_repo_root_does_not_match(self):
        pf = _pf(Path("/repo/app/models/concerns/searchable.rb"))
        assert _member_sub_bucket(pf) != "concerns"

    def test_none_path_returns_empty(self):
        assert _member_sub_bucket(_pf(None)) == ""


class TestRoleBucketSparseExemption:
    def _cluster_names(self, paths, root):
        from chameleon_mcp.bootstrap.clustering import cluster_files

        result = cluster_files([_pf(root / p) for p in paths], repo_root=root)
        return result

    def test_two_app_django_role_cluster_is_dense(self, tmp_path):
        # Two apps' views.py role-bucket into one 2-member cluster; the
        # adaptive floor of 3 must not drop the deliberate framework grouping.
        result = self._cluster_names(
            ["catalog/views.py", "orders/views.py", "lib/a.py", "lib/b.py", "lib/c.py"],
            tmp_path,
        )
        by_bucket = {c.key.path_pattern_bucket: c for c in result.clusters}
        view = by_bucket.get("view:py")
        assert view is not None and view.size == 2
        assert not view.is_sparse
        assert view in result.dense_clusters

    def test_single_member_role_cluster_stays_sparse(self, tmp_path):
        # One file is a location, not a layer -- the exemption starts at two.
        result = self._cluster_names(
            ["catalog/serializers.py", "lib/a.py", "lib/b.py", "lib/c.py"],
            tmp_path,
        )
        ser = next(
            (c for c in result.clusters if c.key.path_pattern_bucket == "serializer:py"), None
        )
        assert ser is not None and ser.size == 1
        assert ser.is_sparse

    def test_non_role_two_member_cluster_stays_sparse(self, tmp_path):
        # An accidental two-file directory grouping keeps the adaptive floor.
        result = self._cluster_names(
            ["misc/one.py", "misc/two.py", "lib/a.py", "lib/b.py", "lib/c.py"],
            tmp_path,
        )
        misc = next(
            (c for c in result.clusters if "misc" in (c.key.path_pattern_bucket or "")), None
        )
        assert misc is not None and misc.size == 2
        assert misc.is_sparse

    def test_two_member_colocated_spec_cluster_is_dense(self, tmp_path):
        # Co-located spec files role-bucket cross-dir; two features' specs are
        # the repo's spec layer and must survive the adaptive floor like any
        # other framework role grouping.
        result = self._cluster_names(
            [
                "src/orders/orders.service.spec.ts",
                "src/inventory/inventory.service.spec.ts",
                "lib/a.ts",
                "lib/b.ts",
                "lib/c.ts",
            ],
            tmp_path,
        )
        spec = next((c for c in result.clusters if c.key.path_pattern_bucket == "spec:ts"), None)
        assert spec is not None and spec.size == 2
        assert not spec.is_sparse
