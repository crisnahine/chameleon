"""merge_profiles must be shape-aware. The git merge driver runs per-file over
profile.json / archetypes.json / rules.json / canonicals.json. The old code
hardcoded the 'archetypes' key and filtered output to _SAFE_TOP_LEVEL_KEYS
(which lacks 'canonicals'/'rules'), so merging a canonicals.json or rules.json
conflict wiped the real payload and merging profile.json zeroed archetype_count.

Covers audit finding SA-BUG-18 (silent data loss + profile load hard-fail).
"""

from __future__ import annotations

import json
from pathlib import Path

from chameleon_mcp import tools


def _write(p: Path, data: dict) -> Path:
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_merge_canonicals_preserves_payload(tmp_path):
    base = _write(tmp_path / "base.json", {"schema_version": 8, "generation": 1, "canonicals": {}})
    ours = _write(
        tmp_path / "ours.json",
        {
            "schema_version": 8,
            "generation": 2,
            "canonicals": {"component": {"witness_path": "a.tsx"}},
        },
    )
    theirs = _write(
        tmp_path / "theirs.json",
        {"schema_version": 8, "generation": 2, "canonicals": {"service": {"witness_path": "b.ts"}}},
    )
    out = tools.merge_profiles(
        repo=str(tmp_path), base=str(base), ours=str(ours), theirs=str(theirs)
    )
    assert out["data"]["status"] == "success"
    merged = json.loads(ours.read_text(encoding="utf-8"))
    # the canonicals payload must NOT be wiped, and must not be replaced by an archetypes key
    assert "canonicals" in merged
    assert merged["canonicals"]  # non-empty
    assert "component" in merged["canonicals"]
    assert merged.get("archetypes", "absent") in ("absent", merged.get("archetypes"))


def test_merge_rules_preserves_payload(tmp_path):
    base = _write(tmp_path / "base.json", {"schema_version": 8, "generation": 1, "rules": {}})
    ours = _write(
        tmp_path / "ours.json",
        {"schema_version": 8, "generation": 2, "rules": {"eslint": ["a"]}},
    )
    theirs = _write(
        tmp_path / "theirs.json",
        {"schema_version": 8, "generation": 2, "rules": {"rubocop": ["b"]}},
    )
    out = tools.merge_profiles(
        repo=str(tmp_path), base=str(base), ours=str(ours), theirs=str(theirs)
    )
    assert out["data"]["status"] == "success"
    merged = json.loads(ours.read_text(encoding="utf-8"))
    assert merged.get("rules"), "rules payload was wiped"
    assert "eslint" in merged["rules"]


def test_merge_profile_json_does_not_zero_archetype_count(tmp_path):
    base = _write(
        tmp_path / "base.json", {"schema_version": 8, "generation": 1, "archetype_count": 5}
    )
    ours = _write(
        tmp_path / "ours.json",
        {"schema_version": 8, "generation": 3, "archetype_count": 17, "language": "typescript"},
    )
    theirs = _write(
        tmp_path / "theirs.json",
        {"schema_version": 8, "generation": 2, "archetype_count": 12, "language": "typescript"},
    )
    out = tools.merge_profiles(
        repo=str(tmp_path), base=str(base), ours=str(ours), theirs=str(theirs)
    )
    assert out["data"]["status"] == "success"
    merged = json.loads(ours.read_text(encoding="utf-8"))
    # must not zero the count; should keep a real count (the newer generation's 17)
    assert merged.get("archetype_count", 0) > 0
    assert merged["archetype_count"] == 17


def test_merge_archetypes_still_unions(tmp_path):
    # regression: the archetypes shape must keep the existing union behavior
    base = _write(tmp_path / "base.json", {"schema_version": 8, "generation": 1, "archetypes": {}})
    ours = _write(
        tmp_path / "ours.json",
        {"schema_version": 8, "generation": 2, "archetypes": {"a": {"cluster_size": 10}}},
    )
    theirs = _write(
        tmp_path / "theirs.json",
        {"schema_version": 8, "generation": 2, "archetypes": {"b": {"cluster_size": 20}}},
    )
    out = tools.merge_profiles(
        repo=str(tmp_path), base=str(base), ours=str(ours), theirs=str(theirs)
    )
    assert out["data"]["status"] == "success"
    merged = json.loads(ours.read_text(encoding="utf-8"))
    assert set(merged["archetypes"]) == {"a", "b"}
    assert merged["archetype_count"] == 2
