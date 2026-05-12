"""Regression test for naming._member_relpaths absolute-path bias.

Bug: any repo whose absolute path contained a test-token segment
(e.g., chameleon/tests/fixtures/eval_repos/ts_minimal/) had its
non-test clusters mis-flagged as tests, producing test-* archetype
names like 'test', 'test-utils-ts', 'test-models-rb'.

Fix landed in commit a154969: _member_relpaths now relativizes member
paths against repo_root before the all-segments test-token check in
_looks_like_test.

If you see suspicious test-* archetype names again, this regression
likely fires here first.
"""
import sys
import tempfile
import unittest
from pathlib import Path

from chameleon_mcp.bootstrap.naming import _looks_like_test, _member_relpaths


class _FakeMember:
    def __init__(self, path: Path):
        self.path = path


class MemberRelpathsBiasTest(unittest.TestCase):
    def test_member_paths_are_repo_relative(self):
        # Simulate a repo at /tmp/.../tests/eval_repos/foo/, with members
        # at src/utils/*.ts inside it.
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp) / "tests" / "eval_repos" / "foo"
            (outer / "src" / "utils").mkdir(parents=True)
            members = []
            for i in range(5):
                p = outer / "src" / "utils" / f"util_{i}.ts"
                p.write_text(f"export const v{i} = {i};\n")
                members.append(_FakeMember(p))

            paths = _member_relpaths(str(outer), members)
            # Every path must be relative to outer; none should start with "tests/".
            for path in paths:
                self.assertFalse(
                    path.startswith("tests/"),
                    f"expected repo-relative path, got {path!r}",
                )
                # Each path should start with src/utils/
                self.assertTrue(path.startswith("src/utils/"))

    def test_looks_like_test_ignores_repo_root_test_token(self):
        # Without the fix, a repo at .../tests/foo/ has all members
        # under "tests/" in absolute form and _looks_like_test returns True.
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp) / "tests" / "eval_repos" / "foo"
            (outer / "src" / "components").mkdir(parents=True)
            members = []
            for name in ["Alert", "Button", "Card", "Input", "Modal"]:
                p = outer / "src" / "components" / f"{name}.tsx"
                p.write_text(f"export const {name} = () => null;\n")
                members.append(_FakeMember(p))

            paths = _member_relpaths(str(outer), members)
            # paths_pattern represents the bucket — for components it would be
            # something like "src/components:tsx", which has no test token.
            result = _looks_like_test("src/components:tsx", paths)
            self.assertFalse(
                result,
                "components cluster should NOT be flagged as tests when "
                "repo root contains 'tests' in its absolute path",
            )


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
