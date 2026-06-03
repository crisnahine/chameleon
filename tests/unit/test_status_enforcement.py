"""Status enforcement-section tests for get_status().

/chameleon-status must surface the active enforcement mode, the block rules
that calibration kept active for this repo, and any block rule calibration
demoted (kept advisory) along with the false-positive rate that demoted it.

Isolation mirrors the sibling enforcement tests (no shared conftest): the
make_trusted_repo factory builds a real repo + config + plugin-data dir under
tmp_path and patches repo resolution so get_status reads the on-disk config and
enforcement.json.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def make_trusted_repo(tmp_path):
    """Factory: a trusted repo with an enforcement config and an isolated data dir.

    Returns ``(repo, data_dir, session_id, file_path, profile_dir)``. The repo's
    resolution (find_repo_root / _compute_repo_id) and plugin-data dir are
    patched so get_status reads the config and enforcement.json under the repo.
    """
    stack = ExitStack()

    def _factory(*, mode: str = "shadow", stop_block_cap: int = 3):
        repo_id = "status_repo_id"
        repo = tmp_path / "repo"
        profile_dir = repo / ".chameleon"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.joinpath("config.json").write_text(
            json.dumps({"enforcement": {"mode": mode, "stop_block_cap": stop_block_cap}}),
            encoding="utf-8",
        )

        data_dir = tmp_path / repo_id
        data_dir.mkdir(parents=True, exist_ok=True)

        file_path = str(repo / "src" / "Widget.ts")
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)

        session_id = "s-status"

        stack.enter_context(patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo))
        stack.enter_context(patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id))
        stack.enter_context(
            patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path)
        )

        return repo, data_dir, session_id, file_path, profile_dir

    try:
        yield _factory
    finally:
        stack.close()


def test_status_reports_enforcement(make_trusted_repo):
    from chameleon_mcp.enforcement_calibration import write_block_rules
    from chameleon_mcp.tools import get_status

    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="shadow")
    write_block_rules(
        profile_dir,
        {
            "phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 9},
            "jsx-presence-mismatch": {"active": False, "fp_rate": 0.05, "sampled": 9},
        },
    )
    out = get_status(str(repo))
    text = json.dumps(out)
    assert "enforcement" in text.lower()
    assert "shadow" in text.lower()
