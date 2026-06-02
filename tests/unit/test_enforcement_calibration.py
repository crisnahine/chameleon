from pathlib import Path

from chameleon_mcp.enforcement_calibration import (
    active_block_rules,
    load_block_rules,
    write_block_rules,
)


def test_roundtrip(tmp_path: Path):
    data = {
        "phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 100},
        "jsx-presence-mismatch": {"active": False, "fp_rate": 0.02, "sampled": 50},
    }
    write_block_rules(tmp_path, data)
    loaded = load_block_rules(tmp_path)
    assert loaded["phantom-import"]["active"] is True
    assert active_block_rules(tmp_path) == {"phantom-import"}


def test_missing_file_is_empty(tmp_path: Path):
    assert load_block_rules(tmp_path) == {}
    assert active_block_rules(tmp_path) == set()


def test_corrupt_file_is_empty(tmp_path: Path):
    (tmp_path / "enforcement.json").write_text("{not json", encoding="utf-8")
    assert active_block_rules(tmp_path) == set()
