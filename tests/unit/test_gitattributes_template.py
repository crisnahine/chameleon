"""The shipped .gitattributes-template must route exactly the artifacts
merge_profiles can structurally merge.

A profile artifact listed with merge=chameleon but unknown to merge_profiles
would fail every merge (git keeps the conflict); an artifact merge_profiles
supports but the template omits silently falls back to git's default
conflict markers, leaving the merge branch dead code.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / ".gitattributes-template"

# Artifacts merge_profiles handles: profile.json via the top-level-key union
# fallback, the rest via the data_key branch in tools.merge_profiles.
MERGEABLE = {
    "profile.json",
    "archetypes.json",
    "rules.json",
    "canonicals.json",
    "conventions.json",
}


def _template_merge_entries() -> set[str]:
    entries = set()
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        m = re.match(r"^\.chameleon/(\S+)\s+merge=chameleon$", line)
        if m:
            entries.add(m.group(1))
    return entries


def test_template_exists():
    assert TEMPLATE.is_file()


def test_every_template_entry_is_mergeable():
    assert _template_merge_entries() <= MERGEABLE


def test_every_mergeable_artifact_is_in_template():
    assert _template_merge_entries() == MERGEABLE


def test_generated_index_artifacts_not_routed_to_driver():
    entries = _template_merge_entries()
    for artifact in ("exports_index.json", "reverse_index.json", "function_catalog.json"):
        assert artifact not in entries
