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


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print(
        f"\nSummary: {_result.testsRun} run, "
        f"{len(_result.failures)} failed, {len(_result.errors)} errored"
    )
    sys.exit(0 if _result.wasSuccessful() else 1)
