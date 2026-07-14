"""Envelope counts: teach_profile / teach_profile_structured / trust_profile
success envelopes gain "idioms_migrated" / "idioms_quarantined" ONLY when
their own call actually ran the idioms.md -> store migration (migrate_idioms_md
returned status == "migrated"), never on a noop against an already-migrated
repo."""

from __future__ import annotations

import json

import pytest

from chameleon_mcp import tools
from chameleon_mcp.core.idiom_store import migrate_idioms_md

CORPUS = """# idioms

## active

### use-api-client
Language: typescript
Archetype: service
Status: active (added 2026-07-01)
Always use the apiClient helper for HTTP calls.

Example:
```
const r = apiClient.get('/x');
```

### Fence Trap: colon name
Language: any
Status: active (added 2026-06-20)
Example fences must not fork sections.

Example:
```
## deprecated
### not-a-real-block
```

### free-form-note
Status: active (added 2026-06-01)
Prefer small components.

## deprecated

### no-raw-sql
Status: deprecated 2026-07-01
Use the query builder instead.
"""

# Adds a fifth block with no rationale -- unparseable, so it quarantines
# without tripping the injection scan (which would refuse the whole teach/
# trust call before migration ever ran; see the poison pre-checks in
# teach_profile / teach_profile_structured / trust_profile).
QUARANTINE_CORPUS = CORPUS + "\n### only-a-header\n"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")


def _repo_with_md(tmp_path, text=CORPUS):
    """Just enough profile surface for teach_profile: no store yet."""
    r = tmp_path / "repo"
    cham = r / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(json.dumps({"generation": 1, "language": "typescript"}))
    (cham / "idioms.md").write_text(text, encoding="utf-8")
    return r


def _loadable_repo_with_md(tmp_path, text=CORPUS):
    """A profile complete enough for load_profile_dir (used by trust_profile):
    the four required JSON artifacts, matching generation, and COMMITTED."""
    r = tmp_path / "repo"
    cham = r / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(json.dumps({"generation": 1, "language": "typescript"}))
    (cham / "archetypes.json").write_text('{"generation": 1, "archetypes": {}}')
    (cham / "canonicals.json").write_text('{"generation": 1, "canonicals": {}}')
    (cham / "rules.json").write_text('{"generation": 1, "rules": {}}')
    (cham / "idioms.md").write_text(text, encoding="utf-8")
    (cham / "COMMITTED").touch()
    return r


def test_teach_on_legacy_repo_surfaces_migration_count(tmp_path):
    repo = _repo_with_md(tmp_path)
    result = tools.teach_profile(str(repo), "Always use the queryBuilder helper.")
    data = result["data"]
    assert data["status"] == "success"
    assert data["idioms_migrated"] == 4
    assert data["idioms_quarantined"] == 0


def test_teach_on_already_migrated_repo_has_neither_key(tmp_path):
    repo = _repo_with_md(tmp_path)
    cham = repo / ".chameleon"
    migrate_idioms_md(cham, repo_id="a" * 64)

    result = tools.teach_profile(str(repo), "Always use the queryBuilder helper.")
    data = result["data"]
    assert data["status"] == "success"
    assert "idioms_migrated" not in data
    assert "idioms_quarantined" not in data


def test_teach_on_legacy_repo_surfaces_quarantine_count(tmp_path):
    repo = _repo_with_md(tmp_path, QUARANTINE_CORPUS)
    result = tools.teach_profile(str(repo), "Always use the queryBuilder helper.")
    data = result["data"]
    assert data["status"] == "success"
    assert data["idioms_migrated"] == 4
    assert data["idioms_quarantined"] == 1


def test_teach_structured_on_legacy_repo_surfaces_migration_count(tmp_path):
    repo = _repo_with_md(tmp_path)
    result = tools.teach_profile_structured(
        str(repo), slug="new-rule", rationale="Prefer the queryBuilder helper.", status="deprecated"
    )
    data = result["data"]
    assert data["status"] == "success"
    assert data["idioms_migrated"] == 4
    assert data["idioms_quarantined"] == 0


def test_trust_on_legacy_repo_surfaces_migration_pair(tmp_path):
    repo = _loadable_repo_with_md(tmp_path)
    result = tools.trust_profile(str(repo), repo.name)
    data = result["data"]
    assert data["status"] == "success"
    assert data["idioms_migrated"] == 4
    assert data["idioms_quarantined"] == 0


def test_trust_on_already_migrated_repo_has_neither_key(tmp_path):
    repo = _loadable_repo_with_md(tmp_path)
    cham = repo / ".chameleon"
    migrate_idioms_md(cham, repo_id="a" * 64)

    result = tools.trust_profile(str(repo), repo.name)
    data = result["data"]
    assert data["status"] == "success"
    assert "idioms_migrated" not in data
    assert "idioms_quarantined" not in data
