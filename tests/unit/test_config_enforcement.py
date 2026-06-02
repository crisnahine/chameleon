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
