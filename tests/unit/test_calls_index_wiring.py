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


# ---------------------------------------------------------------------------
# Refresh heals a missing generated index instead of noop-preserving the hole
# ---------------------------------------------------------------------------


def test_refresh_heals_missing_generated_indexes(tmp_path):
    repo = _make_ts_repo(tmp_path / "repo")
    assert tools.bootstrap_repo(str(repo))["data"]["status"] == "success"
    cham = repo / ".chameleon"

    # Control: with all artifacts intact an immediate refresh noops.
    assert tools.refresh_repo(str(repo))["data"]["status"] == "noop"

    # A deleted calls_index.json must force a re-derive, not a noop that
    # preserves the hole forever (the loader fails open to "no facts").
    (cham / "calls_index.json").unlink()
    healed = tools.refresh_repo(str(repo))
    assert healed["data"]["status"] != "noop"
    assert (cham / "calls_index.json").is_file(), "refresh did not restore calls_index.json"

    # Same gap, same fix, for the TS-only symbol indexes.
    assert tools.refresh_repo(str(repo))["data"]["status"] == "noop"
    (cham / "exports_index.json").unlink()
    healed = tools.refresh_repo(str(repo))
    assert healed["data"]["status"] != "noop"
    assert (cham / "exports_index.json").is_file(), "refresh did not restore exports_index.json"


def test_needs_rederive_index_checks(tmp_path):
    # Direct checks on the shared repair predicate: missing calls_index forces
    # a rebuild for every language; the symbol indexes only bind TS profiles
    # (a Ruby profile never writes them, so their absence must not force a
    # perpetual rebuild).
    cham = tmp_path / ".chameleon"
    cham.mkdir(parents=True)
    for name in (
        "archetypes.json",
        "canonicals.json",
        "rules.json",
        "conventions.json",
        "calls_index.json",
        "function_catalog.json",
        "symbol_signatures.json",
    ):
        (cham / name).write_text("{}", encoding="utf-8")
    (cham / "profile.json").write_text(json.dumps({"language": "ruby"}), encoding="utf-8")
    (cham / "profile.summary.md").write_text("# summary\n", encoding="utf-8")
    (cham / "principles.md").write_text("anti-hallucination protocol\n", encoding="utf-8")

    assert tools._profile_needs_rederive(cham) is False

    (cham / "calls_index.json").unlink()
    assert tools._profile_needs_rederive(cham) is True
    (cham / "calls_index.json").write_text("{not json", encoding="utf-8")
    assert tools._profile_needs_rederive(cham) is True
    (cham / "calls_index.json").write_text("{}", encoding="utf-8")
    assert tools._profile_needs_rederive(cham) is False

    (cham / "function_catalog.json").unlink()
    assert tools._profile_needs_rederive(cham) is True
    (cham / "function_catalog.json").write_text("{}", encoding="utf-8")
    assert tools._profile_needs_rederive(cham) is False

    # symbol_signatures.json (forward definition hydration) is built for every
    # language, so its absence forces a rebuild -- this is how an existing
    # pre-C2.2 profile picks it up on the next /chameleon-refresh.
    (cham / "symbol_signatures.json").unlink()
    assert tools._profile_needs_rederive(cham) is True
    (cham / "symbol_signatures.json").write_text("{}", encoding="utf-8")
    assert tools._profile_needs_rederive(cham) is False

    # TS profiles additionally require the two symbol indexes.
    (cham / "profile.json").write_text(json.dumps({"language": "typescript"}), encoding="utf-8")
    assert tools._profile_needs_rederive(cham) is True
    (cham / "exports_index.json").write_text("{}", encoding="utf-8")
    (cham / "reverse_index.json").write_text("{}", encoding="utf-8")
    assert tools._profile_needs_rederive(cham) is False


# ---------------------------------------------------------------------------
# The workspace-root amend must not drop calls_index.json
# ---------------------------------------------------------------------------


def test_amend_root_profile_preserves_calls_index(tmp_path, monkeypatch):
    # calls_index.json is a protocol file (not auto-carried by the atomic
    # commit), so the workspaces amend must re-emit it verbatim or every
    # monorepo root would lose it right after bootstrap wrote it.
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp.bootstrap.orchestrator import _amend_root_profile_with_workspaces

    cham = tmp_path / ".chameleon"
    cham.mkdir(parents=True)
    for name in ("archetypes.json", "canonicals.json", "rules.json", "conventions.json"):
        (cham / name).write_text("{}", encoding="utf-8")
    (cham / "profile.json").write_text(json.dumps({"language": "typescript"}), encoding="utf-8")
    (cham / "principles.md").write_text("p\n", encoding="utf-8")
    (cham / "idioms.md").write_text("i\n", encoding="utf-8")
    (cham / "profile.summary.md").write_text("s\n", encoding="utf-8")
    payload = {"schema_version": 1, "callees": {}}
    (cham / "calls_index.json").write_text(json.dumps(payload), encoding="utf-8")
    (cham / "COMMITTED").touch()

    _amend_root_profile_with_workspaces(
        cham,
        [{"workspace_path": "apps/web", "repo_id": "x", "profile_dir": "d", "status": "success"}],
    )

    amended = json.loads((cham / "profile.json").read_text(encoding="utf-8"))
    assert amended["workspaces"][0]["workspace_path"] == "apps/web"
    assert json.loads((cham / "calls_index.json").read_text(encoding="utf-8")) == payload


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
