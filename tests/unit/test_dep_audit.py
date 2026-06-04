"""Opt-in dependency / supply-chain audit helper.

The audit is gated behind CHAMELEON_ALLOW_DEP_AUDIT=1 (refuse otherwise), runs the
ecosystem auditors whose manifests exist, and fails open to an "unavailable"
result when a binary or the network is absent. Advisory only.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from chameleon_mcp import dep_audit, tools


class TestGating:
    def test_refuses_without_env_flag(self, tmp_path, monkeypatch):
        monkeypatch.delenv(dep_audit.ALLOW_ENV, raising=False)
        out = tools.dep_audit(str(tmp_path))
        assert out["data"]["status"] == "failed"
        assert dep_audit.ALLOW_ENV in out["data"]["error"]

    def test_is_enabled_reads_env(self, monkeypatch):
        monkeypatch.delenv(dep_audit.ALLOW_ENV, raising=False)
        assert dep_audit.is_enabled() is False
        monkeypatch.setenv(dep_audit.ALLOW_ENV, "1")
        assert dep_audit.is_enabled() is True
        monkeypatch.setenv(dep_audit.ALLOW_ENV, "0")
        assert dep_audit.is_enabled() is False


class TestManifestSelection:
    def test_no_manifests_runs_nothing(self, tmp_path):
        result = dep_audit.run_dep_audit(tmp_path)
        assert result["ran"] == []
        assert result["audits"] == []
        assert any("npm" in s for s in result["skipped"])
        assert any("bundler" in s for s in result["skipped"])

    def test_npm_manifest_triggers_npm_only(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(dep_audit.shutil, "which", lambda _b: None)
        result = dep_audit.run_dep_audit(tmp_path)
        assert result["ran"] == ["npm-audit"]
        assert result["audits"][0]["tool"] == "npm-audit"
        assert result["audits"][0]["status"] == "unavailable"

    def test_gemfile_triggers_bundler_only(self, tmp_path, monkeypatch):
        (tmp_path / "Gemfile.lock").write_text("", encoding="utf-8")
        monkeypatch.setattr(dep_audit.shutil, "which", lambda _b: None)
        result = dep_audit.run_dep_audit(tmp_path)
        assert result["ran"] == ["bundler-audit"]
        assert result["audits"][0]["tool"] == "bundler-audit"
        assert result["audits"][0]["status"] == "unavailable"


class TestNpmAudit:
    def test_parses_severity_summary(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(dep_audit.shutil, "which", lambda _b: "/usr/bin/npm")
        payload = {
            "vulnerabilities": {
                "lodash": {"severity": "high", "via": [{"title": "Prototype Pollution"}]}
            },
            "metadata": {
                "vulnerabilities": {
                    "info": 0,
                    "low": 0,
                    "moderate": 0,
                    "high": 1,
                    "critical": 0,
                    "total": 1,
                }
            },
        }

        def fake_run(args, **_k):
            # npm exits non-zero when vulnerabilities are found; that is still a
            # successful audit with parseable JSON.
            return SimpleNamespace(returncode=1, stdout=json.dumps(payload), stderr="")

        monkeypatch.setattr(dep_audit.subprocess, "run", fake_run)
        result = dep_audit.run_dep_audit(tmp_path)
        npm = result["audits"][0]
        assert npm["status"] == "ok"
        assert npm["total"] == 1
        assert npm["severities"]["high"] == 1
        assert npm["findings"][0]["package"] == "lodash"
        assert "Prototype Pollution" in npm["findings"][0]["via"]

    def test_unparseable_output_fails_open(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(dep_audit.shutil, "which", lambda _b: "/usr/bin/npm")

        def fake_run(args, **_k):
            return SimpleNamespace(returncode=1, stdout="npm ERR! network", stderr="")

        monkeypatch.setattr(dep_audit.subprocess, "run", fake_run)
        result = dep_audit.run_dep_audit(tmp_path)
        assert result["audits"][0]["status"] == "unavailable"

    def test_timeout_fails_open(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(dep_audit.shutil, "which", lambda _b: "/usr/bin/npm")

        def fake_run(args, **_k):
            raise subprocess.TimeoutExpired(cmd=args, timeout=1)

        monkeypatch.setattr(dep_audit.subprocess, "run", fake_run)
        result = dep_audit.run_dep_audit(tmp_path)
        assert result["audits"][0]["status"] == "unavailable"
        assert "timed out" in result["audits"][0]["reason"]


class TestBundlerAudit:
    def test_parses_advisory_blocks(self, tmp_path, monkeypatch):
        (tmp_path / "Gemfile.lock").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            dep_audit.shutil, "which", lambda b: "/usr/bin/bundler-audit" if "audit" in b else None
        )
        out = (
            "Name: rack\n"
            "Advisory: CVE-2023-1\n"
            "Criticality: High\n"
            "Title: Denial of Service\n"
            "\nVulnerabilities found!\n"
        )

        def fake_run(args, **_k):
            return SimpleNamespace(returncode=1, stdout=out, stderr="")

        monkeypatch.setattr(dep_audit.subprocess, "run", fake_run)
        result = dep_audit.run_dep_audit(tmp_path)
        ba = result["audits"][0]
        assert ba["status"] == "ok"
        assert ba["total"] == 1
        assert ba["findings"][0]["package"] == "rack"
        assert ba["findings"][0]["severity"] == "High"


class TestToolEnvelope:
    def test_tool_returns_advisory_envelope_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv(dep_audit.ALLOW_ENV, "1")
        out = tools.dep_audit(str(tmp_path))
        assert out["data"]["advisory"] is True
        assert out["data"]["ran"] == []

    def test_tool_unresolvable_repo_fails(self, monkeypatch):
        monkeypatch.setenv(dep_audit.ALLOW_ENV, "1")
        out = tools.dep_audit("/nonexistent/repo/path/xyz")
        assert out["data"]["status"] == "failed"
