"""An idiom the team keeps ignoring must get a retirement proposal.

The outcome loop is closed on one side only. A calibrated BLOCK rule the team
keeps overriding is auto-demoted to advisory at refresh
(``apply_override_feedback_demotion``). The idiom half has no counterpart: the
miner reinforces an idiom whose finding reached ``addressed`` and has no pass
for one that never does, ``deprecate_record`` is reachable only through a human
/chameleon-teach, and ``resurfaced`` is a terminal ledger status nothing tallies.

So an idiom nobody acts on fires forever, and the signal that it should stop is
already recorded and simply never read. This is the missing symmetry: the same
distinct-session floor the block-rule demotion uses, applied to the
resurfaced-vs-addressed ratio per idiom slug.

Nothing auto-adopts, exactly like the other three passes -- the output is a
candidate under the unhashed ``idiom-candidates/``, which becomes real only
through the same approval path a hand-taught idiom uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chameleon_mcp.stop.miner import _mine_ignored_idioms


class _Request:
    def __init__(self, repo_id="repo-1", repo_root="/tmp/repo"):
        self.repo_id = repo_id
        self.repo_root = repo_root
        self.session_id = "sess-1"


@pytest.fixture
def profile_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    d = tmp_path / ".chameleon"
    d.mkdir()
    return d


_KEY_SEQ = iter(range(10_000))


def _row(slug, status, sessions):
    """One ledger row. Each gets a distinct match_key, because a match_key IS
    the finding identity the decline floor counts."""
    return {
        "kind": "idiom",
        "status": status,
        "claim": f"idiom '{slug}' was not followed here",
        "session_ids": sessions,
        "match_key": f"k-{slug}-{status}-{next(_KEY_SEQ)}",
    }


def _candidates(profile_dir: Path) -> list[str]:
    d = profile_dir / "idiom-candidates"
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def _patch_rows(monkeypatch, rows):
    import chameleon_mcp.review_ledger as rl

    monkeypatch.setattr(
        rl, "_read_findings_rows", lambda _rid: {str(i): r for i, r in enumerate(rows)}
    )


def test_chronically_resurfaced_idiom_gets_a_candidate(profile_dir, monkeypatch):
    _patch_rows(
        monkeypatch,
        [
            _row("safe-open-for-file-reads", "resurfaced", ["s1"]),
            _row("safe-open-for-file-reads", "resurfaced", ["s2"]),
            _row("safe-open-for-file-reads", "resurfaced", ["s3"]),
        ],
    )
    _mine_ignored_idioms(_Request(), profile_dir)
    assert any("safe-open-for-file-reads" in c for c in _candidates(profile_dir))


def test_an_idiom_the_team_acts_on_is_left_alone(profile_dir, monkeypatch):
    _patch_rows(
        monkeypatch,
        [
            _row("atomic-profile-write", "addressed", ["s1"]),
            _row("atomic-profile-write", "addressed", ["s2"]),
            _row("atomic-profile-write", "resurfaced", ["s3"]),
        ],
    )
    _mine_ignored_idioms(_Request(), profile_dir)
    assert _candidates(profile_dir) == []


def test_one_decline_is_below_the_floor(profile_dir, monkeypatch):
    """A single decline must not retire an idiom.

    The row carries every session it was SEEN in, so one finding delivered in
    session A and left unchanged in session B holds two session ids from ONE
    decline. Counting ids would clear a floor of two on that single event.
    """
    _patch_rows(monkeypatch, [_row("tool-response-envelope", "resurfaced", ["s1", "s2", "s3"])])
    _mine_ignored_idioms(_Request(), profile_dir)
    assert _candidates(profile_dir) == []


def test_non_idiom_findings_are_ignored(profile_dir, monkeypatch):
    rows = [
        {
            "kind": "correctness",
            "status": "resurfaced",
            "claim": "idiom 'not-really' something",
            "session_ids": ["s1", "s2", "s3"],
            "match_key": "k1",
        }
    ]
    _patch_rows(monkeypatch, rows)
    _mine_ignored_idioms(_Request(), profile_dir)
    assert _candidates(profile_dir) == []


def test_a_claim_without_a_slug_is_skipped(profile_dir, monkeypatch):
    rows = [
        {
            "kind": "idiom",
            "status": "resurfaced",
            "claim": "no slug in this claim at all",
            "session_ids": ["s1", "s2"],
            "match_key": f"k{i}",
        }
        for i in range(3)
    ]
    _patch_rows(monkeypatch, rows)
    _mine_ignored_idioms(_Request(), profile_dir)
    assert _candidates(profile_dir) == []


def test_a_ledger_read_error_does_not_raise(profile_dir, monkeypatch):
    import chameleon_mcp.review_ledger as rl

    def _boom(_rid):
        raise OSError("ledger gone")

    monkeypatch.setattr(rl, "_read_findings_rows", _boom)
    # Fail-open like every other miner pass: the job must survive.
    _mine_ignored_idioms(_Request(), profile_dir)
