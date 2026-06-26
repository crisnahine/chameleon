"""A comment-only file must not be chosen as a canonical witness.

select_canonicals already deprioritizes empty / whitespace-only files. But a
file that is ALL comments (license header, TODO block) has non-whitespace
content yet no code to mirror -- its re-extracted signature
(top_level_node_kinds) is empty. Such a file taught nothing as the per-edit
"imitate this" exemplar. It must be excluded from the witness pool too, while
files with real structure -- including thin barrel re-exports, whose signature
is a non-empty export/import node set -- stay eligible.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.bootstrap.canonical import select_canonicals
from chameleon_mcp.bootstrap.clustering import cluster_files
from chameleon_mcp.extractors._base import ParsedFile


def _pf(path: Path, kinds: tuple[str, ...] = ("ClassNode",)) -> ParsedFile:
    return ParsedFile(
        path=path,
        content_first_200_bytes="",
        top_level_node_kinds=kinds,
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


def test_comment_only_loses_witness_to_real_sibling(tmp_path):
    repo = tmp_path / "repo"
    # a_comment sorts before z_real, so without the empty-signature exclusion the
    # comment-only file would win the path tie-break and be injected as the
    # "imitate this" exemplar. Its content is non-whitespace (so the existing
    # not-content.strip() guard does NOT catch it) but it has no code structure.
    comment = _write(
        repo,
        "app/services/a_comment.rb",
        "# Copyright 2026\n# This file intentionally left as a header.\n# TODO: implement\n",
    )
    real = _write(repo, "app/services/z_real.rb", "class A\n  def call; end\nend\n")
    result = cluster_files([_pf(comment), _pf(real)], repo, min_cluster_size=2)
    sel = select_canonicals(result.clusters, repo)
    assert sel.selections, "expected a witness"
    witnesses = {s.witness_path.name for s in sel.selections.values()}
    assert "z_real.rb" in witnesses
    assert "a_comment.rb" not in witnesses


def test_barrel_reexport_still_eligible_as_witness(tmp_path):
    # Landmine guard: the exclusion is signature-empty, NOT content-trivial, so a
    # barrel/re-export file (real export nodes => non-empty signature) must stay
    # eligible. A cluster of barrels still yields a witness.
    repo = tmp_path / "repo"
    a = _write(
        repo, "src/features/a/index.ts", "export { A } from './a';\nexport { B } from './b';\n"
    )
    b = _write(
        repo, "src/features/b/index.ts", "export { C } from './c';\nexport { D } from './d';\n"
    )
    pfs = [
        _pf(a, kinds=("ExportDeclaration",)),
        _pf(b, kinds=("ExportDeclaration",)),
    ]
    result = cluster_files(pfs, repo, min_cluster_size=2)
    sel = select_canonicals(result.clusters, repo)
    assert sel.selections, "a barrel cluster must still yield a witness"
    witnesses = {s.witness_path.name for s in sel.selections.values()}
    assert "index.ts" in witnesses
