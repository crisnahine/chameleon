"""Unit tests for chameleon_mcp._thresholds.

Pins the DEFAULTS table and the ``CHAMELEON_<NAME>`` env-override mechanism:
valid overrides apply with correct type coercion, invalid values fall back to
the documented default, and unknown threshold names raise KeyError.

The module resolves ``os.environ`` lazily inside ``threshold()`` (not at import
time), so monkeypatch.setenv is enough for most cases. Per the assignment we
also exercise importlib.reload to prove DEFAULTS survives a reload and that an
override set before reload still resolves on the freshly imported module.
"""

from __future__ import annotations

import importlib

import pytest

from chameleon_mcp import _thresholds


@pytest.fixture(autouse=True)
def _reload_thresholds_after(monkeypatch):
    """Isolate env-var state and restore a clean module after each test.

    _thresholds has no plugin-data dependency and no connection cache, but it
    does read env vars. monkeypatch.setenv/delenv auto-restore on teardown;
    we also reload the module afterward so any test that relied on reload does
    not leak a mutated module object to the next test.
    """
    yield
    importlib.reload(_thresholds)


# --- DEFAULTS table ---------------------------------------------------------


class TestDefaults:
    def test_exact_default_values(self):
        d = _thresholds.DEFAULTS
        assert d["WORKSPACE_FANOUT_CAP"] == 500
        assert d["WARNING_SAMPLE_PATHS"] == 3
        assert d["SPARSE_WARNING_LIMIT"] == 50
        assert d["MAX_EXTENDS_HOPS"] == 8
        assert d["EDIT_OBS_HARD_CAP"] == 50_000
        assert d["EDIT_OBS_SOFT_CAP"] == 10_000
        assert d["EDIT_OBS_AGE_DAYS"] == 90
        assert d["STRUCTURED_TOTAL_CAP"] == 50_000
        assert d["SPAWN_WAIT_SECONDS"] == 3.0
        assert d["LISTEN_BACKLOG"] == 16
        assert d["MAX_CONCAT_FOLDS_PER_FILE"] == 1000
        assert d["CLUSTER_SHAPE_JACCARD_THRESHOLD"] == 0.7
        assert d["CLUSTER_PATH_BUCKET_DEPTH"] == 2
        assert d["RENAMES_OVERLAY_CAP"] == 256
        assert d["DRIFT_BANNER_THRESHOLD"] == 0.4
        assert d["DRIFT_BANNER_MIN_OBSERVATIONS"] == 10
        assert d["DRIFT_BANNER_TTL_SECONDS"] == 7 * 24 * 3600  # 604800
        assert d["CALIBRATION_MAX_FILES"] == 600
        assert d["CALIBRATION_MAX_SIBLINGS"] == 10
        assert d["CALIBRATION_FP_EPSILON"] == 0.001

    def test_default_count_is_twenty(self):
        assert len(_thresholds.DEFAULTS) == 20

    def test_default_keys_are_exactly_the_documented_set(self):
        assert set(_thresholds.DEFAULTS) == {
            "WORKSPACE_FANOUT_CAP",
            "WARNING_SAMPLE_PATHS",
            "SPARSE_WARNING_LIMIT",
            "MAX_EXTENDS_HOPS",
            "EDIT_OBS_HARD_CAP",
            "EDIT_OBS_SOFT_CAP",
            "EDIT_OBS_AGE_DAYS",
            "STRUCTURED_TOTAL_CAP",
            "SPAWN_WAIT_SECONDS",
            "LISTEN_BACKLOG",
            "MAX_CONCAT_FOLDS_PER_FILE",
            "CLUSTER_SHAPE_JACCARD_THRESHOLD",
            "CLUSTER_PATH_BUCKET_DEPTH",
            "RENAMES_OVERLAY_CAP",
            "DRIFT_BANNER_THRESHOLD",
            "DRIFT_BANNER_MIN_OBSERVATIONS",
            "DRIFT_BANNER_TTL_SECONDS",
            "CALIBRATION_MAX_FILES",
            "CALIBRATION_MAX_SIBLINGS",
            "CALIBRATION_FP_EPSILON",
        }

    def test_float_defaults_are_exactly_four(self):
        floats = {k for k, v in _thresholds.DEFAULTS.items() if isinstance(v, float)}
        assert floats == {
            "SPAWN_WAIT_SECONDS",
            "CLUSTER_SHAPE_JACCARD_THRESHOLD",
            "DRIFT_BANNER_THRESHOLD",
            "CALIBRATION_FP_EPSILON",
        }

    def test_int_defaults_are_genuine_ints_not_bools(self):
        # bool is an int subclass; the table should hold no booleans.
        for k, v in _thresholds.DEFAULTS.items():
            assert not isinstance(v, bool), k


# --- _env_name --------------------------------------------------------------


class TestEnvName:
    def test_prefixes_with_chameleon(self):
        assert _thresholds._env_name("WORKSPACE_FANOUT_CAP") == "CHAMELEON_WORKSPACE_FANOUT_CAP"

    def test_no_transformation_of_the_name_body(self):
        assert _thresholds._env_name("DRIFT_BANNER_TTL_SECONDS") == (
            "CHAMELEON_DRIFT_BANNER_TTL_SECONDS"
        )


# --- threshold(): no override returns the default with the default's type ---


class TestThresholdDefaultPath:
    def test_int_default_returned_when_unset(self, monkeypatch):
        monkeypatch.delenv("CHAMELEON_WORKSPACE_FANOUT_CAP", raising=False)
        got = _thresholds.threshold("WORKSPACE_FANOUT_CAP")
        assert got == 500
        assert type(got) is int

    def test_float_default_returned_when_unset(self, monkeypatch):
        monkeypatch.delenv("CHAMELEON_DRIFT_BANNER_THRESHOLD", raising=False)
        got = _thresholds.threshold("DRIFT_BANNER_THRESHOLD")
        assert got == 0.4
        assert type(got) is float

    def test_every_default_resolves_to_its_own_type_when_unset(self, monkeypatch):
        for name, default in _thresholds.DEFAULTS.items():
            monkeypatch.delenv(_thresholds._env_name(name), raising=False)
            got = _thresholds.threshold(name)
            assert got == default
            assert type(got) is type(default), name


# --- threshold(): valid override applied with type coercion -----------------


class TestThresholdValidOverride:
    def test_int_override_applied(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_WORKSPACE_FANOUT_CAP", "99")
        got = _thresholds.threshold("WORKSPACE_FANOUT_CAP")
        assert got == 99
        assert type(got) is int

    def test_float_override_applied(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_DRIFT_BANNER_THRESHOLD", "0.85")
        got = _thresholds.threshold("DRIFT_BANNER_THRESHOLD")
        assert got == 0.85
        assert type(got) is float

    def test_integer_string_into_float_default_is_coerced_to_float(self, monkeypatch):
        # default is float -> float("7") == 7.0, not int 7.
        monkeypatch.setenv("CHAMELEON_DRIFT_BANNER_THRESHOLD", "7")
        got = _thresholds.threshold("DRIFT_BANNER_THRESHOLD")
        assert got == 7.0
        assert type(got) is float

    def test_scientific_notation_into_float_default(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_DRIFT_BANNER_THRESHOLD", "1e-1")
        assert _thresholds.threshold("DRIFT_BANNER_THRESHOLD") == 0.1

    def test_whitespace_padded_int_is_accepted(self, monkeypatch):
        # int(" 42 ") == 42 in CPython, so a padded value coerces cleanly.
        monkeypatch.setenv("CHAMELEON_EDIT_OBS_HARD_CAP", "  42  ")
        assert _thresholds.threshold("EDIT_OBS_HARD_CAP") == 42

    def test_plus_prefixed_int_is_accepted(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_LISTEN_BACKLOG", "+10")
        assert _thresholds.threshold("LISTEN_BACKLOG") == 10

    def test_negative_int_is_accepted_unvalidated(self, monkeypatch):
        # threshold() does no range validation; a negative override passes through.
        monkeypatch.setenv("CHAMELEON_WORKSPACE_FANOUT_CAP", "-5")
        assert _thresholds.threshold("WORKSPACE_FANOUT_CAP") == -5

    def test_zero_override_is_accepted(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_MAX_EXTENDS_HOPS", "0")
        assert _thresholds.threshold("MAX_EXTENDS_HOPS") == 0

    def test_inf_into_float_default_passes_through(self, monkeypatch):
        # float("inf") is a valid float; no validation rejects it.
        monkeypatch.setenv("CHAMELEON_SPAWN_WAIT_SECONDS", "inf")
        got = _thresholds.threshold("SPAWN_WAIT_SECONDS")
        assert got == float("inf")


# --- threshold(): invalid override falls back to default --------------------


class TestThresholdInvalidOverride:
    def test_float_string_into_int_default_falls_back(self, monkeypatch):
        # default is int -> int("3.5") raises ValueError -> default 500.
        monkeypatch.setenv("CHAMELEON_WORKSPACE_FANOUT_CAP", "3.5")
        got = _thresholds.threshold("WORKSPACE_FANOUT_CAP")
        assert got == 500
        assert type(got) is int

    def test_scientific_notation_into_int_default_falls_back(self, monkeypatch):
        # int("1e3") raises ValueError -> default 50000.
        monkeypatch.setenv("CHAMELEON_EDIT_OBS_HARD_CAP", "1e3")
        assert _thresholds.threshold("EDIT_OBS_HARD_CAP") == 50_000

    def test_hex_string_into_int_default_falls_back(self, monkeypatch):
        # int("0x10") raises ValueError (base 10 implied) -> default.
        monkeypatch.setenv("CHAMELEON_WORKSPACE_FANOUT_CAP", "0x10")
        assert _thresholds.threshold("WORKSPACE_FANOUT_CAP") == 500

    def test_non_numeric_string_into_int_default_falls_back(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_WORKSPACE_FANOUT_CAP", "notanumber")
        assert _thresholds.threshold("WORKSPACE_FANOUT_CAP") == 500

    def test_non_numeric_string_into_float_default_falls_back(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_DRIFT_BANNER_THRESHOLD", "abc")
        assert _thresholds.threshold("DRIFT_BANNER_THRESHOLD") == 0.4

    def test_empty_string_falls_back_to_default(self, monkeypatch):
        # os.environ.get returns "" (not None), int("") raises ValueError -> default.
        monkeypatch.setenv("CHAMELEON_WORKSPACE_FANOUT_CAP", "")
        assert _thresholds.threshold("WORKSPACE_FANOUT_CAP") == 500

    def test_nan_string_into_int_default_falls_back(self, monkeypatch):
        # int("nan") raises ValueError -> default 16.
        monkeypatch.setenv("CHAMELEON_LISTEN_BACKLOG", "nan")
        assert _thresholds.threshold("LISTEN_BACKLOG") == 16


# --- threshold(): unknown names -------------------------------------------


class TestThresholdUnknownName:
    def test_unknown_name_raises_keyerror(self):
        with pytest.raises(KeyError):
            _thresholds.threshold("NOPE")

    def test_keyerror_message_names_the_unknown_threshold(self):
        with pytest.raises(KeyError) as ei:
            _thresholds.threshold("NOPE")
        # KeyError str-reprs its arg, so the message is quoted twice; assert substring.
        assert "unknown threshold" in str(ei.value)
        assert "NOPE" in str(ei.value)

    def test_unknown_name_raises_even_with_matching_env_var_set(self, monkeypatch):
        # An env var alone does not register a threshold; the name must be in DEFAULTS.
        monkeypatch.setenv("CHAMELEON_NOPE", "42")
        with pytest.raises(KeyError):
            _thresholds.threshold("NOPE")


# --- threshold_int / threshold_float convenience casts ----------------------


class TestConvenienceCasts:
    def test_threshold_int_on_int_default(self, monkeypatch):
        monkeypatch.delenv("CHAMELEON_WORKSPACE_FANOUT_CAP", raising=False)
        got = _thresholds.threshold_int("WORKSPACE_FANOUT_CAP")
        assert got == 500
        assert type(got) is int

    def test_threshold_int_truncates_float_default(self, monkeypatch):
        # int(0.4) == 0 -- threshold_int floors the float default toward zero.
        monkeypatch.delenv("CHAMELEON_DRIFT_BANNER_THRESHOLD", raising=False)
        got = _thresholds.threshold_int("DRIFT_BANNER_THRESHOLD")
        assert got == 0
        assert type(got) is int

    def test_threshold_int_truncates_float_override(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_SPAWN_WAIT_SECONDS", "3.9")
        got = _thresholds.threshold_int("SPAWN_WAIT_SECONDS")
        assert got == 3  # truncation toward zero, not rounding

    def test_threshold_float_on_int_default(self, monkeypatch):
        monkeypatch.delenv("CHAMELEON_WORKSPACE_FANOUT_CAP", raising=False)
        got = _thresholds.threshold_float("WORKSPACE_FANOUT_CAP")
        assert got == 500.0
        assert type(got) is float

    def test_threshold_float_on_float_default(self, monkeypatch):
        monkeypatch.delenv("CHAMELEON_DRIFT_BANNER_THRESHOLD", raising=False)
        got = _thresholds.threshold_float("DRIFT_BANNER_THRESHOLD")
        assert got == 0.4
        assert type(got) is float

    def test_threshold_int_propagates_unknown_name_keyerror(self):
        with pytest.raises(KeyError):
            _thresholds.threshold_int("NOPE")

    def test_threshold_float_propagates_unknown_name_keyerror(self):
        with pytest.raises(KeyError):
            _thresholds.threshold_float("NOPE")


# --- importlib.reload semantics --------------------------------------------


class TestReloadSemantics:
    def test_defaults_survive_reload_unchanged(self):
        before = dict(_thresholds.DEFAULTS)
        importlib.reload(_thresholds)
        assert dict(_thresholds.DEFAULTS) == before

    def test_override_set_before_reload_still_applies_after(self, monkeypatch):
        # Set env, reload, then resolve on the freshly imported module object.
        monkeypatch.setenv("CHAMELEON_WORKSPACE_FANOUT_CAP", "123")
        reloaded = importlib.reload(_thresholds)
        assert reloaded.threshold("WORKSPACE_FANOUT_CAP") == 123

    def test_override_cleared_returns_default_after_reload(self, monkeypatch):
        monkeypatch.setenv("CHAMELEON_WORKSPACE_FANOUT_CAP", "123")
        importlib.reload(_thresholds)
        assert _thresholds.threshold("WORKSPACE_FANOUT_CAP") == 123
        monkeypatch.delenv("CHAMELEON_WORKSPACE_FANOUT_CAP", raising=False)
        # No reload needed because resolution is lazy, but it must hold after one too.
        importlib.reload(_thresholds)
        assert _thresholds.threshold("WORKSPACE_FANOUT_CAP") == 500
