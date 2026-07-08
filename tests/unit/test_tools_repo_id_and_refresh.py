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

from chameleon_mcp import repo_id, tools


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
    # No git remote -> path-based id. On a case-insensitive filesystem, two path
    # spellings that differ only in case must collapse to one id. Force the
    # case-insensitive branch so the assertion holds on a case-sensitive CI host.
    monkeypatch.setattr(repo_id, "_git_remote_url", lambda _root: None)
    monkeypatch.setattr(repo_id, "_fs_is_case_insensitive", lambda _p: True)
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

    monkeypatch.setattr(repo_id, "_git_remote_url", lambda _root: None)
    # Force the case-insensitive branch so the case-normalized id diverges from
    # the legacy (non-lowercased) id on a case-sensitive CI host too.
    monkeypatch.setattr(repo_id, "_fs_is_case_insensitive", lambda _p: True)
    repo = tmp_path / "MixedCaseRepo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(json.dumps({"schema_version": 7, "generation": 1}))

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
    monkeypatch.setattr(repo_id, "_git_remote_url", lambda _root: None)
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
    monkeypatch.setattr(repo_id, "_git_remote_url", lambda _root: "https://github.com/o/r.git")
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


def test_partial_refresh_preserves_calls_index(tmp_path, monkeypatch):
    """A successful partial refresh must carry calls_index.json forward.

    calls_index.json is a protocol file, so the atomic commit does not copy
    it forward on its own; without an explicit re-emit every partial refresh
    silently wiped the judge's caller facts (and the next refresh then forced
    a full rebuild to heal the hole).
    """
    repo_root = tmp_path / "repo"
    cham = repo_root / ".chameleon"
    cham.mkdir(parents=True)
    src = repo_root / "src"
    src.mkdir()

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
    (cham / "conventions.json").write_text(json.dumps({"generation": 1, "conventions": {}}))
    (cham / "principles.md").write_text("# principles\n")
    (cham / "idioms.md").write_text("# idioms\n")
    (cham / "profile.summary.md").write_text("# summary\n")
    calls_payload = {
        "schema_version": 1,
        "callees": {
            "src/f1.ts": {
                "v1": {
                    "callers": [
                        {"path": "src/f2.ts", "caller": "<module>", "line": 1, "grade": "import"}
                    ],
                    "total": 1,
                    "truncated": False,
                }
            }
        },
    }
    (cham / "calls_index.json").write_text(json.dumps(calls_payload))
    (cham / "COMMITTED").touch()

    repo_id = tools._compute_repo_id(repo_root)

    def _rel(i):
        return f"src/f{i}.ts"

    prev_state = {}
    for i in range(20):
        prev_state[_rel(i)] = {
            "cluster_id": "C1",
            "sha_hint": ("old-sha" if i == 0 else f"sha-{i}"),
        }

    def _sha(p):
        i = int(p.name[1:].split(".")[0])
        return "new-sha" if i == 0 else f"sha-{i}"

    monkeypatch.setattr(
        tools,
        "_reparse_changed_files",
        lambda _root, _paths: {_rel(0): ("C1", "new-sha")},
    )
    monkeypatch.setattr(tools, "_content_sha_hint", _sha)
    monkeypatch.setattr(tools, "_calibrate_block_rules_for_repo", lambda _root: None)

    env = tools._attempt_partial_refresh(repo_root, repo_id, cham, files, prev_state, 1000.0)
    assert env is not None, "expected a successful partial refresh"
    assert env["data"]["status"] == "partial_refresh"

    assert (cham / "calls_index.json").is_file(), "partial refresh dropped calls_index.json"
    assert json.loads((cham / "calls_index.json").read_text()) == calls_payload


def test_partial_refresh_preserves_counterexamples_and_symbol_signatures(tmp_path, monkeypatch):
    """A successful partial refresh must carry counterexamples.json and
    symbol_signatures.json forward.

    Both are protocol files, so the atomic commit does not copy them on its own;
    without an explicit re-emit a partial refresh (the default for a small change)
    silently wiped the taught off-pattern counterexamples and the symbol index.
    """
    repo_root = tmp_path / "repo"
    cham = repo_root / ".chameleon"
    cham.mkdir(parents=True)
    src = repo_root / "src"
    src.mkdir()

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
            {"generation": 1, "archetypes": {"svc": {"cluster_id": "C1", "cluster_size": 20}}}
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
    (cham / "conventions.json").write_text(json.dumps({"generation": 1, "conventions": {}}))
    (cham / "principles.md").write_text("# principles\n")
    (cham / "idioms.md").write_text("# idioms\n")
    (cham / "profile.summary.md").write_text("# summary\n")
    ce_payload = {
        "schema_version": 1,
        "archetypes": {
            "svc": {
                "rule": "import-preference-violation",
                "over": "axios",
                "snippet": "import x from 'axios';",
            }
        },
    }
    (cham / "counterexamples.json").write_text(json.dumps(ce_payload))
    ss_payload = {"schema_version": 1, "files": {}}
    (cham / "symbol_signatures.json").write_text(json.dumps(ss_payload))
    (cham / "COMMITTED").touch()

    repo_id = tools._compute_repo_id(repo_root)

    prev_state = {}
    for i in range(20):
        prev_state[f"src/f{i}.ts"] = {
            "cluster_id": "C1",
            "sha_hint": ("old-sha" if i == 0 else f"sha-{i}"),
        }

    def _sha(p):
        i = int(p.name[1:].split(".")[0])
        return "new-sha" if i == 0 else f"sha-{i}"

    monkeypatch.setattr(
        tools, "_reparse_changed_files", lambda _root, _paths: {"src/f0.ts": ("C1", "new-sha")}
    )
    monkeypatch.setattr(tools, "_content_sha_hint", _sha)
    monkeypatch.setattr(tools, "_calibrate_block_rules_for_repo", lambda _root: None)

    env = tools._attempt_partial_refresh(repo_root, repo_id, cham, files, prev_state, 1000.0)
    assert env is not None and env["data"]["status"] == "partial_refresh"

    assert (cham / "counterexamples.json").is_file(), "partial refresh dropped counterexamples.json"
    assert json.loads((cham / "counterexamples.json").read_text()) == ce_payload
    assert (cham / "symbol_signatures.json").is_file(), (
        "partial refresh dropped symbol_signatures.json"
    )
    assert json.loads((cham / "symbol_signatures.json").read_text()) == ss_payload


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


def test_repo_id_path_case_folding_only_on_case_insensitive_fs(monkeypatch, tmp_path):
    """Path fallback lowercases only on case-insensitive FS; distinct repos on a
    case-sensitive FS (Linux) keep separate ids."""
    from chameleon_mcp import tools

    foo = tmp_path / "RepoFoo"
    foofold = tmp_path / "repofoo"

    monkeypatch.setattr(repo_id, "_fs_is_case_insensitive", lambda p: True)
    tools._REPO_ID_CACHE.clear()
    id1 = tools._compute_repo_id(foo)
    tools._REPO_ID_CACHE.clear()
    id2 = tools._compute_repo_id(foofold)
    assert id1 == id2  # case-insensitive -> same id

    monkeypatch.setattr(repo_id, "_fs_is_case_insensitive", lambda p: False)
    tools._REPO_ID_CACHE.clear()
    id3 = tools._compute_repo_id(foo)
    tools._REPO_ID_CACHE.clear()
    id4 = tools._compute_repo_id(foofold)
    assert id3 != id4  # case-sensitive -> distinct ids


def test_fs_case_insensitive_probe_returns_bool(tmp_path):
    from chameleon_mcp.tools import _fs_is_case_insensitive

    d = tmp_path / "Probe"
    d.mkdir()
    assert isinstance(_fs_is_case_insensitive(d), bool)
    assert _fs_is_case_insensitive(tmp_path / "does-not-exist") is False


# ---- _git_remote_url: warn on timeout, stay fail-open ----


def test_git_remote_url_warns_on_timeout(tmp_path, monkeypatch, capsys):
    """A git config lookup that times out must log a one-line stderr warning so
    the user can see why repo_id fell back to the path, then return None."""
    import subprocess

    def _boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2)

    monkeypatch.setattr(tools.subprocess, "run", _boom)
    repo = tmp_path / "slowrepo"
    repo.mkdir()

    assert tools._git_remote_url(repo) is None

    err = capsys.readouterr().err
    assert "timed out" in err.lower()
    assert "path-based" in err.lower()
    # Single line, no traceback.
    assert err.count("\n") == 1


def test_git_remote_url_silent_on_non_timeout_errors(tmp_path, monkeypatch, capsys):
    """A missing-git / OSError path must stay quiet (no remote is normal); only a
    timeout warrants a warning."""

    def _boom(*_a, **_k):
        raise FileNotFoundError("git not installed")

    monkeypatch.setattr(tools.subprocess, "run", _boom)
    repo = tmp_path / "nogit"
    repo.mkdir()

    assert tools._git_remote_url(repo) is None
    assert capsys.readouterr().err == ""


def test_git_remote_url_timeout_does_not_change_repo_id(tmp_path, monkeypatch):
    """The timeout warning must not alter the fallback identity: a timed-out
    remote lookup still yields the path-based repo_id."""
    import subprocess

    def _boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2)

    monkeypatch.setattr(tools.subprocess, "run", _boom)
    monkeypatch.setattr(repo_id, "_persisted_repo_uuid", lambda _root: None)
    repo = tmp_path / "TimeoutRepo"
    repo.mkdir()

    got = tools._compute_repo_id(repo)
    path_key = (
        str(repo.resolve()).lower() if tools._fs_is_case_insensitive(repo) else str(repo.resolve())
    )
    expected = hashlib.sha256(path_key.encode("utf-8")).hexdigest()
    assert got == expected


# ---- refresh re-baselines the observed-drift window -----------------------


def test_refresh_force_resets_observed_drift(tmp_path):
    import subprocess
    import time

    from chameleon_mcp.drift import observations as obs

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    for i in range(6):
        (repo / "src" / f"comp{i}.ts").write_text(
            f"export const Comp{i} = () => {{ return {i}; }};\n", encoding="utf-8"
        )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    assert tools.bootstrap_repo(str(repo))["data"]["status"] == "success"

    # Key observations on the same id get_drift_status resolves the path to.
    _path, repo_id = tools._resolve_repo_arg(str(repo))
    now = int(time.time())
    for i in range(40):
        obs.record_edit_observation(
            repo_id, f"f{i}.ts", "component", "low", matched_canonical=False, observed_at=now
        )

    before = tools.get_drift_status(str(repo))["data"]["observed_drift_score"]
    assert before is not None and before > 0.5

    assert tools.refresh_repo(str(repo), force=True)["data"]["status"] == "success"

    after = tools.get_drift_status(str(repo))["data"]["observed_drift_score"]
    assert after is None or after <= 0.5


def test_refresh_on_never_bootstrapped_repo_tags_implicit_bootstrap(tmp_path, monkeypatch):
    # finding #5: refresh on a repo with no prior profile implicitly bootstraps
    # (it does NOT refuse -- that is the idempotent-refresh design) and tags the
    # envelope so the caller can distinguish an INITIAL bootstrap from a
    # re-derive. status stays "success" because the write is complete and correct.
    repo = tmp_path / "fresh-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    assert not (repo / ".chameleon").exists()

    captured = {}

    def _fake_bootstrap(path, *, force, paths_glob=None, analysis_root=None):
        captured["force"] = force
        return tools._envelope({"status": "success", "files_indexed": 1})

    monkeypatch.setattr(tools, "bootstrap_repo", _fake_bootstrap)
    env = tools.refresh_repo(str(repo))
    if isinstance(env, str):
        env = json.loads(env)
    data = env.get("data", env)

    assert captured.get("force") is True  # the never-bootstrapped path bootstraps
    assert data.get("status") == "success"
    assert data.get("implicit_bootstrap") is True


def test_refresh_explicit_force_is_not_tagged_implicit(tmp_path, monkeypatch):
    # An explicit re-derive of an EXISTING profile is not an implicit bootstrap,
    # so it must not carry the flag.
    repo = tmp_path / "existing-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")

    def _fake_bootstrap(path, *, force, paths_glob=None, analysis_root=None):
        return tools._envelope({"status": "success", "files_indexed": 1})

    monkeypatch.setattr(tools, "bootstrap_repo", _fake_bootstrap)
    env = tools.refresh_repo(str(repo), force=True)
    if isinstance(env, str):
        env = json.loads(env)
    data = env.get("data", env)
    assert "implicit_bootstrap" not in data
