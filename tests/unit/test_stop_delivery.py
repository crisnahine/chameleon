"""stop/delivery.py: the ledger-based UserPromptSubmit/SessionStart delivery
path (spec sections 3.5, 5.4) -- multi-root discovery reuse, stale-annotate-
never-drop, and render+mark-delivered orchestration.

Multi-root discovery is exercised by patching ``hook_helper._discover_stop_roots``
directly (the same lightweight seam test_stop_package_seams.py patches), rather
than reconstructing the full real-enforcement-state fixture test_stop_multiroot.py
uses for the Stop backstop itself -- this module only cares that delivery
correctly CONSUMES whatever roots discovery returns, not how discovery derives
them (that contract is pinned elsewhere).

Isolation: CHAMELEON_PLUGIN_DATA and CHAMELEON_HMAC_KEY_PATH both point under a
fresh tmp_path, mirroring test_ledger_lifecycle.py.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from chameleon_mcp import review_ledger
from chameleon_mcp.core.finding import Finding
from chameleon_mcp.optouts import _safe_session_marker
from chameleon_mcp.stop import assemble, delivery

SID = "deliv-sess-1"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "hmac.key"))
    yield


def _finding(**over) -> Finding:
    base = dict(
        id="f1",
        kind="correctness",
        severity="high",
        confidence=0.9,
        file="src/a.ts",
        span=(3, 3),
        claim="off by one",
        evidence="",
        excerpt_sha="",
        excerpt="",
        source_lens="correctness",
        status="pending",
        created_at="2026-07-15T00:00:00Z",
        verified="confirmed",
    )
    base.update(over)
    return Finding(**base)


def _repo(tmp_path, name: str, *, rel="src/a.ts", body="line1\nline2\nBUG here\nline4\n") -> Path:
    repo = tmp_path / name
    (repo / Path(rel).parent).mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(body, encoding="utf-8")
    return repo


def _root(repo_id: str, ws_root: Path, plugin_data: Path) -> dict:
    return {
        "ws_root": ws_root,
        "repo_id": repo_id,
        "repo_data": plugin_data / repo_id,
        "files": set(),
        "has_armed": False,
    }


def _not_suppressed(*a, **k):
    return None


# --- deliver_pending_findings: single root -----------------------------------


def test_single_root_live_render_without_cache(tmp_path):
    repo = _repo(tmp_path, "repo-a")
    review_ledger.record_findings("repo-a-id", str(repo), [_finding()])
    root = _root("repo-a-id", repo, tmp_path / "data")

    with (
        patch("chameleon_mcp.hook_helper._discover_stop_roots", return_value=[root]),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", side_effect=_not_suppressed),
    ):
        block = delivery.deliver_pending_findings(repo, SID)

    assert block is not None
    assert block.startswith("<chameleon-context>")
    assert "off by one" in block
    assert "[confirmed]" in block
    assert review_ledger.undelivered_findings("repo-a-id", ws_roots=[str(repo)]) == []


def test_single_root_uses_cached_payload_and_consumes_it(tmp_path):
    repo = _repo(tmp_path, "repo-b")
    f = _finding(claim="cached claim")
    review_ledger.record_findings("repo-b-id", str(repo), [f])
    repo_data = tmp_path / "data" / "repo-b-id"
    assemble.write_delivery_payload(repo_data, SID, "PRE-RENDERED TEXT")
    root = _root("repo-b-id", repo, tmp_path / "data")

    with (
        patch("chameleon_mcp.hook_helper._discover_stop_roots", return_value=[root]),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", side_effect=_not_suppressed),
    ):
        block = delivery.deliver_pending_findings(repo, SID)

    assert block is not None
    assert "PRE-RENDERED TEXT" in block
    # The cache is consumed (one-shot) and the underlying finding is marked
    # delivered even though the emitted text came from the cache, not a live
    # render of this exact finding.
    assert assemble.read_delivery_payload(repo_data, SID) is None
    assert review_ledger.undelivered_findings("repo-b-id", ws_roots=[str(repo)]) == []


def test_no_roots_discovered_returns_none(tmp_path):
    with patch("chameleon_mcp.hook_helper._discover_stop_roots", return_value=[]):
        assert delivery.deliver_pending_findings(tmp_path, SID) is None


def test_no_findings_returns_none(tmp_path):
    repo = _repo(tmp_path, "repo-empty")
    root = _root("repo-empty-id", repo, tmp_path / "data")
    with (
        patch("chameleon_mcp.hook_helper._discover_stop_roots", return_value=[root]),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", side_effect=_not_suppressed),
    ):
        assert delivery.deliver_pending_findings(repo, SID) is None


# --- deliver_pending_findings: multi-root (spec section 3.5) -----------------


def test_multiroot_collects_findings_from_every_workspace(tmp_path):
    repo_web = _repo(tmp_path, "web", rel="src/web.ts")
    repo_api = _repo(tmp_path, "api", rel="app/api.py")
    review_ledger.record_findings(
        "web-id", str(repo_web), [_finding(claim="web bug", file="src/web.ts")]
    )
    review_ledger.record_findings(
        "api-id", str(repo_api), [_finding(claim="api bug", file="app/api.py")]
    )
    roots = [
        _root("web-id", repo_web, tmp_path / "data"),
        _root("api-id", repo_api, tmp_path / "data"),
    ]

    with (
        patch("chameleon_mcp.hook_helper._discover_stop_roots", return_value=roots),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", side_effect=_not_suppressed),
    ):
        block = delivery.deliver_pending_findings(tmp_path / "mono", SID)

    assert block is not None
    assert "web bug" in block
    assert "api bug" in block
    # One shared header/disclaimer, not one per root.
    assert block.count("\U0001f98e") == 1
    assert review_ledger.undelivered_findings("web-id", ws_roots=[str(repo_web)]) == []
    assert review_ledger.undelivered_findings("api-id", ws_roots=[str(repo_api)]) == []


def test_multiroot_suppressed_root_skipped_others_still_deliver(tmp_path):
    repo_web = _repo(tmp_path, "web2", rel="src/web.ts")
    repo_api = _repo(tmp_path, "api2", rel="app/api.py")
    review_ledger.record_findings(
        "web2-id", str(repo_web), [_finding(claim="suppressed web bug", file="src/web.ts")]
    )
    review_ledger.record_findings(
        "api2-id", str(repo_api), [_finding(claim="visible api bug", file="app/api.py")]
    )
    roots = [
        _root("web2-id", repo_web, tmp_path / "data"),
        _root("api2-id", repo_api, tmp_path / "data"),
    ]

    def _suppress_web(ws_root, repo_id, session_id):
        return "session_disable" if repo_id == "web2-id" else None

    with (
        patch("chameleon_mcp.hook_helper._discover_stop_roots", return_value=roots),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", side_effect=_suppress_web),
    ):
        block = delivery.deliver_pending_findings(tmp_path / "mono2", SID)

    assert block is not None
    assert "visible api bug" in block
    assert "suppressed web bug" not in block
    # The suppressed root's finding stays pending -- never delivered, never lost.
    assert len(review_ledger.undelivered_findings("web2-id", ws_roots=[str(repo_web)])) == 1
    assert review_ledger.undelivered_findings("api2-id", ws_roots=[str(repo_api)]) == []


def test_multiroot_runs_migrate_pending_queue_per_root(tmp_path):
    repo = _repo(tmp_path, "legacy-repo")
    repo_data = tmp_path / "data" / "legacy-id"
    repo_data.mkdir(parents=True)
    legacy_path = repo_data / f".judge_pending.{_safe_session_marker('old-sess')}.json"
    legacy_path.write_text(
        json.dumps(
            {
                "turn_key": "t" * 32,
                "completed_ts": 0.0,
                "digests": {},
                "findings": [
                    {"file": "src/a.ts", "line": 3, "message": "legacy bug", "confidence": 0.8}
                ],
            }
        ),
        encoding="utf-8",
    )
    root = _root("legacy-id", repo, tmp_path / "data")

    with (
        patch("chameleon_mcp.hook_helper._discover_stop_roots", return_value=[root]),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", side_effect=_not_suppressed),
    ):
        block = delivery.deliver_pending_findings(repo, SID)

    assert not legacy_path.exists()  # one-time migration consumed the legacy file
    assert block is not None
    assert "legacy bug" in block


# --- staleness: annotate, never drop (spec section 5.4, the NEW path) -------


def test_stale_finding_annotated_never_dropped(tmp_path):
    repo = _repo(tmp_path, "stale-repo")
    from chameleon_mcp.judge import _excerpt_digest
    from chameleon_mcp.stop.verify import _excerpt_window

    sha = _excerpt_digest(_excerpt_window(repo, "src/a.ts", 3))
    review_ledger.record_findings(
        "stale-id", str(repo), [_finding(excerpt_sha=sha, claim="pinned finding")]
    )
    # Change the reviewed line: the pinned excerpt no longer matches.
    (repo / "src" / "a.ts").write_text("line1\nline2\nFIXED now\nline4\n", encoding="utf-8")
    root = _root("stale-id", repo, tmp_path / "data")

    with (
        patch("chameleon_mcp.hook_helper._discover_stop_roots", return_value=[root]),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", side_effect=_not_suppressed),
    ):
        block = delivery.deliver_pending_findings(repo, SID)

    assert block is not None
    assert "pinned finding" in block  # delivered, not dropped
    assert "[stale]" in block
    # Still marked delivered -- annotation, not withholding.
    assert review_ledger.undelivered_findings("stale-id", ws_roots=[str(repo)]) == []


def test_unchanged_excerpt_not_flagged_stale(tmp_path):
    repo = _repo(tmp_path, "fresh-repo")
    from chameleon_mcp.judge import _excerpt_digest
    from chameleon_mcp.stop.verify import _excerpt_window

    sha = _excerpt_digest(_excerpt_window(repo, "src/a.ts", 3))
    review_ledger.record_findings(
        "fresh-id", str(repo), [_finding(excerpt_sha=sha, claim="unchanged finding")]
    )
    root = _root("fresh-id", repo, tmp_path / "data")

    with (
        patch("chameleon_mcp.hook_helper._discover_stop_roots", return_value=[root]),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", side_effect=_not_suppressed),
    ):
        block = delivery.deliver_pending_findings(repo, SID)

    assert block is not None
    assert "unchanged finding" in block
    assert "[stale]" not in block


# --- deliver_dead_session_findings: SessionStart age-bounded delivery -------


def test_dead_session_delivers_old_undelivered_finding(tmp_path):
    repo = _repo(tmp_path, "dead-repo")
    old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 24 * 3600))
    review_ledger.record_findings(
        "dead-id", str(repo), [_finding(claim="old finding", created_at=old_ts)]
    )
    repo_data = tmp_path / "data" / "dead-id"

    text = delivery.deliver_dead_session_findings(repo, "dead-id", repo_data)

    assert text is not None
    assert "old finding" in text
    assert not text.startswith("<chameleon-context>")  # bare text, SessionStart's own shape
    assert review_ledger.undelivered_findings("dead-id", ws_roots=[str(repo)]) == []


def test_dead_session_withholds_fresh_finding(tmp_path):
    repo = _repo(tmp_path, "fresh-dead-repo")
    now_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    review_ledger.record_findings(
        "fresh-dead-id", str(repo), [_finding(claim="just now finding", created_at=now_ts)]
    )
    repo_data = tmp_path / "data" / "fresh-dead-id"

    text = delivery.deliver_dead_session_findings(repo, "fresh-dead-id", repo_data)

    assert text is None
    # Withheld, not lost: still reachable at the next real delivery point.
    assert len(review_ledger.undelivered_findings("fresh-dead-id", ws_roots=[str(repo)])) == 1


def test_dead_session_no_findings_returns_none(tmp_path):
    repo = _repo(tmp_path, "empty-dead-repo")
    repo_data = tmp_path / "data" / "empty-dead-id"
    assert delivery.deliver_dead_session_findings(repo, "empty-dead-id", repo_data) is None


def test_dead_session_unparseable_timestamp_still_delivers(tmp_path):
    repo = _repo(tmp_path, "garbage-ts-repo")
    review_ledger.record_findings(
        "garbage-ts-id", str(repo), [_finding(claim="garbage ts finding", created_at="not-a-date")]
    )
    repo_data = tmp_path / "data" / "garbage-ts-id"

    text = delivery.deliver_dead_session_findings(repo, "garbage-ts-id", repo_data)

    assert text is not None
    assert "garbage ts finding" in text
