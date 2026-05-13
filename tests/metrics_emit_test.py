"""Verify metrics.jsonl emission shape."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from chameleon_mcp.metrics import emit_hook_metric


class EmitHookMetricTest(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("CHAMELEON_PLUGIN_DATA")
        self._tmp = tempfile.mkdtemp()
        os.environ["CHAMELEON_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("CHAMELEON_PLUGIN_DATA", None)
        else:
            os.environ["CHAMELEON_PLUGIN_DATA"] = self._prev

    def test_writes_one_line(self):
        emit_hook_metric(
            "preflight-and-advise",
            elapsed_ms=42,
            repo_id="abcdef",
            advisory_emitted=True,
            trust_state="trusted",
            archetype="util",
            confidence="medium",
        )
        path = Path(self._tmp) / "metrics.jsonl"
        text = path.read_text()
        lines = [l for l in text.splitlines() if l]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["hook"], "preflight-and-advise")
        self.assertEqual(record["elapsed_ms"], 42)
        self.assertEqual(record["repo_id"], "abcdef")
        self.assertTrue(record["advisory_emitted"])
        self.assertEqual(record["trust_state"], "trusted")
        self.assertEqual(record["archetype"], "util")
        self.assertEqual(record["confidence"], "medium")
        self.assertFalse(record["fail_open"])
        self.assertIsNone(record["suppression_reason"])
        self.assertIn("ts", record)

    def test_appends_multiple_lines(self):
        for i in range(5):
            emit_hook_metric(
                "preflight-and-advise",
                elapsed_ms=i,
                repo_id=None,
                advisory_emitted=False,
                suppression_reason="user_disable",
            )
        path = Path(self._tmp) / "metrics.jsonl"
        lines = [l for l in path.read_text().splitlines() if l]
        self.assertEqual(len(lines), 5)
        for line in lines:
            record = json.loads(line)
            self.assertEqual(record["suppression_reason"], "user_disable")

    def test_swallows_bad_path(self):
        # Point CHAMELEON_PLUGIN_DATA at a path we can't write to
        os.environ["CHAMELEON_PLUGIN_DATA"] = "/nonexistent_root_xyz/cannot_write"
        # Should not raise even though write fails
        emit_hook_metric(
            "preflight-and-advise",
            elapsed_ms=1,
            repo_id=None,
            advisory_emitted=False,
        )


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(EmitHookMetricTest))
    print(f"\nSummary: {result.testsRun} run, {len(result.failures)} failed, {len(result.errors)} errored")
    sys.exit(0 if result.wasSuccessful() else 1)
