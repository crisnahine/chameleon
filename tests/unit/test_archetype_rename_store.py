"""apply_archetype_renames writes the idiom STORE, not the generated view.

Before this, a rename on a store-backed profile rewrote idioms.md's
"Archetype:" lines directly, leaving the store untouched — the next store
write's regenerate_views() then re-derived idioms.md from the un-renamed
records and silently reverted the rename. `rename_archetypes` (in
core/idiom_store.py) is the fix: it remaps each record's `archetypes` list,
the source of truth, and regenerates the view from the updated store.

Covers:
  - a migrated store: record archetypes updated, view's "Archetype:" line
    updated (the brief's test, verbatim).
  - deprecated records are renamed too (load_store returns every status;
    the rename must not silently skip anything but active idioms).
  - an unmigrated (legacy, no store) repo still goes through
    apply_archetype_renames' markdown-rewrite fallback untouched.
  - a rename map that matches no record's archetypes is a true no-op:
    zero changed, and the view is not regenerated.
"""

from __future__ import annotations

import json

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


def test_rename_updates_store_and_view(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp.core.idiom_store import (
        IdiomRecord,
        load_store,
        regenerate_views,
        rename_archetypes,
        upsert_idiom,
    )

    profile = tmp_path / "repo" / ".chameleon"
    profile.mkdir(parents=True)
    (profile / "profile.json").write_text('{"generation": 1, "language": "typescript"}')
    upsert_idiom(
        profile,
        IdiomRecord(
            slug="svc-rule",
            title="svc-rule",
            rationale="Service rule.",
            archetypes=["service"],
            status="active",
            added_date="2026-07-14",
            rank=1,
        ),
    )
    regenerate_views(profile)
    assert rename_archetypes(profile, {"service": "svc"}, repo_id=None) == 1
    assert load_store(profile)[0].archetypes == ["svc"]
    assert "Archetype: svc" in (profile / "idioms.md").read_text()


def test_rename_updates_deprecated_records_too(tmp_path):
    """load_store returns every status; a deprecated idiom carrying the
    renamed archetype must be remapped along with active ones, and the
    regenerated view's "## deprecated" section must reflect it."""
    from chameleon_mcp.core.idiom_store import (
        IdiomRecord,
        load_store,
        regenerate_views,
        rename_archetypes,
        upsert_idiom,
    )

    profile = tmp_path / "repo" / ".chameleon"
    profile.mkdir(parents=True)
    (profile / "profile.json").write_text('{"generation": 1, "language": "typescript"}')
    upsert_idiom(
        profile,
        IdiomRecord(
            slug="active-rule",
            title="active-rule",
            rationale="Still-active rule.",
            archetypes=["service"],
            status="active",
            added_date="2026-07-14",
            rank=1,
        ),
    )
    upsert_idiom(
        profile,
        IdiomRecord(
            slug="old-rule",
            title="old-rule",
            rationale="Retired rule, kept for history.",
            archetypes=["service"],
            status="deprecated",
            added_date="2026-05-01",
            deprecated_date="2026-06-01",
            rank=2,
        ),
    )
    # A third record with no matching archetype must be left alone.
    upsert_idiom(
        profile,
        IdiomRecord(
            slug="unrelated-rule",
            title="unrelated-rule",
            rationale="Untouched rule.",
            archetypes=["component"],
            status="active",
            added_date="2026-07-14",
            rank=3,
        ),
    )
    regenerate_views(profile)

    changed = rename_archetypes(profile, {"service": "svc"}, repo_id=None)
    assert changed == 2

    records = {r.slug: r for r in load_store(profile)}
    assert records["active-rule"].archetypes == ["svc"]
    assert records["old-rule"].archetypes == ["svc"]
    assert records["unrelated-rule"].archetypes == ["component"]

    view = (profile / "idioms.md").read_text()
    deprecated_section = view.split("## deprecated", 1)[1]
    assert "Archetype: svc" in deprecated_section
    assert "Archetype: service" not in view


def test_apply_archetype_renames_unmigrated_uses_legacy_rewrite(tmp_path):
    """A repo with no idioms/ store directory must keep going through
    apply_archetype_renames' markdown re.sub fallback: idioms.md text
    changes, and no store directory is created as a side effect."""
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(
        json.dumps({"schema_version": 7, "generation": 1, "language": "typescript"})
    )
    (cham / "archetypes.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "archetypes": {"service": {"cluster_size": 5, "paths_pattern": "src/services:ts"}},
            }
        )
    )
    (cham / "canonicals.json").write_text(json.dumps({"generation": 1, "canonicals": {}}))
    (cham / "rules.json").write_text(json.dumps({"generation": 1, "rules": {}}))
    (cham / "idioms.md").write_text(
        "# idioms\n\n## active\n\n"
        "### svc-rule\n"
        "Language: any\n"
        "Status: active (added 2026-07-14)\n"
        "Archetype: service\n"
        "Always use the shared client.\n\n"
        "## deprecated\n"
    )
    (cham / "COMMITTED").touch()

    res = tools.apply_archetype_renames(str(repo), {"service": "svc-renamed"})
    assert res["data"]["status"] == "success"
    assert res["data"]["renames_applied"] == 1

    idioms_text = (cham / "idioms.md").read_text()
    assert "Archetype: svc-renamed" in idioms_text
    assert "Archetype: service" not in idioms_text
    assert not (cham / "idioms").is_dir()


def test_rename_noop_returns_zero_and_does_not_regenerate_view(tmp_path):
    """A rename map that matches no record's archetypes must be a true
    no-op: zero changed, and the view (and its digest) must not be
    rewritten — regenerate_views must never run when nothing changed."""
    from chameleon_mcp.core.idiom_store import (
        IdiomRecord,
        load_store,
        regenerate_views,
        rename_archetypes,
        upsert_idiom,
    )

    profile = tmp_path / "repo" / ".chameleon"
    profile.mkdir(parents=True)
    (profile / "profile.json").write_text('{"generation": 1, "language": "typescript"}')
    upsert_idiom(
        profile,
        IdiomRecord(
            slug="svc-rule",
            title="svc-rule",
            rationale="Service rule.",
            archetypes=["service"],
            status="active",
            added_date="2026-07-14",
            rank=1,
        ),
    )
    regenerate_views(profile)

    idioms_path = profile / "idioms.md"
    before_text = idioms_path.read_text()
    before_mtime_ns = idioms_path.stat().st_mtime_ns
    before_digest = (profile / "idioms" / ".view_digest").read_text()

    changed = rename_archetypes(profile, {"unrelated-archetype": "whatever"}, repo_id=None)

    assert changed == 0
    assert load_store(profile)[0].archetypes == ["service"]
    assert idioms_path.read_text() == before_text
    assert idioms_path.stat().st_mtime_ns == before_mtime_ns
    assert (profile / "idioms" / ".view_digest").read_text() == before_digest
