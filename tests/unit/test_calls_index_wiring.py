"""Wiring: calls_index.json in the trust surface, txn protocol, and bootstrap.

Merge posture is pinned in test_gitattributes_template.py: calls_index.json is
a generated index, never routed to the merge driver (accept either side and
/chameleon-refresh regenerates it).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from chameleon_mcp import tools
from chameleon_mcp.bootstrap.transaction import _PROTOCOL_FILES
from chameleon_mcp.profile.trust import _HASHED_ARTIFACTS

# ---------------------------------------------------------------------------
# Trust surface
# ---------------------------------------------------------------------------


def test_calls_index_in_trust_surface():
    assert "calls_index.json" in _HASHED_ARTIFACTS


def test_calls_index_trust_surface_ordering():
    # Alphabetical within the tuple: "calls_index.json" sits between
    # "archetypes.json" and "canonicals.json".
    artifacts = list(_HASHED_ARTIFACTS)
    ci = artifacts.index("calls_index.json")
    assert artifacts[ci - 1] == "archetypes.json"
    assert artifacts[ci + 1] == "canonicals.json"


# ---------------------------------------------------------------------------
# Transaction protocol set
# ---------------------------------------------------------------------------


def test_calls_index_in_txn_protocol():
    assert "calls_index.json" in _PROTOCOL_FILES


# ---------------------------------------------------------------------------
# Bootstrap wires calls_index.json into the profile
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp import index_db
    from chameleon_mcp.profile import loader as _loader

    monkeypatch.setattr(index_db, "_INDEX_CONN", None)
    _loader._PROFILE_CACHE.clear()
    _loader._REPO_ROOT_CACHE.clear()
    tools._clear_repo_id_cache()
    yield
    _loader._PROFILE_CACHE.clear()
    _loader._REPO_ROOT_CACHE.clear()
    tools._clear_repo_id_cache()


def _git(repo: Path, *args: str) -> str:
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _make_ts_repo(root: Path) -> Path:
    """Minimal TypeScript git repo for bootstrap tests; alphaService carries a
    same-file this-call (run -> helper) the calls index must record."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "package.json").write_text('{"name": "fixture", "private": true}\n', encoding="utf-8")
    (root / "tsconfig.json").write_text('{"compilerOptions": {"strict": true}}\n', encoding="utf-8")
    (root / ".gitignore").write_text(".chameleon/\n", encoding="utf-8")
    services = root / "src" / "services"
    services.mkdir(parents=True, exist_ok=True)
    (services / "alphaService.ts").write_text(
        "export class AlphaService {\n"
        "  run(x: string) { return this.helper(x) }\n"
        "  helper(x: string) { return x }\n"
        "}\n",
        encoding="utf-8",
    )
    for name in ("Beta", "Gamma", "Delta", "Epsilon", "Zeta"):
        (services / f"{name.lower()}Service.ts").write_text(
            f"export class {name}Service {{\n  run(x: string) {{ return x }}\n}}\n",
            encoding="utf-8",
        )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "baseline")
    return root


def test_bootstrap_writes_calls_index(tmp_path):
    repo = _make_ts_repo(tmp_path / "repo")
    result = tools.bootstrap_repo(str(repo))
    assert result["data"]["status"] == "success"

    artifact = repo / ".chameleon" / "calls_index.json"
    assert artifact.is_file(), "bootstrap did not write calls_index.json"

    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload.get("schema_version") == 1
    entry = payload["callees"]["src/services/alphaService.ts"]["helper"]
    rows = entry["callers"]
    assert {
        "path": "src/services/alphaService.ts",
        "caller": "run",
        "line": 2,
        "grade": "same_file",
    } in rows


def _have_prism() -> bool:
    if not shutil.which("ruby"):
        return False
    try:
        return (
            subprocess.run(
                ["ruby", "-e", "require 'prism'"], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _make_ruby_repo(root: Path) -> Path:
    """Minimal Ruby git repo for bootstrap tests; alpha_service carries a
    same-file bare call (perform -> helper) the calls index must record."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "Gemfile").write_text("source 'https://rubygems.org'\n", encoding="utf-8")
    (root / ".gitignore").write_text(".chameleon/\n", encoding="utf-8")
    services = root / "app" / "services"
    services.mkdir(parents=True, exist_ok=True)
    (services / "alpha_service.rb").write_text(
        "class AlphaService\n  def perform\n    helper\n  end\n\n  def helper\n    1\n  end\nend\n",
        encoding="utf-8",
    )
    for name in ("Beta", "Gamma", "Delta", "Epsilon", "Zeta"):
        (services / f"{name.lower()}_service.rb").write_text(
            f"class {name}Service\n  def run(x)\n    x\n  end\nend\n",
            encoding="utf-8",
        )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "baseline")
    return root


@pytest.mark.skipif(not _have_prism(), reason="ruby + prism gem unavailable")
def test_bootstrap_writes_calls_index_ruby(tmp_path):
    repo = _make_ruby_repo(tmp_path / "repo")
    result = tools.bootstrap_repo(str(repo))
    assert result["data"]["status"] == "success"

    artifact = repo / ".chameleon" / "calls_index.json"
    assert artifact.is_file(), "bootstrap did not write calls_index.json"

    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload.get("schema_version") == 1
    entry = payload["callees"]["app/services/alpha_service.rb"]["helper"]
    rows = entry["callers"]
    assert {
        "path": "app/services/alpha_service.rb",
        "caller": "perform",
        "line": 3,
        "grade": "same_file",
    } in rows
