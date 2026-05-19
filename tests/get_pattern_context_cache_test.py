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
        # Inject a writer race: between the fstat that builds the cache
        # key and the read inside _build, advance the witness's mtime
        # to a distinct value. The post-read re-fstat mitigation must
        # detect the advance and raise OSError so nothing is cached.
        # The fd-based read forces this test to patch _os.read instead
        # of pathlib.Path.read_text (the bytes come from the fd, not a
        # path lookup).
        from chameleon_mcp import _excerpt_cache
        from chameleon_mcp.tools import get_pattern_context

        witness = (self.repo / "src" / "components" / "Widget.tsx").resolve()
        # The witness block does `import os as _os` inside the try; the
        # _build closure reads via `_os.read(fd, n)`. Patch the os.read
        # at the os-module level since _os is the same module object.
        real_read = os.read

        def racey_read(fd, n):
            # Bump the on-disk mtime BEFORE the first read returns, so
            # the post-read re-fstat sees a different mtime.
            try:
                os.utime(witness, ns=(9_000_000_000, 9_000_000_000))
            except Exception:
                pass
            return real_read(fd, n)

        os.read = racey_read
        try:
            d = get_pattern_context(str(self.target))["data"]
        finally:
            os.read = real_read

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


class RepoIdMemoTest(unittest.TestCase):
    """Pin: _compute_repo_id is memoized so a single git config
    subprocess runs per repo_root per process lifetime. Surfaced by
    Lens D performance profiling — the subprocess was ~70% of warm
    get_pattern_context latency."""

    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        self.repo_a = Path(tempfile.mkdtemp())
        self.repo_b = Path(tempfile.mkdtemp())
        for r in (self.repo_a, self.repo_b):
            (r / "package.json").write_text("{}")
        from chameleon_mcp.tools import _compute_repo_id
        _compute_repo_id.cache_clear()

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from chameleon_mcp.tools import _compute_repo_id
        _compute_repo_id.cache_clear()

    def test_same_repo_root_is_memoized(self):
        # Count actual _git_remote_url calls — must run only once across
        # 20 _compute_repo_id calls on the same repo_root.
        from chameleon_mcp import tools as t
        real = t._git_remote_url
        calls = []

        def counter(r):
            calls.append(str(r))
            return real(r)

        t._git_remote_url = counter
        try:
            t._compute_repo_id.cache_clear()
            ids = [t._compute_repo_id(self.repo_a) for _ in range(20)]
        finally:
            t._git_remote_url = real
        self.assertEqual(len(set(ids)), 1, "all 20 calls must return the same id")
        self.assertEqual(len(calls), 1, "_git_remote_url must run only once")

    def test_distinct_repo_roots_get_distinct_ids(self):
        from chameleon_mcp.tools import _compute_repo_id
        _compute_repo_id.cache_clear()
        ida = _compute_repo_id(self.repo_a)
        idb = _compute_repo_id(self.repo_b)
        self.assertNotEqual(ida, idb)

    def test_cache_clear_forces_recompute(self):
        from chameleon_mcp import tools as t
        real = t._git_remote_url
        calls = []

        def counter(r):
            calls.append(1)
            return real(r)

        t._git_remote_url = counter
        try:
            t._compute_repo_id.cache_clear()
            t._compute_repo_id(self.repo_a)  # 1
            t._compute_repo_id(self.repo_a)  # cached
            t._compute_repo_id.cache_clear()
            t._compute_repo_id(self.repo_a)  # 2
        finally:
            t._git_remote_url = real
        self.assertEqual(len(calls), 2)

    def test_memoized_value_matches_uncached(self):
        from chameleon_mcp.tools import _compute_repo_id
        _compute_repo_id.cache_clear()
        first = _compute_repo_id(self.repo_a)
        # Force recompute and verify identical
        _compute_repo_id.cache_clear()
        second = _compute_repo_id(self.repo_a)
        self.assertEqual(first, second)


class ExcerptCacheFdSafetyTest(unittest.TestCase):
    """Pin the fd-based safe_open + enriched cache key closes both
    BUG-R2-001 mtime-preservation and BUG-R2-002 dirent-swap-leak."""

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
        from chameleon_mcp.tools import _compute_repo_id
        _excerpt_cache.clear()
        self.target = self.repo / "src" / "components" / "Other.tsx"
        self.target.write_text("export const Other = () => null;\n")
        # Warm the repo_id memoization. Without this, the os.read
        # monkeypatches below would fire on the subprocess pipe read
        # from `_git_remote_url` BEFORE the witness fd read, making the
        # "first read" guard useless. Warming hoists the subprocess
        # out of the timed window so the very next os.read in the
        # test is the witness chunk read.
        _compute_repo_id.cache_clear()
        _compute_repo_id(self.repo.resolve())

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_mtime_preserving_swap_does_not_poison(self):
        # Adversarial mtime-preservation residual: write attacker
        # content, then os.utime back to the original mtime. The new
        # code reads via _os.read(fd, ...). Patch os.read so the attack
        # fires before the first chunk; the post-read fstat sees a
        # different st_size or st_ctime_ns and fails open.
        from chameleon_mcp import _excerpt_cache
        from chameleon_mcp.tools import get_pattern_context

        witness = (self.repo / "src" / "components" / "Widget.tsx").resolve()
        orig_bytes = witness.read_bytes()
        orig_st = os.stat(witness)
        attacker = b"export const Widget = () => <div>POISONED_R3</div>;\n"
        real_read = os.read
        fired = []

        def racey_read(fd, n):
            # First read on this fd: stage the attack BEFORE returning
            # bytes. write_bytes truncates the witness inode and writes
            # the attacker bytes; os.utime then resets mtime to preserve
            # it. ctime is bumped by os.utime, size differs from orig.
            if not fired:
                fired.append(1)
                try:
                    witness.write_bytes(attacker)
                    os.utime(
                        witness,
                        ns=(orig_st.st_mtime_ns, orig_st.st_mtime_ns),
                    )
                except Exception:
                    pass
            return real_read(fd, n)

        os.read = racey_read
        try:
            d = get_pattern_context(str(self.target))["data"]
        finally:
            os.read = real_read
            # Restore witness byte-exact.
            witness.write_bytes(orig_bytes)
            os.utime(
                witness,
                ns=(orig_st.st_mtime_ns, orig_st.st_mtime_ns),
            )

        # Attack actually fired (the patch ran at least once).
        self.assertEqual(fired, [1])
        # No POISONED content reached the envelope.
        self.assertNotIn("POISONED_R3", d["canonical_excerpt"]["content"])
        # Nothing got cached (post-read fstat caught the size/ctime
        # divergence and raised OSError -> outer except -> empty
        # canonical_data -> get_or_build's exception path skips storage).
        self.assertEqual(len(_excerpt_cache._CACHE), 0)

    def test_dirent_swap_to_outside_repo_does_not_leak(self):
        # BUG-R2-002 closure: swap the witness dirent for a symlink
        # pointing OUT of the repo. With O_NOFOLLOW-opened fd + read-
        # from-fd, the read uses the already-opened inode, not the
        # swapped dirent. Even if the swap "succeeds" mid-read, the
        # decoy's bytes cannot enter the cache because the fd is bound
        # to the original inode.
        from chameleon_mcp import _excerpt_cache
        from chameleon_mcp.tools import get_pattern_context

        witness = (self.repo / "src" / "components" / "Widget.tsx").resolve()
        orig_bytes = witness.read_bytes()
        orig_st = os.stat(witness)
        decoy_dir = Path(tempfile.mkdtemp())
        decoy = decoy_dir / "leaked_secret.txt"
        decoy.write_text("OUT_OF_REPO_SECRET_R3_FIX\n")
        real_read = os.read
        fired = []

        def swap_read(fd, n):
            if not fired:
                fired.append(1)
                try:
                    witness.unlink()
                    witness.symlink_to(decoy)
                except Exception:
                    pass
            return real_read(fd, n)

        os.read = swap_read
        try:
            d = get_pattern_context(str(self.target))["data"]
        finally:
            os.read = real_read
            # Restore: undo any symlink, write original bytes, reset mtime.
            try:
                if witness.is_symlink():
                    witness.unlink()
            except Exception:
                pass
            witness.write_bytes(orig_bytes)
            os.utime(
                witness,
                ns=(orig_st.st_mtime_ns, orig_st.st_mtime_ns),
            )
            decoy.unlink(missing_ok=True)
            try:
                decoy_dir.rmdir()
            except OSError:
                pass

        # Attack actually fired.
        self.assertEqual(fired, [1])
        # Decoy content NEVER appears in the excerpt OR the cache.
        self.assertNotIn("OUT_OF_REPO_SECRET", d["canonical_excerpt"]["content"])
        for v in _excerpt_cache._CACHE.values():
            self.assertNotIn("OUT_OF_REPO_SECRET", v[0])

    def test_normal_case_still_caches_correctly(self):
        from chameleon_mcp import _excerpt_cache
        from chameleon_mcp.tools import get_pattern_context
        d1 = get_pattern_context(str(self.target))["data"]
        self.assertIn("ORIGINAL", d1["canonical_excerpt"]["content"])
        # Second call must be a warm hit.
        d2 = get_pattern_context(str(self.target))["data"]
        self.assertEqual(
            d1["canonical_excerpt"]["content"],
            d2["canonical_excerpt"]["content"],
        )
        self.assertEqual(len(_excerpt_cache._CACHE), 1)

    def test_cache_key_includes_inode_and_size(self):
        # Pin the key shape so a regression that loses st_ino or
        # st_size from the key fails this test.
        from chameleon_mcp import _excerpt_cache
        from chameleon_mcp.tools import get_pattern_context
        get_pattern_context(str(self.target))
        # Exactly one entry; its key tuple has 7 components.
        self.assertEqual(len(_excerpt_cache._CACHE), 1)
        key = next(iter(_excerpt_cache._CACHE))
        # (path_str, st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns, version)
        self.assertEqual(len(key), 7)
        self.assertIsInstance(key[0], str)
        for i in range(1, 7):
            self.assertIsInstance(key[i], int)


class ArchetypePathLocalityTest(unittest.TestCase):
    """When two archetypes share a paths_pattern and AST scoring
    cannot differentiate them, prefer the one whose canonical witness
    lives in a deeper subdir matching the query. Closes the
    cluster_size-only tiebreak gap that left some archetypes
    structurally unreachable (e.g. ef-api's service-plaid shadowed by
    service)."""

    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        self.repo = Path(tempfile.mkdtemp())
        from chameleon_mcp import _excerpt_cache
        _excerpt_cache.clear()

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _plant_two_archetypes_one_bucket(
        self,
        primary_name: str,
        primary_witness_rel: str,
        primary_cluster_size: int,
        secondary_name: str,
        secondary_witness_rel: str,
        secondary_cluster_size: int,
        paths_pattern: str,
    ) -> None:
        """Two archetypes sharing one paths_pattern, primary has higher
        cluster_size, secondary has a deeper-subdir witness. Both
        witnesses get a similar trivial body so AST scoring is the same
        for both (no normative_shape -> ratio = 0 on every candidate,
        falling through to the new tiebreak)."""
        (self.repo / "package.json").write_text("{}")
        for w in (primary_witness_rel, secondary_witness_rel):
            wpath = self.repo / w
            wpath.parent.mkdir(parents=True, exist_ok=True)
            wpath.write_text(f"# witness body for {w}\n")
        pd = self.repo / ".chameleon"
        pd.mkdir(parents=True, exist_ok=True)
        base = {
            "engine_min_version": "0.1.0",
            "generation": 1,
            "schema_version": 1,
        }
        (pd / "profile.json").write_text(json.dumps({**base, "language": "ruby"}))
        (pd / "archetypes.json").write_text(json.dumps({
            **base,
            "archetypes": {
                primary_name: {
                    "paths_pattern": paths_pattern,
                    "cluster_size": primary_cluster_size,
                },
                secondary_name: {
                    "paths_pattern": paths_pattern,
                    "cluster_size": secondary_cluster_size,
                },
            },
        }))
        (pd / "canonicals.json").write_text(json.dumps({
            **base,
            "canonicals": {
                primary_name: [{
                    "witness": {
                        "path": primary_witness_rel,
                        "sha_hint": "p",
                    },
                    "normative_shape": {"ast_query": {}},
                }],
                secondary_name: [{
                    "witness": {
                        "path": secondary_witness_rel,
                        "sha_hint": "s",
                    },
                    "normative_shape": {"ast_query": {}},
                }],
            },
        }))
        (pd / "rules.json").write_text(json.dumps({**base, "rules": {}}))
        (pd / "idioms.md").write_text("# idioms\n")
        (pd / "COMMITTED").write_text("c\n")

    def test_deeper_subdir_witness_wins_over_higher_cluster_size(self):
        # Realistic case modeled on ef-api: `service` (big cluster,
        # generic witness) vs `service-plaid` (small cluster, witness
        # in a deeper subdir).
        from chameleon_mcp.tools import _compute_repo_id, get_archetype
        _compute_repo_id.cache_clear()
        self._plant_two_archetypes_one_bucket(
            primary_name="service",
            primary_witness_rel="app/services/notifier.rb",
            primary_cluster_size=10,
            secondary_name="service-plaid",
            secondary_witness_rel="app/services/plaid/link.rb",
            secondary_cluster_size=2,
            paths_pattern="app/services",
        )
        # Query lives in app/services/plaid/ -> should resolve to
        # service-plaid, NOT service.
        target = self.repo / "app" / "services" / "plaid" / "transfer.rb"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# query file body\n")
        repo_id = _compute_repo_id(self.repo.resolve())
        r = get_archetype(repo_id, str(target))["data"]
        self.assertEqual(
            r["archetype"], "service-plaid",
            f"expected service-plaid; got {r['archetype']} (alternates: {r['alternatives']})",
        )

    def test_same_dir_witnesses_fall_back_to_cluster_size(self):
        # When both witnesses live in the SAME directory, path-locality
        # cannot differentiate -- primary (larger cluster) still wins.
        # This documents the resolver's limit; the bootstrap-level fix
        # (archetype collapsing for same-bucket-same-dir) is separate
        # work.
        from chameleon_mcp.tools import _compute_repo_id, get_archetype
        _compute_repo_id.cache_clear()
        self._plant_two_archetypes_one_bucket(
            primary_name="model",
            primary_witness_rel="app/models/user.rb",
            primary_cluster_size=10,
            secondary_name="model-models-rb",
            secondary_witness_rel="app/models/account.rb",
            secondary_cluster_size=2,
            paths_pattern="app/models",
        )
        target = self.repo / "app" / "models" / "profile.rb"
        target.write_text("# query body\n")
        repo_id = _compute_repo_id(self.repo.resolve())
        r = get_archetype(repo_id, str(target))["data"]
        # cluster_size tiebreak -> primary wins (documented limit).
        self.assertEqual(r["archetype"], "model")
        # secondary should be visible in alternates.
        self.assertIn("model-models-rb", r["alternatives"])

    def test_query_in_parent_dir_does_not_prefer_subdir_archetype(self):
        # Symmetric guard: a query in `app/services/X.rb` (parent dir of
        # the secondary's `app/services/plaid/Y.rb` witness) should NOT
        # be tricked into picking the deeper-subdir archetype -- the
        # query doesn't live there.
        from chameleon_mcp.tools import _compute_repo_id, get_archetype
        _compute_repo_id.cache_clear()
        self._plant_two_archetypes_one_bucket(
            primary_name="service",
            primary_witness_rel="app/services/notifier.rb",
            primary_cluster_size=10,
            secondary_name="service-plaid",
            secondary_witness_rel="app/services/plaid/link.rb",
            secondary_cluster_size=2,
            paths_pattern="app/services",
        )
        target = self.repo / "app" / "services" / "mailer.rb"
        target.write_text("# query in parent dir\n")
        repo_id = _compute_repo_id(self.repo.resolve())
        r = get_archetype(repo_id, str(target))["data"]
        # Path-locality should not promote service-plaid here because
        # the query doesn't live under app/services/plaid/.
        self.assertEqual(r["archetype"], "service")


class BootstrapCollapseTest(unittest.TestCase):
    """Pin the bootstrap-time archetype collapse: archetypes sharing
    a paths_pattern are merged into the largest-cluster one, with the
    smaller's canonicals retained as alternates."""

    def test_collapse_helper_merges_same_pattern(self):
        # Direct unit test against the helper. Same paths_pattern,
        # different cluster sizes → kept is the larger; smaller's
        # canonical entries appended; smaller's archetype entry dropped.
        # Use whatever the actual helper import path turns out to be —
        # adjust if needed.
        from chameleon_mcp.bootstrap.orchestrator import (
            _collapse_same_pattern_archetypes,
        )
        archetypes = {
            "model": {
                "paths_pattern": "app/models:rb",
                "cluster_size": 10,
            },
            "model-models-rb": {
                "paths_pattern": "app/models:rb",
                "cluster_size": 3,
            },
            "controller": {
                "paths_pattern": "app/controllers:rb",
                "cluster_size": 7,
            },
        }
        canonicals = {
            "model": [{"witness": {"path": "app/models/user.rb", "sha_hint": "p"}}],
            "model-models-rb": [{"witness": {"path": "app/models/account.rb", "sha_hint": "s"}}],
            "controller": [{"witness": {"path": "app/controllers/x.rb", "sha_hint": "c"}}],
        }
        new_archetypes, new_canonicals = _collapse_same_pattern_archetypes(
            archetypes, canonicals
        )
        # model-models-rb is gone from archetypes.
        self.assertNotIn("model-models-rb", new_archetypes)
        self.assertIn("model", new_archetypes)
        # Kept's cluster_size accumulates the merged sibling's count.
        self.assertEqual(new_archetypes["model"]["cluster_size"], 13)
        # Controller is untouched.
        self.assertEqual(new_archetypes["controller"]["cluster_size"], 7)
        # Canonicals for model now include both witnesses (kept first).
        kept_canonicals = new_canonicals["model"]
        self.assertEqual(len(kept_canonicals), 2)
        self.assertEqual(kept_canonicals[0]["witness"]["path"], "app/models/user.rb")
        self.assertEqual(kept_canonicals[1]["witness"]["path"], "app/models/account.rb")
        # The dropped archetype's canonicals are removed.
        self.assertNotIn("model-models-rb", new_canonicals)
        # Controller's canonicals are untouched.
        self.assertEqual(len(new_canonicals["controller"]), 1)

    def test_collapse_helper_handles_three_way_share(self):
        from chameleon_mcp.bootstrap.orchestrator import (
            _collapse_same_pattern_archetypes,
        )
        # Three archetypes share a pattern. Kept = highest cluster_size.
        archetypes = {
            "a": {"paths_pattern": "x", "cluster_size": 5},
            "b": {"paths_pattern": "x", "cluster_size": 10},
            "c": {"paths_pattern": "x", "cluster_size": 3},
        }
        canonicals = {
            "a": [{"witness": {"path": "p/a.rb", "sha_hint": "a"}}],
            "b": [{"witness": {"path": "p/b.rb", "sha_hint": "b"}}],
            "c": [{"witness": {"path": "p/c.rb", "sha_hint": "c"}}],
        }
        new_a, new_c = _collapse_same_pattern_archetypes(archetypes, canonicals)
        self.assertEqual(list(new_a), ["b"])  # only the winner
        self.assertEqual(new_a["b"]["cluster_size"], 18)
        # b's canonical first, then a's, then c's (sorted by source cluster_size desc).
        paths = [e["witness"]["path"] for e in new_c["b"]]
        self.assertEqual(paths, ["p/b.rb", "p/a.rb", "p/c.rb"])

    def test_collapse_helper_noop_when_all_unique(self):
        from chameleon_mcp.bootstrap.orchestrator import (
            _collapse_same_pattern_archetypes,
        )
        archetypes = {
            "a": {"paths_pattern": "x", "cluster_size": 5},
            "b": {"paths_pattern": "y", "cluster_size": 3},
        }
        canonicals = {
            "a": [{"witness": {"path": "x/a.rb"}}],
            "b": [{"witness": {"path": "y/b.rb"}}],
        }
        new_a, new_c = _collapse_same_pattern_archetypes(archetypes, canonicals)
        self.assertEqual(new_a, archetypes)
        self.assertEqual(new_c, canonicals)


class R4ConsistencyFixesTest(unittest.TestCase):
    """R4 real-world testing surfaced two consistency gaps:
    (1) empty-string file_path bypassed the M1/M2 slop guard,
    (2) NUL + C0 control bytes survived sanitization."""

    def test_empty_string_input_returns_no_repo_envelope(self):
        # Even from a CWD that is itself a profiled repo, an empty
        # file_path must return the documented no_repo envelope.
        # Pre-fix the M1/M2 guard only checked None + null-byte, so
        # Path("").expanduser() resolved to "." and find_repo_root
        # walked up CWD to find a profile.
        from chameleon_mcp.tools import get_pattern_context

        # Build a tmp profiled repo so we have a CWD with a profile.
        prev_env = {k: os.environ.get(k) for k in
                    ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        repo = Path(tempfile.mkdtemp())
        _write_profiled_repo(
            repo, "src/components/Widget.tsx",
            "export const Widget = () => null;\n",
        )
        prev_cwd = os.getcwd()
        os.chdir(repo)
        try:
            r = get_pattern_context("")
        finally:
            os.chdir(prev_cwd)
            for k, v in prev_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(r["data"]["repo"]["profile_status"], "no_repo")
        self.assertIsNone(r["data"]["repo"]["id"])

    def test_sanitizer_strips_nul_and_c0_controls(self):
        from chameleon_mcp.sanitization import (
            sanitize_for_chameleon_context,
        )
        # NUL (0x00), SOH (0x01), STX (0x02), VT (0x0b), FF (0x0c),
        # SO (0x0e), US (0x1f) all get stripped. Whitespace tab/LF/CR
        # is preserved.
        raw = "a\x00b\x01c\x02d\x0be\x0cf\x0eg\x1fh\ti\nj\rk"
        out = sanitize_for_chameleon_context(raw)
        # All C0 controls except whitespace must be gone:
        for c in ("\x00", "\x01", "\x02", "\x0b", "\x0c", "\x0e", "\x1f"):
            self.assertNotIn(c, out, f"unexpected C0 char {c!r} in output")
        # Whitespace preserved:
        self.assertIn("\t", out)
        self.assertIn("\n", out)
        self.assertIn("\r", out)
        # Plain content preserved:
        self.assertIn("abcdefghi", out.replace("\t", "").replace("\n", "").replace("\r", ""))

    def test_context_transform_version_bumped(self):
        # The sanitizer change is cache-visible; the version MUST be
        # bumped so a daemon that warms a cache pre-upgrade rebuilds
        # post-upgrade.
        from chameleon_mcp import _excerpt_cache
        self.assertGreaterEqual(_excerpt_cache.CONTEXT_TRANSFORM_VERSION, 2)


class R6TransactionCleanupTest(unittest.TestCase):
    """Bootstrap must not leak `..{name}.rename.lock` or `..{name}.tmp/`
    siblings into the repo root. R6 from-scratch lifecycle surfaced this
    on both ef-client and ef-api: every user with a chameleon profile
    saw two untracked artifacts in `git status`."""

    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        # A throwaway repo to bootstrap into; we don't care about the
        # produced profile, only the sibling artifacts.
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / "package.json").write_text("{}")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "App.tsx").write_text(
            "export const App = () => <div>hi</div>;\n"
        )

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_bootstrap_does_not_leak_rename_lock(self):
        from chameleon_mcp.tools import bootstrap_repo
        r = bootstrap_repo(str(self.repo))
        self.assertEqual(r["data"]["status"], "success")
        leaked = self.repo / "..chameleon.rename.lock"
        self.assertFalse(
            leaked.exists(),
            f"bootstrap leaked rename-lock artifact: {leaked}",
        )

    def test_bootstrap_does_not_leak_tmp_dir(self):
        from chameleon_mcp.tools import bootstrap_repo
        r = bootstrap_repo(str(self.repo))
        self.assertEqual(r["data"]["status"], "success")
        leaked = self.repo / "..chameleon.tmp"
        self.assertFalse(
            leaked.exists(),
            f"bootstrap leaked tmp-root artifact: {leaked}",
        )

    def test_bootstrap_leaves_only_dot_chameleon(self):
        # Belt-and-suspenders: the only chameleon-related sibling in the
        # repo root after a bootstrap must be `.chameleon/` itself.
        from chameleon_mcp.tools import bootstrap_repo
        bootstrap_repo(str(self.repo))
        suspects = [
            p for p in self.repo.iterdir()
            if "chameleon" in p.name and p.name != ".chameleon"
        ]
        self.assertEqual(
            suspects, [],
            f"unexpected chameleon-named siblings: {suspects}",
        )


class R8SymlinkedBackupTest(unittest.TestCase):
    """If .chameleon/ is a symlink to external storage, atomic bootstrap
    must NOT leak `..chameleon.backup-<...>` (a dangling symlink) in
    the repo root. Found by R8 BB3."""

    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / "package.json").write_text("{}")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "App.tsx").write_text(
            "export const App = () => null;\n"
        )

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_bootstrap_does_not_leak_backup_when_chameleon_is_symlinked(self):
        # Setup: .chameleon/ is a symlink to external storage.
        external = Path(tempfile.mkdtemp()) / "shared"
        external.mkdir()
        (self.repo / ".chameleon").symlink_to(external)

        from chameleon_mcp.tools import bootstrap_repo
        r = bootstrap_repo(str(self.repo))
        self.assertEqual(r["data"]["status"], "success")

        # Repo root must NOT contain any `..chameleon.backup-*` artifact.
        leaks = sorted(
            p.name for p in self.repo.iterdir()
            if p.name.startswith("..chameleon.backup-")
        )
        self.assertEqual(
            leaks, [],
            f"bootstrap leaked symlinked backup: {leaks}",
        )

    def test_regular_chameleon_dir_still_cleaned_up(self):
        # Regression guard: with a REGULAR .chameleon/ (not symlinked),
        # the existing rmtree cleanup still works (no `..chameleon.backup-*`
        # leak, no `..chameleon.tmp/` leak).
        from chameleon_mcp.tools import bootstrap_repo
        # First bootstrap creates the profile.
        bootstrap_repo(str(self.repo))
        # Re-bootstrap with force=True triggers the backup/rename/cleanup path.
        r = bootstrap_repo(str(self.repo), force=True)
        self.assertEqual(r["data"]["status"], "success")
        suspects = sorted(
            p.name for p in self.repo.iterdir()
            if "chameleon" in p.name and p.name != ".chameleon"
        )
        self.assertEqual(
            suspects, [],
            f"unexpected chameleon-related siblings: {suspects}",
        )


class R9MCPSlopConsistencyTest(unittest.TestCase):
    """All path-accepting MCP tools must return graceful envelopes
    on slop input (non-str, empty, null-byte, overlong). Found by
    R9 BUG-R9-001/002 + ef-api Bug 1/2."""

    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # detect_repo
    def test_detect_repo_null_byte_is_graceful(self):
        from chameleon_mcp.tools import detect_repo
        r = detect_repo("/some/path/with\x00null.tsx")
        self.assertIn("data", r)  # graceful envelope, not a raise
        # Shape: detect_repo returns its standard no_repo / failure envelope.

    def test_detect_repo_empty_does_not_leak_cwd(self):
        from chameleon_mcp.tools import detect_repo
        r = detect_repo("")
        # Must NOT return the server's CWD repo data.
        self.assertIn("data", r)
        # The data field should NOT contain a real repo_root path.
        # Either profile_status=no_repo OR repo_root is null/empty.
        if "repo_root" in r["data"]:
            self.assertFalse(
                r["data"]["repo_root"],
                f"detect_repo('') leaked repo_root: {r['data']['repo_root']}",
            )

    def test_detect_repo_overlong_is_graceful(self):
        from chameleon_mcp.tools import detect_repo
        r = detect_repo("a" * 5000)
        self.assertIn("data", r)  # no raise

    # get_archetype
    def test_get_archetype_null_byte_file_path_is_graceful(self):
        from chameleon_mcp.tools import get_archetype
        r = get_archetype(
            "0" * 64,  # fake hex
            "/some/path/with\x00null.tsx",
        )
        self.assertIn("data", r)

    def test_get_archetype_accepts_path_form_repo(self):
        # Bug 4: passing the absolute repo path (not hex repo_id) was
        # silently returning archetype: null. After fix, behavior must
        # match passing the hex.
        from chameleon_mcp.tools import (
            _compute_repo_id,
            bootstrap_repo,
            get_archetype,
        )

        repo = Path(tempfile.mkdtemp())
        (repo / "package.json").write_text("{}")
        (repo / "src" / "components").mkdir(parents=True)
        # Need 2+ similar files to produce a real archetype after
        # clustering; a single file collapses to no clusters.
        for i in range(3):
            (repo / "src" / "components" / f"Comp{i}.tsx").write_text(
                f"export const C{i} = () => 1;\n"
            )
        bootstrap_repo(str(repo))
        _compute_repo_id.cache_clear()
        target = str(repo / "src" / "components" / "Comp0.tsx")
        repo_id = _compute_repo_id(repo.resolve())
        # Hex form (current contract)
        hex_result = get_archetype(repo_id, target)["data"]
        # Sanity: this setup must produce a non-None archetype so the
        # equality below is meaningful (not None == None).
        self.assertIsNotNone(
            hex_result["archetype"],
            "test setup failure: bootstrap produced no archetype",
        )
        # Path form (the bug case)
        path_result = get_archetype(str(repo), target)["data"]
        # Both should resolve to the SAME archetype (the new helper
        # routes path → hex; not_found case stays consistent).
        self.assertEqual(
            hex_result["archetype"], path_result["archetype"],
            f"path-form repo gave different archetype: hex={hex_result['archetype']!r} path={path_result['archetype']!r}",
        )

    # get_pattern_context — length cap (already had null-byte etc.)
    def test_get_pattern_context_overlong_is_graceful(self):
        from chameleon_mcp.tools import get_pattern_context
        r = get_pattern_context("x" * 8192)
        self.assertIn("data", r)
        self.assertEqual(r["data"]["repo"]["profile_status"], "no_repo")

    # lint_file
    def test_lint_file_wrong_content_type_is_graceful(self):
        from chameleon_mcp.tools import lint_file
        # int instead of str for content
        r = lint_file("0" * 64, "fake-arch", 12345)
        self.assertIn("data", r)  # no TypeError

    # bootstrap_repo / refresh_repo
    def test_bootstrap_repo_null_byte_is_graceful(self):
        from chameleon_mcp.tools import bootstrap_repo
        r = bootstrap_repo("/tmp/with\x00null/repo")
        self.assertIn("data", r)
        self.assertEqual(r["data"].get("status"), "failed")

    def test_refresh_repo_null_byte_is_graceful(self):
        from chameleon_mcp.tools import refresh_repo
        r = refresh_repo("/tmp/with\x00null/repo")
        self.assertIn("data", r)
        self.assertIn("status", r["data"])


class R10DaemonBacklogTest(unittest.TestCase):
    """R10 found ECONNREFUSED on 100 parallel connects with backlog=16
    (79/100 and 83/100 failures across ef-client + ef-api agents).
    Single-threaded daemon plus parallel-agent flows need a larger
    listen queue. Guard the constant so a future "optimization" can't
    regress it."""

    def test_listen_backlog_at_least_128(self):
        from chameleon_mcp.daemon import _LISTEN_BACKLOG
        self.assertGreaterEqual(
            _LISTEN_BACKLOG, 128,
            "Backlog must be at least 128 to absorb parallel-agent bursts; "
            "R10 reproduced ECONNREFUSED at backlog=16 under 100-parallel load."
        )


class R10IdiomsFileCapTest(unittest.TestCase):
    """R10 found idioms.md grows unbounded: the 50KB per-call cap doesn't
    stop sustained drift. After ~200 small teaches the file passed 100KB
    while the envelope cap of 8000 chars meant nothing past the first ~80
    idioms was ever consumed. Enforce a 200KB cumulative cap with a clear
    error pointing at /chameleon-refresh or manual trim."""

    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        self.repo = Path(tempfile.mkdtemp())
        _write_profiled_repo(self.repo, "src/components/Widget.tsx", "export const X = 1;")

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_small_teach_still_accepted(self):
        """Sanity: pre-cap teaches must succeed."""
        from chameleon_mcp.tools import teach_profile
        r = teach_profile(str(self.repo), "small idiom about widgets")
        self.assertEqual(r["data"]["status"], "success", f"unexpected: {r}")

    def test_cumulative_growth_past_cap_is_rejected(self):
        from chameleon_mcp.tools import teach_profile
        # Pre-fill idioms.md to just under the cap with 4 large-but-legal
        # teaches (49KB each, well under the 50KB per-call cap). After 4
        # teaches idioms.md is ~196KB; the next 49KB teach pushes total
        # past the 200KB cap and must be rejected.
        large = "x" * 49_000
        for i in range(4):
            r = teach_profile(str(self.repo), large)
            self.assertEqual(
                r["data"]["status"], "success",
                f"pre-cap teach #{i} unexpectedly failed: {r['data']}"
            )
        r = teach_profile(str(self.repo), large)
        self.assertEqual(r["data"]["status"], "failed", f"unexpected: {r['data']}")
        err = r["data"].get("error", "")
        self.assertIn("cumulative cap", err)
        self.assertIn("/chameleon-refresh", err)

    def test_rejected_teach_does_not_grow_file(self):
        from chameleon_mcp.tools import teach_profile
        large = "y" * 49_000
        for _ in range(4):
            teach_profile(str(self.repo), large)
        idioms_path = self.repo / ".chameleon" / "idioms.md"
        size_before = idioms_path.stat().st_size
        r = teach_profile(str(self.repo), large)
        self.assertEqual(r["data"]["status"], "failed")
        size_after = idioms_path.stat().st_size
        self.assertEqual(
            size_before, size_after,
            "rejected teach must leave idioms.md byte-identical"
        )


class GetRulesPathFormTest(unittest.TestCase):
    """get_rules previously only accepted hex repo_id via
    _resolve_repo_root_by_id; passing an absolute path silently returned
    {rules: []} even though get_pattern_context surfaced the same rules
    through its envelope. User reported the discrepancy: rules visible in
    get_pattern_context output, get_rules returns empty. Same fix shape
    as v0.5.2 Bug 5 (get_canonical_excerpt) and v0.5.10 (get_archetype):
    switch to _resolve_repo_arg to accept both forms."""

    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_ALLOW_TMP_REPO")}
        os.environ["CHAMELEON_PLUGIN_DATA"] = tempfile.mkdtemp()
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        self.repo = Path(tempfile.mkdtemp())
        _write_profiled_repo(self.repo, "src/components/Widget.tsx",
                             "export const X = 1;")
        pd = self.repo / ".chameleon"
        base = {"engine_min_version": "0.1.0", "generation": 1, "schema_version": 1}
        rules = {
            **base,
            "rules": {
                "widget": [
                    {"id": "use-fc", "severity": "warn", "msg": "prefer FC"},
                    {"id": "no-default-export", "severity": "error",
                     "msg": "named exports only"},
                ],
            },
        }
        (pd / "rules.json").write_text(json.dumps(rules))

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_path_form_returns_non_empty_rules(self):
        from chameleon_mcp.tools import get_rules
        r = get_rules(str(self.repo))
        rules = r["data"]["rules"]
        self.assertEqual(
            len(rules), 1,
            f"expected one archetype rule set with path-form repo, got: {rules}"
        )
        arch_name, entries = rules[0]
        self.assertEqual(arch_name, "widget")
        self.assertEqual(len(entries), 2)

    def test_path_form_with_archetype_filter(self):
        from chameleon_mcp.tools import get_rules
        r = get_rules(str(self.repo), archetype="widget")
        rules = r["data"]["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0][0], "widget")

    def test_path_form_filter_for_unknown_archetype_is_empty(self):
        from chameleon_mcp.tools import get_rules
        r = get_rules(str(self.repo), archetype="nonexistent")
        self.assertEqual(r["data"]["rules"], [])

    def test_nonexistent_path_still_returns_empty(self):
        from chameleon_mcp.tools import get_rules
        r = get_rules("/no/such/dir/at/all")
        self.assertEqual(r["data"]["rules"], [])


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print(
        f"\nSummary: {_result.testsRun} run, "
        f"{len(_result.failures)} failed, {len(_result.errors)} errored"
    )
    sys.exit(0 if _result.wasSuccessful() else 1)
