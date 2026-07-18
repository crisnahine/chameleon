"""The profile's compatibility floor must be a real floor, not the writer's version.

`engine_min_version` was aliased to the package `__version__`, so every profile
declared "you need at least the exact engine that wrote me" and the loader's read
gate refused every older engine. Measured: 24 cached engines from 3.0.0 to 4.4.16
all load a current (schema 8) profile once the stamp is neutralised, so the
declared floor was wrong by the entire tested range. In a mixed-version team that
means one member's refresh strips everyone else of all guidance until they upgrade.

The field was also overloaded: refresh staleness detection reads the same key and
legitimately needs the WRITER's version, so the two meanings are split across
`engine_version` (writer) and `engine_min_version` (floor).
"""

from __future__ import annotations

import json
from pathlib import Path

from chameleon_mcp import __version__
from chameleon_mcp import tools as t
from chameleon_mcp.bootstrap.orchestrator import ENGINE_MIN_VERSION
from chameleon_mcp.profile.loader import _version_tuple


def test_floor_is_not_the_current_version():
    # The defect in one line: a floor that tracks the release makes every release
    # incompatible with the one before it.
    assert ENGINE_MIN_VERSION != __version__, (
        "engine_min_version must be an explicit compatibility floor, not the "
        "writing engine's version"
    )


def test_floor_is_at_or_below_the_oldest_verified_engine():
    # 3.0.0 is the oldest engine empirically confirmed to load a current profile.
    assert _version_tuple(ENGINE_MIN_VERSION) <= _version_tuple("3.0.0")


def test_a_profile_written_now_loads_on_an_older_engine():
    # The user-visible contract: a colleague one patch release behind must still
    # get guidance from a profile this engine wrote.
    older = "4.4.15"
    assert _version_tuple(older) >= _version_tuple(ENGINE_MIN_VERSION), (
        f"an engine at {older} would be refused a profile written by {__version__}"
    )


def _profile_dir(tmp_path: Path, body: dict) -> Path:
    pd = tmp_path / ".chameleon"
    pd.mkdir()
    (pd / "archetypes.json").write_text(json.dumps(body), encoding="utf-8")
    return pd


def test_refresh_staleness_reads_the_writer_version_not_the_floor(tmp_path):
    # Splitting the field must not break the other consumer: an engine upgrade
    # still has to force a re-cluster. With a static floor, reading the floor
    # here would report "changed" on every refresh forever.
    pd = _profile_dir(
        tmp_path,
        {
            "schema_version": 8,
            "generation": 1,
            "archetypes": {},
            "engine_version": "4.0.0",
            "engine_min_version": "3.0.0",
        },
    )
    assert t._engine_version_changed(pd, "4.4.16") is True
    assert t._engine_version_changed(pd, "4.0.0") is False


def test_refresh_staleness_falls_back_for_pre_split_profiles(tmp_path):
    # Profiles written before the split carry only engine_min_version, which for
    # them IS the writer's version, so staleness must still work.
    pd = _profile_dir(
        tmp_path,
        {
            "schema_version": 8,
            "generation": 1,
            "archetypes": {},
            "engine_min_version": "4.0.0",
        },
    )
    assert t._engine_version_changed(pd, "4.4.16") is True
    assert t._engine_version_changed(pd, "4.0.0") is False
