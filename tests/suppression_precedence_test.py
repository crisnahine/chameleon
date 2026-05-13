"""Verify suppression precedence: .skip > CHAMELEON_DISABLE > session_disable > pause > none."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from chameleon_mcp.optouts import (
    is_chameleon_suppressed,
    write_pause,
    write_session_disable,
)

# is_chameleon_suppressed(repo_root, repo_id, session_id=None) -> str | None


class SuppressionPrecedenceTest(unittest.TestCase):
    def setUp(self):
        self._prev_data = os.environ.get("CHAMELEON_PLUGIN_DATA")
        self._prev_disable = os.environ.get("CHAMELEON_DISABLE")

        # Temp dir for plugin data (markers land here)
        self._plugin_data = tempfile.mkdtemp()
        os.environ["CHAMELEON_PLUGIN_DATA"] = self._plugin_data

        # Fake repo root with a .chameleon dir (no .skip by default)
        self._repo = Path(tempfile.mkdtemp())
        (self._repo / ".chameleon").mkdir()

        # Stable repo_id — just a string key under CHAMELEON_PLUGIN_DATA
        self._repo_id = "test-suppression-precedence"
        self._session = "session-precedence-test"

    def tearDown(self):
        for key, prev in [
            ("CHAMELEON_PLUGIN_DATA", self._prev_data),
            ("CHAMELEON_DISABLE", self._prev_disable),
        ]:
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev

    def _reason(self) -> str | None:
        return is_chameleon_suppressed(self._repo, self._repo_id, self._session)

    # ------------------------------------------------------------------
    # 5 single-flag baselines
    # ------------------------------------------------------------------

    def test_baseline_no_suppression(self):
        self.assertIsNone(self._reason())

    def test_only_repo_skip(self):
        (self._repo / ".chameleon" / ".skip").touch()
        self.assertEqual(self._reason(), "repo_skip")

    def test_only_user_disable(self):
        os.environ["CHAMELEON_DISABLE"] = "1"
        self.assertEqual(self._reason(), "user_disable")

    def test_only_session_disable(self):
        write_session_disable(self._repo_id, self._session)
        self.assertEqual(self._reason(), "session_disable")

    def test_only_pause(self):
        write_pause(self._repo_id, minutes=15)
        self.assertEqual(self._reason(), "pause")

    # ------------------------------------------------------------------
    # Layered: .skip beats everything
    # ------------------------------------------------------------------

    def test_skip_beats_user_disable(self):
        (self._repo / ".chameleon" / ".skip").touch()
        os.environ["CHAMELEON_DISABLE"] = "1"
        self.assertEqual(self._reason(), "repo_skip")

    def test_skip_beats_session_disable(self):
        (self._repo / ".chameleon" / ".skip").touch()
        write_session_disable(self._repo_id, self._session)
        self.assertEqual(self._reason(), "repo_skip")

    # ------------------------------------------------------------------
    # Layered: user_disable beats session/pause
    # ------------------------------------------------------------------

    def test_user_disable_beats_session_disable(self):
        os.environ["CHAMELEON_DISABLE"] = "1"
        write_session_disable(self._repo_id, self._session)
        self.assertEqual(self._reason(), "user_disable")

    def test_user_disable_beats_pause(self):
        os.environ["CHAMELEON_DISABLE"] = "1"
        write_pause(self._repo_id, minutes=15)
        self.assertEqual(self._reason(), "user_disable")

    # ------------------------------------------------------------------
    # Layered: session beats pause
    # ------------------------------------------------------------------

    def test_session_disable_beats_pause(self):
        write_session_disable(self._repo_id, self._session)
        write_pause(self._repo_id, minutes=15)
        self.assertEqual(self._reason(), "session_disable")

    # ------------------------------------------------------------------
    # All four flags simultaneously: .skip wins
    # ------------------------------------------------------------------

    def test_all_four_flags_skip_wins(self):
        (self._repo / ".chameleon" / ".skip").touch()
        os.environ["CHAMELEON_DISABLE"] = "1"
        write_session_disable(self._repo_id, self._session)
        write_pause(self._repo_id, minutes=15)
        self.assertEqual(self._reason(), "repo_skip")


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(
        unittest.TestLoader().loadTestsFromTestCase(SuppressionPrecedenceTest)
    )
    print(
        f"\nSummary: {result.testsRun} run, "
        f"{len(result.failures)} failed, "
        f"{len(result.errors)} errored"
    )
    sys.exit(0 if result.wasSuccessful() else 1)
