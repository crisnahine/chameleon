"""doctor must report the largest silent class instead of reporting it green.

``advisory_emission`` diagnoses ONE dead loop: trusted edits whose archetype
never resolves, which means archetype resolution is broken and the remedy is
/chameleon-refresh. It filters to trusted rows for that reason, and rightly so
-- an untrusted row's null archetype says nothing about resolution.

The consequence is that the check reports "ok" with detail "no trusted preflight
rows recorded for this repo" for the one state where chameleon is most
completely dead. An untrusted repo injects nothing on every edit: after the one
untrusted prompt per session, PreToolUse returns an empty object and stays
silent for the rest of the session. Zero trusted rows is exactly what that
produces, so the dead-install detector was structurally blind to the deadest
install there is -- and the remedy is /chameleon-trust, not /chameleon-refresh.

That is a second diagnosis, not a correction to the first, so it is a second
check. This one is also what a moved or re-cloned checkout looks like: the trust
grant records an absolute repo root, so the profile is present and ungranted at
the new path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from chameleon_mcp import doctor as doctor_mod
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


def _make_repo(root: Path) -> Path:
    (root / ".chameleon").mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / ".chameleon" / "profile.json").write_text("{}", encoding="utf-8")
    return root


def _row(repo_id: str, *, trust_state: str, suppression_reason=None, file_rel="src/a.ts") -> dict:
    return {
        "ts": "2026-07-27T00:00:00Z",
        "hook": "preflight-and-advise",
        "repo_id": repo_id,
        "elapsed_ms": 4,
        "advisory_emitted": False,
        "trust_state": trust_state,
        "suppression_reason": suppression_reason,
        "archetype": None,
        "file_rel": file_rel,
    }


def _write_metrics(rows) -> None:
    path = Path(os.environ["CHAMELEON_PLUGIN_DATA"]) / "metrics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _check(name: str, repo: Path):
    checks = doctor_mod.doctor()["data"]["checks"]
    return next((c for c in checks if c["name"] == name), None)


class TestAdvisorySuppressed:
    def test_untrusted_window_warns_and_prescribes_trust(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        _write_metrics([_row(repo_id, trust_state="untrusted") for _ in range(6)])
        c = _check("advisory_suppressed", repo)
        assert c is not None, "check must exist"
        assert c["status"] == "warn"
        assert "/chameleon-trust" in str(c["detail"])

    def test_trusted_window_is_ok(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        _write_metrics([_row(repo_id, trust_state="trusted") for _ in range(6)])
        assert _check("advisory_suppressed", repo)["status"] == "ok"

    def test_trust_prompt_dedup_rows_count_as_suppressed(self, tmp_path, monkeypatch):
        # Every edit after the one prompt per session lands here, and it is the
        # bulk of a real untrusted session.
        repo = _make_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        _write_metrics(
            [
                _row(repo_id, trust_state="untrusted", suppression_reason="trust_prompt_dedup")
                for _ in range(6)
            ]
        )
        assert _check("advisory_suppressed", repo)["status"] == "warn"

    def test_a_deliberate_pause_is_not_reported_as_breakage(self, tmp_path, monkeypatch):
        # /chameleon-disable and /chameleon-pause-15m are the user's own choice;
        # nagging about them would make doctor noise rather than signal.
        repo = _make_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        _write_metrics(
            [_row(repo_id, trust_state="trusted", suppression_reason="paused") for _ in range(6)]
        )
        assert _check("advisory_suppressed", repo)["status"] == "ok"

    def test_a_mostly_trusted_window_is_ok(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        _write_metrics([_row(repo_id, trust_state="untrusted") for _ in range(2)])
        _write_metrics([_row(repo_id, trust_state="trusted") for _ in range(6)])
        assert _check("advisory_suppressed", repo)["status"] == "ok"

    def test_too_few_rows_is_ok(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        repo_id = tools._compute_repo_id(repo.resolve())
        _write_metrics([_row(repo_id, trust_state="untrusted") for _ in range(3)])
        assert _check("advisory_suppressed", repo)["status"] == "ok"

    def test_another_repos_rows_do_not_warn(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        _write_metrics([_row("f" * 64, trust_state="untrusted") for _ in range(6)])
        assert _check("advisory_suppressed", repo)["status"] == "ok"

    def test_no_metrics_file_is_ok(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        assert _check("advisory_suppressed", repo)["status"] == "ok"
