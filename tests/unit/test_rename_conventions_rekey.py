"""apply_archetype_renames must rekey EVERY per-archetype conventions section.

conventions.json keys each per-archetype section by archetype name at the
second level (e.g. conventions.required_guards = {<archetype>: ...}). A rename
re-emits conventions.json, so every archetype-keyed section must have its keys
remapped to the new name. The edit-time hot path looks conventions up by the
NEW archetype name with NO alias fallback, so a section left under the old key
is silently dropped for the renamed archetype.

Two of these sections are consumed by direct per-archetype lookup at edit time
and therefore functionally lost when not rekeyed:
  - required_guards: the advisory authz hint (lint_file + the re-lint).
  - test_pairing: the paired-test reminder (cochange).
The rest surface only in repo-wide renders, so a stale key there is cosmetic --
but the rename must still rekey them so the artifact stays internally consistent
and a future per-archetype consumer cannot silently regress.

layering is repo-level (not archetype-keyed) and must be carried verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chameleon_mcp import tools
from chameleon_mcp.profile.trust import grant_trust, hash_profile

# Every per-archetype conventions section (conventions.py empty_conventions),
# minus the repo-level "layering". A rename must remap the second-level key of
# each of these from the old archetype name to the new one.
_PER_ARCHETYPE_SECTIONS = (
    "imports",
    "import_ordering",
    "naming",
    "inheritance",
    "method_calls",
    "key_exports",
    "body_shape",
    "required_guards",
    "error_handling",
    "doc_coverage",
    "test_pairing",
    "callable_signatures",
    "class_contract",
)


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


def _conventions_payload() -> dict:
    """A conventions block with EVERY per-archetype section keyed by svc-old,
    plus a repo-level layering section that is not archetype-keyed."""
    per_archetype_values = {
        "imports": ["axios"],
        "import_ordering": ["external", "internal"],
        "naming": "camelCase",
        "inheritance": {"dominant_base": "BaseService"},
        "method_calls": {"this.charge": 4},
        "key_exports": ["PaymentService"],
        "body_shape": {"median_loc": 18},
        "required_guards": {"guard": "requireAuth", "share": 0.9},
        "error_handling": "try_catch",
        "doc_coverage": {"share": 0.5},
        "test_pairing": {"share": 0.8, "convention": "sibling-spec"},
        "callable_signatures": {"run": "run(input: string): string"},
        "class_contract": {"required_methods": ["run"]},
    }
    block = {
        section: {"svc-old": per_archetype_values[section]} for section in _PER_ARCHETYPE_SECTIONS
    }
    # Repo-level: keyed by edge/report, NOT by archetype. Must survive verbatim.
    block["layering"] = {"forbidden_edges": [{"from": "src/services", "to": "src/web"}]}
    return {"generation": 1, "min_sample_size": 5, "conventions": block}


def test_rename_rekeys_every_per_archetype_conventions_section(tmp_path):
    repo, cham = _make_profile_repo(tmp_path)
    payload = _conventions_payload()
    (cham / "conventions.json").write_text(json.dumps(payload), encoding="utf-8")

    res = tools.apply_archetype_renames(str(repo), {"svc-old": "payment-service"})["data"]
    assert res["status"] == "success"
    assert res["renames_applied"] == 1

    conv = json.loads((cham / "conventions.json").read_text(encoding="utf-8"))["conventions"]

    # Every per-archetype section must be remapped: old key gone, new key present
    # with the original value intact.
    for section in _PER_ARCHETYPE_SECTIONS:
        sub = conv[section]
        assert "svc-old" not in sub, f"rename left a dangling old key in conventions.{section}"
        assert "payment-service" in sub, f"rename did not rekey conventions.{section}"
        assert sub["payment-service"] == payload["conventions"][section]["svc-old"]

    # The two sections consumed by direct per-archetype edit-time lookup are the
    # load-bearing ones (authz hint + paired-test reminder) -- assert explicitly.
    assert conv["required_guards"] == {"payment-service": {"guard": "requireAuth", "share": 0.9}}
    assert conv["test_pairing"] == {"payment-service": {"share": 0.8, "convention": "sibling-spec"}}

    # Repo-level layering is not archetype-keyed and must be carried verbatim.
    assert conv["layering"] == {"forbidden_edges": [{"from": "src/services", "to": "src/web"}]}

    # The rekeyed conventions artifact is part of the trust-hashed surface.
    assert res["new_profile_sha256"] == hash_profile(cham)
