"""Tests for the bump-version.sh --validate-profiles command.

A major version bump can raise the engine's supported profile schema_version.
Committed .chameleon/profile.json files in the repo (test fixtures, sample
profiles) carry their own schema_version. --validate-profiles scans for those
and warns when one would be unsupported by the engine's MAX_SUPPORTED_SCHEMA_VERSION,
so a release that breaks profile compatibility is surfaced before it ships.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "bump-version.sh"


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )


def test_validate_profiles_flag_is_recognized():
    # The flag must not fall through to the "unknown flag" branch.
    result = _run("--validate-profiles", str(REPO_ROOT / "scripts"))
    assert "unknown flag" not in result.stderr
    assert result.returncode == 0


def test_validate_profiles_clean_when_schema_supported(tmp_path: Path):
    prof = tmp_path / "repoA" / ".chameleon"
    prof.mkdir(parents=True)
    (prof / "profile.json").write_text(json.dumps({"schema_version": 1}))

    result = _run("--validate-profiles", str(tmp_path))
    assert result.returncode == 0
    assert "INCOMPATIBLE" not in result.stdout


def test_validate_profiles_warns_on_unsupported_schema(tmp_path: Path):
    prof = tmp_path / "repoB" / ".chameleon"
    prof.mkdir(parents=True)
    # A schema far above any engine support level.
    (prof / "profile.json").write_text(json.dumps({"schema_version": 9999}))

    result = _run("--validate-profiles", str(tmp_path))
    assert "INCOMPATIBLE" in result.stdout
    assert "repoB" in result.stdout or "profile.json" in result.stdout


def test_validate_profiles_ignores_malformed_profile(tmp_path: Path):
    prof = tmp_path / "repoC" / ".chameleon"
    prof.mkdir(parents=True)
    (prof / "profile.json").write_text("{ not valid json")

    # Must not crash on a malformed profile; treat as nothing to validate.
    result = _run("--validate-profiles", str(tmp_path))
    assert result.returncode == 0
