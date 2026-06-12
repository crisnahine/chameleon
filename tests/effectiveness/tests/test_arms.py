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
