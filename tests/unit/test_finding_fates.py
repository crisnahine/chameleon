"""Unit tests for the finding-fate ledger + tools in chameleon_mcp.

Covers the write/read round trip, HMAC tamper-evidence (including the aggregate
excluding tampered rows), the digest-not-prose privacy posture, fate-vocabulary
normalization (the three skills' synonyms), per-surface/per-lens precision
aggregation, the MCP tool wrappers (fail-open, unknown-fate swallow), and
fail-open on a missing/corrupt ledger.

Isolation: CHAMELEON_PLUGIN_DATA and CHAMELEON_HMAC_KEY_PATH both point under a
fresh tmp_path, so the ledger and signing key never touch the developer's state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chameleon_mcp.review_ledger import (
    _fates_path,
    finding_digest,
    per_lens_precision,
    read_finding_fates,
    record_finding_fate,
)

REPO = "f" * 64


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac.key"))
    yield


def test_record_and_read_round_trip():
    rec = record_finding_fate(
        REPO,
        message="retry count is 2 not 3",
        file="app/client.rb",
        line=47,
        lens="correctness",
        confidence_at_emit=0.82,
        fate="accepted",
        surface="pr-review",
    )
    assert rec["hmac"]  # signed
    assert rec["fate"] == "accepted"
    assert rec["lens"] == "correctness"
    assert rec["confidence_at_emit"] == 0.82
    assert rec["surface"] == "pr-review"
    assert rec["finding_digest"] == finding_digest("retry count is 2 not 3", "app/client.rb", 47)

    history = read_finding_fates(REPO)
    assert history["total"] == 1
    assert history["unverified"] == 0
    stored = history["records"][0]
    assert stored["verified"] is True
    assert stored["reviewer"]


def test_no_finding_prose_is_persisted():
    finding_text = "the exact wording of the finding must never hit disk"
    record_finding_fate(REPO, message=finding_text, fate="declined", lens="perf")
    raw = _fates_path(REPO).read_text()
    assert finding_text not in raw
    assert "wording" not in raw
    assert finding_digest(finding_text, None, None) in raw


def test_finding_digest_is_whitespace_and_case_stable():
    a = finding_digest("Retry  Count  IS 2", "a.rb", 5)
    b = finding_digest("retry count is 2", "a.rb", 5)
    assert a == b
    # A different location is a different finding.
    assert finding_digest("retry count is 2", "a.rb", 6) != a


def test_fate_synonyms_normalize():
    cases = {
        "agree": "accepted",
        "AGREE": "accepted",
        "push back": "declined",
        "reject": "declined",
        "convert": "converted",
        "converted-to-check": "converted",
    }
    for raw, canon in cases.items():
        rec = record_finding_fate(REPO, message=f"m-{raw}", fate=raw)
        assert rec["fate"] == canon, f"{raw!r} should normalize to {canon!r}"


def test_unknown_fate_raises():
    with pytest.raises(ValueError):
        record_finding_fate(REPO, message="m", fate="maybe-later")


def test_out_of_range_confidence_is_dropped():
    rec = record_finding_fate(REPO, message="m", fate="accepted", confidence_at_emit=1.7)
    assert rec["confidence_at_emit"] is None
    rec2 = record_finding_fate(REPO, message="m2", fate="accepted", confidence_at_emit="oops")
    assert rec2["confidence_at_emit"] is None


def test_per_surface_per_lens_precision_math():
    # deep-work correctness: 3 accepted, 1 declined -> 0.75; 2 converted excluded.
    for i in range(3):
        record_finding_fate(
            REPO, message=f"c-a-{i}", fate="accepted", lens="correctness", surface="deep-work"
        )
    record_finding_fate(
        REPO, message="c-d", fate="declined", lens="correctness", surface="deep-work"
    )
    for i in range(2):
        record_finding_fate(
            REPO, message=f"c-x-{i}", fate="converted", lens="correctness", surface="deep-work"
        )
    # deep-work perf: only a converted -> precision null (no accept/decline).
    record_finding_fate(REPO, message="p-x", fate="converted", lens="perf", surface="deep-work")
    # pr-review correctness is a SEPARATE surface (survival rate), not pooled in.
    for i in range(2):
        record_finding_fate(
            REPO, message=f"r-a-{i}", fate="accepted", lens="correctness", surface="pr-review"
        )

    stats = per_lens_precision(REPO)
    assert stats["unverified"] == 0
    assert "overall" not in stats, "no incoherent cross-surface overall"

    dw = stats["surfaces"]["deep-work"]
    corr = dw["lenses"]["correctness"]
    assert (corr["accepted"], corr["declined"], corr["converted"]) == (3, 1, 2)
    assert corr["precision"] == pytest.approx(0.75)
    assert dw["lenses"]["perf"]["precision"] is None
    assert dw["overall"]["precision"] == pytest.approx(0.75)  # 3/(3+1) across dw lenses

    pr = stats["surfaces"]["pr-review"]
    assert pr["lenses"]["correctness"]["precision"] == pytest.approx(1.0)


def test_hmac_tamper_evidence_flips_verified():
    record_finding_fate(REPO, message="m", fate="accepted", lens="correctness")
    path = _fates_path(REPO)
    row = json.loads(path.read_text().strip())
    row["fate"] = "declined"  # silently rewrite the fate, keep the old signature
    path.write_text(json.dumps(row) + "\n")

    history = read_finding_fates(REPO)
    assert history["records"][0]["verified"] is False
    assert history["unverified"] == 1


def test_precision_excludes_tampered_rows():
    # A tampered (HMAC-failing) row must not skew the aggregate, and must be counted.
    record_finding_fate(
        REPO, message="ok", fate="accepted", lens="correctness", surface="deep-work"
    )
    record_finding_fate(
        REPO, message="bad", fate="accepted", lens="correctness", surface="deep-work"
    )
    path = _fates_path(REPO)
    lines = path.read_text().splitlines()
    row = json.loads(lines[-1])
    row["fate"] = "declined"  # flip without re-signing
    lines[-1] = json.dumps(row)
    path.write_text("\n".join(lines) + "\n")

    stats = per_lens_precision(REPO)
    assert stats["unverified"] == 1
    corr = stats["surfaces"]["deep-work"]["lenses"]["correctness"]
    # Only the 1 verified accepted row counts; the tampered "declined" is excluded.
    assert (corr["accepted"], corr["declined"]) == (1, 0)
    assert corr["precision"] == pytest.approx(1.0)


def test_signing_failure_records_unsigned_not_dropped(monkeypatch):
    import chameleon_mcp.review_ledger as rl

    def _boom(_record):
        raise RuntimeError("no signing key")

    monkeypatch.setattr(rl, "_sign", _boom)
    rec = record_finding_fate(REPO, message="m", fate="accepted", lens="correctness")
    assert rec["hmac"] is None  # written unsigned, not dropped
    history = read_finding_fates(REPO)
    assert history["total"] == 1
    assert history["records"][0]["verified"] is False


def test_fail_open_on_missing_and_corrupt_ledger():
    # Missing ledger -> empty, no raise.
    empty = read_finding_fates(REPO)
    assert empty == {"repo_id": REPO, "records": [], "total": 0, "unverified": 0}
    assert per_lens_precision(REPO)["surfaces"] == {}
    # A corrupt line is skipped, not fatal.
    record_finding_fate(REPO, message="good", fate="accepted", lens="correctness")
    path = _fates_path(REPO)
    with open(path, "a", encoding="utf-8") as f:
        f.write("{not json at all\n")
    history = read_finding_fates(REPO)
    assert history["total"] == 1  # the good row survives; the garbage line is dropped


def test_no_repo_id_is_empty():
    assert read_finding_fates(None)["records"] == []
    assert per_lens_precision(None)["surfaces"] == {}


# --- MCP tool wrappers (the shipped, model-callable surface) ----------------------


def test_tool_record_and_stats_round_trip():
    from chameleon_mcp import tools

    r = tools.record_finding_fate(
        REPO,
        "agree",
        "retry is 2 not 3",
        file="a.rb",
        line=5,
        lens="correctness",
        surface="pr-review",
    )
    assert r["data"]["status"] == "ok"
    assert r["data"]["recorded"] is True
    assert r["data"]["record"]["fate"] == "accepted"

    s = tools.get_finding_fate_stats(REPO)
    assert s["data"]["surfaces"]["pr-review"]["lenses"]["correctness"]["accepted"] == 1


def test_tool_swallows_unknown_fate_never_raises():
    # The wrapper's whole purpose over the raw ledger: never raise, recorded False.
    from chameleon_mcp import tools

    r = tools.record_finding_fate(REPO, "maybe-later", "m")
    assert r["data"]["status"] == "failed"
    assert r["data"]["recorded"] is False


def test_tool_rejects_blank_args():
    from chameleon_mcp import tools

    assert tools.record_finding_fate(REPO, "", "m")["data"]["status"] == "failed"
    assert tools.record_finding_fate(REPO, "accepted", "")["data"]["status"] == "failed"
    assert tools.record_finding_fate("", "accepted", "m")["data"]["status"] == "failed"


def test_tool_stats_degraded_shape_matches_healthy():
    # A no_repo read must carry the same keys as healthy so a consumer parses one schema.
    from chameleon_mcp import tools

    d = tools.get_finding_fate_stats("/no/such/repo/path/xyz")["data"]
    assert d["status"] == "no_repo"
    assert d["surfaces"] == {}
    assert d["unverified"] == 0
