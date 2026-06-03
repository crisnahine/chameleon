"""The chameleon-trust skill doc must match the real hashed-artifact set.

Trust staleness is computed over ``_HASHED_ARTIFACTS`` in
``chameleon_mcp.profile.trust``. The skill text tells the user which files,
when changed, re-stale their trust. If the doc omits an artifact (it dropped
``enforcement.json`` and undercounted the total as 9), a user would wrongly
believe editing that file leaves trust intact. Keep the doc and the source in
lockstep.
"""

from __future__ import annotations

import re
from pathlib import Path

from chameleon_mcp.profile.trust import _HASHED_ARTIFACTS

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / "skills" / "chameleon-trust" / "SKILL.md"


def test_trust_skill_lists_every_hashed_artifact():
    text = SKILL.read_text(encoding="utf-8")
    missing = [a for a in _HASHED_ARTIFACTS if a not in text]
    assert not missing, f"chameleon-trust SKILL.md omits hashed artifacts: {missing}"


def test_trust_skill_artifact_count_matches_source():
    text = SKILL.read_text(encoding="utf-8")
    m = re.search(r"any of the (\d+) hashed profile artifacts", text)
    assert m, "expected an 'any of the N hashed profile artifacts' phrase in SKILL.md"
    assert int(m.group(1)) == len(_HASHED_ARTIFACTS)
