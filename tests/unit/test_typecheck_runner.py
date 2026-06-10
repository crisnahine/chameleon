"""Opt-in repo-local ``tsc --noEmit`` runner for the auto-pass router.

Gated behind CHAMELEON_ALLOW_TSC=1; resolves tsc only from the repo's own
node_modules/.bin (never PATH, never a download); returns one of three states
(unavailable / clean / errors). Unavailable is a recorded fact, never a
failure, and a config error must never read as a clean run.
"""

from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace

from chameleon_mcp import typecheck
from chameleon_mcp._thresholds import threshold_int


def _repo(tmp_path, *, tsconfig=True, tsc=True):
    if tsconfig:
        (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    if tsc:
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "tsc").write_text("#!/bin/sh\n", encoding="utf-8")
    return tmp_path


class TestGating:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv(typecheck.ALLOW_ENV, raising=False)
        assert typecheck.is_enabled() is False

    def test_enabled_only_on_exact_1(self, monkeypatch):
        monkeypatch.setenv(typecheck.ALLOW_ENV, "1")
        assert typecheck.is_enabled() is True
        monkeypatch.setenv(typecheck.ALLOW_ENV, "0")
        assert typecheck.is_enabled() is False
        monkeypatch.setenv(typecheck.ALLOW_ENV, "true")
        assert typecheck.is_enabled() is False


class TestAvailability:
    def test_no_tsconfig_is_unavailable(self, tmp_path):
        _repo(tmp_path, tsconfig=False)
        result = typecheck.run_tsc(tmp_path)
        assert result["status"] == "unavailable"
        assert "tsconfig.json" in result["reason"]

    def test_no_repo_local_binary_is_unavailable(self, tmp_path):
        _repo(tmp_path, tsc=False)
        result = typecheck.run_tsc(tmp_path)
        assert result["status"] == "unavailable"
        assert "node_modules" in result["reason"]

    def test_path_is_never_consulted(self, tmp_path, monkeypatch):
        # A globally-installed tsc on PATH must not be picked up: the runner
        # executes only the repo's own dependency.
        _repo(tmp_path, tsc=False)
        monkeypatch.setattr(shutil, "which", lambda _b: "/usr/local/bin/tsc")
        result = typecheck.run_tsc(tmp_path)
        assert result["status"] == "unavailable"


class TestRun:
    def test_diagnostics_parse_to_errors_with_dedup_and_posix_paths(self, tmp_path, monkeypatch):
        _repo(tmp_path)
        out = (
            "src/a.ts(1,5): error TS2322: Type 'string' is not assignable.\n"
            "src/a.ts(9,1): error TS2304: Cannot find name 'x'.\n"
            "src\\b.ts(2,1): error TS2304: Cannot find name 'y'.\n"
            "Found 3 errors.\n"
        )

        def fake_run(args, **_k):
            return SimpleNamespace(returncode=2, stdout=out, stderr="")

        monkeypatch.setattr(typecheck.subprocess, "run", fake_run)
        result = typecheck.run_tsc(tmp_path)
        assert result["status"] == "errors"
        assert sorted(result["files"]) == ["src/a.ts", "src/b.ts"]
        assert result["diagnostics"] == 3

    def test_clean_run(self, tmp_path, monkeypatch):
        _repo(tmp_path)

        def fake_run(args, **_k):
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(typecheck.subprocess, "run", fake_run)
        result = typecheck.run_tsc(tmp_path)
        assert result == {"status": "clean", "files": []}

    def test_nonzero_exit_without_diagnostics_is_unavailable_never_clean(
        self, tmp_path, monkeypatch
    ):
        _repo(tmp_path)

        def fake_run(args, **_k):
            return SimpleNamespace(
                returncode=1,
                stdout="error TS18003: No inputs were found in config file.\n",
                stderr="",
            )

        monkeypatch.setattr(typecheck.subprocess, "run", fake_run)
        result = typecheck.run_tsc(tmp_path)
        assert result["status"] == "unavailable"
        assert "exited 1" in result["reason"]

    def test_timeout_is_unavailable(self, tmp_path, monkeypatch):
        _repo(tmp_path)

        def fake_run(args, **_k):
            raise subprocess.TimeoutExpired(cmd=args, timeout=1)

        monkeypatch.setattr(typecheck.subprocess, "run", fake_run)
        result = typecheck.run_tsc(tmp_path)
        assert result["status"] == "unavailable"
        assert "timed out" in result["reason"]

    def test_spawn_failure_is_unavailable(self, tmp_path, monkeypatch):
        _repo(tmp_path)

        def fake_run(args, **_k):
            raise OSError("exec format error")

        monkeypatch.setattr(typecheck.subprocess, "run", fake_run)
        assert typecheck.run_tsc(tmp_path)["status"] == "unavailable"

    def test_spawn_arguments(self, tmp_path, monkeypatch):
        _repo(tmp_path)
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(typecheck.subprocess, "run", fake_run)
        typecheck.run_tsc(tmp_path)
        expected_bin = str(tmp_path / "node_modules" / ".bin" / "tsc")
        assert captured["args"] == [expected_bin, "--noEmit", "--pretty", "false"]
        assert captured["kwargs"]["cwd"] == str(tmp_path)
        assert captured["kwargs"]["timeout"] == threshold_int("AUTOPASS_TSC_TIMEOUT_SECONDS")

    def test_garbage_output_never_raises(self, tmp_path, monkeypatch):
        _repo(tmp_path)

        def fake_run(args, **_k):
            return SimpleNamespace(returncode=0, stdout="\x00\xff garbage \n???", stderr=None)

        monkeypatch.setattr(typecheck.subprocess, "run", fake_run)
        assert typecheck.run_tsc(tmp_path)["status"] == "clean"

        def fake_run_rc(args, **_k):
            return SimpleNamespace(returncode=7, stdout="\x00garbage", stderr="more garbage")

        monkeypatch.setattr(typecheck.subprocess, "run", fake_run_rc)
        assert typecheck.run_tsc(tmp_path)["status"] == "unavailable"
