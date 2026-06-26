"""An absolute paths_glob must not crash discovery with NotImplementedError.

paths_glob is a user-supplied bootstrap_repo parameter, and an absolute glob
(e.g. '/abs/repo/**/*.ts') is a natural mistake. pathlib.Path.glob raises
NotImplementedError('Non-relative patterns are unsupported') on an absolute
pattern. _glob_candidates ran base.glob(pattern) with no guard, so the whole
bootstrap aborted with a raw traceback instead of a clean failure. The sibling
expand_workspace_globs_with_diagnostics already wraps the identical call in
try/except (ValueError, IndexError, NotImplementedError, OSError); discovery
must do the same so one bad glob never aborts the run.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from chameleon_mcp.bootstrap.discovery import _glob_candidates


def test_glob_candidates_absolute_pattern_does_not_crash(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const x = 1;\n", encoding="utf-8")
    # An absolute pattern: pathlib raises NotImplementedError without the guard.
    abs_pattern = str(repo / "**" / "*.ts")
    result = _glob_candidates(repo, abs_pattern)
    # Must return cleanly (an absolute pattern simply matches nothing relative to
    # the base), never raise.
    assert isinstance(result, list)


def test_bootstrap_with_absolute_paths_glob_does_not_raise(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (repo / "package.json").write_text('{"name":"t","version":"1.0.0"}', encoding="utf-8")

    from chameleon_mcp.tools import bootstrap_repo

    os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
    try:
        # Must return an envelope (possibly a failure / empty derivation), never
        # propagate a raw NotImplementedError out of the tool.
        result = bootstrap_repo(str(repo), paths_glob=str(repo / "**" / "*.ts"))
    except NotImplementedError as exc:  # pragma: no cover - the bug we are fixing
        pytest.fail(f"absolute paths_glob crashed bootstrap: {exc}")
    assert isinstance(result, dict)
