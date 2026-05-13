"""Verify the schema_version-too-high refusal path.

A planted profile with schema_version greater than the loader's
MAX_SUPPORTED_SCHEMA_VERSION must fail closed with a clear surface,
not a confusing crash deep in the loader.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from chameleon_mcp.profile.loader import (
    MAX_SUPPORTED_SCHEMA_VERSION,
    ProfileLoadError,
    load_profile_dir,
)
from chameleon_mcp.tools import detect_repo, get_pattern_context


def _write_committed_profile(profile_dir: Path, schema_version: int) -> None:
    """Write a minimal-but-coherent profile artifact set with the given
    schema_version into profile_dir."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    base = {
        "engine_min_version": "0.1.0",
        "generation": 1,
        "schema_version": schema_version,
    }
    (profile_dir / "profile.json").write_text(json.dumps(base))
    (profile_dir / "archetypes.json").write_text(json.dumps({**base, "archetypes": {}}))
    (profile_dir / "canonicals.json").write_text(json.dumps({**base, "canonicals": {}}))
    (profile_dir / "rules.json").write_text(json.dumps({**base, "rules": {}}))
    (profile_dir / "idioms.md").write_text("")
    (profile_dir / "COMMITTED").write_text("committed-at: 2026-01-01T00:00:00Z\n")


class SchemaVersionTooHighTest(unittest.TestCase):
    def setUp(self):
        self._prev_data = os.environ.get("CHAMELEON_PLUGIN_DATA")
        self._prev_allow_tmp = os.environ.get("CHAMELEON_ALLOW_TMP_REPO")
        self._tmp = tempfile.mkdtemp()
        os.environ["CHAMELEON_PLUGIN_DATA"] = self._tmp
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        # Build a fake repo with a too-new profile
        self._repo = Path(tempfile.mkdtemp())
        # Make it pass find_repo_root: needs a repo marker
        (self._repo / "package.json").write_text("{}")
        # Plant a profile with schema_version above the cap
        _write_committed_profile(self._repo / ".chameleon", schema_version=MAX_SUPPORTED_SCHEMA_VERSION + 50)

    def tearDown(self):
        for k, v in [("CHAMELEON_PLUGIN_DATA", self._prev_data), ("CHAMELEON_ALLOW_TMP_REPO", self._prev_allow_tmp)]:
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_loader_refuses_too_new_schema(self):
        with self.assertRaises(ProfileLoadError) as ctx:
            load_profile_dir(self._repo / ".chameleon")
        self.assertIn("schema_version", str(ctx.exception).lower())

    def test_detect_repo_surfaces_unsupported(self):
        # detect_repo takes a file path, not a repo path
        target = self._repo / "index.ts"
        target.write_text("export const x = 1;\n")
        result = detect_repo(str(target))
        status = result.get("data", {}).get("profile_status")
        self.assertEqual(status, "profile_unsupported_schema_version", f"got {status!r}; full result: {result}")

    def test_get_pattern_context_returns_profile_corrupted(self):
        target = self._repo / "src" / "x.ts"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("export const x = 1;\n")
        result = get_pattern_context(str(target))
        status = result.get("data", {}).get("repo", {}).get("profile_status")
        # get_pattern_context collapses unsupported_schema to profile_corrupted
        # via its bare except-catch around load_profile_dir
        self.assertIn(status, {"profile_corrupted", "profile_unsupported_schema_version"})

    def test_supported_schema_loads_ok(self):
        # Sanity: writing the supported schema version should still load
        _write_committed_profile(self._repo / ".chameleon", schema_version=MAX_SUPPORTED_SCHEMA_VERSION)
        loaded = load_profile_dir(self._repo / ".chameleon")
        self.assertIsNotNone(loaded)


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(SchemaVersionTooHighTest))
    print(f"\nSummary: {result.testsRun} run, {len(result.failures)} failed, {len(result.errors)} errored")
    sys.exit(0 if result.wasSuccessful() else 1)
