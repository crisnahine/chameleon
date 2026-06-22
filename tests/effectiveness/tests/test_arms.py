"""Arm parsing, env construction, and config application."""

from __future__ import annotations

import json

import pytest
from tests.effectiveness.arms import ArmError, apply_arm_config, arm_env, parse_arms


def test_parse_three_arms():
    specs = parse_arms("off,shadow,enforce", None)
    assert [s.name for s in specs] == ["off", "shadow", "enforce"]
    assert [s.disable_env for s in specs] == [True, False, False]
    assert [s.base_mode for s in specs] == ["shadow", "shadow", "enforce"]


def test_unknown_arm_rejected():
    with pytest.raises(ArmError, match="unknown arm"):
        parse_arms("off,turbo", None)


def test_toggle_creates_paired_arm_from_shadow():
    specs = parse_arms("off,shadow", "judge_crossfile_facts")
    assert [s.name for s in specs] == [
        "off",
        "shadow",
        "shadow~judge_crossfile_facts=false",
    ]
    toggled = specs[-1]
    assert toggled.toggle_key == "judge_crossfile_facts"
    assert toggled.toggle_value is False
    assert toggled.base_mode == "shadow"


def test_toggle_rejects_non_boolean_key():
    with pytest.raises(ArmError, match="boolean"):
        parse_arms("shadow", "stop_block_cap")
    with pytest.raises(ArmError, match="boolean"):
        parse_arms("shadow", "mode")


def test_toggle_needs_non_off_base():
    with pytest.raises(ArmError, match="non-off"):
        parse_arms("off", "judge_crossfile_facts")


def test_arm_env_only_off_carries_disable():
    off, shadow = parse_arms("off,shadow", None)
    base = {"CHAMELEON_PLUGIN_DATA": "/x"}
    assert arm_env(off, base)["CHAMELEON_DISABLE"] == "1"
    assert "CHAMELEON_DISABLE" not in arm_env(shadow, base)
    # base env must not be mutated
    assert "CHAMELEON_DISABLE" not in base


def test_apply_arm_config_writes_mode_and_toggle(tmp_path):
    cham = tmp_path / ".chameleon"
    cham.mkdir()
    (cham / "config.json").write_text(
        json.dumps({"schema_version": "1.0", "production_ref": "origin/main"})
    )
    specs = parse_arms("enforce", "judge_crossfile_facts")
    apply_arm_config(specs[0], tmp_path)
    data = json.loads((cham / "config.json").read_text())
    assert data["enforcement"]["mode"] == "enforce"
    assert data["production_ref"] == "origin/main"  # untouched keys preserved

    apply_arm_config(specs[1], tmp_path)
    data = json.loads((cham / "config.json").read_text())
    assert data["enforcement"]["judge_crossfile_facts"] is False


def test_apply_arm_config_creates_config_when_missing(tmp_path):
    (tmp_path / ".chameleon").mkdir()
    spec = parse_arms("shadow", None)[0]
    apply_arm_config(spec, tmp_path)
    data = json.loads((tmp_path / ".chameleon" / "config.json").read_text())
    assert data["enforcement"]["mode"] == "shadow"


def test_apply_arm_config_disables_auto_refresh(tmp_path):
    """The arm-setup commit makes the cloned profile look stale; a mid-session
    auto-refresh would pollute the session diff with profile churn and charge
    one arm a re-derivation the other never pays. Every arm pins it off."""
    cham = tmp_path / ".chameleon"
    cham.mkdir()
    (cham / "config.json").write_text(
        json.dumps({"auto_refresh": {"enabled": True, "drift_threshold": 0.2}})
    )
    spec = parse_arms("shadow", None)[0]
    apply_arm_config(spec, tmp_path)
    data = json.loads((cham / "config.json").read_text())
    assert data["auto_refresh"]["enabled"] is False
    assert data["auto_refresh"]["drift_threshold"] == 0.2  # sibling keys preserved


# --- env-flag toggle (nearby_signatures / CHAMELEON_NEARBY_SIGNATURES) -------


def test_env_toggle_creates_paired_arm_via_env_not_config():
    from tests.effectiveness.arms import arm_env, parse_arms

    specs = parse_arms("off,shadow", "nearby_signatures")
    assert "shadow~nearby_signatures=on" in [s.name for s in specs]
    paired = next(s for s in specs if s.env_key)
    assert paired.env_key == "CHAMELEON_NEARBY_SIGNATURES"
    assert paired.toggle_key is None  # not a config enforcement key
    # the paired arm sets the env var for its sessions
    assert arm_env(paired, {})["CHAMELEON_NEARBY_SIGNATURES"] == "1"
    # the off arm never carries the toggle
    off = next(s for s in specs if s.disable_env)
    assert "CHAMELEON_NEARBY_SIGNATURES" not in arm_env(off, {})


def test_env_toggle_disables_default_on_feature():
    # counterexample is default-ON, so its A/B paired arm turns it OFF ("0") to
    # isolate the feature's effect against the base shadow arm.
    from tests.effectiveness.arms import arm_env, parse_arms

    specs = parse_arms("off,shadow", "counterexample")
    assert "shadow~counterexample=off" in [s.name for s in specs]
    paired = next(s for s in specs if s.env_key)
    assert paired.env_key == "CHAMELEON_COUNTEREXAMPLE"
    assert arm_env(paired, {})["CHAMELEON_COUNTEREXAMPLE"] == "0"
    # the base shadow arm leaves the feature at its default (on, unset)
    shadow = next(s for s in specs if s.name == "shadow")
    assert "CHAMELEON_COUNTEREXAMPLE" not in arm_env(shadow, {})


def test_env_toggle_does_not_write_to_config(tmp_path):
    from tests.effectiveness.arms import apply_arm_config, parse_arms

    paired = next(s for s in parse_arms("off,shadow", "nearby_signatures") if s.env_key)
    cham = tmp_path / ".chameleon"
    cham.mkdir()
    (cham / "config.json").write_text('{"enforcement": {"mode": "shadow"}}', encoding="utf-8")
    apply_arm_config(paired, tmp_path)
    import json

    enforcement = json.loads((cham / "config.json").read_text())["enforcement"]
    # the env toggle must NOT leak into config.json as a bogus enforcement key
    assert "CHAMELEON_NEARBY_SIGNATURES" not in enforcement
    assert "nearby_signatures" not in enforcement


def test_unknown_toggle_error_lists_env_toggles():
    import pytest
    from tests.effectiveness.arms import ArmError, parse_arms

    with pytest.raises(ArmError, match="env toggles"):
        parse_arms("off,shadow", "bogus_toggle")
