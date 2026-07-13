"""Tool-level tests for get_override_audit and the get_status override panel.

get_override_audit wraps build_override_audit in the standard envelope and
resolves the repo arg. get_status now carries an ``overrides`` panel alongside
the bootstrap calibration, framed as a distinct axis. These tests drive the
real tool entry points against an on-disk drift.db + metrics log.

Isolation: CHAMELEON_PLUGIN_DATA at tmp_path; repo resolution patched so the
repo path maps to a known repo_id whose drift.db holds the override rows.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from chameleon_mcp.drift import observations as obs
from chameleon_mcp.drift.observations import record_override

REPO_ID = "f" * 64


def _close_drift_conns() -> None:
    for conn in list(obs._DRIFT_CONN.values()):
        try:
            conn.close()
        except Exception:
            pass
    obs._DRIFT_CONN.clear()


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    _close_drift_conns()
    yield
    _close_drift_conns()


@pytest.fixture
def repo_with_overrides(tmp_path):
    stack = ExitStack()
    repo = tmp_path / "repo"
    profile_dir = repo / ".chameleon"
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.joinpath("config.json").write_text(
        json.dumps({"enforcement": {"mode": "enforce"}}), encoding="utf-8"
    )
    stack.enter_context(patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo))
    stack.enter_context(patch("chameleon_mcp.tools._compute_repo_id", return_value=REPO_ID))
    try:
        yield repo
    finally:
        stack.close()


def test_get_override_audit_bad_arg():
    from chameleon_mcp.tools import get_override_audit

    out = get_override_audit("")
    assert out["data"]["status"] == "failed"


def test_get_override_audit_returns_envelope(repo_with_overrides):
    from chameleon_mcp.tools import get_override_audit

    for _ in range(5):
        record_override(REPO_ID, "import-preference-violation")
    out = get_override_audit(str(repo_with_overrides))
    data = out["data"]
    assert data["total_overrides"] == 5
    assert data["rules"]["import-preference-violation"]["overrides"] == 5
    assert "import-preference-violation" in data["flagged"]  # rate 1.0, 5 events


def test_get_status_carries_override_panel(repo_with_overrides):
    from chameleon_mcp.tools import get_status

    for _ in range(5):
        record_override(REPO_ID, "import-preference-violation")
    out = get_status(str(repo_with_overrides))
    enf = out["data"]["enforcement"]
    assert "overrides" in enf
    assert enf["overrides"]["total_overrides"] == 5


def test_get_status_omits_override_panel_when_no_overrides(repo_with_overrides):
    from chameleon_mcp.tools import get_status

    out = get_status(str(repo_with_overrides))
    enf = out["data"]["enforcement"]
    # Panel omitted entirely: no drift.db override history at all, matching
    # the documented conditional-presence contract -- not "present but empty".
    assert "overrides" not in enf
