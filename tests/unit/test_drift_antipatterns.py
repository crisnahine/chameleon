"""Drift-derived anti-pattern signals (C2.3 / SP7).

drift.db stores no wrong-way code, but it does record which rules edits in an
archetype repeatedly bumped against (rule_overrides) and the archetype's
off-pattern edit rate (decision_log violations_raised). The reader surfaces those
per archetype, above a floor, so /chameleon-auto-idiom can propose a
counterexample-bearing idiom (the deriver reads a flagged file for the actual
wrong-way form). These tests pin the reader logic against a seeded drift.db.
"""

from __future__ import annotations

import time
from unittest.mock import patch


def _now() -> int:
    return int(time.time())


def test_reader_surfaces_recurring_rule_per_archetype(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    from chameleon_mcp.drift.observations import (
        archetype_antipattern_signals,
        record_override,
    )

    repo_id = "repo_ap"
    now = _now()
    for i in range(4):
        record_override(
            repo_id,
            "import-preference-violation",
            rel_path=f"src/a{i}.ts",
            archetype="service",
            observed_at=now - 100,
        )
    record_override(
        repo_id,
        "naming-convention-violation",
        rel_path="src/b.ts",
        archetype="service",
        observed_at=now - 100,
    )

    sig = archetype_antipattern_signals(repo_id, window_days=30, min_count=3)
    assert "service" in sig
    rules = sig["service"]["rules"]
    assert rules[0]["rule"] == "import-preference-violation"
    assert rules[0]["count"] == 4
    assert rules[0]["distinct_files"] == 4
    # The below-floor naming rule still appears in the (capped) list, but the
    # archetype surfaced because the import rule cleared the floor.
    assert any(r["rule"] == "naming-convention-violation" for r in rules)


def test_reader_floor_excludes_one_off_archetypes(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    from chameleon_mcp.drift.observations import (
        archetype_antipattern_signals,
        record_override,
    )

    record_override(
        "repo_floor", "some-rule", rel_path="x.ts", archetype="util", observed_at=_now()
    )
    sig = archetype_antipattern_signals("repo_floor", window_days=30, min_count=3)
    assert sig == {}


def test_reader_respects_window(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    from chameleon_mcp.drift.observations import (
        archetype_antipattern_signals,
        record_override,
    )

    old = _now() - 60 * 86_400  # 60 days ago, outside a 30d window
    for i in range(5):
        record_override(
            "repo_win", "old-rule", rel_path=f"o{i}.ts", archetype="service", observed_at=old
        )
    sig = archetype_antipattern_signals("repo_win", window_days=30, min_count=3)
    assert sig == {}


def test_reader_includes_archetype_on_violation_rate(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    from chameleon_mcp.drift.observations import (
        archetype_antipattern_signals,
        record_decision,
    )

    now = _now()
    for i in range(3):
        record_decision(
            "repo_viol",
            f"src/c{i}.ts",
            archetype="controller",
            match_quality="ast",
            confidence_band="high",
            violations_raised=2,
            outcome="would-block",
            observed_at=now - 100,
        )
    sig = archetype_antipattern_signals("repo_viol", window_days=30, min_count=3)
    assert "controller" in sig
    assert sig["controller"]["violation_edits"] == 3
    assert sig["controller"]["total_edits"] == 3


def test_reader_missing_db_is_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    from chameleon_mcp.drift.observations import archetype_antipattern_signals

    assert archetype_antipattern_signals("no_such_repo", window_days=30, min_count=3) == {}


def test_get_drift_antipatterns_tool_wraps_and_filters(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    from chameleon_mcp import tools

    fake = {
        "service": {
            "rules": [{"rule": "import-preference-violation", "count": 4}],
            "violation_edits": 0,
            "total_edits": 4,
        },
        "controller": {"rules": [], "violation_edits": 3, "total_edits": 3},
    }
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    with patch("chameleon_mcp.tools._resolve_repo_arg", return_value=(repo, "repo_id_xyz")):
        with patch(
            "chameleon_mcp.drift.observations.archetype_antipattern_signals", return_value=fake
        ):
            out = tools.get_drift_antipatterns(str(repo))
            assert out["data"]["archetypes"] == fake
            # Filter to one archetype.
            out2 = tools.get_drift_antipatterns(str(repo), archetype="service")
            assert set(out2["data"]["archetypes"]) == {"service"}


def test_get_drift_antipatterns_tool_fails_open_no_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    from chameleon_mcp import tools

    with patch("chameleon_mcp.tools._resolve_repo_arg", return_value=(None, None)):
        out = tools.get_drift_antipatterns("/nope")
        assert out["data"]["archetypes"] == {}
