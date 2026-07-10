"""WP-C5 producer-truth: a REAL bootstrap of a 2-package @scope monorepo must
CAPTURE the cross-package edge and WRITE it to the plugin-data cross index.

This is the anti-inert-seam gate (the check sibling item #8 lacked): it fails if
any producer link is dead -- the real TS extractor's import rows, the per-ws
capture, the in-memory carry through BootstrapReport, the coordinator JOIN, or
the plugin-data write. The index lives in the plugin data dir (not a repo
.chameleon) so the common pure-coordinator monorepo -- which has no root profile
-- is covered without materializing a new trust anchor (a security review moved
it here). Needs node + typescript, so it skips when they are absent.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import chameleon_mcp.bootstrap.orchestrator as o

_TS_NODE_MODULES = (
    Path(__file__).resolve().parents[2] / "plugin" / "mcp" / "node_modules" / "typescript"
)
_HAVE_TS = shutil.which("node") is not None and _TS_NODE_MODULES.is_dir()

pytestmark = pytest.mark.skipif(
    not _HAVE_TS, reason="needs node + typescript for a real TS bootstrap"
)


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.delenv("CHAMELEON_CROSSWS_INDEX", raising=False)
    yield


def _w(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _build_monorepo(root: Path) -> None:
    # Root carries its own source so it bootstraps to `success` and _amend runs
    # (v1 scope = root-has-profile monorepos), plus a package-glob workspace list.
    _w(root, "package.json", json.dumps({"name": "mono", "workspaces": ["packages/*"]}))
    # Rich, patterned root source so the root itself bootstraps to `success`
    # (v1 covers root-has-profile monorepos; pure-coordinator roots are a
    # documented follow-up). Two clear archetypes: services and models.
    for i in range(8):
        _w(
            root,
            f"src/services/service{i}.ts",
            f"export class Service{i} {{\n  run() {{ return {i}; }}\n}}\n",
        )
    for i in range(8):
        _w(
            root,
            f"src/models/model{i}.ts",
            f"export interface Model{i} {{ id: number; }}\nexport const default{i} = {{ id: {i} }};\n",
        )
    # package A exports foo (resolved via package.json main).
    _w(root, "packages/a/package.json", json.dumps({"name": "@scope/a", "main": "index.ts"}))
    _w(root, "packages/a/index.ts", "export function foo() { return 1; }\nexport const bar = 2;\n")
    # package B imports foo from @scope/a -- the cross-workspace edge.
    _w(root, "packages/b/package.json", json.dumps({"name": "@scope/b"}))
    _w(root, "packages/b/b.ts", "import { foo } from '@scope/a';\nexport const usesFoo = foo();\n")


def _ws(report, path_suffix):
    return next(
        (
            w
            for w in report.workspace_reports
            if str(w.get("workspace_path", "")).endswith(path_suffix)
        ),
        None,
    )


def test_producer_seam_is_live_end_to_end(tmp_path):
    # The anti-inert-seam gate (the check sibling item #8 lacked): a REAL bootstrap
    # must CAPTURE the cross-package candidate and CARRY it -- through the real TS
    # extractor, BootstrapReport, and the workspace-report merge -- so the
    # producer half is provably not dead. (The coordinator JOIN that consumes
    # these and writes the artifact needs a root profile to host it; the common
    # pure-coordinator case is covered by test_pure_coordinator_gap below.)
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_monorepo(repo)
    report = o.bootstrap_repo(repo)

    wb = _ws(report, "packages/b")
    assert wb is not None and wb["status"] == "success"
    assert wb["package_name"] == "@scope/b"
    assert wb["ws_mono_rel"] == "packages/b"
    cands = wb["cross_candidates"]
    assert any(
        c["module"] == "@scope/a" and c["name"] == "foo" and c["importer"] == "b.ts" for c in cands
    ), f"cross-package candidate not captured by the real bootstrap: {cands}"

    wa = _ws(report, "packages/a")
    assert wa is not None and wa["package_name"] == "@scope/a"


def _cross_index_path(tmp_path, repo):
    # Plugin-data location (a security review moved it OFF the trust-hashed profile
    # surface): ~/.local/share/chameleon/<coordinator repo_id>/cross_reverse_index.json.
    from chameleon_mcp.tools import _compute_repo_id

    return tmp_path / "data" / _compute_repo_id(repo.resolve()) / "cross_reverse_index.json"


def test_pure_coordinator_cross_index_written_to_plugin_data(tmp_path):
    # The common pure-coordinator monorepo (success_workspaces_only, no root
    # profile) now GETS a cross index -- written to plugin-data, so no coordinator
    # profile / trust anchor is materialized. Full end-to-end: capture -> JOIN ->
    # write, on the common shape.
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_monorepo(repo)
    report = o.bootstrap_repo(repo)
    assert report.status == "success_workspaces_only"
    # NOT a repo-resident artifact (no new trust anchor).
    assert not (repo / ".chameleon" / "cross_reverse_index.json").exists()

    cross = _cross_index_path(tmp_path, repo)
    assert cross.is_file(), "cross index not written to plugin data dir"
    data = json.loads(cross.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["packages"].get("@scope/a") == "packages/a"
    tgt = data["targets"].get("packages/a/index.ts")
    assert tgt is not None, f"no cross edge to @scope/a; targets={list(data['targets'])}"
    assert any("packages/b/b.ts" in row.get("path", "") for row in tgt.get("foo", []))


def test_kill_switch_disables_capture_and_write(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_CROSSWS_INDEX", "0")
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_monorepo(repo)
    report = o.bootstrap_repo(repo)
    wb = _ws(report, "packages/b")
    assert wb is not None
    assert wb["cross_candidates"] == []  # capture gated off
    assert not _cross_index_path(tmp_path, repo).exists()
