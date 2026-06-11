"""Task-7 wiring: calls_index.json in the trust surface, txn protocol, and merge driver."""

from __future__ import annotations

import json
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
    """Minimal TypeScript git repo for bootstrap tests."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "package.json").write_text('{"name": "fixture", "private": true}\n', encoding="utf-8")
    (root / "tsconfig.json").write_text('{"compilerOptions": {"strict": true}}\n', encoding="utf-8")
    (root / ".gitignore").write_text(".chameleon/\n", encoding="utf-8")
    for name in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta"):
        p = root / "src" / "services" / f"{name.lower()}Service.ts"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
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
    assert "callees" in payload


# ---------------------------------------------------------------------------
# merge_profiles: calls_index.json conflict takes theirs wholesale
# ---------------------------------------------------------------------------


def _write(p: Path, data: dict) -> Path:
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_merge_calls_index_takes_theirs(tmp_path):
    ours_callees = {"src/a.ts": {"foo": {"callers": [], "total": 0, "truncated": False}}}
    theirs_callees = {"src/b.ts": {"bar": {"callers": [], "total": 0, "truncated": False}}}

    base = _write(tmp_path / "base.json", {"schema_version": 1, "callees": {}})
    ours = _write(tmp_path / "ours.json", {"schema_version": 1, "callees": ours_callees})
    theirs = _write(tmp_path / "theirs.json", {"schema_version": 1, "callees": theirs_callees})

    out = tools.merge_profiles(
        repo=str(tmp_path), base=str(base), ours=str(ours), theirs=str(theirs)
    )
    assert out["data"]["status"] == "success"

    merged = json.loads(ours.read_text(encoding="utf-8"))
    # calls_index merge takes theirs wholesale
    assert merged["callees"] == theirs_callees
    assert "src/b.ts" in merged["callees"]
