"""Regression tests for the orchestrator/transaction stale-index carry-forward fix.

Covers two behavioral fixes that share one mechanism (the atomic-commit
carry-forward loop):

  1. exports_index.json / reverse_index.json / function_catalog.json must NOT be
     carried forward stale when the current build did not (re)write them -- be it
     a best-effort build that raised, or a re-derive whose detected language no
     longer builds that index (e.g. TypeScript -> Ruby flips off the symbol
     indexes). They are dropped (treated as protocol files) so absence fails open
     in every reader instead of serving a stale symbol index that drives false
     phantom-import / cross-file findings.

  2. The monorepo-amend path (which adds the workspaces array right after a root
     bootstrap) re-emits those three indexes verbatim, so a monorepo root does
     not lose them moments after they were written now that the commit drops
     rather than carries them.
"""

from __future__ import annotations

import json
from pathlib import Path

from chameleon_mcp.bootstrap.transaction import (
    _PROTOCOL_FILES,
    COMMITTED_SENTINEL,
    atomic_profile_commit,
    is_committed,
)

_STALE_INDEXES = (
    "exports_index.json",
    "reverse_index.json",
    "function_catalog.json",
)


def _seed_committed_profile(target: Path, *, with_stale_indexes: bool = True) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / COMMITTED_SENTINEL).write_text("committed-at=1\npid=1\n")
    (target / "profile.json").write_text('{"language": "typescript"}')
    (target / "archetypes.json").write_text("{}")
    (target / "canonicals.json").write_text("{}")
    (target / "rules.json").write_text("{}")
    if with_stale_indexes:
        for name in _STALE_INDEXES:
            (target / name).write_text(f'{{"stale": "{name}"}}')


def test_protocol_set_includes_the_three_symbol_indexes():
    for name in _STALE_INDEXES:
        assert name in _PROTOCOL_FILES, f"{name} must be drop-stale, not carried forward"


def test_build_raised_drops_stale_symbol_indexes(tmp_path: Path):
    """A re-derive whose index builders raised (best-effort try/except) leaves the
    indexes unwritten; the prior copies must NOT be carried forward."""
    target = tmp_path / ".chameleon"
    _seed_committed_profile(target, with_stale_indexes=True)

    # New build writes the protocol artifacts but, modeling the builders raising,
    # writes none of the three symbol indexes into the txn.
    with atomic_profile_commit(target) as txn:
        (txn / "profile.json").write_text('{"language": "typescript", "v": 2}')

    assert is_committed(target)
    assert json.loads((target / "profile.json").read_text())["v"] == 2
    for name in _STALE_INDEXES:
        assert not (target / name).exists(), f"stale {name} was carried forward"


def test_language_switch_drops_prior_language_symbol_indexes(tmp_path: Path):
    """A force re-derive whose language flips TypeScript -> Ruby skips the symbol
    index build entirely; the prior TS indexes must NOT ship in the Ruby profile."""
    target = tmp_path / ".chameleon"
    _seed_committed_profile(target, with_stale_indexes=True)

    # Ruby derive: the orchestrator's `if language in ("typescript", "python")`
    # gate is skipped, so no index is written into the txn.
    with atomic_profile_commit(target) as txn:
        (txn / "profile.json").write_text('{"language": "ruby"}')

    assert is_committed(target)
    assert json.loads((target / "profile.json").read_text())["language"] == "ruby"
    for name in _STALE_INDEXES:
        assert not (target / name).exists(), f"prior-language {name} carried into the Ruby profile"


def test_freshly_built_index_survives_same_language_rederive(tmp_path: Path):
    """A same-language successful re-derive that writes a fresh index into the txn
    keeps it -- the drop only applies when the current build did not write it."""
    target = tmp_path / ".chameleon"
    _seed_committed_profile(target, with_stale_indexes=True)

    with atomic_profile_commit(target) as txn:
        (txn / "profile.json").write_text('{"language": "typescript"}')
        for name in _STALE_INDEXES:
            (txn / name).write_text(f'{{"fresh": "{name}"}}')

    assert is_committed(target)
    for name in _STALE_INDEXES:
        assert (target / name).exists()
        assert json.loads((target / name).read_text()) == {"fresh": name}


def test_user_non_protocol_sibling_still_carried_forward(tmp_path: Path):
    """The drop-stale change must not regress genuine user-dropped content: a
    non-protocol sibling still carries forward across a commit."""
    target = tmp_path / ".chameleon"
    _seed_committed_profile(target, with_stale_indexes=False)
    (target / "team-notes.txt").write_text("hand-written")

    with atomic_profile_commit(target) as txn:
        (txn / "profile.json").write_text('{"language": "typescript", "v": 2}')

    assert (target / "team-notes.txt").read_text() == "hand-written"


def test_monorepo_amend_reemits_symbol_indexes(tmp_path: Path):
    """The monorepo workspaces-amend re-emits the three symbol indexes verbatim, so
    a root profile does not lose them now that the commit drops rather than carries
    protocol files."""
    from chameleon_mcp.bootstrap.orchestrator import _amend_root_profile_with_workspaces

    target = tmp_path / ".chameleon"
    _seed_committed_profile(target, with_stale_indexes=True)
    index_payloads = {name: (target / name).read_text() for name in _STALE_INDEXES}

    _amend_root_profile_with_workspaces(
        target,
        [
            {
                "workspace_path": "packages/app",
                "repo_id": "abc",
                "profile_dir": "/tmp/x",
                "status": "success",
            }
        ],
    )

    assert is_committed(target)
    profile = json.loads((target / "profile.json").read_text())
    assert profile["workspaces"][0]["workspace_path"] == "packages/app"
    for name in _STALE_INDEXES:
        assert (target / name).exists(), f"amend dropped {name}"
        assert (target / name).read_text() == index_payloads[name]
