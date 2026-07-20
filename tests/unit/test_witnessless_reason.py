"""Witnessless-archetype honesty: why an archetype has no canonical witness.

Bootstrap stamps ``witnessless_reason`` ("poisoning_only" | "no_eligible") on an
archetype it emits with no canonical, the same-pattern collapse strips the
marker when a merge hands the keeper a real witness, and get_canonical_excerpt
maps the stamp onto an accurate no_witness sentence instead of the generic one.
"""

from __future__ import annotations

import json

import pytest

from chameleon_mcp import tools
from chameleon_mcp.bootstrap.orchestrator import (
    _collapse_same_pattern_archetypes,
    _witnessless_reason,
)


class _FakeKey:
    def __init__(self, tag: str) -> None:
        self._tag = tag

    def to_dict(self) -> dict:
        return {"tag": self._tag}


class _FakeCluster:
    def __init__(self, tag: str) -> None:
        self.key = _FakeKey(tag)
        self.split_tag = ""


class _FakeSelection:
    def __init__(self, poisoning=(), no_eligible=()) -> None:
        self.clusters_failing_poisoning_only = list(poisoning)
        self.clusters_without_eligible_canonical = list(no_eligible)


# --------------------------------------------------------------------------- #
# _witnessless_reason: which witnessless category a cluster fell into
# --------------------------------------------------------------------------- #


def test_reason_poisoning_only():
    cluster = _FakeCluster("parsers")
    sel = _FakeSelection(poisoning=[_FakeCluster("parsers")])
    assert _witnessless_reason(cluster, sel) == "poisoning_only"


def test_reason_no_eligible():
    cluster = _FakeCluster("specs")
    sel = _FakeSelection(no_eligible=[_FakeCluster("specs")])
    assert _witnessless_reason(cluster, sel) == "no_eligible"


def test_reason_none_for_witnessed_cluster():
    cluster = _FakeCluster("services")
    sel = _FakeSelection(poisoning=[_FakeCluster("parsers")], no_eligible=[_FakeCluster("specs")])
    assert _witnessless_reason(cluster, sel) is None


def test_reason_fails_open_on_broken_selection():
    cluster = _FakeCluster("parsers")
    assert _witnessless_reason(cluster, object()) is None


# --------------------------------------------------------------------------- #
# collapse: a keeper that inherits a loser's canonical loses the stale marker
# --------------------------------------------------------------------------- #


def test_collapse_strips_marker_when_keeper_gains_witness():
    archetypes = {
        "big": {
            "paths_pattern": "src/*.ts",
            "cluster_size": 4,
            "witnessless_reason": "poisoning_only",
        },
        "small": {"paths_pattern": "src/*.ts", "cluster_size": 1},
    }
    canonicals = {"small": [{"witness": {"path": "src/a.ts"}}]}
    merged_arch, merged_canon = _collapse_same_pattern_archetypes(archetypes, canonicals)
    assert "small" not in merged_arch
    assert merged_canon["big"], "keeper should inherit the loser's canonical"
    assert "witnessless_reason" not in merged_arch["big"]


def test_collapse_keeps_marker_when_still_witnessless():
    archetypes = {
        "big": {
            "paths_pattern": "src/*.ts",
            "cluster_size": 4,
            "witnessless_reason": "poisoning_only",
        },
        "small": {"paths_pattern": "src/*.ts", "cluster_size": 1},
    }
    merged_arch, merged_canon = _collapse_same_pattern_archetypes(archetypes, {})
    assert merged_arch["big"]["witnessless_reason"] == "poisoning_only"
    assert merged_canon["big"] == []


def test_collapse_no_merge_keeps_marker():
    archetypes = {
        "lonely": {
            "paths_pattern": "lib/*.ts",
            "cluster_size": 2,
            "witnessless_reason": "no_eligible",
        }
    }
    merged_arch, _ = _collapse_same_pattern_archetypes(archetypes, {})
    assert merged_arch["lonely"]["witnessless_reason"] == "no_eligible"


# --------------------------------------------------------------------------- #
# get_canonical_excerpt: the stamp surfaces as the accurate no_witness sentence
# --------------------------------------------------------------------------- #


@pytest.fixture
def witnessless_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac.key"))
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(
        json.dumps({"generation": 1, "language": "typescript", "schema_version": 8})
    )
    (cham / "archetypes.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "archetypes": {
                    "poisoned": {
                        "summary": "parsers",
                        "cluster_size": 3,
                        "witnessless_reason": "poisoning_only",
                    },
                    "ineligible": {
                        "summary": "specs",
                        "cluster_size": 2,
                        "witnessless_reason": "no_eligible",
                    },
                    "legacy": {"summary": "older profile, no stamp", "cluster_size": 2},
                },
            }
        )
    )
    (cham / "rules.json").write_text(json.dumps({"generation": 1, "rules": {}}))
    (cham / "canonicals.json").write_text(json.dumps({"generation": 1, "canonicals": {}}))
    (cham / "conventions.json").write_text(json.dumps({"generation": 1, "conventions": {}}))
    (cham / "COMMITTED").touch()
    return repo


def _reason_for(repo, archetype: str) -> str:
    res = tools.get_canonical_excerpt(str(repo), archetype)
    data = res["data"]
    assert data["status"] == "no_witness"
    assert data["content"] is None
    return data["reason"]


def test_excerpt_reason_names_poisoning(witnessless_repo):
    reason = _reason_for(witnessless_repo, "poisoned")
    assert "dangerous-pattern" in reason
    assert "withheld" in reason


def test_excerpt_reason_names_no_eligible(witnessless_repo):
    reason = _reason_for(witnessless_repo, "ineligible")
    assert "no candidate was eligible" in reason
    assert "dangerous-pattern" not in reason


def test_excerpt_reason_falls_back_to_generic(witnessless_repo):
    reason = _reason_for(witnessless_repo, "legacy")
    assert "all candidates excluded" in reason
