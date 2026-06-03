"""Regression coverage for hardening fixes surfaced by the full-QA sweep.

Three independent read-tool defects, each reproduced from a hostile or
degraded input that the tool must survive without raising:

  - ``lint_file`` resolved its ``repo`` argument through
    ``_resolve_repo_root_by_id`` directly, so a non-resolvable path
    (nonexistent absolute path, embedded NUL, ``../`` traversal) reached
    ``repo_data_dir`` and crashed on ``Path.mkdir`` instead of returning the
    documented stub envelope. It must fail open like every sibling tool.
  - ``get_pattern_context`` mapped every profile-load failure to
    ``profile_corrupted``. A profile written by a newer engine
    (``schema_version`` above ``MAX_SUPPORTED_SCHEMA_VERSION``) must surface
    as ``profile_unsupported_schema_version``, matching ``detect_repo``.
  - ``merge_profiles`` assumed both sides of an ``archetypes`` merge were
    dicts. One side carrying a JSON array must produce a clean failed
    envelope, not an unhandled ``AttributeError``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chameleon_mcp import tools


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp.profile import loader as _loader

    _loader._PROFILE_CACHE.clear()
    _loader._REPO_ROOT_CACHE.clear()
    tools._clear_repo_id_cache()
    yield
    _loader._PROFILE_CACHE.clear()
    _loader._REPO_ROOT_CACHE.clear()
    tools._clear_repo_id_cache()


# ---- lint_file fail-open on a hostile repo argument ------------------------


@pytest.mark.parametrize(
    "repo_arg",
    [
        "/does/not/exist/repo",  # absolute path, no such dir
        "/tmp/test\x00repo",  # embedded NUL byte
        "../../../../etc",  # path traversal
    ],
)
def test_lint_file_fails_open_on_unresolvable_repo(repo_arg):
    # Must return a stub envelope, never raise.
    result = tools.lint_file(repo=repo_arg, archetype="component", content="const x = 1;\n")
    data = result["data"]
    assert data["stub"] is True
    assert "repo could not be resolved" in (data.get("stub_reason") or "")


# ---- get_pattern_context labels an unsupported-schema profile correctly ----


def _write_min_profile(repo: Path, schema_version) -> Path:
    (repo / ".chameleon").mkdir(parents=True, exist_ok=True)
    (repo / "src").mkdir(parents=True, exist_ok=True)
    src = repo / "src" / "x.ts"
    src.write_text("export const x = 1;\n", encoding="utf-8")
    (repo / ".chameleon" / "profile.json").write_text(
        json.dumps({"schema_version": schema_version, "repo_id": "x", "language": "typescript"}),
        encoding="utf-8",
    )
    return src


def test_get_pattern_context_reports_unsupported_schema_not_corrupted(tmp_path):
    src = _write_min_profile(tmp_path, 999)
    result = tools.get_pattern_context(str(src))
    assert result["data"]["repo"]["profile_status"] == "profile_unsupported_schema_version"


def test_get_pattern_context_reports_corrupted_on_unparseable_profile(tmp_path):
    (tmp_path / ".chameleon").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    src = tmp_path / "src" / "x.ts"
    src.write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / ".chameleon" / "profile.json").write_text("{garbage", encoding="utf-8")
    result = tools.get_pattern_context(str(src))
    assert result["data"]["repo"]["profile_status"] == "profile_corrupted"


# ---- merge_profiles fails open when one side's archetypes is an array ------


def test_merge_profiles_fails_open_when_archetypes_is_array(tmp_path):
    base = tmp_path / "base.json"
    ours = tmp_path / "ours.json"
    theirs = tmp_path / "theirs.json"
    common = {"schema_version": 2, "repo_id": "r", "generation": 2}
    base.write_text(json.dumps({**common, "archetypes": {}}))
    ours.write_text(
        json.dumps(
            {
                **common,
                "archetypes": {
                    "foo": {"cluster_size": 5, "canonical_witness": "a.ts", "summary": "x"}
                },
            }
        )
    )
    theirs.write_text(json.dumps({**common, "archetypes": ["not", "a", "dict"]}))

    result = tools.merge_profiles(repo="", base=str(base), ours=str(ours), theirs=str(theirs))
    data = result["data"]
    assert data["status"] == "failed"
    assert "archetypes" in (data.get("error") or "")
