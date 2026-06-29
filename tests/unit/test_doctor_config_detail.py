"""The doctor config_json check must accurately report which v0.6.0 features are
ON by default when there is no config.json. The old wording ('v0.5.x defaults',
'opt into ... auto_refresh') was wrong — auto_refresh and auto_rename are ON by
default — and misled /chameleon-status into reporting 'v0.6.0 features off'."""

from __future__ import annotations

from chameleon_mcp import tools


def test_doctor_no_config_detail_states_auto_refresh_on(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)  # no config.json present
    monkeypatch.chdir(repo)

    checks = tools.doctor().get("data", {}).get("checks", [])
    cj = next((c for c in checks if c.get("name") == "config_json"), None)
    assert cj is not None
    detail = str(cj["detail"]).lower()
    assert "auto_refresh" in detail
    assert "on by default" in detail
    # the old misleading wording must be gone
    assert "v0.5.x defaults" not in detail
    assert "opt into v0.6.0 features (canonical_ref, auto_refresh" not in detail


def test_doctor_config_found_from_subdirectory(tmp_path, monkeypatch):
    # Regression: doctor must walk to the repo root, not read Path.cwd()/.chameleon
    # directly — else it reports a configured repo as unconfigured from any subdir
    # (a monorepo workspace, app/ under a Rails repo), misleading /chameleon-status.
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    (repo / ".chameleon" / "config.json").write_text(
        '{"schema_version": "chameleon-config-0.9.0", "production_ref": "main"}'
    )
    sub = repo / "app" / "models"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)

    checks = tools.doctor().get("data", {}).get("checks", [])
    cj = next((c for c in checks if c.get("name") == "config_json"), None)
    assert cj is not None and cj["status"] == "ok"
    assert cj["detail"]["production_ref"] == "main"
