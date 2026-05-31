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
