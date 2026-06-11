"""apply_archetype_renames must carry calls_index.json through the txn.

calls_index.json is a protocol file: atomic_profile_commit never copies it
forward on its own, so any artifact the rename txn does not re-emit is
deleted by the dir swap. A rename never invalidates caller facts (the index
is keyed by file paths and callable names, not archetype names), so the txn
must carry the artifact forward verbatim — same posture as the
partial-refresh path, including the 16MB loader ceiling.

Covers:
  - synthetic profile: byte-identical carry-forward + the returned trust
    hash actually covering the artifact (calls_index.json is in
    _HASHED_ARTIFACTS, so dropping it must change the hash)
  - an over-ceiling artifact is dropped, not carried (mirrors the loader,
    which would refuse to serve it anyway)
  - end-to-end on a real TS bootstrap: a fresh profile's caller facts
    survive the auto-rename that /chameleon-init applies

The rename txn never branches on language, so the real-bootstrap leg runs
TS only; the synthetic legs cover the carry logic for both extractors.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from chameleon_mcp import tools
from chameleon_mcp.profile.trust import grant_trust, hash_profile


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp import index_db
    from chameleon_mcp.profile import loader as _loader

    monkeypatch.setattr(index_db, "_INDEX_CONN", None)
    _loader._PROFILE_CACHE.clear()
    _loader._REPO_ROOT_CACHE.clear()
    tools._clear_repo_id_cache()
    yield
    _loader._PROFILE_CACHE.clear()
    _loader._REPO_ROOT_CACHE.clear()
    tools._clear_repo_id_cache()


def _make_profile_repo(root: Path) -> tuple[Path, Path]:
    """Minimal committed profile with one renameable archetype."""
    repo = root / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(
        json.dumps({"schema_version": 7, "generation": 1, "language": "typescript"})
    )
    (cham / "archetypes.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "archetypes": {
                    "svc-old": {"cluster_size": 7, "paths_pattern": "src/services:ts"},
                },
            }
        )
    )
    (cham / "canonicals.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "canonicals": {
                    "svc-old": [{"witness": {"path": "src/services/payment.ts", "sha_hint": "ab"}}]
                },
            }
        )
    )
    (cham / "rules.json").write_text(json.dumps({"generation": 1, "rules": {"svc-old": {"x": 1}}}))
    (cham / "idioms.md").write_text("# idioms\n")
    (cham / "COMMITTED").touch()
    grant_trust(tools._compute_repo_id(repo), cham)
    return repo, cham


_CALLS_INDEX_PAYLOAD = json.dumps(
    {
        "schema_version": 1,
        "callees": {
            "src/services/payment.ts": {
                "charge": {
                    "callers": [
                        {
                            "path": "src/services/checkout.ts",
                            "caller": "submitOrder",
                            "line": 12,
                            "grade": "import",
                        }
                    ],
                    "total": 1,
                    "truncated": False,
                }
            }
        },
    },
    indent=2,
    sort_keys=True,
)


def test_rename_carries_calls_index_verbatim_and_hashes_it(tmp_path):
    repo, cham = _make_profile_repo(tmp_path)
    (cham / "calls_index.json").write_text(_CALLS_INDEX_PAYLOAD, encoding="utf-8")

    res = tools.apply_archetype_renames(str(repo), {"svc-old": "payment-service"})["data"]
    assert res["status"] == "success"
    assert res["renames_applied"] == 1

    carried = cham / "calls_index.json"
    assert carried.is_file(), "rename txn dropped calls_index.json"
    assert carried.read_text(encoding="utf-8") == _CALLS_INDEX_PAYLOAD

    # The reported trust hash must cover the carried artifact: removing it
    # has to change the hash, or a dropped index would slip past trust.
    assert res["new_profile_sha256"] == hash_profile(cham)
    carried.unlink()
    assert hash_profile(cham) != res["new_profile_sha256"]


def test_rename_drops_over_ceiling_calls_index(tmp_path):
    """An artifact past the 16MB loader ceiling is dropped, not carried —
    the loader would refuse to serve it, so carrying it only bloats the
    profile. Mirrors the partial-refresh posture exactly."""
    repo, cham = _make_profile_repo(tmp_path)
    (cham / "calls_index.json").write_text(
        '{"schema_version": 1, "callees": {"pad": "' + "x" * 16_000_001 + '"}}',
        encoding="utf-8",
    )

    res = tools.apply_archetype_renames(str(repo), {"svc-old": "payment-service"})["data"]
    assert res["status"] == "success"
    assert not (cham / "calls_index.json").exists()


_SERVICE_BODY = """import {{ formatLabel }} from '../lib/format'

export class {name}Service {{
  run(input: string): string {{
    return formatLabel(input)
  }}
}}
"""

_LIB_BODY = """export function formatLabel(value: string): string {
  return value.trim().toLowerCase()
}
"""


def test_real_bootstrap_rename_keeps_caller_facts(tmp_path):
    """End-to-end: bootstrap a TS repo whose services all call a shared lib
    helper (so calls_index.json has edges), apply a rename, and verify the
    caller facts survive byte-for-byte."""
    repo = tmp_path / "tsrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / "package.json").write_text('{"name": "fixture", "private": true}\n', encoding="utf-8")
    (repo / "tsconfig.json").write_text('{"compilerOptions": {"strict": true}}\n', encoding="utf-8")
    lib = repo / "src" / "lib" / "format.ts"
    lib.parent.mkdir(parents=True)
    lib.write_text(_LIB_BODY, encoding="utf-8")
    for name in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta"):
        p = repo / "src" / "services" / f"{name.lower()}Service.ts"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_SERVICE_BODY.format(name=name), encoding="utf-8")

    env = tools.bootstrap_repo(str(repo))
    assert env["data"]["status"] == "success"

    cham = repo / ".chameleon"
    before = (cham / "calls_index.json").read_text(encoding="utf-8")
    edges = json.loads(before)["callees"]
    assert edges, "fixture bootstrap produced no caller facts"

    arch_names = list(
        json.loads((cham / "archetypes.json").read_text(encoding="utf-8"))["archetypes"]
    )
    assert arch_names
    res = tools.apply_archetype_renames(str(repo), {arch_names[0]: "renamed-qa28"})["data"]
    assert res["status"] == "success"

    assert (cham / "calls_index.json").is_file(), "rename txn dropped calls_index.json"
    assert (cham / "calls_index.json").read_text(encoding="utf-8") == before
    assert res["new_profile_sha256"] == hash_profile(cham)
