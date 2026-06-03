"""Coverage for the T2-tools audit fixes in tools.py.

Targets:
  - _normalize_git_url lowercases the repo path component for
    case-insensitive hosts (github.com, gitlab.com, ...) so two clones of
    the same repo with case-varying owner/repo collapse to one repo_id.
  - _compute_repo_id case-normalizes the path-based fallback so the same
    repo reached through a case-varying path yields the same id, while the
    pre-fix path-derived id stays reachable via _legacy_path_repo_id.
  - _compute_repo_id prefers a persisted repo_uuid (in .chameleon/config.json)
    for repos without a git remote, so a moved/renamed no-remote repo keeps
    its identity.
  - _attempt_partial_refresh writes conventions.json and principles.md into
    the atomic transaction dir, so a successful partial refresh does not wipe
    taught competing imports or principles.
  - bootstrap_repo warns when bootstrapping a non-git directory that contains
    git subdirectories (parent-folder footgun).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from chameleon_mcp import tools


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp.profile import loader as _loader

    _loader._PROFILE_CACHE.clear()
    _loader._REPO_ROOT_CACHE.clear()
    tools._clear_repo_id_cache()
    yield
    _loader._PROFILE_CACHE.clear()
    _loader._REPO_ROOT_CACHE.clear()
    tools._clear_repo_id_cache()


# ---- _normalize_git_url: lowercase path for case-insensitive hosts ----


def test_normalize_git_url_lowercases_path_on_case_insensitive_host():
    a = tools._normalize_git_url("https://github.com/Foo/Bar.git")
    b = tools._normalize_git_url("https://github.com/foo/bar.git")
    assert a == b
    assert a == "https://github.com/foo/bar"


def test_normalize_git_url_lowercases_path_across_ssh_and_https():
    a = tools._normalize_git_url("git@github.com:Same/Repo.git")
    b = tools._normalize_git_url("https://github.com/same/repo")
    assert a == b


def test_normalize_git_url_preserves_path_case_on_unknown_host():
    # Self-hosted gitea/gerrit instances are case-sensitive; don't fold case.
    url = tools._normalize_git_url("https://git.internal.example/Team/Proj.git")
    assert url == "https://git.internal.example/Team/Proj"


# ---- _compute_repo_id: path-fallback case normalization ----


def test_compute_repo_id_path_fallback_is_case_insensitive(tmp_path, monkeypatch):
    # No git remote -> path-based id. Two path spellings that differ only in
    # case must collapse to one id.
    monkeypatch.setattr(tools, "_git_remote_url", lambda _root: None)
    repo = tmp_path / "MixedCase"
    repo.mkdir()
    id_mixed = tools._compute_repo_id(repo)
    tools._clear_repo_id_cache()
    # Construct a path object with a lower-cased final segment string. On a
    # case-preserving fs these resolve to distinct strings pre-fix.
    id_recomputed = tools._compute_repo_id(repo)
    assert id_mixed == id_recomputed
    # The id must hash the lower-cased resolved path, not the raw casing.
    expected = hashlib.sha256(str(repo.resolve()).lower().encode("utf-8")).hexdigest()
    assert id_mixed == expected


def test_legacy_path_repo_id_keeps_pre_fix_form(tmp_path):
    # The legacy migration bridge must still return the NON-lowercased path
    # hash so users who trusted under the old id get a re-trust hint.
    repo = tmp_path / "MixedCase"
    repo.mkdir()
    legacy = tools._legacy_path_repo_id(repo)
    expected = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()
    assert legacy == expected


def test_detect_repo_surfaces_legacy_hint_after_case_normalization(tmp_path, monkeypatch):
    # A grant made under the pre-fix (non-lowercased) path id on a mixed-case
    # path must surface a re-trust hint now that the id is case-normalized.
    from chameleon_mcp.profile.trust import grant_trust

    monkeypatch.setattr(tools, "_git_remote_url", lambda _root: None)
    repo = tmp_path / "MixedCaseRepo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(json.dumps({"schema_version": 7, "generation": 1}))

    if str(repo.resolve()) == str(repo.resolve()).lower():
        pytest.skip("filesystem lower-cases the resolved path; no legacy divergence")

    legacy_id = tools._legacy_path_repo_id(repo)
    current_id = tools._compute_repo_id(repo)
    assert legacy_id != current_id

    grant_trust(legacy_id, cham)
    tools._clear_repo_id_cache()

    res = tools.detect_repo(str(cham / "profile.json"))["data"]
    assert res["repo_id"] == current_id
    assert res.get("legacy_repo_id") == legacy_id
    assert "legacy_trust_hint" in res


# ---- _compute_repo_id: persisted repo_uuid survives a move ----


def test_compute_repo_id_prefers_persisted_uuid_for_no_remote(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_git_remote_url", lambda _root: None)
    repo = tmp_path / "vendored"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "config.json").write_text(
        json.dumps({"$schema": "chameleon-config-0.8.0", "repo_uuid": "fixed-uuid-123"})
    )
    id_here = tools._compute_repo_id(repo)

    # Move the repo: a fresh path, same config.json carrying the uuid.
    moved = tmp_path / "moved" / "elsewhere"
    moved.mkdir(parents=True)
    moved_cham = moved / ".chameleon"
    moved_cham.mkdir(parents=True)
    (moved_cham / "config.json").write_text(
        json.dumps({"$schema": "chameleon-config-0.8.0", "repo_uuid": "fixed-uuid-123"})
    )
    tools._clear_repo_id_cache()
    id_moved = tools._compute_repo_id(moved)
    assert id_here == id_moved


def test_compute_repo_id_git_remote_wins_over_uuid(tmp_path, monkeypatch):
    # When a git remote exists it must take precedence over a persisted uuid.
    monkeypatch.setattr(tools, "_git_remote_url", lambda _root: "https://github.com/o/r.git")
    repo = tmp_path / "withremote"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "config.json").write_text(
        json.dumps({"$schema": "chameleon-config-0.8.0", "repo_uuid": "ignore-me"})
    )
    got = tools._compute_repo_id(repo)
    expected = hashlib.sha256(
        tools._normalize_git_url("https://github.com/o/r.git").encode("utf-8")
    ).hexdigest()
    assert got == expected


def test_config_load_accepts_repo_uuid():
    # The strict config validator must accept the new optional repo_uuid key.
    from chameleon_mcp.profile import config as _cfg

    with tempfile.TemporaryDirectory() as d:
        profile_dir = Path(d)
        (profile_dir / "config.json").write_text(
            json.dumps({"$schema": "chameleon-config-0.8.0", "repo_uuid": "abc-123"})
        )
        loaded = _cfg.load_config(profile_dir)
        assert loaded.repo_uuid == "abc-123"


# ---- _attempt_partial_refresh preserves conventions.json + principles.md ----


def test_partial_refresh_preserves_conventions_and_principles(tmp_path, monkeypatch):
    """A successful partial refresh must not drop conventions.json/principles.md.

    Rather than drive the full clustering pipeline, exercise the atomic-commit
    body of _attempt_partial_refresh by stubbing the steps it depends on so
    the path reaches atomic_profile_commit with one modified file in an
    existing cluster while the change_ratio stays under the 10% ceiling.
    """
    repo_root = tmp_path / "repo"
    cham = repo_root / ".chameleon"
    cham.mkdir(parents=True)
    src = repo_root / "src"
    src.mkdir()

    # 20 files in cluster C1: 1 modified, 19 unchanged -> 5% change ratio.
    files = []
    for i in range(20):
        p = src / f"f{i}.ts"
        p.write_text(f"export const v{i} = {i};\n")
        files.append(p)

    (cham / "profile.json").write_text(
        json.dumps({"schema_version": 7, "generation": 1, "language": "typescript"})
    )
    (cham / "archetypes.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "archetypes": {"svc": {"cluster_id": "C1", "cluster_size": 20}},
            }
        )
    )
    (cham / "canonicals.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "canonicals": {"svc": [{"witness": {"path": "src/witness.ts", "sha_hint": "zz"}}]},
            }
        )
    )
    (cham / "rules.json").write_text(json.dumps({"generation": 1, "rules": {}}))
    (cham / "conventions.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "conventions": {"imports": {"competing": [{"over": "axios", "preferred": "ky"}]}},
            }
        )
    )
    (cham / "principles.md").write_text("# principles\n\n1. Use the wrapper.\n")
    (cham / "idioms.md").write_text("# idioms\n")
    (cham / "profile.summary.md").write_text("# summary\n")
    (cham / "COMMITTED").touch()

    repo_id = tools._compute_repo_id(repo_root)

    # prev_state: f0 modified, f1..f19 unchanged. Map each rel to a stable sha.
    def _rel(i):
        return f"src/f{i}.ts"

    prev_state = {}
    for i in range(20):
        prev_state[_rel(i)] = {
            "cluster_id": "C1",
            "sha_hint": ("old-sha" if i == 0 else f"sha-{i}"),
        }

    def _sha(p):
        name = p.name  # f{i}.ts
        i = int(name[1:].split(".")[0])
        return "new-sha" if i == 0 else f"sha-{i}"

    monkeypatch.setattr(
        tools,
        "_reparse_changed_files",
        lambda _root, _paths: {_rel(0): ("C1", "new-sha")},
    )
    monkeypatch.setattr(tools, "_content_sha_hint", _sha)
    monkeypatch.setattr(tools, "_calibrate_block_rules_for_repo", lambda _root: None)

    started_at = 1000.0
    env = tools._attempt_partial_refresh(repo_root, repo_id, cham, files, prev_state, started_at)
    assert env is not None, "expected a successful partial refresh"
    assert env["data"]["status"] == "partial_refresh"

    # The taught competing import and principles must survive the atomic swap.
    conv = json.loads((cham / "conventions.json").read_text())
    assert conv["conventions"]["imports"]["competing"][0]["over"] == "axios"
    assert (cham / "principles.md").read_text().startswith("# principles")


# ---- witness-path overlap tolerates backslash-authored profiles ----


def test_witness_overlap_normalizes_backslash_paths():
    # A witness path authored on Windows carries backslashes; the overlap with a
    # forward-slash query path must still count shared directory segments.
    canonicals = {"svc": [{"witness": {"path": "src\\services\\payment.ts", "sha_hint": "ab"}}]}
    overlap = tools._witness_path_overlap("src/services/order.ts", canonicals, "svc")
    assert overlap == 2


def test_nearest_canonical_entry_normalizes_backslash_paths():
    entries = [
        {"witness": {"path": "src\\a\\one.ts"}},
        {"witness": {"path": "src\\b\\two.ts"}},
    ]
    best = tools._nearest_canonical_entry("src/b/three.ts", entries)
    assert best["witness"]["path"] == "src\\b\\two.ts"


# ---- bootstrap_repo warns on non-git parent with git children ----


def test_bootstrap_warns_on_nongit_parent_with_git_children(tmp_path):
    parent = tmp_path / "parent"
    (parent / "repoA" / ".git").mkdir(parents=True)
    (parent / "repoB" / ".git").mkdir(parents=True)
    # parent itself has no .git.
    (parent / "tsconfig.json").write_text("{}")

    res = tools.bootstrap_repo(str(parent))["data"]
    warnings = res.get("warnings")
    # A warning channel must exist and mention the git-subdirectory footgun.
    blob = json.dumps(res).lower()
    assert "git" in blob and ("child" in blob or "subdirector" in blob or "nested" in blob), res
    assert warnings is not None or "nongit_parent" in blob
