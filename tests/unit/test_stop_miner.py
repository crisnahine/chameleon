"""The self-learning idiom miner (stop/miner.py): the detached job's
end-of-run tail stage. Driven in-process (``miner.run_miner`` called
directly, never the real job subprocess) against seeded findings-ledger and
override-audit fixtures under ``tmp_path`` -- CONFTEST GUARD applies (no real
``claude -p`` anywhere in this module).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chameleon_mcp import review_ledger
from chameleon_mcp.core.budget import TurnBudget
from chameleon_mcp.core.finding import Finding
from chameleon_mcp.core.idiom_candidates import load_candidates, write_candidate
from chameleon_mcp.core.idiom_store import load_store, store_dir
from chameleon_mcp.stop import miner
from chameleon_mcp.stop.scheduler import JobRequest

REPO_ID = "miner-test-repo"
SID = "miner-sess-1"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(key_file))
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    yield


def _repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / ".chameleon").mkdir(parents=True, exist_ok=True)
    return root


def _request(repo_root: Path, session_id: str = SID) -> JobRequest:
    return JobRequest(
        repo_root=repo_root,
        repo_id=REPO_ID,
        session_id=session_id,
        files=(),
        intent_tokens=(),
        lens_names=(),
        model="sonnet",
        heartbeat_path=repo_root / "hb",
    )


def _budget() -> TurnBudget:
    return TurnBudget.for_hook(total_seconds=30.0, token_ceiling=10_000)


def _events() -> list[dict]:
    from chameleon_mcp.exec_log import read_check_events

    out = read_check_events(REPO_ID, SID, limit=500)
    return [e for e in out["events"] if e.get("check") == "review_job"]


def _correctness_finding(**over) -> Finding:
    base = dict(
        id="f1",
        kind="correctness",
        severity="high",
        confidence=0.9,
        file="src/widget.ts",
        span=(3, 3),
        claim="Always resolve the api client via getClient(), never new ApiClient() directly.",
        evidence="",
        excerpt_sha="",
        excerpt="",
        source_lens="correctness",
        status="pending",
        created_at="2026-07-16T00:00:00Z",
    )
    base.update(over)
    return Finding(**base)


def _idiom_finding(slug: str, title: str, **over) -> Finding:
    base = dict(
        id="idiom-f1",
        kind="idiom",
        severity="high",
        confidence=0.8,
        file="src/widget.ts",
        span=(5, 5),
        claim=f"idiom '{slug}' ({title}): violates the taught idiom",
        evidence="",
        excerpt_sha="",
        excerpt="",
        source_lens="idiom",
        status="pending",
        created_at="2026-07-16T00:00:00Z",
    )
    base.update(over)
    return Finding(**base)


# --- signal 2: recurring correctness/duplication findings -> new candidate --


def test_signal2_recurring_finding_across_sessions_becomes_learned_candidate(tmp_path):
    repo_root = _repo(tmp_path)
    finding = _correctness_finding()
    for sid in ("s1", "s2", "s3"):
        review_ledger.record_findings(REPO_ID, str(repo_root), [finding], session_id=sid)

    miner.run_miner(_request(repo_root), _budget())

    rows = load_candidates(repo_root / ".chameleon")
    assert len(rows) == 1
    (row,) = rows
    assert row["source"] == "learned"
    assert row["occurrences"] >= 3
    assert set(row["session_ids"]) >= {"s1", "s2", "s3"}
    assert "getClient" in row["rationale"]


def test_signal2_below_recurrence_floor_produces_no_candidate(tmp_path):
    repo_root = _repo(tmp_path)
    finding = _correctness_finding(claim="a one-off finding nobody else hit")
    review_ledger.record_findings(REPO_ID, str(repo_root), [finding], session_id="s1")

    miner.run_miner(_request(repo_root), _budget())

    assert load_candidates(repo_root / ".chameleon") == []


# --- signal 3: over-overridden rule -> deprecation/loosening candidate ------


def test_signal3_flagged_override_rule_becomes_deprecation_candidate(tmp_path, monkeypatch):
    from chameleon_mcp.drift.observations import record_override
    from chameleon_mcp.metrics import emit_hook_metric

    repo_root = _repo(tmp_path)
    # 6 overrides + 4 would-blocks -> rate 0.6, over the min-events floor and
    # the high-rate threshold (mirrors test_review_ledger.py's own fixture).
    for i in range(6):
        record_override(REPO_ID, "import-preference-violation", rel_path=f"f{i}.ts")
    for _ in range(4):
        emit_hook_metric(
            "posttool-verify",
            elapsed_ms=0,
            repo_id=REPO_ID,
            advisory_emitted=True,
            would_block=True,
            rule="import-preference-violation",
            file_rel="x.ts",
        )

    miner.run_miner(_request(repo_root), _budget())

    rows = load_candidates(repo_root / ".chameleon")
    assert len(rows) == 1
    (row,) = rows
    assert row["source"] == "learned"
    assert "import-preference-violation" in row["rationale"]
    assert "import-preference-violation" in row["title"]


def test_signal3_no_flagged_rules_produces_no_candidate(tmp_path):
    repo_root = _repo(tmp_path)
    miner.run_miner(_request(repo_root), _budget())
    assert load_candidates(repo_root / ".chameleon") == []


# --- signal 1: addressed idiom finding reinforces an EXISTING candidate -----


def test_signal1_addressed_idiom_finding_reinforces_existing_candidate_only(tmp_path):
    repo_root = _repo(tmp_path)
    profile_dir = repo_root / ".chameleon"

    # A candidate for "foo-idiom" already exists (from a prior mine, or a
    # manually seeded proposal) -- reinforcement should land on it.
    write_candidate(
        profile_dir,
        slug="foo-idiom",
        title="Foo Idiom",
        rationale="Use foo instead of bar.",
        source="learned",
        evidence="original evidence",
    )

    reinforced = _idiom_finding("foo-idiom", "Foo Idiom", id="idiom-reinforced")
    review_ledger.record_findings(REPO_ID, str(repo_root), [reinforced], session_id="s1")
    review_ledger.mark_addressed(REPO_ID, [reinforced.match_key])

    # A SECOND idiom finding whose slug has NO existing candidate -- must NOT
    # mint a bare new one (design ambiguity #5).
    bare = _idiom_finding("bar-idiom", "Bar Idiom", id="idiom-bare")
    review_ledger.record_findings(REPO_ID, str(repo_root), [bare], session_id="s1")
    review_ledger.mark_addressed(REPO_ID, [bare.match_key])

    miner.run_miner(_request(repo_root), _budget())

    rows = {r["slug"]: r for r in load_candidates(profile_dir)}
    assert set(rows) == {"foo-idiom"}
    reinforced_row = rows["foo-idiom"]
    assert "original evidence" in reinforced_row["evidence"]
    assert "reinforced" in reinforced_row["evidence"]
    assert reinforced_row["occurrences"] >= 2


def test_signal1_pending_not_addressed_idiom_finding_is_not_reinforced(tmp_path):
    repo_root = _repo(tmp_path)
    profile_dir = repo_root / ".chameleon"
    write_candidate(
        profile_dir,
        slug="foo-idiom",
        title="Foo Idiom",
        rationale="r",
        source="learned",
        evidence="original evidence",
    )
    still_pending = _idiom_finding("foo-idiom", "Foo Idiom")
    review_ledger.record_findings(REPO_ID, str(repo_root), [still_pending], session_id="s1")
    # deliberately NOT marked addressed

    miner.run_miner(_request(repo_root), _budget())

    (row,) = load_candidates(profile_dir)
    assert row["evidence"] == "original evidence"


# --- CHAMELEON_IDIOM_MINER=0 kill switch ------------------------------------


def test_env_kill_switch_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_IDIOM_MINER", "0")
    repo_root = _repo(tmp_path)
    finding = _correctness_finding()
    for sid in ("s1", "s2", "s3"):
        review_ledger.record_findings(REPO_ID, str(repo_root), [finding], session_id=sid)

    miner.run_miner(_request(repo_root), _budget())

    assert load_candidates(repo_root / ".chameleon") == []


# --- fail-open: a raising sub-step never crashes the job --------------------


def test_raising_ledger_read_fails_open_no_candidate_no_crash_emits_check_event(
    tmp_path, monkeypatch
):
    repo_root = _repo(tmp_path)

    def _boom(_repo_id):
        raise RuntimeError("ledger corrupt")

    monkeypatch.setattr(review_ledger, "_read_findings_rows", _boom)

    # Must not raise.
    miner.run_miner(_request(repo_root), _budget())

    assert load_candidates(repo_root / ".chameleon") == []
    events = _events()
    assert any(e.get("status") == "miner_error" for e in events)


def test_raising_override_audit_fails_open_no_candidate_no_crash(tmp_path, monkeypatch):
    repo_root = _repo(tmp_path)

    def _boom(_repo_id, *_a, **_k):
        raise RuntimeError("override audit corrupt")

    monkeypatch.setattr(review_ledger, "build_override_audit", _boom)

    miner.run_miner(_request(repo_root), _budget())

    assert load_candidates(repo_root / ".chameleon") == []
    events = _events()
    assert any(e.get("status") == "miner_error" for e in events)


# --- budget guard ------------------------------------------------------------


def test_zero_remaining_budget_skips_the_miner_entirely(tmp_path, monkeypatch):
    repo_root = _repo(tmp_path)
    finding = _correctness_finding()
    for sid in ("s1", "s2", "s3"):
        review_ledger.record_findings(REPO_ID, str(repo_root), [finding], session_id=sid)

    exhausted = TurnBudget.for_hook(total_seconds=0.0, token_ceiling=10_000)
    miner.run_miner(_request(repo_root), exhausted)

    assert load_candidates(repo_root / ".chameleon") == []
    events = _events()
    assert any(e.get("status") == "miner_skipped" for e in events)


# --- nothing auto-adopts: the live idiom store is never touched -------------


def test_miner_never_writes_to_the_live_idiom_store(tmp_path):
    repo_root = _repo(tmp_path)
    profile_dir = repo_root / ".chameleon"

    finding = _correctness_finding()
    for sid in ("s1", "s2", "s3"):
        review_ledger.record_findings(REPO_ID, str(repo_root), [finding], session_id=sid)

    from chameleon_mcp.drift.observations import record_override
    from chameleon_mcp.metrics import emit_hook_metric

    for i in range(6):
        record_override(REPO_ID, "some-rule", rel_path=f"f{i}.ts")
    for _ in range(4):
        emit_hook_metric(
            "posttool-verify",
            elapsed_ms=0,
            repo_id=REPO_ID,
            advisory_emitted=True,
            would_block=True,
            rule="some-rule",
            file_rel="x.ts",
        )

    write_candidate(
        profile_dir,
        slug="foo-idiom",
        title="Foo Idiom",
        rationale="r",
        source="learned",
        evidence="e",
    )
    idiom = _idiom_finding("foo-idiom", "Foo Idiom")
    review_ledger.record_findings(REPO_ID, str(repo_root), [idiom], session_id="s1")
    review_ledger.mark_addressed(REPO_ID, [idiom.match_key])

    miner.run_miner(_request(repo_root), _budget())

    # Real candidates DID get written...
    assert len(load_candidates(profile_dir)) >= 2
    # ...but the live idiom store (idioms/) was never created or touched.
    assert not store_dir(profile_dir).exists()
    assert load_store(profile_dir) == []
