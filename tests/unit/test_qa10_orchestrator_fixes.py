"""Bootstrap/orchestrator fixes from the 10-tester QA run.

- #8: refresh must not crash with UnicodeDecodeError when the existing idioms.md
  is not valid UTF-8 (the preserve step caught only OSError, but UnicodeDecodeError
  is a ValueError subclass).
- T8: a coordinator-only monorepo root that profiles its workspaces returns
  status success_workspaces_only and must NOT also carry the root's stale
  "no language signals" error string.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from chameleon_mcp import tools

_FIXTURES = Path(__file__).resolve().parents[1] / "journey" / "fixtures"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    tools._clear_repo_id_cache()
    yield
    tools._clear_repo_id_cache()


def test_refresh_survives_non_utf8_idioms(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    for i in range(6):
        (repo / "src" / f"c{i}.ts").write_text(f"export const C{i} = () => {i};\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    assert tools.bootstrap_repo(str(repo))["data"]["status"] == "success"

    # Corrupt idioms.md to non-UTF8 bytes (e.g. a latin1 / lone-high-byte file).
    (repo / ".chameleon" / "idioms.md").write_bytes(b"# idioms\n\xff\xfe taught \xe9\xe8\n")

    result = tools.refresh_repo(str(repo), force=True)  # must not raise
    assert result["data"]["status"] in ("success", "noop")


def test_workspaces_only_bootstrap_has_no_error_string(tmp_path):
    src = _FIXTURES / "ts_monorepo"
    if not src.is_dir():
        pytest.skip("ts_monorepo fixture missing")
    repo = tmp_path / "mono"
    shutil.copytree(src, repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    data = tools.bootstrap_repo(str(repo))["data"]
    assert data["status"] == "success_workspaces_only"
    assert not data.get("error")
