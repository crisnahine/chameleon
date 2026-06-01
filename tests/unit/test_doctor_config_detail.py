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
