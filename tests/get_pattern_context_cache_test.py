"""Tests for get_pattern_context dedup refactor (#0) + excerpt cache (#1).

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/get_pattern_context_cache_test.py
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


def _write_profiled_repo(repo: Path, witness_rel: str, witness_body: str) -> None:
    """Plant a minimal committed profile whose single archetype's canonical
    witness points at `witness_rel` (a real file we also create)."""
    (repo / "package.json").write_text("{}")
    wpath = repo / witness_rel
    wpath.parent.mkdir(parents=True, exist_ok=True)
    wpath.write_text(witness_body)

    pd = repo / ".chameleon"
    pd.mkdir(parents=True, exist_ok=True)
    base = {"engine_min_version": "0.1.0", "generation": 1, "schema_version": 1}
    bucket = "src/components"
    (pd / "profile.json").write_text(json.dumps({**base, "language": "typescript"}))
    (pd / "archetypes.json").write_text(json.dumps({
        **base,
        "archetypes": {"widget": {"paths_pattern": bucket, "cluster_size": 1}},
    }))
    (pd / "canonicals.json").write_text(json.dumps({
        **base,
        "canonicals": {"widget": [{
            "witness": {"path": witness_rel, "sha_hint": "deadbeef"},
            "normative_shape": {"ast_query": {}},
        }]},
    }))
    (pd / "rules.json").write_text(json.dumps({**base, "rules": {}}))
    (pd / "idioms.md").write_text("# idioms\n\n## active\n\n## deprecated\n")
    (pd / "COMMITTED").write_text("committed-at: 2026-01-01T00:00:00Z\n")


class DedupRefactorTest(unittest.TestCase):
    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        self.repo = Path(tempfile.mkdtemp())
        _write_profiled_repo(
            self.repo, "src/components/Widget.tsx",
            "export const Widget = () => <div>hi</div>;\n",
        )
        from chameleon_mcp import _excerpt_cache
        _excerpt_cache.clear()

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_content_signal_helper_matches_inline_logic(self):
        from chameleon_mcp.tools import _content_signal_for_path
        f = self.repo / "src" / "components" / "Widget.tsx"
        self.assertEqual(_content_signal_for_path(f), "none")
        missing = self.repo / "nope.tsx"
        self.assertEqual(_content_signal_for_path(missing), "none")


class ArchetypeReuseTest(unittest.TestCase):
    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        self.repo = Path(tempfile.mkdtemp())
        _write_profiled_repo(
            self.repo, "src/components/Widget.tsx",
            "export const Widget = () => <div>hi</div>;\n",
        )
        from chameleon_mcp import _excerpt_cache
        _excerpt_cache.clear()

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_public_get_archetype_contract_unchanged(self):
        from chameleon_mcp.tools import _compute_repo_id, get_archetype
        target = self.repo / "src" / "components" / "Other.tsx"
        target.write_text("export const Other = () => null;\n")
        repo_id = _compute_repo_id(self.repo.resolve())
        r = get_archetype(repo_id, str(target))["data"]
        self.assertEqual(
            set(r.keys()),
            {"archetype", "alternatives", "content_signal_match", "confidence_band"},
        )
        self.assertEqual(r["archetype"], "widget")

    def test_get_pattern_context_resolves_archetype_and_excerpt(self):
        from chameleon_mcp.tools import get_pattern_context
        target = self.repo / "src" / "components" / "Other.tsx"
        target.write_text("export const Other = () => null;\n")
        d = get_pattern_context(str(target))["data"]
        self.assertEqual(d["archetype"]["archetype"], "widget")
        self.assertIn("hi", d["canonical_excerpt"]["content"])
        self.assertEqual(d["repo"]["trust_state"], "untrusted")

    def test_corrupt_profile_json_still_profile_corrupted(self):
        # Probe deletion regression guard: a corrupt profile.json must still
        # yield profile_corrupted via the load_profile_dir except handler.
        (self.repo / ".chameleon" / "profile.json").write_text("{ not json")
        from chameleon_mcp.tools import get_pattern_context
        target = self.repo / "src" / "components" / "Other.tsx"
        target.write_text("x\n")
        d = get_pattern_context(str(target))["data"]
        self.assertEqual(d["repo"]["profile_status"], "profile_corrupted")


class ExcerptCacheModuleTest(unittest.TestCase):
    def setUp(self):
        from chameleon_mcp import _excerpt_cache
        _excerpt_cache.clear()

    def test_get_miss_then_hit(self):
        from chameleon_mcp import _excerpt_cache
        calls = []

        def build():
            calls.append(1)
            return ("SANITIZED", False)

        k = ("/abs/Witness.tsx", 12345, _excerpt_cache.CONTEXT_TRANSFORM_VERSION)
        self.assertEqual(_excerpt_cache.get_or_build(k, build), ("SANITIZED", False))
        self.assertEqual(_excerpt_cache.get_or_build(k, build), ("SANITIZED", False))
        self.assertEqual(len(calls), 1, "second call must be a cache hit")

    def test_distinct_keys_are_independent(self):
        from chameleon_mcp import _excerpt_cache
        a = _excerpt_cache.get_or_build(("a", 1, 1), lambda: ("A", False))
        b = _excerpt_cache.get_or_build(("b", 1, 1), lambda: ("B", True))
        self.assertEqual(a, ("A", False))
        self.assertEqual(b, ("B", True))

    def test_lru_eviction_at_cap(self):
        from chameleon_mcp import _excerpt_cache
        cap = _excerpt_cache._CAP
        for i in range(cap + 5):
            _excerpt_cache.get_or_build((f"k{i}", 0, 1), lambda i=i: (str(i), False))
        # Oldest (k0) evicted; rebuild must run again.
        rebuilt = []
        _excerpt_cache.get_or_build(
            ("k0", 0, 1), lambda: rebuilt.append(1) or ("0", False)
        )
        self.assertEqual(rebuilt, [1])

    def test_clear_empties_cache(self):
        from chameleon_mcp import _excerpt_cache
        _excerpt_cache.get_or_build(("x", 0, 1), lambda: ("X", False))
        _excerpt_cache.clear()
        n = []
        _excerpt_cache.get_or_build(("x", 0, 1), lambda: n.append(1) or ("X", False))
        self.assertEqual(n, [1])

    def test_lru_recency_on_hit_protects_touched_key(self):
        # Pins move_to_end-on-hit: a pure FIFO (no recency) would evict r0
        # here and fail this test.
        from chameleon_mcp import _excerpt_cache
        cap = _excerpt_cache._CAP
        for i in range(cap):  # fill exactly to cap: r0(oldest)..r{cap-1}
            _excerpt_cache.get_or_build(
                (f"r{i}", 0, 1), lambda i=i: (str(i), False)
            )
        # Touch r0 -> hit -> move_to_end makes it most-recent; r1 is now oldest.
        _excerpt_cache.get_or_build(("r0", 0, 1), lambda: ("UNUSED", False))
        # Overflow by one -> evicts the current oldest (r1), NOT r0.
        _excerpt_cache.get_or_build(("rNEW", 0, 1), lambda: ("new", False))
        # r0 must still be cached (recency protected it): build must NOT run.
        r0 = []
        v0 = _excerpt_cache.get_or_build(
            ("r0", 0, 1), lambda: r0.append(1) or ("X", False)
        )
        self.assertEqual(r0, [], "r0 was touched; LRU recency must protect it")
        self.assertEqual(v0, ("0", False))
        # r1 must have been evicted (it became oldest after r0's touch).
        r1 = []
        _excerpt_cache.get_or_build(
            ("r1", 0, 1), lambda: r1.append(1) or ("r1b", False)
        )
        self.assertEqual(r1, [1], "r1 was the oldest; it must have been evicted")


class ExcerptCacheIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        self.repo = Path(tempfile.mkdtemp())
        _write_profiled_repo(
            self.repo, "src/components/Widget.tsx",
            "export const Widget = () => <div>ORIGINAL</div>;\n",
        )
        from chameleon_mcp import _excerpt_cache
        _excerpt_cache.clear()
        self.target = self.repo / "src" / "components" / "Other.tsx"
        self.target.write_text("export const Other = () => null;\n")

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_repeated_call_hits_cache(self):
        from chameleon_mcp import _excerpt_cache
        from chameleon_mcp.tools import get_pattern_context
        get_pattern_context(str(self.target))
        sentinel = {"built": 0}
        real = _excerpt_cache.get_or_build

        def spy(key, build):
            def wrapped():
                sentinel["built"] += 1
                return build()
            return real(key, wrapped)

        _excerpt_cache.get_or_build = spy
        try:
            d = get_pattern_context(str(self.target))["data"]
        finally:
            _excerpt_cache.get_or_build = real
        self.assertIn("ORIGINAL", d["canonical_excerpt"]["content"])
        self.assertEqual(sentinel["built"], 0, "witness re-read despite warm cache")

    def test_in_place_witness_edit_busts_cache(self):
        # The bug the original mtime_token design would have shipped.
        from chameleon_mcp.tools import get_pattern_context
        w = self.repo / "src" / "components" / "Widget.tsx"
        d1 = get_pattern_context(str(self.target))["data"]
        self.assertIn("ORIGINAL", d1["canonical_excerpt"]["content"])
        w.write_text("export const Widget = () => <div>EDITED</div>;\n")
        os.utime(w, ns=(2_000_000_000, 2_000_000_000))  # distinct mtime
        d2 = get_pattern_context(str(self.target))["data"]
        self.assertIn("EDITED", d2["canonical_excerpt"]["content"])
        self.assertNotIn("ORIGINAL", d2["canonical_excerpt"]["content"])

    def test_transform_version_bump_busts_cache(self):
        from chameleon_mcp import _excerpt_cache
        from chameleon_mcp.tools import get_pattern_context
        get_pattern_context(str(self.target))
        _excerpt_cache.CONTEXT_TRANSFORM_VERSION += 1
        try:
            rebuilt = []
            real = _excerpt_cache.get_or_build

            def spy(key, build):
                def wrapped():
                    rebuilt.append(1)
                    return build()
                return real(key, wrapped)

            _excerpt_cache.get_or_build = spy
            try:
                get_pattern_context(str(self.target))
            finally:
                _excerpt_cache.get_or_build = real
            self.assertEqual(rebuilt, [1], "version bump must force rebuild")
        finally:
            _excerpt_cache.CONTEXT_TRANSFORM_VERSION -= 1


class SlopInputTest(unittest.TestCase):
    """get_pattern_context must return a graceful no_repo envelope for
    slop inputs (None, null-byte string) — matching the documented
    fail-open contract used for paths outside any repo. Surfaced by
    Round-1 real-world testing on ef-api."""

    def test_none_input_returns_no_repo_envelope(self):
        from chameleon_mcp.tools import get_pattern_context
        r = get_pattern_context(None)
        self.assertIn("data", r)
        self.assertEqual(r["data"]["repo"]["profile_status"], "no_repo")
        self.assertIsNone(r["data"]["repo"]["id"])

    def test_null_byte_input_returns_no_repo_envelope(self):
        from chameleon_mcp.tools import get_pattern_context
        r = get_pattern_context("/some/path/with\x00null.tsx")
        self.assertIn("data", r)
        self.assertEqual(r["data"]["repo"]["profile_status"], "no_repo")
        self.assertIsNone(r["data"]["repo"]["id"])

    def test_empty_string_still_returns_envelope(self):
        # Regression guard: the existing "" handling must not change.
        from chameleon_mcp.tools import get_pattern_context
        r = get_pattern_context("")
        self.assertIn("data", r)
        # The existing behavior returns no_profile (cwd may resolve to a
        # repo without a chameleon profile) OR no_repo. Either is the
        # documented graceful envelope — both must hold.
        self.assertIn(
            r["data"]["repo"]["profile_status"],
            {"no_repo", "no_profile"},
        )


class ExcerptCacheRaceMitigationTest(unittest.TestCase):
    """Pin the post-read re-stat mitigation: if a writer races and
    advances the witness mtime between safe_open and read_text, the
    cache MUST fail open to empty canonical_data, never store a
    key/content-mismatched entry."""

    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        self.repo = Path(tempfile.mkdtemp())
        _write_profiled_repo(
            self.repo, "src/components/Widget.tsx",
            "export const Widget = () => <div>ORIGINAL</div>;\n",
        )
        from chameleon_mcp import _excerpt_cache
        _excerpt_cache.clear()
        self.target = self.repo / "src" / "components" / "Other.tsx"
        self.target.write_text("export const Other = () => null;\n")

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_mtime_advance_mid_read_fails_open_not_poison(self):
        # Inject a writer race: between the stat that builds the cache
        # key and the read_text inside _build, advance the witness's
        # mtime to a distinct value. The mitigation must detect the
        # advance and raise OSError so nothing is cached.
        import pathlib
        from chameleon_mcp import _excerpt_cache
        from chameleon_mcp.tools import get_pattern_context

        witness = (self.repo / "src" / "components" / "Widget.tsx").resolve()
        real_read = pathlib.Path.read_text

        def racey_read(self_, *a, **kw):
            # Bump the on-disk mtime BEFORE the read returns, so the
            # post-read re-stat sees a different mtime.
            try:
                if self_.resolve() == witness:
                    os.utime(witness, ns=(9_000_000_000, 9_000_000_000))
            except Exception:
                pass
            return real_read(self_, *a, **kw)

        pathlib.Path.read_text = racey_read
        try:
            d = get_pattern_context(str(self.target))["data"]
        finally:
            pathlib.Path.read_text = real_read

        # Mitigation: fail open to empty canonical_data, NOT a poisoned
        # cache entry.
        self.assertEqual(d["canonical_excerpt"]["content"], "")
        self.assertIsNone(d["canonical_excerpt"]["witness_path"])
        # No stale entry stored.
        self.assertEqual(len(_excerpt_cache._CACHE), 0)

    def test_normal_no_race_still_caches(self):
        # Regression guard: in the no-race case, the cache still
        # populates and returns the correct excerpt.
        from chameleon_mcp import _excerpt_cache
        from chameleon_mcp.tools import get_pattern_context
        d = get_pattern_context(str(self.target))["data"]
        self.assertIn("ORIGINAL", d["canonical_excerpt"]["content"])
        self.assertEqual(len(_excerpt_cache._CACHE), 1)


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print(
        f"\nSummary: {_result.testsRun} run, "
        f"{len(_result.failures)} failed, {len(_result.errors)} errored"
    )
    sys.exit(0 if _result.wasSuccessful() else 1)
