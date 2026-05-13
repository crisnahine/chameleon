"""Verify .hook_errors.log rotation behavior."""
import sys
import tempfile
import unittest
from pathlib import Path

from chameleon_mcp.log_rotation import (
    MAX_ROTATIONS,
    ROTATE_THRESHOLD_BYTES,
    rotate_if_needed,
)


class RotateIfNeededTest(unittest.TestCase):
    def test_no_op_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            rotate_if_needed(Path(tmp) / "absent.log")  # should not raise

    def test_no_op_when_under_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.log"
            log.write_bytes(b"x" * 1024)
            rotate_if_needed(log)
            self.assertTrue(log.exists())
            self.assertFalse((Path(tmp) / "a.log.1").exists())

    def test_rotates_when_over_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.log"
            log.write_bytes(b"x" * (ROTATE_THRESHOLD_BYTES + 1))
            rotate_if_needed(log)
            self.assertFalse(log.exists())
            self.assertTrue((Path(tmp) / "a.log.1").exists())

    def test_caps_at_max_rotations(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.log"
            # Pre-populate .1 .. .5 (or MAX_ROTATIONS)
            for i in range(1, MAX_ROTATIONS + 1):
                (Path(tmp) / f"a.log.{i}").write_bytes(b"old")
            log.write_bytes(b"x" * (ROTATE_THRESHOLD_BYTES + 1))
            rotate_if_needed(log)
            # Old .5 should be gone (or replaced); .1..MAX should all exist
            for i in range(1, MAX_ROTATIONS + 1):
                self.assertTrue((Path(tmp) / f"a.log.{i}").exists(), f"missing rotation {i}")
            self.assertFalse((Path(tmp) / f"a.log.{MAX_ROTATIONS + 1}").exists())
            self.assertFalse(log.exists())


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(RotateIfNeededTest))
    print(f"\nSummary: {result.testsRun} run, {len(result.failures)} failed, {len(result.errors)} errored")
    sys.exit(0 if result.wasSuccessful() else 1)
