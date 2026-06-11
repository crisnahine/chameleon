"""Shared fixtures for the effectiveness unit tests.

Every test runs against an isolated chameleon data dir and opts into
tmp-path repos (the temp-dir refusal guard would otherwise reject every
tmp_path fixture repo). No test here may spawn a real claude subprocess.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_chameleon_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "chameleon_data"))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "exec_hmac.key"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir(exist_ok=True)
