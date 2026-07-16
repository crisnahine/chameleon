"""G1': pinned-evidence layer on the turn-end judge Finding.

A finding can now carry excerpt_sha (a hash of the code excerpt it was reviewed
against), evidence_cmds (pinned outputs of green-lit executable checks), and a
suggested_fix. The point is honesty across a time gap: a finding reviewed in a
detached/async pass, then surfaced a turn later, must be visibly flagged if the
cited code changed since review -- annotated, never dropped (the refuter stays
the only dropper). These tests pin the helpers and the additive, advisory,
fail-open contract.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from chameleon_mcp.hook_helper import _pending_findings_block
from chameleon_mcp.judge import (
    Finding,
    _coerce_findings,
    _excerpt_digest,
    _excerpt_sha_stale,
    attach_evidence_cmd,
    excerpt_is_stale,
    pin_excerpt,
)
from chameleon_mcp.optouts import _safe_session_marker
from chameleon_mcp.safe_open import excerpt_window as _excerpt_window


def _f(**kw) -> Finding:
    kw.setdefault("message", "retry count is 2 not 3")
    kw.setdefault("confidence", 0.8)
    return Finding(**kw)


def test_new_fields_are_additive_and_default_none():
    f = _f()
    assert f.excerpt_sha is None
    assert f.evidence_cmds is None
    assert f.suggested_fix is None
    # Positional construction of the legacy four fields still works (no reorder).
    legacy = Finding("msg", 0.5, "a.rb", 7)
    assert (legacy.message, legacy.confidence, legacy.file, legacy.line) == ("msg", 0.5, "a.rb", 7)
    assert legacy.excerpt_sha is None


def test_pin_and_stale_detection():
    f = _f(file="a.rb", line=5)
    pin_excerpt(f, "def call\n  retry 2\nend\n")
    assert f.excerpt_sha  # a short hex digest
    # Same text -> not stale; changed text -> stale.
    assert excerpt_is_stale(f, "def call\n  retry 2\nend\n") is False
    assert excerpt_is_stale(f, "def call\n  retry 3\nend\n") is True


def test_unpinned_finding_is_never_stale():
    # No excerpt_sha (the common case) -> the check is a no-op, never a false stale.
    f = _f()
    assert f.excerpt_sha is None
    assert excerpt_is_stale(f, "anything at all") is False
    # None current excerpt (unreadable) also never fabricates staleness.
    assert excerpt_is_stale(f, None) is False


def test_pin_is_whitespace_insensitive_at_the_edges_only():
    # Trailing-newline / surrounding-blank churn should not read as a code change,
    # but an interior change must. (Pinning normalizes only leading/trailing ws.)
    f = _f()
    pin_excerpt(f, "  def call; end  \n")
    assert excerpt_is_stale(f, "def call; end") is False
    assert excerpt_is_stale(f, "def call; end2") is True


def test_attach_evidence_cmd_pins_output_hash():
    f = _f()
    attach_evidence_cmd(f, "grep -c retry a.rb", "2\n")
    attach_evidence_cmd(f, "rspec a_spec.rb", "1 example, 0 failures\n")
    assert isinstance(f.evidence_cmds, list) and len(f.evidence_cmds) == 2
    first = f.evidence_cmds[0]
    assert first["cmd"] == "grep -c retry a.rb"
    assert first["output_sha256"]  # the output is pinned by hash, not stored raw
    assert "2\n" not in str(first)  # raw output is not carried, only its digest


def test_coerce_findings_accepts_suggested_fix():
    out = _coerce_findings(
        [
            {
                "message": "off by one",
                "confidence": 0.9,
                "file": "a.rb",
                "line": 5,
                "suggested_fix": "use <= not <",
            }
        ]
    )
    assert len(out) == 1
    assert out[0].suggested_fix == "use <= not <"
    # A finding with no suggested_fix still coerces (additive, optional).
    out2 = _coerce_findings([{"message": "m", "confidence": 0.5}])
    assert out2[0].suggested_fix is None


# --- async delivery: annotate-on-stale, never drop (excerpt-level) -----------------

_SID = "sess"


def _write_pending(repo_data, finding: dict, digests: dict | None = None) -> None:
    path = repo_data / f".judge_pending.{_safe_session_marker(_SID)}.json"
    path.write_text(
        json.dumps(
            {
                "turn_key": "t",
                "completed_ts": 0,
                "digests": digests or {},
                "findings": [finding],
            }
        )
    )


def _repo_with_bug(tmp_path, n_lines: int = 100) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    body = [f"line{i}" for i in range(1, n_lines + 1)]
    body[2] = "BUG here"  # line 3
    (repo / "a.rb").write_text("\n".join(body) + "\n")
    return repo


def test_delivery_clean_when_excerpt_unchanged(tmp_path):
    repo = _repo_with_bug(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    sha = _excerpt_digest(_excerpt_window(repo, "a.rb", 3))
    _write_pending(
        data,
        {"file": "a.rb", "line": 3, "message": "off by one", "confidence": 0.9, "excerpt_sha": sha},
    )
    block = _pending_findings_block(repo, data, _SID)
    assert block is not None and "off by one" in block
    assert "stale" not in block.lower()


def test_delivery_annotates_stale_when_excerpt_changed(tmp_path):
    repo = _repo_with_bug(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    sha = _excerpt_digest(_excerpt_window(repo, "a.rb", 3))
    # Change the finding's own line: the excerpt now differs.
    (repo / "a.rb").write_text((repo / "a.rb").read_text().replace("BUG here", "FIXED now"))
    _write_pending(
        data,
        {"file": "a.rb", "line": 3, "message": "off by one", "confidence": 0.9, "excerpt_sha": sha},
    )
    block = _pending_findings_block(repo, data, _SID)
    # Delivered (NOT dropped) but flagged stale.
    assert block is not None and "off by one" in block
    assert "stale: code changed since review" in block


def test_delivery_recovers_finding_when_file_changed_elsewhere(tmp_path):
    repo = _repo_with_bug(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    old_whole = hashlib.sha256((repo / "a.rb").read_bytes()[:1_000_000]).hexdigest()[:16]
    sha = _excerpt_digest(_excerpt_window(repo, "a.rb", 3))
    # Edit far outside the +/-25 window around line 3, so the whole-file digest
    # changes but the cited excerpt does not. The coarse whole-file drop would
    # have lost this finding; excerpt-level recovers it clean.
    text = (repo / "a.rb").read_text().replace("line80", "line80_edited")
    (repo / "a.rb").write_text(text)
    _write_pending(
        data,
        {"file": "a.rb", "line": 3, "message": "off by one", "confidence": 0.9, "excerpt_sha": sha},
        digests={"a.rb": old_whole},  # populated: old code would have dropped on mismatch
    )
    block = _pending_findings_block(repo, data, _SID)
    assert block is not None and "off by one" in block
    assert "stale" not in block.lower()  # excerpt unchanged -> clean, not stale


def test_delivery_drops_when_file_gone(tmp_path):
    repo = _repo_with_bug(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    sha = _excerpt_digest(_excerpt_window(repo, "a.rb", 3))
    (repo / "a.rb").unlink()  # the reviewed code no longer exists
    _write_pending(
        data,
        {"file": "a.rb", "line": 3, "message": "off by one", "confidence": 0.9, "excerpt_sha": sha},
    )
    assert _pending_findings_block(repo, data, _SID) is None


def test_delivery_drops_out_of_repo_path_without_reading(tmp_path):
    # rel is untrusted model output: an absolute out-of-repo path or a ../ traversal
    # must be dropped by containment, never read (no exfil, no hot-path read).
    repo = _repo_with_bug(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    _write_pending(
        data,
        {
            "file": "/etc/passwd",
            "line": 1,
            "message": "x",
            "confidence": 0.5,
            "excerpt_sha": "dead",
        },
    )
    assert _pending_findings_block(repo, data, _SID) is None
    _write_pending(data, {"file": "../../secret.txt", "line": 1, "message": "y", "confidence": 0.5})
    assert _pending_findings_block(repo, data, _SID) is None


def test_delivery_no_excerpt_sha_annotates_stale_not_dropped(tmp_path):
    # FLIPPED (phase-3 task 6, spec section 5.4): a finding WITHOUT a pinned
    # excerpt whose whole-file digest changed since review used to be dropped
    # silently. "One policy at every delivery point ... silent drops are
    # removed" -- it now surfaces annotated `[stale: code changed since
    # review]` instead, the refuter stays the only dropper.
    repo = _repo_with_bug(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    old = hashlib.sha256((repo / "a.rb").read_bytes()[:1_000_000]).hexdigest()[:16]
    (repo / "a.rb").write_text((repo / "a.rb").read_text() + "extra\n")  # whole-file digest changes
    _write_pending(
        data,
        {"file": "a.rb", "line": 3, "message": "z", "confidence": 0.9},
        digests={"a.rb": old},
    )
    block = _pending_findings_block(repo, data, _SID)
    assert block is not None and "z" in block
    assert "stale: code changed since review" in block

    # ...and delivered clean (no stale tag) when the whole-file digest still
    # matches.
    cur = hashlib.sha256((repo / "a.rb").read_bytes()[:1_000_000]).hexdigest()[:16]
    _write_pending(
        data,
        {"file": "a.rb", "line": 3, "message": "z", "confidence": 0.9},
        digests={"a.rb": cur},
    )
    block = _pending_findings_block(repo, data, _SID)
    assert block is not None and "z" in block
    assert "stale" not in block.lower()


def test_delivery_renders_suggested_fix(tmp_path):
    repo = _repo_with_bug(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    sha = _excerpt_digest(_excerpt_window(repo, "a.rb", 3))
    _write_pending(
        data,
        {
            "file": "a.rb",
            "line": 3,
            "message": "off by one",
            "confidence": 0.9,
            "excerpt_sha": sha,
            "suggested_fix": "use <= not <",
        },
    )
    block = _pending_findings_block(repo, data, _SID)
    assert "suggested fix: use <= not <" in block


def test_empty_current_excerpt_is_never_stale():
    # The unified check (helper AND delivery share it): an empty/blank current
    # excerpt is can't-tell, never fabricated staleness.
    f = _f()
    pin_excerpt(f, "def call; end")
    assert excerpt_is_stale(f, "") is False
    assert excerpt_is_stale(f, "   \n  ") is False
    assert _excerpt_sha_stale("abc123", "") is False
    assert _excerpt_sha_stale("abc123", None) is False


def test_pin_empty_excerpt_leaves_unpinned():
    f = _f()
    pin_excerpt(f, "   \n  ")
    assert f.excerpt_sha is None
    pin_excerpt(f, None)
    assert f.excerpt_sha is None
