import json
from pathlib import Path

import pytest

from chameleon_mcp.profile.config import ChameleonConfigError, load_config


def _write(tmp_path: Path, obj: dict) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return tmp_path


def test_default_enforcement_is_shadow(tmp_path):
    cfg = load_config(tmp_path)  # no file
    assert cfg.enforcement.mode == "shadow"
    assert cfg.enforcement.stop_backstop is True
    assert cfg.enforcement.stop_block_cap == 3


def test_enforce_mode_parsed(tmp_path):
    d = _write(tmp_path, {"enforcement": {"mode": "enforce", "stop_block_cap": 5}})
    cfg = load_config(d)
    assert cfg.enforcement.mode == "enforce"
    assert cfg.enforcement.stop_block_cap == 5


def test_bad_mode_rejected(tmp_path):
    d = _write(tmp_path, {"enforcement": {"mode": "nuke"}})
    with pytest.raises(ChameleonConfigError):
        load_config(d)


def test_unknown_enforcement_key_rejected(tmp_path):
    d = _write(tmp_path, {"enforcement": {"mode": "off", "wat": 1}})
    with pytest.raises(ChameleonConfigError):
        load_config(d)


def test_idiom_review_defaults_on_judge_off(tmp_path):
    cfg = load_config(tmp_path)  # no file
    assert cfg.enforcement.idiom_review is True
    assert cfg.enforcement.idiom_judge is False


def test_idiom_flags_parsed(tmp_path):
    d = _write(
        tmp_path,
        {"enforcement": {"mode": "enforce", "idiom_review": False, "idiom_judge": True}},
    )
    cfg = load_config(d)
    assert cfg.enforcement.idiom_review is False
    assert cfg.enforcement.idiom_judge is True


def test_idiom_review_must_be_bool(tmp_path):
    d = _write(tmp_path, {"enforcement": {"idiom_review": "yes"}})
    with pytest.raises(ChameleonConfigError):
        load_config(d)


def test_idiom_judge_must_be_bool(tmp_path):
    d = _write(tmp_path, {"enforcement": {"idiom_judge": 1}})
    with pytest.raises(ChameleonConfigError):
        load_config(d)


def test_stale_test_advisory_defaults_on(tmp_path):
    cfg = load_config(tmp_path)  # no file
    assert cfg.enforcement.stale_test_advisory is True


def test_stale_test_advisory_parsed(tmp_path):
    d = _write(tmp_path, {"enforcement": {"stale_test_advisory": False}})
    cfg = load_config(d)
    assert cfg.enforcement.stale_test_advisory is False


def test_stale_test_advisory_must_be_bool(tmp_path):
    d = _write(tmp_path, {"enforcement": {"stale_test_advisory": "sure"}})
    with pytest.raises(ChameleonConfigError):
        load_config(d)


def test_changeset_completeness_defaults_on(tmp_path):
    cfg = load_config(tmp_path)  # no file
    assert cfg.enforcement.changeset_completeness is True


def test_changeset_completeness_parsed(tmp_path):
    d = _write(tmp_path, {"enforcement": {"changeset_completeness": False}})
    cfg = load_config(d)
    assert cfg.enforcement.changeset_completeness is False


def test_changeset_completeness_must_be_bool(tmp_path):
    d = _write(tmp_path, {"enforcement": {"changeset_completeness": "sure"}})
    with pytest.raises(ChameleonConfigError):
        load_config(d)


def test_crossfile_existence_advisory_defaults_on(tmp_path):
    cfg = load_config(tmp_path)  # no file
    assert cfg.enforcement.crossfile_existence_advisory is True


def test_crossfile_existence_advisory_parsed(tmp_path):
    d = _write(tmp_path, {"enforcement": {"crossfile_existence_advisory": False}})
    cfg = load_config(d)
    assert cfg.enforcement.crossfile_existence_advisory is False


def test_crossfile_existence_advisory_must_be_bool(tmp_path):
    d = _write(tmp_path, {"enforcement": {"crossfile_existence_advisory": "sure"}})
    with pytest.raises(ChameleonConfigError):
        load_config(d)
