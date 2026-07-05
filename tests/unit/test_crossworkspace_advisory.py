"""WP-C5 Step 8: the Stop cross-workspace existence advisory (consumer).

Drives ``_crossworkspace_existence_advisory_lines`` directly with a crafted
monorepo: a workspace file that removed an export, a coordinator cross index in
the plugin data dir, and a sibling-workspace importer still referencing the
name. No bootstrap needed -- the consumer reads the on-disk plugin-data index +
workspace profile.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from chameleon_mcp.enforcement import EnforcementState, FileState
from chameleon_mcp.hook_helper import (
    _crossworkspace_existence_advisory_lines,
    _resolve_coordinator_cross_index,
)
from chameleon_mcp.symbol_index import CROSSWS_SCHEMA_VERSION

COORD = "coordid"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    # The fixture repo lives under a temp dir; allow find_repo_root to accept it.
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.delenv("CHAMELEON_CROSSWS_INDEX", raising=False)
    yield


def _w(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _build(
    tmp_path, *, edited_exports="export const bar = 1;\n", importer_body=None, packages=None
):
    repo = tmp_path / "repo"
    # workspace A: profile with a parent (coordinator) back-reference + the edited file
    _w(
        repo,
        "packages/a/.chameleon/profile.json",
        json.dumps({"workspace": {"parent": {"repo_id": COORD, "workspace_path": "packages/a"}}}),
    )
    edited = _w(repo, "packages/a/index.ts", edited_exports)  # 'foo' removed (only bar left)
    # workspace B: the sibling importer that still uses foo
    _w(
        repo,
        "packages/b/b.ts",
        importer_body if importer_body is not None else "import { foo } from '@scope/a';\nfoo();\n",
    )
    # coordinator cross index in plugin-data
    idx = tmp_path / "data" / COORD / "cross_reverse_index.json"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(
        json.dumps(
            {
                "schema_version": CROSSWS_SCHEMA_VERSION,
                "targets": {
                    "packages/a/index.ts": {"foo": [{"path": "packages/b/b.ts", "line": 1}]}
                },
                "packages": packages or {"@scope/a": "packages/a"},
            }
        ),
        encoding="utf-8",
    )
    return repo, edited


def _state(edited):
    st = EnforcementState()
    st.files[str(edited)] = FileState()
    return st


def _cfg(mode="enforce"):
    return SimpleNamespace(mode=mode)


def test_flags_cross_workspace_break(tmp_path):
    repo, edited = _build(tmp_path)
    lines = _crossworkspace_existence_advisory_lines(
        repo_root=repo, state=_state(edited), cfg=_cfg()
    )
    text = "\n".join(lines)
    assert "foo" in text
    assert "packages/a/index.ts" in text
    assert "packages/b/b.ts:1" in text
    assert "another workspace" in text.lower()


def test_still_exported_no_break(tmp_path):
    repo, edited = _build(tmp_path, edited_exports="export function foo() {}\n")
    assert (
        _crossworkspace_existence_advisory_lines(repo_root=repo, state=_state(edited), cfg=_cfg())
        == []
    )


def test_importer_no_longer_references_name_no_break(tmp_path):
    # B dropped the use of foo -> live re-verify suppresses (no stale-index fire).
    repo, edited = _build(tmp_path, importer_body="export const unrelated = 1;\n")
    assert (
        _crossworkspace_existence_advisory_lines(repo_root=repo, state=_state(edited), cfg=_cfg())
        == []
    )


def test_same_turn_repoint_to_other_known_package_suppressed(tmp_path):
    # B repointed foo to a DIFFERENT KNOWN workspace package (@scope/c is in the
    # packages map) -> the removal from @scope/a no longer affects B -> suppress.
    repo, edited = _build(
        tmp_path,
        importer_body="import { foo } from '@scope/c';\nfoo();\n",
        packages={"@scope/a": "packages/a", "@scope/c": "packages/c"},
    )
    assert (
        _crossworkspace_existence_advisory_lines(repo_root=repo, state=_state(edited), cfg=_cfg())
        == []
    )


def test_repoint_to_relative_into_owning_still_breaks(tmp_path):
    # NEVER-MISS: B still imports foo via a RELATIVE path that targets package a
    # (the owning package). The bareword is present and the spec is not another
    # known package, so the genuine break must STILL fire -- a name-prefix-only
    # suppression would wrongly drop this.
    repo, edited = _build(tmp_path, importer_body="import { foo } from '../a/index';\nfoo();\n")
    lines = _crossworkspace_existence_advisory_lines(
        repo_root=repo, state=_state(edited), cfg=_cfg()
    )
    assert "foo" in "\n".join(lines)


def test_repoint_to_external_unmapped_keeps_advisory(tmp_path):
    # A repoint to an UNMAPPED bare package (external npm dep) is ambiguous, so the
    # advisory is KEPT (tolerable noise beats a miss). Safe direction.
    repo, edited = _build(tmp_path, importer_body="import { foo } from 'lodash';\nfoo();\n")
    lines = _crossworkspace_existence_advisory_lines(
        repo_root=repo, state=_state(edited), cfg=_cfg()
    )
    assert "foo" in "\n".join(lines)


def test_still_imports_from_target_package_still_breaks(tmp_path):
    # Control: B still imports foo from @scope/a -> genuine break still fires.
    repo, edited = _build(tmp_path)  # default importer imports from '@scope/a'
    lines = _crossworkspace_existence_advisory_lines(
        repo_root=repo, state=_state(edited), cfg=_cfg()
    )
    assert "foo" in "\n".join(lines)


def test_off_mode_and_kill_switch(tmp_path, monkeypatch):
    repo, edited = _build(tmp_path)
    assert (
        _crossworkspace_existence_advisory_lines(
            repo_root=repo, state=_state(edited), cfg=_cfg(mode="off")
        )
        == []
    )
    monkeypatch.setenv("CHAMELEON_CROSSWS_INDEX", "0")
    assert (
        _crossworkspace_existence_advisory_lines(repo_root=repo, state=_state(edited), cfg=_cfg())
        == []
    )


def test_no_cross_index_fails_open(tmp_path):
    repo, edited = _build(tmp_path)
    (tmp_path / "data" / COORD / "cross_reverse_index.json").unlink()
    assert (
        _crossworkspace_existence_advisory_lines(repo_root=repo, state=_state(edited), cfg=_cfg())
        == []
    )


def test_resolve_coordinator_reads_parent(tmp_path):
    repo, _edited = _build(tmp_path)
    mono_root, res = _resolve_coordinator_cross_index(repo / "packages/a")
    assert mono_root == repo
    assert res is not None
    ri, packages = res
    assert packages == {"@scope/a": "packages/a"}


def test_no_parent_no_coordinator(tmp_path):
    # A single-repo workspace with no parent back-reference -> no cross index.
    _w(tmp_path / "solo", ".chameleon/profile.json", json.dumps({"workspace": {}}))
    mono_root, res = _resolve_coordinator_cross_index(tmp_path / "solo")
    assert mono_root is None and res is None
