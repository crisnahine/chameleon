"""The shipped .gitattributes-template must route exactly the artifacts the
merge driver is meant to see.

Two intentional classes:
- MERGEABLE: artifacts merge_profiles structurally merges.
- DECLINE_TO_MERGE: protocol files the driver deliberately fails on. A failed
  custom driver leaves your side's content whole with the path flagged
  conflicted — never raw conflict markers inside live profile state (the
  runtime reads COMMITTED / principles.md / profile.summary.md, so markers in
  them are live damage, not just an ugly diff). Resolution is accept-a-side +
  /chameleon-refresh.

An artifact in neither set but listed in the template would silently change
merge behavior; an artifact merge_profiles supports but the template omits
falls back to git's default conflict markers, leaving the merge branch dead
code.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / ".gitattributes-template"

# Artifacts merge_profiles handles: profile.json via the top-level-key union
# fallback, the JSON artifacts via the data_key branch, and idioms.md via the
# markdown union-by-slug branch in tools.merge_profiles.
MERGEABLE = {
    "profile.json",
    "archetypes.json",
    "rules.json",
    "canonicals.json",
    "conventions.json",
    "idioms.md",
}

# Routed to the driver so it can DECLINE: keeps conflict markers out of
# runtime-read protocol files (qa25: a real two-branch merge landed markers in
# COMMITTED and profile.summary.md, and is_committed half-trusted the result).
DECLINE_TO_MERGE = {
    "COMMITTED",
    "principles.md",
    "profile.summary.md",
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


def test_every_template_entry_is_mergeable_or_deliberately_declined():
    assert _template_merge_entries() <= MERGEABLE | DECLINE_TO_MERGE


def test_every_routed_artifact_is_in_template():
    assert _template_merge_entries() == MERGEABLE | DECLINE_TO_MERGE


def test_generated_index_artifacts_not_routed_to_driver():
    entries = _template_merge_entries()
    for artifact in (
        "exports_index.json",
        "reverse_index.json",
        "function_catalog.json",
        "calls_index.json",
        "symbol_signatures.json",
    ):
        assert artifact not in entries
