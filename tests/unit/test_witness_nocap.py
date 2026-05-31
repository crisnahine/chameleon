"""No-cap witness injection: the canonical witness is no longer dropped at 200KB.

The old 200KB ceiling REJECTED a larger witness to an empty excerpt (the worst
quality outcome). The ceiling is now 5MB so any real source file injects in
full, and a pathological >5MB witness is FLAGGED (truncated/oversize) instead of
silently returning nothing.
"""

from __future__ import annotations

import json

from chameleon_mcp.profile.trust import grant_trust
from chameleon_mcp.tools import WITNESS_MAX_BYTES, _compute_repo_id, get_canonical_excerpt

ARCH = "service"
WITNESS = "service.ts"
SAFE_LINE = "export const a = 1;\n"  # secret-free, passes is_safe_canonical


def _repo_with_witness(tmp_path, monkeypatch, *, witness_bytes: int):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(json.dumps({"generation": 1, "language": "typescript"}))
    (cham / "archetypes.json").write_text(
        json.dumps({"generation": 1, "archetypes": {ARCH: {"summary": "svc"}}})
    )
    (cham / "rules.json").write_text(json.dumps({"generation": 1, "rules": {}}))
    (cham / "canonicals.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "canonicals": {ARCH: [{"witness": {"path": WITNESS, "sha_hint": "x"}}]},
            }
        )
    )
    (cham / "COMMITTED").touch()
    reps = (witness_bytes // len(SAFE_LINE)) + 1
    (repo / WITNESS).write_text(SAFE_LINE * reps)
    grant_trust(_compute_repo_id(repo), cham)
    return repo


def test_witness_over_old_200kb_cap_now_injects_fully(tmp_path, monkeypatch):
    # ~320KB: over the OLD 200KB cap (would have returned ""), under the 5MB ceiling.
    repo = _repo_with_witness(tmp_path, monkeypatch, witness_bytes=320_000)
    res = get_canonical_excerpt(str(repo), ARCH)["data"]
    assert res.get("status") not in ("untrusted", "oversize", "unreadable")
    assert len(res.get("content") or "") > 200_000  # full content, not dropped


def test_witness_over_5mb_is_flagged_not_silent(tmp_path, monkeypatch):
    repo = _repo_with_witness(tmp_path, monkeypatch, witness_bytes=WITNESS_MAX_BYTES + 100_000)
    res = get_canonical_excerpt(str(repo), ARCH)["data"]
    assert res.get("status") == "oversize"  # explicit, not an empty success
    assert res.get("truncated") is True
    assert res.get("witness_path") == WITNESS  # model still learns the witness exists


def test_witness_max_bytes_is_5mb():
    assert WITNESS_MAX_BYTES == 5 * 1024 * 1024
