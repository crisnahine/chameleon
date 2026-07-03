"""doctor must detect dead advisory loops, not just broken plumbing.

Three real installs reported all-healthy while chameleon did nothing:
a profile missing calls_index.json with a dead advisory loop, a session
whose every correctness-judge spawn failed, and a trusted profile whose
preflight rows never resolved an archetype. Each gets a doctor check:

  - ``profile_artifacts``: the cwd profile's generated artifacts exist and
    parse (calls_index.json + function_catalog.json always; exports/reverse
    for TypeScript) — missing/corrupt warns naming the file and prescribing
    /chameleon-refresh.
  - ``judge_spawn_health``: the recent session attestations (last 5) show
    only degraded correctness-judge spawns and no completion — warn.
  - ``advisory_emission``: the recent trusted preflight metric rows for this
    repo are all archetype-null — warn (advisories are not firing).

Each check fails OPEN: absence of a profile / attestations / metrics is ok,
never a crash.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from chameleon_mcp import index_db, tools


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    index_db.close_index_connections()
    tools._clear_repo_id_cache()
    from chameleon_mcp.profile import loader as _loader

    _loader._REPO_ROOT_CACHE.clear()
    yield
    index_db.close_index_connections()
    tools._clear_repo_id_cache()
    _loader._REPO_ROOT_CACHE.clear()


_ALL_TS_ARTIFACTS = (
    "calls_index.json",
    "function_catalog.json",
    "exports_index.json",
    "reverse_index.json",
)

# The core generated artifacts every real profile writes for every language (an
# empty archetypes==0 bootstrap still writes all of these); doctor requires them,
# so a healthy fixture must carry them or it reads as a dead install.
_CORE_ARTIFACTS = (
    "archetypes.json",
    "canonicals.json",
    "conventions.json",
    "rules.json",
    "enforcement.json",
)


def _make_profile(repo: Path, *, language: str = "typescript", artifacts=_ALL_TS_ARTIFACTS):
    cham = repo / ".chameleon"
    cham.mkdir(parents=True, exist_ok=True)
    (cham / "COMMITTED").write_text("committed-at=1.0\npid=1\n", encoding="utf-8")
    (cham / "profile.json").write_text(
        json.dumps({"schema_version": 8, "generation": 1, "language": language}),
        encoding="utf-8",
    )
    for name in (*_CORE_ARTIFACTS, *artifacts):
        (cham / name).write_text("{}", encoding="utf-8")
    return cham


def _check(name: str) -> dict:
    checks = tools.doctor()["data"]["checks"]
    return next(c for c in checks if c["name"] == name)


# --------------------------------------------------------------------------
# profile_artifacts


class TestProfileArtifacts:
    def test_missing_calls_index_warns_naming_file(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_profile(
            repo,
            artifacts=("function_catalog.json", "exports_index.json", "reverse_index.json"),
        )
        monkeypatch.chdir(repo)
        c = _check("profile_artifacts")
        assert c["status"] == "warn"
        assert "calls_index.json" in str(c["detail"])
        assert "/chameleon-refresh" in str(c["detail"])

    def test_corrupt_artifact_warns_naming_file(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        cham = _make_profile(repo)
        (cham / "function_catalog.json").write_text("{garbage", encoding="utf-8")
        monkeypatch.chdir(repo)
        c = _check("profile_artifacts")
        assert c["status"] == "warn"
        assert "function_catalog.json" in str(c["detail"])

    def test_ts_profile_missing_exports_index_warns(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_profile(
            repo, artifacts=("calls_index.json", "function_catalog.json", "reverse_index.json")
        )
        monkeypatch.chdir(repo)
        c = _check("profile_artifacts")
        assert c["status"] == "warn"
        assert "exports_index.json" in str(c["detail"])

    def test_ruby_profile_does_not_require_ts_indexes(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_profile(
            repo, language="ruby", artifacts=("calls_index.json", "function_catalog.json")
        )
        monkeypatch.chdir(repo)
        assert _check("profile_artifacts")["status"] == "ok"

    def test_healthy_ts_profile_is_ok(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_profile(repo)
        monkeypatch.chdir(repo)
        assert _check("profile_artifacts")["status"] == "ok"

    def test_no_profile_in_cwd_is_ok(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)
        assert _check("profile_artifacts")["status"] == "ok"


# --------------------------------------------------------------------------
# judge_spawn_health


def _attest(repo_id: str, session_id: str, checks: list[dict]):
    from chameleon_mcp.review_ledger import record_session_attestation

    out = record_session_attestation(repo_id, {"session_id": session_id, "checks": checks})
    assert out["appended"]


_DEGRADED = {
    "check": "correctness_judge",
    "status": "degraded_spawn",
    "reason": "spawn_exec_error",
    "count": 2,
}
_STARTED = {"check": "correctness_judge", "status": "spawned", "reason": "started", "count": 2}
_COMPLETED = {"check": "correctness_judge", "status": "spawned", "reason": "completed", "count": 2}


class TestJudgeSpawnHealth:
    def test_all_degraded_spawns_warn(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_profile(repo)
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        # Every attempt emits a "spawned/started" row; only "spawned/completed"
        # marks success, so started+degraded with no completion is a dead reviewer.
        _attest(repo_id, "s1", [_STARTED, _DEGRADED])
        _attest(repo_id, "s2", [_STARTED, _DEGRADED])
        c = _check("judge_spawn_health")
        assert c["status"] == "warn"
        assert "failing to spawn" in str(c["detail"])
        assert "spawn_exec_error" in str(c["detail"])

    def test_completed_spawn_is_ok(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_profile(repo)
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        _attest(repo_id, "s1", [_STARTED, _DEGRADED])
        _attest(repo_id, "s2", [_STARTED, _COMPLETED])
        assert _check("judge_spawn_health")["status"] == "ok"

    def test_window_caps_at_last_five_records(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_profile(repo)
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        # An old healthy session followed by five all-degraded sessions: the
        # completion has scrolled out of the 5-record window, so doctor warns.
        _attest(repo_id, "s0", [_STARTED, _COMPLETED])
        for i in range(1, 6):
            _attest(repo_id, f"s{i}", [_STARTED, _DEGRADED])
        assert _check("judge_spawn_health")["status"] == "warn"

    def test_no_attestations_is_ok(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_profile(repo)
        monkeypatch.chdir(repo)
        assert _check("judge_spawn_health")["status"] == "ok"

    def test_skipped_only_sessions_are_ok(self, tmp_path, monkeypatch):
        # mode_off / digest-dup skips are not spawn attempts; no warn.
        repo = tmp_path / "repo"
        _make_profile(repo)
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        _attest(
            repo_id,
            "s1",
            [{"check": "correctness_judge", "status": "skipped", "reason": "mode_off", "count": 1}],
        )
        assert _check("judge_spawn_health")["status"] == "ok"


# --------------------------------------------------------------------------
# advisory_emission


def _metric_row(repo_id: str, *, archetype, trust_state="trusted", file_rel=None, hook=None):
    return {
        "ts": "2026-06-11T00:00:00Z",
        "hook": hook or "preflight-and-advise",
        "repo_id": repo_id,
        "elapsed_ms": 5,
        "advisory_emitted": archetype is not None,
        "trust_state": trust_state,
        "archetype": archetype,
        "file_rel": file_rel,
    }


def _write_metrics(rows):
    path = Path(os.environ["CHAMELEON_PLUGIN_DATA"]) / "metrics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


class TestAdvisoryEmission:
    def test_all_null_archetype_trusted_rows_warn(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_profile(repo)
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        _write_metrics([_metric_row(repo_id, archetype=None) for _ in range(5)])
        c = _check("advisory_emission")
        assert c["status"] == "warn"
        assert "/chameleon-refresh" in str(c["detail"])
        assert "/chameleon-status" in str(c["detail"])

    def test_any_resolved_archetype_is_ok(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_profile(repo)
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        _write_metrics([_metric_row(repo_id, archetype=None) for _ in range(5)])
        _write_metrics([_metric_row(repo_id, archetype="component")])
        assert _check("advisory_emission")["status"] == "ok"

    def test_under_five_rows_is_ok(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_profile(repo)
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        _write_metrics([_metric_row(repo_id, archetype=None) for _ in range(4)])
        assert _check("advisory_emission")["status"] == "ok"

    def test_other_repo_rows_do_not_warn(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_profile(repo)
        monkeypatch.chdir(repo)
        _write_metrics([_metric_row("f" * 64, archetype=None) for _ in range(5)])
        assert _check("advisory_emission")["status"] == "ok"

    def test_unsupported_file_rows_do_not_warn(self, tmp_path, monkeypatch):
        # Rows attributed to non-source files (README edits) are normal nulls.
        repo = tmp_path / "repo"
        _make_profile(repo)
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        _write_metrics(
            [_metric_row(repo_id, archetype=None, file_rel="README.md") for _ in range(5)]
        )
        assert _check("advisory_emission")["status"] == "ok"

    def test_untrusted_rows_do_not_warn(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_profile(repo)
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        _write_metrics(
            [_metric_row(repo_id, archetype=None, trust_state="untrusted") for _ in range(5)]
        )
        assert _check("advisory_emission")["status"] == "ok"

    def test_no_metrics_file_is_ok(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _make_profile(repo)
        monkeypatch.chdir(repo)
        assert _check("advisory_emission")["status"] == "ok"


class TestDoctorChecksEveryWiredHook:
    """doctor must verify every wired hook script, including stop-backstop.

    The Stop / SubagentStop backstop hosts turn-end enforcement and the
    correctness judge. If doctor never checks it, a missing or
    non-executable stop-backstop reads as a healthy install.
    """

    _ALL_HOOKS = (
        "session-start",
        "preflight-and-advise",
        "posttool-recorder",
        "posttool-verify",
        "callout-detector",
        "stop-backstop",
    )

    def test_doctor_checks_all_six_hooks(self, tmp_path, monkeypatch):
        plugin_root = tmp_path / "plugin"
        hooks = plugin_root / "hooks"
        hooks.mkdir(parents=True)
        for name in self._ALL_HOOKS:
            script = hooks / name
            script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            script.chmod(0o755)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        checks = tools.doctor()["data"]["checks"]
        names = {c["name"] for c in checks}
        for name in self._ALL_HOOKS:
            assert f"hook_{name}" in names, f"doctor did not check hook {name}"
        sb = next(c for c in checks if c["name"] == "hook_stop-backstop")
        assert sb["status"] == "ok"
