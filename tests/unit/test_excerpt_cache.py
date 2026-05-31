"""Unit tests for chameleon_mcp._excerpt_cache.

Pins the process-global LRU memo: get/build, hit/miss (build-once),
eviction at capacity, recency (LRU) ordering, identity passthrough,
clear(), and the CHAMELEON_EXCERPT_CACHE_CAP override (read at import
time, so each test reloads the module to reset _CAP and the global
_CACHE).

There is no conftest.py; isolation is done inline. The module reads its
cap from the environment at import time and keeps a process-global
OrderedDict, so the autouse fixture pins CHAMELEON_PLUGIN_DATA at
tmp_path, removes any inherited cap override, reloads the module to a
clean state, and clears the cache on teardown.
"""

from __future__ import annotations

import importlib

import pytest

from chameleon_mcp import _excerpt_cache as _ec_mod


def _fresh(monkeypatch, tmp_path, cap=None):
    """Reload the module with a given cap env (or unset) and return it.

    Reloading is the only way to re-evaluate _CAP = _resolve_cap() and to
    drop the process-global _CACHE that prior tests may have populated.
    """
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    if cap is None:
        monkeypatch.delenv("CHAMELEON_EXCERPT_CACHE_CAP", raising=False)
    else:
        monkeypatch.setenv("CHAMELEON_EXCERPT_CACHE_CAP", cap)
    mod = importlib.reload(_ec_mod)
    mod.clear()
    return mod


@pytest.fixture(autouse=True)
def _isolate_excerpt_cache(tmp_path, monkeypatch):
    """Reset _CAP + _CACHE to defaults around every test."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("CHAMELEON_EXCERPT_CACHE_CAP", raising=False)
    mod = importlib.reload(_ec_mod)
    mod.clear()
    yield
    mod.clear()
    # Restore a clean module state for whatever imports it next.
    importlib.reload(_ec_mod)


def _putter(mod):
    """A get_or_build helper that records every build() invocation."""
    builds = []

    def put(key, value=None):
        if value is None:
            value = (f"v-{key}", False)

        def build():
            builds.append(key)
            return value

        return mod.get_or_build((key,), build)

    return put, builds


# ---------------------------------------------------------------------------
# module-level constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_context_transform_version_is_3(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path)
        assert mod.CONTEXT_TRANSFORM_VERSION == 3

    def test_default_cap_is_64(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path)
        assert mod._CAP == 64

    def test_cache_starts_empty(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path)
        assert len(mod._CACHE) == 0


# ---------------------------------------------------------------------------
# _resolve_cap parsing (the env override lives here, read at import)
# ---------------------------------------------------------------------------


class TestResolveCap:
    def test_unset_returns_64(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CHAMELEON_EXCERPT_CACHE_CAP", raising=False)
        assert _ec_mod._resolve_cap() == 64

    def test_positive_int(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_EXCERPT_CACHE_CAP", "5")
        assert _ec_mod._resolve_cap() == 5

    def test_large_positive_int(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_EXCERPT_CACHE_CAP", "1000")
        assert _ec_mod._resolve_cap() == 1000

    def test_zero_falls_back_to_64(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_EXCERPT_CACHE_CAP", "0")
        assert _ec_mod._resolve_cap() == 64

    def test_negative_falls_back_to_64(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_EXCERPT_CACHE_CAP", "-3")
        assert _ec_mod._resolve_cap() == 64

    def test_non_int_falls_back_to_64(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_EXCERPT_CACHE_CAP", "abc")
        assert _ec_mod._resolve_cap() == 64

    def test_float_string_falls_back_to_64(self, tmp_path, monkeypatch):
        # int("3.5") raises ValueError -> fallback, not truncation to 3.
        monkeypatch.setenv("CHAMELEON_EXCERPT_CACHE_CAP", "3.5")
        assert _ec_mod._resolve_cap() == 64

    def test_hex_string_falls_back_to_64(self, tmp_path, monkeypatch):
        # int("0x10") with default base 10 raises ValueError -> fallback.
        monkeypatch.setenv("CHAMELEON_EXCERPT_CACHE_CAP", "0x10")
        assert _ec_mod._resolve_cap() == 64

    def test_empty_string_falls_back_to_64(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_EXCERPT_CACHE_CAP", "")
        assert _ec_mod._resolve_cap() == 64

    def test_surrounding_whitespace_is_parsed(self, tmp_path, monkeypatch):
        # int() strips surrounding whitespace, so "  7  " -> 7.
        monkeypatch.setenv("CHAMELEON_EXCERPT_CACHE_CAP", "  7  ")
        assert _ec_mod._resolve_cap() == 7

    def test_env_override_applied_after_reload(self, tmp_path, monkeypatch):
        # The override is read at import time; reload re-evaluates _CAP.
        mod = _fresh(monkeypatch, tmp_path, cap="9")
        assert mod._CAP == 9


# ---------------------------------------------------------------------------
# get_or_build: hit / miss / build-once
# ---------------------------------------------------------------------------


class TestGetOrBuild:
    def test_miss_calls_build_and_returns_value(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path)
        put, builds = _putter(mod)
        assert put("a") == ("v-a", False)
        assert builds == ["a"]
        assert len(mod._CACHE) == 1

    def test_hit_does_not_rebuild(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path)
        put, builds = _putter(mod)
        put("a")
        # Second call with a different build() must NOT invoke build again,
        # and must return the originally cached value.
        again = mod.get_or_build(("a",), lambda: ("SHOULD_NOT_BUILD", True))
        assert again == ("v-a", False)
        assert builds == ["a"]  # build ran exactly once
        assert len(mod._CACHE) == 1

    def test_distinct_keys_each_build_once(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path)
        put, builds = _putter(mod)
        put("a")
        put("b")
        put("a")  # hit
        put("b")  # hit
        assert builds == ["a", "b"]
        assert len(mod._CACHE) == 2

    def test_returns_stored_object_identity(self, tmp_path, monkeypatch):
        # The cache returns the SAME tuple object it stored, not a copy.
        mod = _fresh(monkeypatch, tmp_path)
        stored = ("payload", False)
        mod.get_or_build(("id",), lambda: stored)
        got = mod.get_or_build(("id",), lambda: ("other", True))
        assert got is stored

    def test_truncated_flag_is_preserved(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path)
        first = mod.get_or_build(("t",), lambda: ("body", True))
        assert first == ("body", True)
        hit = mod.get_or_build(("t",), lambda: ("X", False))
        assert hit == ("body", True)

    def test_key_is_a_tuple_and_matched_by_value(self, tmp_path, monkeypatch):
        # Equal-by-value tuple keys collide on the cache; distinct tuples don't.
        mod = _fresh(monkeypatch, tmp_path)
        builds = []
        mod.get_or_build(("p", 1, 3), lambda: (builds.append("first") or "a", False))
        # A freshly constructed but equal tuple is a hit.
        hit = mod.get_or_build(("p", 1, 3), lambda: (builds.append("second") or "b", False))
        assert hit == ("a", False)
        # A different tuple is a miss.
        miss = mod.get_or_build(("p", 1, 4), lambda: (builds.append("third") or "c", False))
        assert miss == ("c", False)
        assert builds == ["first", "third"]


# ---------------------------------------------------------------------------
# eviction at capacity + LRU recency ordering
# ---------------------------------------------------------------------------


class TestEviction:
    def test_eviction_at_capacity_drops_oldest(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path, cap="3")
        put, _ = _putter(mod)
        put("a")
        put("b")
        put("c")
        assert list(mod._CACHE.keys()) == [("a",), ("b",), ("c",)]
        put("d")  # over cap -> evict LRU which is 'a'
        assert ("a",) not in mod._CACHE
        assert list(mod._CACHE.keys()) == [("b",), ("c",), ("d",)]
        assert len(mod._CACHE) == 3

    def test_hit_promotes_to_mru_and_protects_from_eviction(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path, cap="3")
        put, _ = _putter(mod)
        put("a")
        put("b")
        put("c")
        put("a")  # touch 'a' -> now MRU, 'b' becomes LRU
        assert list(mod._CACHE.keys()) == [("b",), ("c",), ("a",)]
        put("d")  # evict LRU 'b', NOT the recently-touched 'a'
        assert ("b",) not in mod._CACHE
        assert ("a",) in mod._CACHE
        assert list(mod._CACHE.keys()) == [("c",), ("a",), ("d",)]

    def test_size_never_exceeds_cap(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path, cap="2")
        put, _ = _putter(mod)
        put("x")
        put("y")
        put("z")  # one over-cap insert evicts exactly one
        assert len(mod._CACHE) == 2
        assert list(mod._CACHE.keys()) == [("y",), ("z",)]

    def test_cap_of_one_keeps_only_latest(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path, cap="1")
        put, _ = _putter(mod)
        put("a")
        put("b")
        assert list(mod._CACHE.keys()) == [("b",)]
        assert len(mod._CACHE) == 1

    def test_fill_to_exactly_cap_does_not_evict(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path, cap="64")
        put, _ = _putter(mod)
        for i in range(64):
            put(i)
        assert len(mod._CACHE) == 64
        assert ("0",) not in mod._CACHE  # keys are tuples of ints here
        assert (0,) in mod._CACHE
        assert (63,) in mod._CACHE

    def test_one_over_default_cap_evicts_first(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path)  # default 64
        put, _ = _putter(mod)
        for i in range(65):
            put(i)
        assert len(mod._CACHE) == 64
        assert (0,) not in mod._CACHE  # first inserted, evicted
        assert (1,) in mod._CACHE
        assert (64,) in mod._CACHE


# ---------------------------------------------------------------------------
# clear()
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_empties_the_cache(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path)
        put, _ = _putter(mod)
        put("a")
        put("b")
        assert len(mod._CACHE) == 2
        mod.clear()
        assert len(mod._CACHE) == 0

    def test_build_runs_again_after_clear(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path)
        put, builds = _putter(mod)
        put("a")
        mod.clear()
        put("a")  # miss again -> rebuild
        assert builds == ["a", "a"]

    def test_clear_on_empty_cache_is_noop(self, tmp_path, monkeypatch):
        mod = _fresh(monkeypatch, tmp_path)
        mod.clear()  # must not raise
        assert len(mod._CACHE) == 0
