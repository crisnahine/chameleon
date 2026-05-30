"""Env-free coverage for the bootstrap clustering -> canonical pipeline.

Previously only `_member_sub_bucket` had a dedicated test; the clustering
ALGORITHM and canonical witness selection — the heart of "auto-derive
conventions" — were exercised only via the env-gated qa_*.py batteries (not run
in CI). This drives synthetic ParsedFiles through cluster_files +
select_canonicals against real on-disk files, with no subprocess or env
dependency.
"""
from __future__ import annotations

from pathlib import Path

from chameleon_mcp.bootstrap.canonical import select_canonicals
from chameleon_mcp.bootstrap.clustering import cluster_files
from chameleon_mcp.extractors._base import ParsedFile


def _pf(path, *, kinds=("ClassNode",), named=0, jsx=False, default_kind=None) -> ParsedFile:
    return ParsedFile(
        path=path,
        content_first_200_bytes="",
        top_level_node_kinds=kinds,
        default_export_kind=default_kind,
        named_export_count=named,
        import_specifiers=(),
        has_jsx=jsx,
    )


def _write(repo: Path, rel: str, body: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _services(repo: Path, n: int) -> list[ParsedFile]:
    out = []
    for i in range(n):
        p = _write(repo, f"app/services/svc_{i}.rb", f"class Svc{i}\n  def call; {i}; end\nend\n")
        out.append(_pf(p))
    return out


def test_same_shape_files_cluster_together(tmp_path):
    repo = tmp_path / "repo"
    result = cluster_files(_services(repo, 4), repo, min_cluster_size=2)
    assert result.clusters, "clustering produced no clusters"
    assert max(c.size for c in result.clusters) == 4


def test_distinct_shapes_form_distinct_clusters(tmp_path):
    repo = tmp_path / "repo"
    pfs = _services(repo, 3)
    for i in range(3):
        p = _write(repo, f"app/components/Comp{i}.tsx", f"export function Comp{i}() {{ return {i}; }}\n")
        pfs.append(_pf(p, kinds=("FunctionDeclaration",), jsx=True))
    result = cluster_files(pfs, repo, min_cluster_size=2)
    assert len(result.clusters) >= 2


def test_select_canonicals_picks_a_real_witness(tmp_path):
    repo = tmp_path / "repo"
    result = cluster_files(_services(repo, 3), repo, min_cluster_size=2)
    sel = select_canonicals(result.clusters, repo)
    assert sel.selections, "no canonical witness selected"
    witness = next(iter(sel.selections.values())).witness_path
    assert witness.exists()
    assert witness.suffix == ".rb"


def test_schema_version_constants_agree():
    # Regression guard for the inert-bump bug: the bootstrap WRITES profiles
    # using orchestrator.PROFILE_SCHEMA_VERSION, which must track the engine's
    # CURRENT_SCHEMA_VERSION and the loader's MAX_SUPPORTED. They drifted once
    # (orchestrator left at 7 while schema/loader moved to 8), so a freshly
    # bootstrapped profile was stamped with a stale version.
    from chameleon_mcp.bootstrap.orchestrator import PROFILE_SCHEMA_VERSION
    from chameleon_mcp.profile.loader import MAX_SUPPORTED_SCHEMA_VERSION
    from chameleon_mcp.profile.schema import CURRENT_SCHEMA_VERSION

    assert PROFILE_SCHEMA_VERSION == CURRENT_SCHEMA_VERSION == MAX_SUPPORTED_SCHEMA_VERSION
