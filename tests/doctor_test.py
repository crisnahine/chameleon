"""Verify chameleon doctor returns a sane structure."""
import os
import sys
import tempfile
import unittest

from chameleon_mcp.tools import doctor


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("CHAMELEON_PLUGIN_DATA")
        self._tmp = tempfile.mkdtemp()
        os.environ["CHAMELEON_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("CHAMELEON_PLUGIN_DATA", None)
        else:
            os.environ["CHAMELEON_PLUGIN_DATA"] = self._prev

    def test_doctor_returns_envelope(self):
        result = doctor()
        self.assertIn("data", result)
        data = result["data"]
        self.assertIn("overall", data)
        self.assertIn(data["overall"], {"ok", "warn", "error"})
        self.assertIn("checks", data)
        self.assertIsInstance(data["checks"], list)
        self.assertGreater(len(data["checks"]), 5)  # at least 6 checks
        self.assertIn("summary", data)
        self.assertEqual(
            data["summary"]["ok"] + data["summary"]["warn"] + data["summary"]["error"],
            data["summary"]["total"],
        )

    def test_doctor_includes_python_check(self):
        result = doctor()
        names = [c["name"] for c in result["data"]["checks"]]
        self.assertIn("python_version", names)
        self.assertIn("plugin_data_writable", names)


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(DoctorTest))
    print(f"\nSummary: {result.testsRun} run, {len(result.failures)} failed, {len(result.errors)} errored")
    sys.exit(0 if result.wasSuccessful() else 1)
