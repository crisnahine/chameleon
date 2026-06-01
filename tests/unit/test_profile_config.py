"""Unit tests for chameleon_mcp.profile.config.

``load_config(profile_dir)`` reads ``<profile_dir>/config.json`` for the
v0.6.0 UX features (branch pinning, auto-refresh, trust friction, auto-rename).
The contract:

  - absent file -> a default ``ChameleonConfig`` (v0.5.x behavior), never raises
  - a present, well-formed file parses into the dataclass with correct types
    (drift_threshold coerced to float, canonical_ref whitespace-stripped, etc.)
  - a present but malformed file raises ``ChameleonConfigError`` -- and only
    then. This covers bad JSON, duplicate keys, depth bombs, unknown keys
    (top-level + per-section), wrong value types, out-of-range numbers,
    bad enum values, and symlink/unsafe-read failures.

No conftest.py exists; per-user state isolation is replicated inline via an
autouse fixture setting CHAMELEON_PLUGIN_DATA. ``load_config`` reads the dir
passed to it (not the env var) so the fixture is belt-and-suspenders, matching
the convention used by the sibling unit tests.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from chameleon_mcp.profile.config import (
    CONFIG_FILENAME,
    CURRENT_SCHEMA,
    AutoRefreshConfig,
    ChameleonConfig,
    ChameleonConfigError,
    TrustConfig,
    load_config,
)


@pytest.fixture(autouse=True)
def _isolate_plugin_data(tmp_path: Path, monkeypatch):
    """Point chameleon's per-user state at an isolated tmp dir.

    Mirrors the inline isolation other unit tests use (there is no conftest).
    plugin_paths reads CHAMELEON_PLUGIN_DATA lazily, so no module reload needed.
    """
    data_dir = tmp_path / "_pdata"
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(data_dir))
    return data_dir


def _write(profile_dir: Path, payload) -> Path:
    """Write config.json. dict/list -> json.dumps; str -> raw bytes verbatim."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    path = profile_dir / CONFIG_FILENAME
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Defaults / absent file
# --------------------------------------------------------------------------
class TestAbsentFile:
    def test_missing_file_returns_full_defaults(self, tmp_path: Path):
        d = tmp_path / "profile"
        d.mkdir()
        c = load_config(d)
        assert c == ChameleonConfig()
        assert c.schema_version == CURRENT_SCHEMA
        assert c.canonical_ref is None
        assert c.auto_rename is True
        assert c.auto_refresh == AutoRefreshConfig(
            enabled=True, drift_threshold=0.2, max_age_hours=168
        )
        # auto-trust on refresh is the default (opt out with auto_preserve_when=null)
        assert c.trust == TrustConfig(auto_preserve_when="always")
        assert c.branch_pinning_enabled is False

    def test_missing_profile_dir_returns_defaults(self, tmp_path: Path):
        # the directory itself does not exist -> path.is_file() is False
        d = tmp_path / "does-not-exist"
        assert load_config(d) == ChameleonConfig()

    def test_config_dataclass_is_frozen(self):
        c = ChameleonConfig()
        with pytest.raises(Exception) as exc:
            c.auto_rename = False  # type: ignore[misc]
        assert exc.type.__name__ == "FrozenInstanceError"


# --------------------------------------------------------------------------
# Well-formed parse
# --------------------------------------------------------------------------
class TestValidParse:
    def test_full_config_parses_exactly(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(
            d,
            {
                "$schema": "chameleon-config-0.6.0",
                "canonical_ref": "origin/develop",
                "auto_refresh": {
                    "enabled": False,
                    "drift_threshold": 0.5,
                    "max_age_hours": 24,
                },
                "trust": {"auto_preserve_when": "pulled_from_remote"},
                "auto_rename": False,
            },
        )
        c = load_config(d)
        assert c == ChameleonConfig(
            schema_version="chameleon-config-0.6.0",
            canonical_ref="origin/develop",
            auto_refresh=AutoRefreshConfig(enabled=False, drift_threshold=0.5, max_age_hours=24),
            trust=TrustConfig(auto_preserve_when="pulled_from_remote"),
            auto_rename=False,
        )
        assert c.branch_pinning_enabled is True

    def test_empty_object_yields_defaults(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {})
        assert load_config(d) == ChameleonConfig()

    def test_canonical_ref_whitespace_is_stripped(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"canonical_ref": "  origin/main  "})
        c = load_config(d)
        assert c.canonical_ref == "origin/main"
        assert c.branch_pinning_enabled is True

    def test_canonical_ref_null_disables_pinning(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"canonical_ref": None})
        c = load_config(d)
        assert c.canonical_ref is None
        assert c.branch_pinning_enabled is False

    def test_drift_threshold_int_coerced_to_float(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"auto_refresh": {"drift_threshold": 1}})
        c = load_config(d)
        assert c.auto_refresh.drift_threshold == 1.0
        assert isinstance(c.auto_refresh.drift_threshold, float)

    def test_drift_threshold_boundaries_inclusive(self, tmp_path: Path):
        d = tmp_path / "profile"
        for v in (0.0, 1.0):
            _write(d, {"auto_refresh": {"drift_threshold": v}})
            assert load_config(d).auto_refresh.drift_threshold == v

    def test_custom_schema_string_preserved(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"$schema": "custom-future-v9"})
        assert load_config(d).schema_version == "custom-future-v9"

    def test_trust_auto_preserve_explicit_null(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"trust": {"auto_preserve_when": None}})
        assert load_config(d).trust.auto_preserve_when is None

    def test_partial_auto_refresh_fills_other_defaults(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"auto_refresh": {"enabled": False}})
        ar = load_config(d).auto_refresh
        assert ar.enabled is False
        assert ar.drift_threshold == 0.2
        assert ar.max_age_hours == 168


# --------------------------------------------------------------------------
# Malformed JSON / structure
# --------------------------------------------------------------------------
class TestMalformedJson:
    def test_invalid_json_raises(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, "{ not valid json ")
        with pytest.raises(ChameleonConfigError, match="not valid/safe JSON"):
            load_config(d)

    def test_duplicate_keys_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, '{"auto_rename": true, "auto_rename": false}')
        with pytest.raises(ChameleonConfigError, match="duplicate key"):
            load_config(d)

    def test_excessive_nesting_depth_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        nested: dict = {}
        cur = nested
        for _ in range(70):  # MAX_JSON_DEPTH is 64
            cur["a"] = {}
            cur = cur["a"]
        _write(d, json.dumps(nested))
        with pytest.raises(ChameleonConfigError, match="depth"):
            load_config(d)

    def test_top_level_array_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, "[1, 2, 3]")
        with pytest.raises(ChameleonConfigError, match="top-level must be an object"):
            load_config(d)

    def test_top_level_scalar_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, "42")
        with pytest.raises(ChameleonConfigError, match="top-level must be an object"):
            load_config(d)


# --------------------------------------------------------------------------
# Unknown keys (strict sections)
# --------------------------------------------------------------------------
class TestUnknownKeys:
    def test_unknown_top_level_key_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"totally_unknown": 1})
        with pytest.raises(ChameleonConfigError, match="unknown top-level key"):
            load_config(d)

    def test_unknown_auto_refresh_key_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"auto_refresh": {"bogus": 1}})
        with pytest.raises(ChameleonConfigError, match="unknown key.*auto_refresh"):
            load_config(d)

    def test_unknown_trust_key_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"trust": {"bogus": 1}})
        with pytest.raises(ChameleonConfigError, match="unknown key.*trust"):
            load_config(d)


# --------------------------------------------------------------------------
# Wrong value types
# --------------------------------------------------------------------------
class TestWrongTypes:
    def test_schema_not_string_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"$schema": 123})
        with pytest.raises(ChameleonConfigError, match=r"`\$schema` must be a string"):
            load_config(d)

    def test_canonical_ref_wrong_type_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"canonical_ref": 5})
        with pytest.raises(ChameleonConfigError, match="canonical_ref"):
            load_config(d)

    def test_canonical_ref_blank_string_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"canonical_ref": "   "})
        with pytest.raises(ChameleonConfigError, match="non-empty string or null"):
            load_config(d)

    def test_auto_rename_not_bool_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"auto_rename": "yes"})
        with pytest.raises(ChameleonConfigError, match="`auto_rename` must be bool"):
            load_config(d)

    def test_auto_refresh_not_object_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"auto_refresh": "nope"})
        with pytest.raises(ChameleonConfigError, match="`auto_refresh` must be an object"):
            load_config(d)

    def test_trust_not_object_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"trust": [1, 2]})
        with pytest.raises(ChameleonConfigError, match="`trust` must be an object"):
            load_config(d)

    def test_auto_refresh_enabled_not_bool_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"auto_refresh": {"enabled": "true"}})
        with pytest.raises(ChameleonConfigError, match="`auto_refresh.enabled` must be bool"):
            load_config(d)

    def test_drift_threshold_bool_rejected(self, tmp_path: Path):
        # bool is an int subclass; the loader must explicitly reject it.
        d = tmp_path / "profile"
        _write(d, {"auto_refresh": {"drift_threshold": True}})
        with pytest.raises(ChameleonConfigError, match="drift_threshold.*in .0, 1."):
            load_config(d)

    def test_max_age_hours_bool_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"auto_refresh": {"max_age_hours": True}})
        with pytest.raises(ChameleonConfigError, match="max_age_hours.*positive int"):
            load_config(d)

    def test_max_age_hours_float_rejected(self, tmp_path: Path):
        # max_age_hours must be a strict int, not a float
        d = tmp_path / "profile"
        _write(d, {"auto_refresh": {"max_age_hours": 24.0}})
        with pytest.raises(ChameleonConfigError, match="max_age_hours.*positive int"):
            load_config(d)


# --------------------------------------------------------------------------
# Out-of-range / enum values
# --------------------------------------------------------------------------
class TestRangeAndEnum:
    @pytest.mark.parametrize("bad", [1.1, -0.1, 2.0, -1])
    def test_drift_threshold_out_of_range_rejected(self, tmp_path: Path, bad):
        d = tmp_path / "profile"
        _write(d, {"auto_refresh": {"drift_threshold": bad}})
        with pytest.raises(ChameleonConfigError, match="drift_threshold"):
            load_config(d)

    @pytest.mark.parametrize("bad", [0, -5, -1])
    def test_max_age_hours_non_positive_rejected(self, tmp_path: Path, bad):
        d = tmp_path / "profile"
        _write(d, {"auto_refresh": {"max_age_hours": bad}})
        with pytest.raises(ChameleonConfigError, match="max_age_hours"):
            load_config(d)

    def test_invalid_auto_preserve_when_rejected(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"trust": {"auto_preserve_when": "nonsense"}})
        with pytest.raises(ChameleonConfigError, match="auto_preserve_when"):
            load_config(d)

    def test_valid_auto_preserve_when_accepted(self, tmp_path: Path):
        d = tmp_path / "profile"
        _write(d, {"trust": {"auto_preserve_when": "pulled_from_remote"}})
        assert load_config(d).trust.auto_preserve_when == "pulled_from_remote"


# --------------------------------------------------------------------------
# Unsafe-read path (symlink) -> ChameleonConfigError
# --------------------------------------------------------------------------
class TestUnsafeRead:
    def test_symlinked_config_rejected(self, tmp_path: Path):
        # config.json is trust-hashed, so safe_read_profile_artifact opens it
        # with O_NOFOLLOW; a symlink there must surface as ChameleonConfigError,
        # not a default config and not a raw OSError.
        d = tmp_path / "profile"
        d.mkdir()
        real = tmp_path / "real_config.json"
        real.write_text('{"auto_rename": false}', encoding="utf-8")
        link = d / CONFIG_FILENAME
        os.symlink(real, link)
        with pytest.raises(ChameleonConfigError, match="cannot read"):
            load_config(d)


def test_auto_preserve_when_accepts_always(tmp_path):
    """`trust.auto_preserve_when: "always"` re-grants trust after ANY refresh
    (manual or auto), so a user who opted into trusting this repo isn't
    re-prompted on every refresh."""
    from chameleon_mcp.profile.config import load_config

    pd = tmp_path
    (pd / "config.json").write_text(
        '{"$schema":"chameleon-config-0.6.0","trust":{"auto_preserve_when":"always"}}',
        encoding="utf-8",
    )
    cfg = load_config(pd)
    assert cfg.trust.auto_preserve_when == "always"
