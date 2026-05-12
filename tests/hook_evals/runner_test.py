"""Unit tests for tests/hook_evals/runner.py.

The runner itself runs scenario JSON against get_pattern_context. These
tests verify the runner's own logic without depending on real fixtures.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runner import assert_scenario, ScenarioResult


class AssertScenarioTest(unittest.TestCase):
    def _response(self, **overrides):
        base = {
            "data": {
                "repo": {
                    "id": "fake",
                    "profile_status": "profile_present",
                    "trust_state": "trusted",
                },
                "archetype": {"archetype": "utility_cluster_abc"},
                "canonical_excerpt": {"text": "export const x = 1;"},
                "rules": [["no-default-export", "Avoid default exports"]],
                "idioms": "Use named exports.",
            }
        }
        base["data"].update(overrides)
        return base

    def test_archetype_match_passes(self):
        scenario = {
            "name": "t",
            "fixture_repo": "ts_minimal",
            "file_path": "src/utils/foo.ts",
            "file_content": "",
            "trust_state": "trusted",
            "expected": {"archetype_name": "utility_cluster_abc"},
        }
        result = assert_scenario(scenario, self._response())
        self.assertEqual(result.status, "PASS")

    def test_archetype_mismatch_fails(self):
        scenario = {
            "name": "t",
            "fixture_repo": "ts_minimal",
            "file_path": "src/utils/foo.ts",
            "file_content": "",
            "trust_state": "trusted",
            "expected": {"archetype_name": "definitely_not_real_archetype"},
        }
        result = assert_scenario(scenario, self._response())
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(any("archetype" in m for m in result.mismatches))

    def test_canonical_substring_match(self):
        scenario = {
            "name": "t",
            "fixture_repo": "ts_minimal",
            "file_path": "src/utils/foo.ts",
            "file_content": "",
            "trust_state": "trusted",
            "expected": {
                "archetype_name": "utility_cluster_abc",
                "canonical_excerpt_includes": ["export const"],
            },
        }
        result = assert_scenario(scenario, self._response())
        self.assertEqual(result.status, "PASS")

    def test_schema_rot_detection(self):
        scenario = {
            "name": "t",
            "fixture_repo": "ts_minimal",
            "file_path": "src/utils/foo.ts",
            "file_content": "",
            "trust_state": "trusted",
            "expected": {"archetype_name": "utility_cluster_abc"},
        }
        result = assert_scenario(
            scenario,
            self._response(repo={"id": "x", "profile_status": "profile_corrupted", "trust_state": "trusted"}),
        )
        self.assertEqual(result.status, "SCHEMA_ROT")
        self.assertIn("refresh_eval_fixtures", " ".join(result.mismatches))


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromModule(sys.modules[__name__]))
    print(f"\nSummary: {result.testsRun} run, {len(result.failures)} failed, {len(result.errors)} errored")
    sys.exit(0 if result.wasSuccessful() else 1)
