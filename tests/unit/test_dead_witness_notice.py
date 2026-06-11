"""A deleted canonical witness must be flagged, not served as a silent empty excerpt.

When the witness file that was recorded in canonicals.json no longer exists on
disk, get_canonical_excerpt must return content="" with missing=True so the hook
can render a refresh hint instead of silently degrading tier-2 injection.
"""

from __future__ import annotations

import json

from chameleon_mcp.profile.trust import grant_trust
from chameleon_mcp.tools import _compute_repo_id, get_canonical_excerpt

ARCH = "service"
WITNESS = "service.ts"
SAFE_LINE = "export const a = 1;\n"


def _repo_with_witness(tmp_path, monkeypatch):
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
    (repo / WITNESS).write_text(SAFE_LINE * 3)
    grant_trust(_compute_repo_id(repo), cham)
    return repo


def test_deleted_witness_yields_missing_flag(tmp_path, monkeypatch):
    """Deleting the witness file after derivation must set missing=True, not serve empty silently."""
    repo = _repo_with_witness(tmp_path, monkeypatch)

    # Sanity: witness present -> content flows.
    res_before = get_canonical_excerpt(str(repo), ARCH)["data"]
    assert res_before.get("content"), "expected content before deletion"
    assert not res_before.get("missing"), "missing flag must be absent when witness exists"

    # Remove the witness file to simulate post-derivation deletion.
    (repo / WITNESS).unlink()

    res = get_canonical_excerpt(str(repo), ARCH)["data"]
    assert res.get("content") == "", f"content must be empty, got {res.get('content')!r}"
    assert res.get("missing") is True, f"missing flag must be True, got {res.get('missing')!r}"
    # witness_path is preserved so callers know which file to recover.
    assert res.get("witness_path") == WITNESS
