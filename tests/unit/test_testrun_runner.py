"""Opt-in repo-local test runner for the auto-pass router.

Gated behind CHAMELEON_ALLOW_TESTS=1; resolves a runner only from the repo's own
node_modules/.bin (never PATH); returns one of three states (unavailable / clean /
failures). Unavailable is a recorded fact, never a failure, and a non-zero exit
with no runner resolved must never read as a failure or a clean run. Mirrors the
tsc runner contract.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from chameleon_mcp import testrun
from chameleon_mcp._thresholds import threshold_int


def _repo(tmp_path, *, runner="vitest"):
    if runner:
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / runner).write_text("#!/bin/sh\n", encoding="utf-8")
    return tmp_path


class TestGating:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv(testrun.ALLOW_ENV, raising=False)
        assert testrun.is_enabled() is False

    def test_enabled_only_on_exact_1(self, monkeypatch):
        monkeypatch.setenv(testrun.ALLOW_ENV, "1")
        assert testrun.is_enabled() is True
        monkeypatch.setenv(testrun.ALLOW_ENV, "true")
        assert testrun.is_enabled() is False


class TestAvailability:
    def test_no_repo_local_runner_is_unavailable(self, tmp_path):
        _repo(tmp_path, runner=None)
        result = testrun.run_tests(tmp_path)
        assert result["status"] == "unavailable"
        assert "node_modules" in result["reason"]

    def test_vitest_preferred_over_jest(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "vitest").write_text("#!/bin/sh\n", encoding="utf-8")
        (bin_dir / "jest").write_text("#!/bin/sh\n", encoding="utf-8")
        captured = {}

        def fake_run(args, **_k):
            captured["args"] = args
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(testrun.subprocess, "run", fake_run)
        testrun.run_tests(tmp_path)
        assert captured["args"][0].endswith("vitest")


class TestRun:
    def test_clean_run(self, tmp_path, monkeypatch):
        _repo(tmp_path)

        def fake_run(args, **_k):
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        monkeypatch.setattr(testrun.subprocess, "run", fake_run)
        result = testrun.run_tests(tmp_path)
        assert result["status"] == "clean"
        assert result["runner"] == "vitest"

    def test_nonzero_exit_with_test_output_is_failures(self, tmp_path, monkeypatch):
        _repo(tmp_path)

        def fake_run(args, **_k):
            return SimpleNamespace(returncode=1, stdout="Tests:  2 failed, 5 passed", stderr="")

        monkeypatch.setattr(testrun.subprocess, "run", fake_run)
        result = testrun.run_tests(tmp_path)
        assert result["status"] == "failures"
        assert result["exit_code"] == 1

    def test_nonzero_exit_without_test_output_is_unavailable(self, tmp_path, monkeypatch):
        # A config / missing-dep error (non-zero exit, no test summary) is "no
        # signal", never a test failure -- mirrors the tsc config-error case.
        _repo(tmp_path)

        def fake_run(args, **_k):
            return SimpleNamespace(
                returncode=1, stdout="", stderr="Error: Cannot find module 'vitest/config'"
            )

        monkeypatch.setattr(testrun.subprocess, "run", fake_run)
        result = testrun.run_tests(tmp_path)
        assert result["status"] == "unavailable"
        assert "config error" in result["reason"]

    def test_timeout_is_unavailable(self, tmp_path, monkeypatch):
        _repo(tmp_path)

        def fake_run(args, **_k):
            raise subprocess.TimeoutExpired(cmd=args, timeout=1)

        monkeypatch.setattr(testrun.subprocess, "run", fake_run)
        assert testrun.run_tests(tmp_path)["status"] == "unavailable"

    def test_spawn_failure_is_unavailable(self, tmp_path, monkeypatch):
        _repo(tmp_path)

        def fake_run(args, **_k):
            raise OSError("exec format error")

        monkeypatch.setattr(testrun.subprocess, "run", fake_run)
        assert testrun.run_tests(tmp_path)["status"] == "unavailable"

    def test_spawn_arguments(self, tmp_path, monkeypatch):
        _repo(tmp_path)
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(testrun.subprocess, "run", fake_run)
        testrun.run_tests(tmp_path)
        expected_bin = str(tmp_path / "node_modules" / ".bin" / "vitest")
        assert captured["args"][0] == expected_bin
        assert captured["kwargs"]["cwd"] == str(tmp_path)
        assert captured["kwargs"]["timeout"] == threshold_int("AUTOPASS_TESTRUN_TIMEOUT_SECONDS")
