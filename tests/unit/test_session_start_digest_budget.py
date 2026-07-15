"""SessionStart operational digest + its budget trim (phase 4).

session_start() injects a curated ~2k-char operational digest instead of the
full ~13.6k-char using-chameleon SKILL.md body. The digest itself is a stable
module constant (_using_chameleon_digest); _fit_digest_to_budget is the one
piece of new per-call logic -- it is the ONLY compressible part of the
SessionStart emission when the total (conventions + digest + banners +
dead-session delivery) would exceed SESSION_START_DELIVERY_TOKEN_CEILING.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp import review_ledger
from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.core.budget import approx_tokens
from chameleon_mcp.core.finding import Finding
from chameleon_mcp.hook_helper import _fit_digest_to_budget, _using_chameleon_digest
from chameleon_mcp.tools import _compute_repo_id

_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugin"


def test_digest_is_nonempty_and_curated():
    digest = _using_chameleon_digest()
    assert isinstance(digest, str)
    assert digest  # non-empty
    # Load-bearing content the brief requires the digest to carry.
    assert "SessionStart" in digest
    assert "PreToolUse" in digest
    assert "PostToolUse" in digest
    assert "Stop" in digest
    assert "UserPromptSubmit" in digest
    assert "get_blast_radius" in digest  # comprehension-tools pointer
    assert "chameleon: drift" in digest  # drift banner meaning
    assert "production drift" in digest  # production-drift banner meaning
    assert "available on demand" in digest  # pointer to the full skill
    # Roughly the ~2k order of magnitude the brief targets, and nowhere near
    # the old ~13,588-char full SKILL.md body.
    assert 500 < len(digest) < 6000


def test_digest_is_stable_across_calls():
    # A stable constant, not re-derived per call.
    assert _using_chameleon_digest() == _using_chameleon_digest()


def test_fit_returns_full_text_when_budget_is_generous():
    digest = _using_chameleon_digest()
    fitted = _fit_digest_to_budget(digest, approx_tokens(digest) + 100)
    assert fitted == digest


def test_fit_trims_on_paragraph_boundaries_when_budget_is_tight():
    digest = _using_chameleon_digest()
    total = approx_tokens(digest)
    fitted = _fit_digest_to_budget(digest, total // 3)
    assert fitted != digest
    assert fitted  # still something, not empty, given a positive budget
    assert approx_tokens(fitted) <= total // 3
    # No mid-sentence cut: every kept chunk is a whole paragraph from the
    # original digest.
    original_paragraphs = digest.split("\n\n")
    for para in fitted.split("\n\n"):
        assert para in original_paragraphs


def test_fit_returns_empty_when_budget_is_zero_or_negative():
    digest = _using_chameleon_digest()
    assert _fit_digest_to_budget(digest, 0) == ""
    assert _fit_digest_to_budget(digest, -50) == ""


def test_fit_fails_open_to_full_text_on_internal_error():
    digest = _using_chameleon_digest()
    with patch("chameleon_mcp.core.budget.approx_tokens", side_effect=RuntimeError("boom")):
        fitted = _fit_digest_to_budget(digest, 10)
    assert fitted == digest


def test_fit_handles_empty_input_without_crashing():
    assert _fit_digest_to_budget("", 500) == ""
    assert _fit_digest_to_budget("", 0) == ""


def _old_finding(n: int) -> Finding:
    old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 24 * 3600))
    return Finding(
        id=f"f{n}",
        kind="correctness",
        severity="high",
        confidence=0.9,
        file="src/a.ts",
        span=(n, n),
        claim=f"a wordy repeated finding claim padded out to eat real budget #{n} " * 6,
        evidence="",
        excerpt_sha="",
        excerpt="",
        source_lens="correctness",
        status="pending",
        created_at=old_ts,
        verified="confirmed",
    )


def test_session_start_shrinks_digest_under_a_large_dead_session_delivery(tmp_path, monkeypatch):
    """End-to-end proof of the brief's core budgeting requirement: the WHOLE
    SessionStart emission (conventions + digest + banners + dead-session
    delivery) stays under SESSION_START_DELIVERY_TOKEN_CEILING, and when a
    large dead-session delivery alone eats most of that ceiling, the digest --
    the one compressible part -- is what shrinks (never the findings, which
    render whole or stay pending, per the ledger's own greedy packer)."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.ts").write_text("line1\nline2\nBUG here\nline4\n", encoding="utf-8")
    (repo / ".git").mkdir()  # a hard repo-root marker find_repo_root recognizes
    repo_id = _compute_repo_id(repo)

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_PLUGIN_ROOT))
    monkeypatch.setattr("chameleon_mcp.hook_helper._maybe_auto_refresh", lambda *a, **k: None)

    # Enough old, undelivered findings to push deliver_dead_session_findings's
    # OWN render up against its 2,500-token ceiling by itself -- note ws_root
    # must be the RESOLVED path: find_repo_root always resolves symlinks
    # (e.g. macOS /tmp -> /private/tmp) before it ever hands repo_root to
    # deliver_dead_session_findings, and undelivered_findings scopes strictly
    # by that exact ws_root string.
    findings = [_old_finding(i) for i in range(40)]
    review_ledger.record_findings(repo_id, str(repo.resolve()), findings)

    monkeypatch.chdir(repo)
    captured: list[str] = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": str(repo)}))),
        patch("sys.stdout") as out,
    ):
        out.write = captured.append
        from chameleon_mcp.hook_helper import session_start

        session_start()
    ctx = json.loads("".join(captured))["hookSpecificOutput"]["additionalContext"]

    # The dead-session findings actually delivered in bulk (proves the
    # pressure was real, not a no-op fixture): each finding's claim repeats
    # "eat real budget" six times, so a healthy chunk of the 40 findings made
    # it through the ledger's own greedy pack.
    assert ctx.count("eat real budget") > 100

    # The digest -- the ONE compressible part -- was squeezed out entirely:
    # the dead-session delivery alone already fills the shared
    # SESSION_START_DELIVERY_TOKEN_CEILING, leaving no headroom. This is the
    # documented trade-off: actionable review findings outrank static
    # operational reference prose, never the other way around.
    assert "Hook lifecycle:" not in ctx
    assert "Honesty:" not in ctx

    # The WHOLE emission still respects the shared ceiling (small slack for
    # wrapper tags/banners the budget calc approximates, not measures exactly)
    # -- proving the total is actually bounded, not just additive on top of
    # the old unconditional full-digest injection (which would have landed
    # ~900 tokens higher here).
    ceiling = threshold_int("SESSION_START_DELIVERY_TOKEN_CEILING")
    assert approx_tokens(ctx) <= ceiling + 100
