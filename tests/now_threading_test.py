"""Verify `now=` threads from tools.bootstrap_repo down to select_canonicals.

Enables refresh_eval_fixtures.sh to pin time for deterministic witness
selection. Seam already existed at canonical.py:152; this test guards
the plumbing.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp import tools
import chameleon_mcp.bootstrap.canonical as canonical_mod


class NowThreadingTest(unittest.TestCase):
    def test_bootstrap_repo_threads_now_to_select_canonicals(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "package.json").write_text("{}")
            (repo / "src").mkdir()
            for i in range(6):
                (repo / "src" / f"util_{i}.ts").write_text(
                    f"export const v{i} = {i};\n"
                )

            real_select = canonical_mod.select_canonicals
            with patch.object(
                canonical_mod, "select_canonicals", wraps=real_select
            ) as mock_select:
                tools.bootstrap_repo(str(repo), now=12345.0)

            self.assertTrue(mock_select.called)
            now_values = [
                call.kwargs.get("now") for call in mock_select.call_args_list
            ]
            self.assertIn(12345.0, now_values)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
