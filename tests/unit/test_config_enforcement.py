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


def test_unknown_enforcement_key_tolerated(tmp_path):
    # config.json is committed and travels to teammates on different chameleon
    # versions. An enforcement key a newer version added must not brick an older
    # engine: the unknown key is ignored, the known keys still parse.
    d = _write(tmp_path, {"enforcement": {"mode": "enforce", "some_future_key": True}})
    cfg = load_config(d)
    assert cfg.enforcement.mode == "enforce"


def test_unknown_enforcement_key_does_not_corrupt_known_keys(tmp_path):
    d = _write(
        tmp_path,
        {"enforcement": {"mode": "shadow", "stop_block_cap": 7, "v99_flag": "x"}},
    )
    cfg = load_config(d)
    assert cfg.enforcement.mode == "shadow"
    assert cfg.enforcement.stop_block_cap == 7


def test_bad_value_on_known_enforcement_key_still_rejected(tmp_path):
    # Tolerating unknown keys must not weaken validation of known ones: a wrong
    # type on a known key is still a hard error so a real typo is caught.
    d = _write(tmp_path, {"enforcement": {"mode": "shadow", "stop_block_cap": "lots"}})
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


def test_correctness_judge_default_on_no_file(tmp_path):
    assert load_config(tmp_path).enforcement.correctness_judge is True


def test_correctness_judge_default_on_when_block_omits_key(tmp_path):
    # A committed config that sets any enforcement key but omits correctness_judge
    # must keep it on: the coerce default has to match the dataclass default, or
    # the turn-end correctness reviewer silently disables itself for every repo
    # that ships a config.json (every other enforcement field gets this right).
    d = _write(tmp_path, {"enforcement": {"mode": "enforce"}})
    assert load_config(d).enforcement.correctness_judge is True


def test_correctness_judge_explicit_off(tmp_path):
    d = _write(tmp_path, {"enforcement": {"correctness_judge": False}})
    assert load_config(d).enforcement.correctness_judge is False


def test_correctness_judge_must_be_bool(tmp_path):
    d = _write(tmp_path, {"enforcement": {"correctness_judge": "yes"}})
    with pytest.raises(ChameleonConfigError):
        load_config(d)
