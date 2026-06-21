"""B3: generated-file canonical-witness exclusion.

A purely-structural cluster can mix machine-generated files (GraphQL resolvers,
Prisma client, protobuf stubs, *.gen.* output) with hand-written ones. Telling
the AI to "follow" a generated file teaches it to mimic codegen output instead
of the team's conventions, so generated files are excluded from the canonical
witness pool. This is witness-selection only: clustering, archetype membership,
and the ClusterKey 7-tuple are untouched (no re-clustering of existing repos).
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.bootstrap.canonical import select_canonicals
from chameleon_mcp.bootstrap.clustering import cluster_files
from chameleon_mcp.bootstrap.discovery import is_eligible_as_canonical, is_generated_path
from chameleon_mcp.extractors._base import ParsedFile


def _pf(path: Path) -> ParsedFile:
    return ParsedFile(
        path=path,
        content_first_200_bytes="",
        top_level_node_kinds=("ClassNode",),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
    )


def _write(repo: Path, rel: str, body: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_is_generated_path_detects_generated_markers():
    assert is_generated_path("src/__generated__/resolver.ts")
    assert is_generated_path("app/api/types.gen.ts")
    assert is_generated_path("web/schema.generated.tsx")
    assert is_generated_path("proto/foo_pb2.py")
    assert is_generated_path("pkg/foo.pb.go")
    # TypeScript/JavaScript protobuf (protoc-gen-ts / grpc-web).
    assert is_generated_path("rpc/service.pb.ts")
    assert is_generated_path("rpc/service_pb.js")


def test_is_generated_path_does_not_overmatch_handwritten():
    assert not is_generated_path("src/components/Button.tsx")
    assert not is_generated_path("src/oxygen.ts")  # 'gen' inside a word
    assert not is_generated_path("src/regenerate.ts")  # 'gen' inside a word
    assert not is_generated_path("app/services/payment_service.rb")
    assert not is_generated_path("src/generators/make.ts")  # 'generators' != 'generated'
    # A bare "generated" directory of HAND-WRITTEN code must not be excluded;
    # only the unambiguous __generated__ codegen marker counts.
    assert not is_generated_path("app/generated/generators/make_code.ts")


def test_is_eligible_as_canonical_rejects_generated():
    assert not is_eligible_as_canonical("src/__generated__/r.ts")
    assert not is_eligible_as_canonical("api/types.gen.ts")
    assert is_eligible_as_canonical("src/components/Button.tsx")  # normal still eligible


def test_handwritten_wins_witness_over_generated_sibling(tmp_path):
    repo = tmp_path / "repo"
    # Same dir + same AST shape => same cluster. The generated filename sorts
    # BEFORE the hand-written one lexicographically, so without B3 the tie-break
    # would pick the generated file; with B3 it is excluded from the pool.
    real = _write(repo, "app/services/svc_real.rb", "class A\n  def call; end\nend\n")
    gen = _write(repo, "app/services/svc_data.gen.rb", "class B\n  def call; end\nend\n")
    pfs = [_pf(real), _pf(gen)]
    result = cluster_files(pfs, repo, min_cluster_size=2)
    sel = select_canonicals(result.clusters, repo)
    assert sel.selections, "expected a witness"
    witnesses = {s.witness_path.name for s in sel.selections.values()}
    assert "svc_real.rb" in witnesses
    assert "svc_data.gen.rb" not in witnesses


def test_all_generated_cluster_has_no_witness_but_does_not_crash(tmp_path):
    # A cluster whose every member is generated yields no eligible canonical.
    # That must be handled gracefully (no crash; the cluster is reported as
    # having no clean witness), not picked-anyway.
    repo = tmp_path / "repo"
    a = _write(repo, "app/services/a.gen.rb", "class A\n  def call; end\nend\n")
    b = _write(repo, "app/services/b.gen.rb", "class B\n  def call; end\nend\n")
    result = cluster_files([_pf(a), _pf(b)], repo, min_cluster_size=2)
    sel = select_canonicals(result.clusters, repo)
    chosen = {s.witness_path.name for s in sel.selections.values()}
    assert "a.gen.rb" not in chosen and "b.gen.rb" not in chosen
    # The cluster is surfaced as lacking an eligible canonical, not crashed over.
    assert sel.clusters_without_eligible_canonical or not sel.selections
