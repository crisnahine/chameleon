"""Trust-hash surface: store truth is hashed, the generated view is not."""

from __future__ import annotations

import hashlib

from chameleon_mcp.core.idiom_store import IdiomRecord, migrate_idioms_md, upsert_idiom
from chameleon_mcp.profile.trust import _HASHED_ARTIFACTS, hash_profile


def _profile(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    p = tmp_path / "repo" / ".chameleon"
    p.mkdir(parents=True)
    (p / "profile.json").write_text('{"generation": 1, "language": "typescript"}')
    (p / "idioms.md").write_text("# idioms\n\n## active\n\n## deprecated\n")
    return p


def _legacy_reference_hash(profile_dir):
    """Today's algorithm, inlined: the unmigrated hash must never drift from it."""
    h = hashlib.sha256()
    for filename in _HASHED_ARTIFACTS:
        artifact = profile_dir / filename
        if not artifact.is_file():
            continue
        h.update(b"\x00" + filename.encode("utf-8") + b"\x00")
        h.update(artifact.read_bytes())
    return h.hexdigest()


def test_unmigrated_profile_hashes_exactly_as_today(tmp_path, monkeypatch):
    profile = _profile(tmp_path, monkeypatch)
    assert hash_profile(profile) == _legacy_reference_hash(profile)
    (profile / "idioms.md").write_text("# idioms\n\n## active\n\n### x\nBody.\n\n## deprecated\n")
    assert hash_profile(profile) == _legacy_reference_hash(profile)


def test_migrated_profile_ignores_view_and_hashes_store(tmp_path, monkeypatch):
    profile = _profile(tmp_path, monkeypatch)
    migrate_idioms_md(profile, repo_id=None)
    base = hash_profile(profile)
    # Rewriting the generated view must not flip trust.
    (profile / "idioms.md").write_text("# idioms\n\nhand edited view\n")
    assert hash_profile(profile) == base
    # A store record change must.
    upsert_idiom(
        profile,
        IdiomRecord(
            slug="new-rule",
            title="new-rule",
            rationale="A new rule.",
            rank=1,
            added_date="2026-07-14",
        ),
    )
    assert hash_profile(profile) != base


def test_metadata_files_are_not_hashed(tmp_path, monkeypatch):
    profile = _profile(tmp_path, monkeypatch)
    migrate_idioms_md(profile, repo_id=None)
    base = hash_profile(profile)
    (profile / "idioms" / ".view_digest").write_text("0" * 64 + "\n")
    (profile / "idioms" / ".quarantine.md").write_text("# quarantined\n")
    assert hash_profile(profile) == base
