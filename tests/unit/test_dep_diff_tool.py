"""End-to-end test for the scan_dependency_changes MCP tool.

Exercises the real no-network git path (diff base_ref...HEAD) against a temp
git repo, proving the tool fetches manifest diffs and routes them through the
pure parser. The parsing logic itself is unit-tested in test_dep_diff.py.
"""

from __future__ import annotations

import subprocess

import pytest

from chameleon_mcp import tools


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")


@pytest.fixture(autouse=True)
def _allow_tmp_repo(monkeypatch):
    # Temp-dir repos are refused by default; opt in for the test fixtures.
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")


def test_scan_dependency_changes_finds_supply_chain_signals(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    pkg = repo / "package.json"
    pkg.write_text(
        '{\n  "dependencies": {\n    "react": "^18.0.0"\n  },\n  "scripts": {\n    "build": "tsc"\n  }\n}\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    pkg.write_text(
        '{\n  "dependencies": {\n    "react": "^18.0.0",\n    "left-pad": "^1.3.0"\n  },\n'
        '  "scripts": {\n    "build": "tsc",\n    "postinstall": "node ./scripts/setup.js"\n  }\n}\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add dep + install hook")

    result = tools.scan_dependency_changes(str(repo), base_ref="main")
    payload = result["data"] if "data" in result else result
    assert payload["status"] == "ok"
    checks = {f["check"] for f in payload["findings"]}
    assert "install-script" in checks
    assert "new-dependency" in checks
    assert payload["summary"]["fix"] >= 1


def test_scan_dependency_changes_no_manifest_change_is_clean(tmp_path):
    repo = tmp_path / "repo2"
    _init_repo(repo)
    (repo / "app.ts").write_text("export const x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app.ts").write_text("export const x = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change source only")

    result = tools.scan_dependency_changes(str(repo), base_ref="main")
    payload = result["data"] if "data" in result else result
    assert payload["status"] == "ok"
    assert payload["findings"] == []


def test_scan_dependency_changes_non_git_degrades(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = tools.scan_dependency_changes(str(plain), base_ref="main")
    payload = result["data"] if "data" in result else result
    assert payload["status"] in ("degraded", "failed")


def test_scan_dependency_changes_unresolvable_repo_fails():
    result = tools.scan_dependency_changes("/nonexistent/repo/xyz", base_ref="main")
    payload = result["data"] if "data" in result else result
    assert payload["status"] == "failed"


def test_tool_output_is_sanitized(tmp_path):
    # A control byte in an untrusted manifest value must be stripped before it
    # reaches the model through the tool envelope (injection guard).
    repo = tmp_path / "repo_sanitize"
    _init_repo(repo)
    (repo / "package.json").write_text('{\n  "scripts": {\n    "build": "tsc"\n  }\n}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "package.json").write_text(
        '{\n  "scripts": {\n    "build": "tsc",\n    "postinstall": "node \x07evil.js"\n  }\n}\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "evil hook")

    result = tools.scan_dependency_changes(str(repo), base_ref="main")
    payload = result["data"] if "data" in result else result
    # There must be an install-script finding, and its echoed text must have the
    # control byte stripped (check the actual string, not repr which escapes it).
    assert payload["findings"], "expected the postinstall hook to be flagged"
    for f in payload["findings"]:
        assert "\x07" not in f["evidence"]
        assert "\x07" not in f["message"]
        assert "\x07" not in str(f["detail"])


def test_tool_signals_truncation(tmp_path, monkeypatch):
    # A diff larger than the cap must not silently drop content; the envelope
    # signals truncation (no silent caps).
    monkeypatch.setenv("CHAMELEON_DEP_DIFF_MAX_BYTES", "20")
    repo = tmp_path / "repo_trunc"
    _init_repo(repo)
    (repo / "package.json").write_text('{\n  "dependencies": {}\n}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "package.json").write_text(
        '{\n  "dependencies": {\n    "left-pad": "^1.3.0",\n    "right-pad": "^1.0.0"\n  }\n}\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "deps")

    result = tools.scan_dependency_changes(str(repo), base_ref="main")
    payload = result["data"] if "data" in result else result
    assert payload.get("truncated") is True


def test_failed_envelopes_include_findings_key():
    result = tools.scan_dependency_changes("/nonexistent/xyz", base_ref="main")
    payload = result["data"] if "data" in result else result
    assert payload["status"] == "failed"
    assert payload.get("findings") == []

    result2 = tools.scan_dependency_changes("/nonexistent/xyz", base_ref="")
    payload2 = result2["data"] if "data" in result2 else result2
    assert payload2.get("findings") == []
