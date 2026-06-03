"""Fixes from the 10-tester QA run that live in tools.py.

- T6: a lone-surrogate file_path must be rejected by _validate_file_path_arg so
  read tools fail open instead of raising UnicodeEncodeError in find_repo_root.
- #4: merge_profiles must fail open (clean envelope) on a top-level non-dict JSON
  side, not raise AttributeError. The 2.1.2 guard only covered the nested
  archetypes:[array] shape.
- #5: detect_repo must respect the COMMITTED sentinel so it agrees with the read
  path; a present-but-uncommitted profile is not profile_present/trusted.
- T11: get_drift_status must fail open on an over-NAME_MAX opaque repo_id.
- T12: get_drift_status on a repo with no profile must recommend /chameleon-init,
  not /chameleon-trust.
"""

from __future__ import annotations

import json

import pytest

from chameleon_mcp import tools


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    tools._clear_repo_id_cache()
    yield
    tools._clear_repo_id_cache()


# ---- T6: surrogate file_path -------------------------------------------------


def test_validate_file_path_rejects_lone_surrogate():
    assert tools._validate_file_path_arg("/repo/sr\ud800c/x.ts") is False


def test_get_pattern_context_fails_open_on_surrogate_path():
    # Must not raise UnicodeEncodeError; returns a clean envelope.
    result = tools.get_pattern_context("/repo/sr\ud800c/x.ts")
    assert isinstance(result, dict) and "data" in result


# ---- #4: merge_profiles top-level non-dict ----------------------------------


def test_merge_profiles_fails_open_on_top_level_array(tmp_path):
    base = tmp_path / "base.json"
    ours = tmp_path / "ours.json"
    theirs = tmp_path / "theirs.json"
    base.write_text(json.dumps({"schema_version": 2, "repo_id": "r", "generation": 1}))
    ours.write_text(json.dumps([]))  # top-level array, not an object
    theirs.write_text(json.dumps({"schema_version": 2, "repo_id": "r", "generation": 2}))

    result = tools.merge_profiles(repo="", base=str(base), ours=str(ours), theirs=str(theirs))
    assert result["data"]["status"] == "failed"


# ---- #5: detect_repo respects COMMITTED -------------------------------------


def test_detect_repo_uncommitted_profile_is_not_present(tmp_path):
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(
        json.dumps({"schema_version": 8, "repo_id": "x", "language": "typescript", "generation": 1})
    )
    # No COMMITTED sentinel: the read path cannot load this, so detect_repo must
    # not call it profile_present/trusted.
    (repo / "src").mkdir()
    sample = repo / "src" / "x.ts"
    sample.write_text("export const x = 1;\n")

    status = tools.detect_repo(str(sample))["data"]["profile_status"]
    assert status != "profile_present"


# ---- T11: get_drift_status NAME_MAX -----------------------------------------


def test_get_drift_status_fails_open_on_overlong_repo_id():
    long_id = "z" * 300  # single component over NAME_MAX (255)
    result = tools.get_drift_status(long_id)
    assert isinstance(result, dict) and "data" in result


# ---- T12: get_drift_status no-profile message -------------------------------


def test_get_drift_status_no_profile_recommends_init(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "x.ts").write_text("export const x = 1;\n")
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    rec = tools.get_drift_status(str(repo))["data"]["recommended_action"]
    assert "chameleon-init" in rec


# ---- #2: refresh must not orphan trust on a remote-less, config-less repo ----


def _tiny_ts_repo(repo, n=6):
    (repo / "src").mkdir(parents=True)
    for i in range(n):
        (repo / "src" / f"c{i}.ts").write_text(f"export const C{i} = () => {i};\n")
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)


def test_refresh_preserves_trust_when_repo_id_flips(tmp_path):
    repo = tmp_path / "repo"
    _tiny_ts_repo(repo)
    assert tools.bootstrap_repo(str(repo))["data"]["status"] == "success"
    # Simulate a profile committed WITHOUT config.json: the repo then resolves by
    # path hash, and the next refresh persists a repo_uuid that flips the id.
    cfg = repo / ".chameleon" / "config.json"
    if cfg.exists():
        cfg.unlink()
    tools._clear_repo_id_cache()
    tools.trust_profile(str(repo), repo.name)

    sample = str(repo / "src" / "c0.ts")
    assert tools.detect_repo(sample)["data"]["trust_state"] == "trusted"

    tools.refresh_repo(str(repo), force=True)
    tools._clear_repo_id_cache()

    # The id flipped path->uuid; trust must follow to the current id, not orphan.
    assert tools.detect_repo(sample)["data"]["trust_state"] == "trusted"


# ---- #12: apply_archetype_renames rewrites idioms.md archetype references ----


def test_apply_renames_rewrites_idioms_archetype(tmp_path):
    repo = tmp_path / "repo"
    _tiny_ts_repo(repo, n=8)
    assert tools.bootstrap_repo(str(repo))["data"]["status"] == "success"
    archs = json.loads((repo / ".chameleon" / "archetypes.json").read_text())["archetypes"]
    name = sorted(archs)[0]
    tools.trust_profile(str(repo), repo.name)

    tools.teach_profile_structured(
        str(repo), slug="my-idiom", rationale="always wrap the thing", archetype=name
    )
    idioms = (repo / ".chameleon" / "idioms.md").read_text()
    assert f"Archetype: {name}" in idioms

    new_name = name + "-renamed"
    tools.apply_archetype_renames(str(repo), {name: new_name})
    idioms2 = (repo / ".chameleon" / "idioms.md").read_text()
    assert f"Archetype: {new_name}" in idioms2
    assert not __import__("re").search(rf"(?m)^Archetype: {name}$", idioms2)


# ---- T14-B: a user-initiated teach must not stale the user's own trust -------


def test_teach_preserves_trust(tmp_path):
    repo = tmp_path / "repo"
    _tiny_ts_repo(repo, n=8)
    assert tools.bootstrap_repo(str(repo))["data"]["status"] == "success"
    tools.trust_profile(str(repo), repo.name)
    sample = str(repo / "src" / "c0.ts")
    assert tools.detect_repo(sample)["data"]["trust_state"] == "trusted"

    tools.teach_profile_structured(str(repo), slug="keep-trust", rationale="wrap the thing")
    tools._clear_repo_id_cache()

    # idioms.md is a hashed artifact; the user's own teach must keep trust.
    assert tools.detect_repo(sample)["data"]["trust_state"] == "trusted"


# ---- #7 follow-up: public refresh/bootstrap must fail OPEN on a read-only repo


def test_refresh_and_bootstrap_fail_open_on_readonly_repo(tmp_path):
    import stat

    repo = tmp_path / "repo"
    _tiny_ts_repo(repo)
    assert tools.bootstrap_repo(str(repo))["data"]["status"] == "success"
    # Revoke write on the repo root so the next atomic commit's mkdir fails.
    repo.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        # Must RETURN a clean failed envelope, never raise ProfileCommitError.
        assert tools.refresh_repo(str(repo), force=True)["data"]["status"] == "failed"
        assert tools.bootstrap_repo(str(repo), force=True)["data"]["status"] == "failed"
    finally:
        repo.chmod(stat.S_IRWXU)
